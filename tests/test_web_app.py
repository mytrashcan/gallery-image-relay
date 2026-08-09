from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from PIL import Image

import web_app
from Module.memory_gallery import MemoryGalleryStore
from web_app import create_app


def image_bytes(size=(64, 64), *, fmt="PNG", color="#336699") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, color).save(output, format=fmt)
    return output.getvalue()


def make_store(*, clock=None, max_items=10, max_bytes=1024 * 1024, ttl=3600, thumb=480):
    return MemoryGalleryStore(
        max_items=max_items,
        max_bytes=max_bytes,
        max_image_bytes=max_bytes,
        ttl_seconds=ttl,
        thumbnail_width=thumb,
        clock=clock or __import__("time").time,
    )


def make_client(monkeypatch, tmp_path, store=None) -> tuple[TestClient, MemoryGalleryStore]:
    static_dir = tmp_path / "web_static"
    static_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(web_app.app_config, "web_static_dir", str(static_dir))
    monkeypatch.setattr(web_app.app_config, "web_ingest_token", "test-secret")
    monkeypatch.setattr(web_app.app_config, "turnstile_secret", "")
    monkeypatch.setattr(web_app, "_like_ip_seen", web_app.LRUCache(100))
    monkeypatch.setattr(web_app, "_like_ip_rate", {})
    monkeypatch.setattr(web_app, "_feed_ip_rate", {})
    gallery_store = store or make_store()
    return TestClient(create_app(gallery_store)), gallery_store


def ingest(client: TestClient, data: bytes, filename="sample.png", **params):
    return client.post(
        "/internal/images",
        params={"filename": filename, **params},
        content=data,
        headers={"X-Ingest-Token": "test-secret", "Content-Type": "application/octet-stream"},
    )


def test_ingest_serves_image_without_writing_to_disk(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)

    result = ingest(
        client,
        image_bytes(),
        title="title",
        link="https://example.com/post",
        gallery="test",
    )

    assert result.status_code == 200
    item = result.json()
    assert item["url"].startswith("/images/")
    response = client.get(item["url"])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert not (tmp_path / "web_static" / "images").exists()


def test_ingest_requires_shared_secret(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)

    response = client.post("/internal/images", content=image_bytes())

    assert response.status_code == 401


def test_ingest_rejects_invalid_image(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)

    response = ingest(client, b"not-an-image")

    assert response.status_code == 415


def test_ingest_non_ascii_token_returns_401_not_500(monkeypatch, tmp_path):
    """비ASCII 헤더는 str hmac.compare_digest에서 TypeError → 500이 되면 안 된다."""
    client, _ = make_client(monkeypatch, tmp_path)

    response = client.post(
        "/internal/images",
        content=image_bytes(),
        headers={b"x-ingest-token": "caf\xe9".encode("latin-1")},
    )

    assert response.status_code == 401


def test_ingest_empty_body_with_valid_token_returns_415_not_401(monkeypatch, tmp_path):
    """launcher의 기동 프로브 계약: 빈 바디 + 올바른 토큰 = 415(인증 통과), 틀린 토큰 = 401."""
    client, _ = make_client(monkeypatch, tmp_path)

    assert ingest(client, b"").status_code == 415
    wrong = client.post(
        "/internal/images", content=b"", headers={"X-Ingest-Token": "wrong-token"}
    )
    assert wrong.status_code == 401


def test_store_evicts_oldest_by_item_limit():
    store = make_store(max_items=2)
    first = store.put(image_bytes(color="red"), "1.png")
    second = store.put(image_bytes(color="green"), "2.png")
    third = store.put(image_bytes(color="blue"), "3.png")

    assert store.get(first["id"]) is None
    assert store.get(second["id"]) is not None
    assert store.get(third["id"]) is not None
    assert store.stats()["items"] == 2


def test_store_evicts_oldest_by_total_bytes():
    first_data = image_bytes(size=(128, 128), color="red")
    second_data = image_bytes(size=(128, 128), color="blue")
    store = make_store(max_bytes=len(first_data) + len(second_data) - 1, thumb=0)

    first = store.put(first_data, "1.png")
    second = store.put(second_data, "2.png")

    assert store.get(first["id"]) is None
    assert store.get(second["id"]) is not None
    assert store.stats()["memory_bytes"] <= store.max_bytes


def test_store_expires_items_by_ttl():
    now = [1000.0]
    store = make_store(clock=lambda: now[0], ttl=60)
    item = store.put(image_bytes(), "sample.png")

    now[0] += 61

    assert store.get(item["id"]) is None
    assert store.snapshot(10) == []


def test_process_restart_starts_with_empty_store(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)
    item = ingest(client, image_bytes()).json()
    assert client.get(item["url"]).status_code == 200

    restarted_client, restarted_store = make_client(monkeypatch, tmp_path, make_store())

    assert restarted_store.snapshot(10) == []
    assert restarted_client.get(item["url"]).status_code == 404


def test_large_image_has_in_memory_thumbnail(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)

    item = ingest(client, image_bytes((1200, 800), fmt="JPEG"), "large.jpg").json()

    assert item["thumb"].endswith("?thumbnail=1")
    thumb = client.get(item["thumb"])
    original = client.get(item["url"])
    assert thumb.status_code == 200
    assert len(thumb.content) < len(original.content)
    assert thumb.headers["cache-control"] == "no-store"


def test_duplicate_content_is_suppressed(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)
    data = image_bytes()

    assert ingest(client, data, "first.png").json()
    assert ingest(client, data, "second.png").json() == {}


def test_like_state_is_kept_in_memory(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)
    item = ingest(client, image_bytes()).json()

    first = client.post(f"/like/{item['id']}")
    second = client.post(f"/like/{item['id']}")

    assert first.json()["likes"] == 1
    assert second.json()["likes"] == 1


def test_health_reports_memory_and_freshness(monkeypatch, tmp_path):
    now = [1000.0]
    store = make_store(clock=lambda: now[0])
    client, _ = make_client(monkeypatch, tmp_path, store)
    monkeypatch.setattr(web_app.app_config, "web_freshness_seconds", 30)
    ingest(client, image_bytes())
    now[0] += 31

    health = client.get("/healthz").json()

    assert health["items"] == 1
    assert health["memory_bytes"] > 0
    assert health["ingest_configured"] is True
    assert health["fresh"] is False


def test_health_empty_store_goes_stale_after_grace(monkeypatch, tmp_path):
    """전부 만료돼 빈 스토어가 fresh=true로 돌아가 크롤러 전멸을 가리면 안 된다."""
    now = [1000.0]
    store = make_store(clock=lambda: now[0], ttl=60)
    client, _ = make_client(monkeypatch, tmp_path, store)
    monkeypatch.setattr(web_app.app_config, "web_freshness_seconds", 30)

    assert client.get("/healthz").json()["fresh"] is True  # 기동 직후 그레이스

    ingest(client, image_bytes())
    now[0] += 61  # TTL 경과 → 스토어가 다시 빔

    health = client.get("/healthz").json()
    assert health["items"] == 0
    assert health["fresh"] is False


def test_startup_removes_legacy_disk_cache(monkeypatch, tmp_path):
    legacy = tmp_path / "web_static" / "images" / "thumbs"
    legacy.mkdir(parents=True)
    (legacy / "old.jpg").write_bytes(b"old")

    make_client(monkeypatch, tmp_path)

    assert not (tmp_path / "web_static" / "images").exists()


def test_feed_and_api_docs_policy(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)
    ingest(client, image_bytes(), gallery="test")

    feed = client.get("/feed")

    assert feed.status_code == 200
    assert feed.json()[0]["gallery"] == "test"
    assert feed.headers["cache-control"] == "no-store"
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path).status_code == 404


def _access_record(path: str):
    import logging

    return logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 0,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "POST", path, "1.1", 200), None,
    )


def test_internal_routes_are_excluded_from_access_log(monkeypatch, tmp_path):
    """제목·링크가 쿼리스트링으로 오는 ingest 라인이 journald(디스크)에 남지 않아야 한다."""
    import logging

    make_client(monkeypatch, tmp_path)
    make_client(monkeypatch, tmp_path)  # 재호출에도 필터는 1개만 (중복 설치 방지)

    filters = [
        f for f in logging.getLogger("uvicorn.access").filters
        if isinstance(f, web_app._InternalRouteAccessFilter)
    ]
    assert len(filters) == 1
    assert filters[0].filter(_access_record("/internal/images?title=제목&link=url")) is False
    assert filters[0].filter(_access_record("/feed")) is True


def test_security_headers_present(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)

    response = client.get("/healthz")

    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert "max-age=" in response.headers["strict-transport-security"]


def test_verify_rejects_oversized_body_before_turnstile_call(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app.app_config, "turnstile_secret", "test-secret")
    siteverify = MagicMock(return_value=True)
    monkeypatch.setattr(web_app, "_ts_siteverify", siteverify)

    response = client.post("/verify", json={"token": "x" * web_app._VERIFY_MAX_BYTES})

    assert response.status_code == 413
    siteverify.assert_not_called()


def test_verify_rejects_oversized_stream_without_content_length(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app.app_config, "turnstile_secret", "test-secret")
    siteverify = MagicMock(return_value=True)
    monkeypatch.setattr(web_app, "_ts_siteverify", siteverify)
    data = b"x" * (web_app._VERIFY_MAX_BYTES + 1)

    response = client.post(
        "/verify",
        content=iter((data[:100], data[100:])),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    siteverify.assert_not_called()


def test_verify_rejects_non_string_token_without_turnstile_call(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app.app_config, "turnstile_secret", "test-secret")
    siteverify = MagicMock(return_value=True)
    monkeypatch.setattr(web_app, "_ts_siteverify", siteverify)

    response = client.post("/verify", json={"token": ["unexpected"]})

    assert response.status_code == 403
    siteverify.assert_not_called()


def test_verify_accepts_valid_string_token(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)
    monkeypatch.setattr(web_app.app_config, "turnstile_secret", "test-secret")
    siteverify = MagicMock(return_value=True)
    monkeypatch.setattr(web_app, "_ts_siteverify", siteverify)

    response = client.post(
        "/verify",
        json={"token": "turnstile-token"},
        headers={"cf-connecting-ip": "203.0.113.8"},
    )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert "ts_ok=" in response.headers["set-cookie"]
    siteverify.assert_called_once_with("turnstile-token", "203.0.113.8")


def test_static_pages_do_not_include_ad_network_hooks():
    static_dir = Path(web_app.__file__).parent / "web_static"
    rejected_markers = (
        "adsbygoogle",
        "google-adsense-account",
        "pagead2.googlesyndication.com",
        "kakao_ad_area",
        "t1.kakaocdn.net",
        "data-ad-unit",
    )

    for page in static_dir.glob("*.html"):
        html = page.read_text(encoding="utf-8").lower()
        assert not any(marker in html for marker in rejected_markers), page.name

    assert not (static_dir / "ads.txt").exists()
    app_paths = {route.path for route in create_app(make_store()).routes}
    assert "/ads.txt" not in app_paths


def animated_gif_bytes(size=(64, 64), frames=3) -> bytes:
    colors = ("#ff0000", "#00ff00", "#0000ff")
    images = [Image.new("RGB", size, colors[i % 3]) for i in range(frames)]
    out = io.BytesIO()
    images[0].save(out, format="GIF", save_all=True, append_images=images[1:], duration=100, loop=0)
    return out.getvalue()


def noisy_png_bytes(size=(128, 128)) -> bytes:
    import os

    image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def test_like_rate_limit_returns_429_past_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "_LIKE_RATE_MAX", 3)
    client, _ = make_client(monkeypatch, tmp_path)
    items = [
        ingest(client, image_bytes(color=f"#0000{i:02x}"), f"rl{i}.png").json() for i in range(5)
    ]

    headers = {"cf-connecting-ip": "9.9.9.9"}
    statuses = [client.post(f"/like/{it['id']}", headers=headers).status_code for it in items]

    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]


def test_feed_rate_limit_returns_429_past_threshold(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app, "_FEED_RATE_MAX", 3)
    client, _ = make_client(monkeypatch, tmp_path)

    headers = {"cf-connecting-ip": "8.8.8.8"}
    statuses = [client.get("/feed", headers=headers).status_code for _ in range(5)]

    assert statuses[:3] == [200, 200, 200]
    assert 429 in statuses[3:]


def test_feed_exposes_image_dimensions(monkeypatch, tmp_path):
    """프론트가 로드 전에 카드 높이를 예약할 수 있도록 원본 치수를 내려보낸다."""
    client, _ = make_client(monkeypatch, tmp_path)
    ingest(client, image_bytes((1200, 800), fmt="JPEG"), "dim.jpg")

    feed = client.get("/feed").json()

    assert feed[0]["w"] == 1200
    assert feed[0]["h"] == 800


def test_discovery_endpoints_are_served(monkeypatch, tmp_path):
    import shutil

    client, _ = make_client(monkeypatch, tmp_path)
    static = tmp_path / "web_static"
    for f in ("robots.txt", "sitemap.xml", "security.txt", "manifest.json"):
        shutil.copy(f"web_static/{f}", static / f)

    robots = client.get("/robots.txt")
    assert robots.status_code == 200
    assert "Sitemap: https://dcselfie.win/sitemap.xml" in robots.text
    assert "Disallow: /internal/" in robots.text

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    assert "<loc>https://dcselfie.win/</loc>" in sitemap.text

    sec = client.get("/.well-known/security.txt")
    assert sec.status_code == 200
    assert "Contact: mailto:" in sec.text and "Expires:" in sec.text

    manifest = client.get("/manifest.json")
    assert manifest.status_code == 200
    assert manifest.headers["content-type"].startswith("application/manifest+json")
    assert manifest.json()["display"] == "standalone"


def test_ingest_rejects_oversized_content_length_before_reading_body(monkeypatch, tmp_path):
    monkeypatch.setattr(web_app.app_config, "web_ingest_max_mb", 0)
    client, _ = make_client(monkeypatch, tmp_path)

    assert ingest(client, image_bytes()).status_code == 413


def test_ingest_rejects_oversized_streamed_body_without_content_length(monkeypatch, tmp_path):
    """Content-Length를 속이거나 생략(chunked)해도 스트리밍 단계에서 잘려야 한다."""
    monkeypatch.setattr(web_app.app_config, "web_ingest_max_mb", 0)
    client, _ = make_client(monkeypatch, tmp_path)
    data = image_bytes()

    response = client.post(
        "/internal/images",
        content=iter([data[:100], data[100:]]),  # generator → chunked, CL 없음
        headers={"X-Ingest-Token": "test-secret", "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413


def test_animated_gif_over_per_image_limit_returns_413(monkeypatch, tmp_path):
    """애니메이션은 JPEG 재압축이 불가능하므로 상한 초과 시 명시적으로 거절한다."""
    store = MemoryGalleryStore(
        max_items=10, max_bytes=1024 * 1024, max_image_bytes=100,
        ttl_seconds=3600, thumbnail_width=480,
    )
    client, _ = make_client(monkeypatch, tmp_path, store)

    assert ingest(client, animated_gif_bytes(), "anim.gif").status_code == 413


def test_animated_gif_under_limit_is_served_as_gif(monkeypatch, tmp_path):
    client, _ = make_client(monkeypatch, tmp_path)

    item = ingest(client, animated_gif_bytes(), "anim.gif").json()

    assert item["url"].endswith(".gif")
    assert item["thumb"] == item["url"]  # 애니메이션은 프레임 손실 방지를 위해 썸네일 생략
    response = client.get(item["url"])
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/gif"


def test_oversized_static_image_is_recompressed_to_jpeg(monkeypatch, tmp_path):
    store = MemoryGalleryStore(
        max_items=10, max_bytes=1024 * 1024, max_image_bytes=16 * 1024,
        ttl_seconds=3600, thumbnail_width=480,
    )
    client, _ = make_client(monkeypatch, tmp_path, store)
    png = noisy_png_bytes()  # 노이즈 PNG ≈ 50KB > 16KB 상한

    item = ingest(client, png, "big.png").json()

    assert item["url"].endswith(".jpg")
    response = client.get(item["url"])
    assert response.headers["content-type"] == "image/jpeg"
    assert len(response.content) <= 16 * 1024
