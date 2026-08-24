"""Ephemeral web gallery backed only by bounded process memory."""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, Response

from honeypot_trap import HoneypotRecorder, install_trap
from Module.config import app_config
from Module.lru_cache import LRUCache
from Module.memory_gallery import ImageTooLarge, InvalidImage, MemoryGalleryStore


def _static_dir() -> Path:
    return Path(app_config.web_static_dir)


def _ttl() -> int:
    return app_config.web_image_ttl_seconds


def _max_items() -> int:
    return app_config.web_feed_max_items


def _build_store() -> MemoryGalleryStore:
    mib = 1024 * 1024
    return MemoryGalleryStore(
        max_items=_max_items(),
        max_bytes=app_config.web_memory_max_mb * mib,
        max_image_bytes=app_config.web_image_max_mb * mib,
        ttl_seconds=_ttl(),
        thumbnail_width=app_config.web_thumb_width,
    )


def _purge_legacy_disk_cache() -> None:
    """Delete files created by the retired filesystem-backed gallery."""
    legacy = _static_dir() / "images"
    if not legacy.exists():
        return
    for path in sorted(legacy.rglob("*"), reverse=True):
        try:
            path.unlink() if path.is_file() else path.rmdir()
        except OSError as exc:
            raise RuntimeError(f"기존 디스크 이미지 캐시 삭제 실패: {path}") from exc
    try:
        legacy.rmdir()
    except OSError as exc:
        raise RuntimeError(f"기존 디스크 이미지 캐시 디렉터리 삭제 실패: {legacy}") from exc

# 클라이언트 localStorage 기반 1회 제한은 우회 가능하므로(스크립트로 반복 호출),
# 서버에서도 IP 기준 최소 방어선을 둔다: (IP, 이미지) 조합당 1회 + IP당 분당 호출 수 제한.
_like_ip_seen = LRUCache(5000)
_like_ip_rate: dict[str, tuple[int, float]] = {}
_LIKE_RATE_WINDOW = 60.0
_LIKE_RATE_MAX = 30

# /feed 폴링 남용(스크래핑 등) 방지용 IP별 속도 제한. 프론트는 5초마다 폴링(분당
# 12회)하므로, 여러 탭/공유 IP를 감안해 정상 사용은 절대 안 걸릴 만큼 여유를 둔다.
_feed_ip_rate: dict[str, tuple[int, float]] = {}
_FEED_RATE_WINDOW = 60.0
_FEED_RATE_MAX = 120


def _client_ip(request: Request) -> str:
    """Cloudflare 경유 시 실제 클라이언트 IP, 아니면 소켓 상의 IP.

    cf-connecting-ip 헤더는 클라이언트가 위조할 수 있으므로, WEB_ORIGIN_SECRET이
    설정돼 있으면 같은 값의 x-origin-secret 헤더가 동반될 때만 신뢰한다(미설정 시
    기존 동작 유지). 오리진 포트가 외부에 직접 노출돼도 헤더 위조로 rate limit·
    Turnstile 우회를 할 수 없게 하는 최소 방어선.
    """
    ip = request.headers.get("cf-connecting-ip")
    if ip is not None:
        secret = app_config.web_origin_secret
        supplied = request.headers.get("x-origin-secret", "")
        if not secret or hmac.compare_digest(
            secret.encode(), supplied.encode("latin-1", "backslashreplace")
        ):
            return ip
    return request.client.host if request.client else "unknown"


def _rate_limited(bucket: dict, ip: str, window: float, max_count: int) -> bool:
    """슬라이딩 윈도우 방식 IP별 요청 수 제한. 용도별로 별도 bucket을 넘겨 재사용한다."""
    now = time.time()
    count, start = bucket.get(ip, (0, now))
    if now - start > window:
        count, start = 0, now
    count += 1
    bucket[ip] = (count, start)
    if len(bucket) > 10000:  # 매핑이 무한정 커지지 않도록 만료분 청소
        cutoff = now - window
        for k, (_, s) in list(bucket.items()):
            if s < cutoff:
                del bucket[k]
    return count > max_count


# ── Cloudflare Turnstile (봇 차단) ──
# 시크릿이 설정돼 있을 때만 활성화. 사이트키는 공개값(HTML에 주입), 시크릿은 .env에만 둔다.
_TS_COOKIE = "ts_ok"
_TS_TTL = 86400  # 한 번 통과하면 24시간 유지
_VERIFY_MAX_BYTES = 16 * 1024


def _ts_sitekey() -> str:
    return app_config.turnstile_sitekey


def _ts_secret() -> str:
    return app_config.turnstile_secret


def _ts_enabled() -> bool:
    return bool(_ts_secret())


async def _read_verify_body(request: Request) -> bytes | None:
    """Read a small Turnstile payload without allowing unbounded buffering."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _VERIFY_MAX_BYTES:
                return None
        except ValueError:
            pass

    data = bytearray()
    async for chunk in request.stream():
        if len(data) + len(chunk) > _VERIFY_MAX_BYTES:
            return None
        data.extend(chunk)
    return bytes(data)


def _ts_make_cookie(ip: str) -> str:
    """쿠키 서명을 발급 클라이언트 IP에 바인딩한다.

    만료시각만 서명하면 쿠키를 다른 클라이언트에 복사해 Turnstile을 대량 우회할 수
    있으므로, 서명 대상에 IP를 포함해 쿠키 이동(share)을 무효화한다.
    """
    exp = int(time.time()) + _TS_TTL
    payload = f"{exp}.{ip}"
    sig = hmac.new(_ts_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{exp}.{sig}"


def _ts_cookie_valid(value: str, ip: str) -> bool:
    if not value or "." not in value:
        return False
    exp_s, sig = value.split(".", 1)
    try:
        exp = int(exp_s)
    except ValueError:
        return False
    if time.time() > exp:
        return False
    payload = f"{exp}.{ip}"
    good = hmac.new(_ts_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, good)


def _ts_siteverify(token: str, remoteip: str) -> bool:
    data = urllib.parse.urlencode({
        "secret": _ts_secret(),
        "response": token or "",
        "remoteip": remoteip or "",
    }).encode()
    req = urllib.request.Request(
        "https://challenges.cloudflare.com/turnstile/v0/siteverify", data=data
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return bool(json.load(r).get("success", False))
    except Exception:
        return False


# ── 긴급 점검(서버 닫기) 모드 ──
# 플래그 파일이 존재하거나 WEB_MAINTENANCE=1 이면 점검 페이지를 보여준다.
# dcselfie.sh down/up 으로 토글(웹 서버 재시작 없이 즉시 반영).
def _maintenance_file() -> Path:
    cfg = app_config.maintenance_file_path
    if cfg.name != ".maintenance":
        return cfg
    return Path(app_config.web_static_dir).parent / ".maintenance"


def _maintenance_on() -> bool:
    return app_config.web_maintenance or _maintenance_file().exists()


class _InternalRouteAccessFilter(logging.Filter):
    """uvicorn 액세스 로그에서 /internal/* 요청 라인을 제거한다.

    ingest 요청은 이미지 제목·원본 링크가 쿼리스트링에 실려 오는데, 액세스 로그로
    journald(디스크)에 영구 기록되면 '이미지 관련 데이터를 디스크에 남기지 않는다'는
    목표가 무색해진다. uvicorn AccessFormatter의 record.args는
    (client_addr, method, full_path, http_version, status_code) 튜플이다."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        return not (
            isinstance(args, tuple)
            and len(args) >= 3
            and str(args[2]).startswith("/internal/")
        )


def _install_access_log_privacy_filter() -> None:
    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _InternalRouteAccessFilter) for f in access_logger.filters):
        access_logger.addFilter(_InternalRouteAccessFilter())


def create_app(store: MemoryGalleryStore | None = None) -> FastAPI:
    static_dir = _static_dir()
    static_dir.mkdir(parents=True, exist_ok=True)
    _purge_legacy_disk_cache()
    _install_access_log_privacy_filter()
    gallery_store = store or _build_store()
    ingest_slots = asyncio.Semaphore(2)
    # 공개용 이미지 피드일 뿐 소비 대상 API가 아니므로 대화형 문서/스키마는 끈다
    # (불필요한 내부 라우트 노출 방지).
    app = FastAPI(title="Gallery Image Relay", docs_url=None, redoc_url=None, openapi_url=None)

    def _page(name: str) -> HTMLResponse:
        f = static_dir / name
        if not f.exists():
            return HTMLResponse(f"<h1>{name} not found</h1>", status_code=404)
        return HTMLResponse(f.read_text(encoding="utf-8"))

    @app.middleware("http")
    async def cache_control(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        resp = await call_next(request)
        if path.startswith("/images/") or path in ("/feed", "/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.middleware("http")
    async def security_headers(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        resp = await call_next(request)
        # 클릭재킹/MIME 스니핑/과도한 리퍼러 유출 방지 + HTTPS 유지. 부작용 위험이
        # 없는 헤더만 넣는다. CSP는 넣지 않음: 이 사이트는 GA/Turnstile 스크립트를
        # 쓰고 페이지 자체 로직도 인라인 <script>라, 제대로 안 맞춘 CSP는 사이트
        # 기능 자체를 조용히 깨뜨릴 수 있다.
        # 도입하려면 Content-Security-Policy-Report-Only로 먼저 위반 로그를 보고
        # 점진적으로 좁혀야 한다(현재 인프라로는 라이브 검증 불가).
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        resp.headers["Strict-Transport-Security"] = "max-age=15552000"
        return resp

    def _maintenance_response() -> HTMLResponse:
        m = static_dir / "maintenance.html"
        body = m.read_text(encoding="utf-8") if m.exists() else "<h1>잠시 점검 중입니다</h1>"
        return HTMLResponse(body, status_code=503, headers={"Retry-After": "3600"})

    @app.get("/", response_class=HTMLResponse)
    async def index() -> Response:
        if _maintenance_on():
            return _maintenance_response()
        idx = static_dir / "index.html"
        if not idx.exists():
            return HTMLResponse("<h1>Gallery starting...</h1>")
        html = idx.read_text(encoding="utf-8")
        html = html.replace("{{TURNSTILE_SITEKEY}}", _ts_sitekey() if _ts_enabled() else "")
        return HTMLResponse(html)

    @app.get("/policy", response_class=HTMLResponse)
    async def policy() -> Response:
        return _page("policy.html")

    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy() -> Response:
        return _page("privacy.html")

    @app.get("/about", response_class=HTMLResponse)
    async def about() -> Response:
        return _page("about.html")

    @app.get("/changelog", response_class=HTMLResponse)
    async def changelog() -> Response:
        return _page("changelog.html")

    @app.get("/request", response_class=HTMLResponse)
    async def request_gallery() -> Response:
        return _page("request.html")

    # 브라우저가 루트 경로로 직접 요청하는 파비콘들. 내용이 안 바뀌므로 캐시 헤더 부여.
    for _fname, _mime in (
        ("favicon.ico", "image/x-icon"),
        ("favicon-16x16.png", "image/png"),
        ("favicon-32x32.png", "image/png"),
        ("apple-touch-icon.png", "image/png"),
        ("og-image.jpg", "image/jpeg"),  # 링크 공유 미리보기(Open Graph)
        ("robots.txt", "text/plain"),    # Sitemap 참조 포함 (CF 관리형 robots와 병합됨)
        ("sitemap.xml", "application/xml"),
        ("manifest.json", "application/manifest+json"),  # PWA
        ("icon-192.png", "image/png"),
        ("icon-512.png", "image/png"),
    ):
        def _make_favicon_route(
            fname: str = _fname,
            mime: str = _mime,
        ) -> Callable[[], Awaitable[Response]]:
            async def _serve() -> Response:
                f = static_dir / fname
                if not f.exists():
                    return PlainTextResponse("", status_code=404)
                return FileResponse(f, media_type=mime, headers={"Cache-Control": "public, max-age=604800"})
            return _serve

        app.add_api_route(f"/{_fname}", _make_favicon_route(), methods=["GET"])

    @app.get("/.well-known/security.txt", response_class=PlainTextResponse)
    async def security_txt() -> Response:
        # RFC 9116: 보안 취약점 제보 연락처
        f = static_dir / "security.txt"
        if not f.exists():
            return PlainTextResponse("", status_code=404)
        return PlainTextResponse(f.read_text(encoding="utf-8"),
                                 headers={"Cache-Control": "public, max-age=86400"})

    @app.post("/internal/images")
    async def ingest_image(
        request: Request,
        filename: str = Query("", max_length=255),
        title: str = Query("", max_length=500),
        link: str = Query("", max_length=2048),
        gallery: str = Query("", max_length=100),
    ) -> Response:
        expected = app_config.web_ingest_token
        supplied = request.headers.get("x-ingest-token", "")
        # bytes로 비교: str 오버로드는 non-ASCII 입력에 TypeError를 던지므로(latin-1로
        # 디코드된 헤더에 임의 바이트가 올 수 있음) 그대로 쓰면 401 대신 500이 된다.
        if not expected or not hmac.compare_digest(
            expected.encode(), supplied.encode("latin-1", "backslashreplace")
        ):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        max_bytes = app_config.web_ingest_max_mb * 1024 * 1024
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > max_bytes:
                    return JSONResponse({"error": "payload too large"}, status_code=413)
            except ValueError:
                return JSONResponse({"error": "invalid content length"}, status_code=400)
        async with ingest_slots:
            data = bytearray()
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > max_bytes:
                    return JSONResponse({"error": "payload too large"}, status_code=413)
            try:
                item = await asyncio.to_thread(
                    gallery_store.put, bytes(data), filename, title, link, gallery
                )
            except InvalidImage:
                return JSONResponse({"error": "invalid image"}, status_code=415)
            except ImageTooLarge:
                return JSONResponse({"error": "image too large"}, status_code=413)
        return JSONResponse(item)

    @app.get("/images/{image_id}")
    async def image(image_id: str, thumbnail: bool = False) -> Response:
        found = gallery_store.get(image_id, thumbnail=thumbnail)
        if found is None:
            return Response(status_code=404, headers={"Cache-Control": "no-store"})
        data, media_type = found
        return Response(data, media_type=media_type, headers={"Cache-Control": "no-store"})

    @app.get("/feed")
    async def feed(request: Request, limit: int = Query(60, ge=1, le=200)) -> Response:
        ip = _client_ip(request)
        if _rate_limited(_feed_ip_rate, ip, _FEED_RATE_WINDOW, _FEED_RATE_MAX):
            return JSONResponse({"error": "too many requests"}, status_code=429)
        if _maintenance_on() and request.headers.get("cf-connecting-ip"):
            return JSONResponse({"error": "maintenance"}, status_code=503)
        # Cloudflare를 거친 공개 요청(cf-connecting-ip 존재)만 Turnstile 통과를 요구한다.
        # 로컬 직접 접근(대시보드 등)은 헤더가 없어 그대로 허용.
        if _ts_enabled() and request.headers.get("cf-connecting-ip"):
            if not _ts_cookie_valid(request.cookies.get(_TS_COOKIE, ""), ip):
                return JSONResponse({"error": "verification required"}, status_code=403)
        return JSONResponse(gallery_store.snapshot(limit))

    @app.post("/verify")
    async def verify(request: Request) -> Response:
        if not _ts_enabled():
            return JSONResponse({"ok": True})
        raw_body = await _read_verify_body(request)
        if raw_body is None:
            return JSONResponse({"error": "payload too large"}, status_code=413)
        try:
            body = json.loads(raw_body)
        except (ValueError, UnicodeError, RecursionError):
            body = {}
        token = body.get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            return JSONResponse({"ok": False}, status_code=403)
        ip = request.headers.get("cf-connecting-ip", "")
        ok = await asyncio.to_thread(_ts_siteverify, token, ip)
        if not ok:
            return JSONResponse({"ok": False}, status_code=403)
        resp = JSONResponse({"ok": True})
        resp.set_cookie(_TS_COOKIE, _ts_make_cookie(_client_ip(request)), max_age=_TS_TTL,
                        httponly=True, samesite="lax", secure=True)
        return resp

    @app.post("/like/{image_id}")
    async def like(image_id: str, request: Request) -> Response:
        ip = _client_ip(request)
        if _rate_limited(_like_ip_rate, ip, _LIKE_RATE_WINDOW, _LIKE_RATE_MAX):
            return JSONResponse({"error": "too many requests"}, status_code=429)
        if _like_ip_seen.add_if_absent((ip, image_id)):
            n = gallery_store.likes(image_id)
            if n is None:
                return JSONResponse({"error": "not found"}, status_code=404)
            return JSONResponse({"id": image_id, "likes": n})
        n = gallery_store.increment_likes(image_id)
        if n is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        return JSONResponse({"id": image_id, "likes": n})

    @app.get("/healthz")
    async def healthz() -> Response:
        stats = gallery_store.stats()
        latest_age = stats["latest_age_seconds"]
        if latest_age is None:
            # 빈 스토어는 기동 직후 그레이스 동안만 정상으로 본다. TTL로 전부 만료돼
            # 비어 있는 것은 크롤러 전멸 신호인데, 무조건 fresh=true면 장애를 가린다.
            fresh = stats["uptime_seconds"] <= app_config.web_freshness_seconds
        else:
            fresh = latest_age <= app_config.web_freshness_seconds
        return JSONResponse({
            "ok": True,
            **stats,
            "fresh": fresh,
            "ttl": _ttl(),
            "ingest_configured": bool(app_config.web_ingest_token),
            "maintenance": _maintenance_on(),
        })

    install_trap(app, HoneypotRecorder())
    return app
