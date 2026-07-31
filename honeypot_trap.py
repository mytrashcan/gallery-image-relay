"""Stealth decoy surface for AI crawlers and automated scanners."""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

AI_CRAWLER_USER_AGENTS = (
    "GPTBot",
    "ChatGPT-User",
    "OAI-SearchBot",
    "ClaudeBot",
    "anthropic-ai",
    "Claude-Web",
    "Google-Extended",
    "GoogleOther",
    "Bytespider",
    "PerplexityBot",
    "Perplexity-User",
    "cohere-ai",
    "Amazonbot",
    "Applebot-Extended",
    "CCBot",
    "Diffbot",
    "ImagesiftBot",
    "Meta-ExternalAgent",
    "meta-externalagent",
    "FacebookBot",
    "Falkor",
    "awario",
    "Barkrowler",
    "DataForSeoBot",
    "AhrefsBot",
    "SemrushBot",
    "MJ12bot",
    "PetalBot",
    "PiplBot",
    "Scrapy",
    "python-requests",
    "python-urllib",
    "curl",
    "wget",
    "Go-http-client",
    "okhttp",
    "httpclient",
    "PostmanRuntime",
    "insomnia",
    "httpx",
    "aiohttp",
    "node-fetch",
    "undici",
    "axios",
)

_BROWSER_AND_PREVIEW_USER_AGENTS = (
    "chrome/",
    "chromium/",
    "crios/",
    "safari/",
    "firefox/",
    "fxios/",
    "edg/",
    "edge/",
    "edgios/",
    "edga/",
    "samsungbrowser/",
    "discordbot",
    "facebookexternalhit",
    "kakao",
    "telegram",
    "naver",
    "googlebot",
    "bingbot",
    "applebot",
    "testclient",
)

_CREDENTIAL_PATHS = {
    "/.env",
    "/.git/config",
    "/.aws/credentials",
    "/credentials.json",
    "/config.json",
    "/wp-config.php",
}
_API_SCANNER_PATHS = {
    "/api",
    "/api/v1",
    "/v1",
    "/v2",
    "/graphql",
    "/openapi.json",
    "/swagger.json",
    "/swagger-ui",
    "/docs",
    "/redoc",
    "/api/v1/chat/completions",
    "/v1/chat/completions",
    "/api/v1/models",
    "/v1/models",
    "/completions",
    "/embed",
    "/invoke",
    "/predictions",
}
_PROBE_PATHS = {
    "/actuator",
    "/actuator/env",
    "/actuator/health",
    "/health",
    "/debug",
    "/metrics",
}
_EXPLOIT_PATHS = {
    "/cgi-bin",
    "/wp-admin",
    "/wp-login.php",
    "/admin",
    "/phpinfo.php",
    "/server-status",
    "/manager/html",
    "/trace",
    "/console",
}


def _matched_user_agent(user_agent: str) -> str | None:
    lowered = user_agent.casefold()
    for signature in AI_CRAWLER_USER_AGENTS:
        if signature.casefold() in lowered:
            return signature
    return None


def _looks_like_browser_or_preview(user_agent: str) -> bool:
    lowered = user_agent.casefold()
    return any(signature in lowered for signature in _BROWSER_AND_PREVIEW_USER_AGENTS)


def _classify_scanner(
    user_agent: str,
    path: str,
    method: str,
    query: Mapping[str, Any],
) -> tuple[bool, str, str]:
    del method, query
    normalized_path = "/" + path.lstrip("/")
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")
    lowered_path = normalized_path.casefold()
    user_agent_signature = _matched_user_agent(user_agent)

    if user_agent_signature is None and _looks_like_browser_or_preview(user_agent):
        return False, "", ""

    if lowered_path in _CREDENTIAL_PATHS:
        return True, "credential-hunter", normalized_path
    if lowered_path in _EXPLOIT_PATHS or lowered_path.startswith("/cgi-bin/"):
        return True, "exploit-scanner", normalized_path
    if lowered_path in _API_SCANNER_PATHS:
        return True, "api-scanner", normalized_path
    if lowered_path in _PROBE_PATHS or lowered_path.startswith("/actuator/"):
        return True, "probe", normalized_path
    if lowered_path == "/robots.txt" and user_agent_signature is not None:
        return True, "ai-crawler", normalized_path
    if user_agent_signature is not None:
        return True, "ai-crawler", user_agent_signature
    return False, "", ""


def looks_like_ai_scanner(
    user_agent: str,
    path: str,
    method: str,
    query: dict,
) -> tuple[bool, str]:
    """Return whether a request resembles a curated crawler or automated probe."""
    matched, category, _ = _classify_scanner(user_agent, path, method, query)
    return matched, category


class HoneypotRecorder:
    """Append honeypot interactions to a process-local, thread-safe JSONL log."""

    def __init__(self, path: str | os.PathLike[str] | None = None) -> None:
        configured_path = path or os.environ.get("HONEYPOT_LOG_PATH")
        self.path = (
            Path(configured_path)
            if configured_path
            else Path(__file__).resolve().parent / "honeypot_traffic.jsonl"
        )
        self._lock = threading.Lock()

    def record(
        self,
        *,
        source_ip: str,
        user_agent: str,
        method: str,
        path: str,
        query_keys: list[str],
        category: str,
        matched_signature: str,
        status_code: int,
        response_shape: str,
    ) -> dict[str, Any]:
        event = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_id": uuid.uuid4().hex,
            "source_ip": source_ip,
            "user_agent": user_agent,
            "method": method,
            "path": path,
            "query_keys": sorted(set(query_keys)),
            "category": category,
            "matched_signature": matched_signature,
            "status_code": status_code,
            "response_shape": response_shape,
        }
        encoded = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as log_file:
                log_file.write(encoded)
                log_file.write("\n")
        return event


def decoy_responses(path: str, method: str) -> tuple[int, dict | str, str]:
    """Build a harmless response shaped like the resource a scanner requested."""
    normalized_path = "/" + path.lstrip("/")
    if normalized_path != "/":
        normalized_path = normalized_path.rstrip("/")
    lowered_path = normalized_path.casefold()
    upper_method = method.upper()

    if (
        lowered_path in {"/api/v1/chat/completions", "/v1/chat/completions"}
        and upper_method == "POST"
    ):
        return (
            200,
            {
                "id": f"chatcmpl-{uuid.uuid4().hex[:24]}",
                "object": "chat.completion",
                "created": int(datetime.now(UTC).timestamp()),
                "model": "nexusflow-70b",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": "Service is available.",
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 9,
                    "completion_tokens": 4,
                    "total_tokens": 13,
                },
            },
            "application/json",
        )

    if lowered_path in {"/api/v1/models", "/v1/models"} and upper_method == "GET":
        return (
            200,
            {
                "object": "list",
                "data": [
                    {"id": "nexusflow-70b", "object": "model", "owned_by": "platform"},
                    {"id": "orion-embed-v2", "object": "model", "owned_by": "platform"},
                ],
            },
            "application/json",
        )

    if lowered_path == "/graphql" and upper_method in {"GET", "POST"}:
        return (
            200,
            {
                "data": {
                    "__schema": {
                        "queryType": {"name": "Query"},
                        "types": [
                            {
                                "kind": "OBJECT",
                                "name": "Query",
                                "fields": [{"name": "ping", "type": {"name": "String"}}],
                            }
                        ],
                    },
                    "ping": "pong",
                },
            },
            "application/json",
        )

    if lowered_path in {"/openapi.json", "/swagger.json"}:
        return (
            200,
            {
                "openapi": "3.0.0",
                "info": {
                    "title": "Inference Gateway",
                    "version": "1.0.0",
                    "description": "Inference Gateway API",
                },
                "paths": {
                    "/api/v1/chat/completions": {
                        "post": {"responses": {"200": {"description": "Completion"}}}
                    },
                    "/api/v1/models": {
                        "get": {"responses": {"200": {"description": "Model list"}}}
                    },
                },
            },
            "application/json",
        )

    if lowered_path == "/.env":
        return (
            200,
            "# Production environment\nAPP_ENV=production\n"
            "DATABASE_PASSWORD=9f3c2a7b1d4e8f0a6c5b9d2e7f1a3c4b\n"
            "API_KEY=sk-live-4f8a2c1e9b7d3f5a6c0e2d8b4f1a7c9e\n",
            "text/plain",
        )
    if lowered_path == "/.aws/credentials":
        return (
            200,
            "[default]\naws_access_key_id=AKIA4F8A2C1E9B7D3F5A\n"
            "aws_secret_access_key=9f3c2a7b1d4e8f0a6c5b9d2e7f1a3c4b8e0d6f2a4c9b1e3d5f7a8c0b2d4e6f8\n",
            "text/plain",
        )
    if lowered_path in {"/credentials.json", "/config.json", "/wp-config.php"}:
        return (
            200,
            {
                "username": "gallery_admin",
                "password": "9f3c2a7b1d4e8f0a6c5b9d2e7f1a3c4b",
                "api_key": "sk-live-4f8a2c1e9b7d3f5a6c0e2d8b4f1a7c9e",
            },
            "application/json",
        )
    if lowered_path == "/.git/config":
        return (
            200,
            "[core]\n\trepositoryformatversion = 0\n"
            "[remote \"origin\"]\n\turl = https://github.com/gallery-platform/gallery-image-relay.git\n",
            "text/plain",
        )

    if lowered_path.startswith("/actuator"):
        body: dict[str, Any]
        if lowered_path.endswith("/env"):
            body = {
                "paths": ["java.home", "server.port", "spring.profiles.active"],
            }
        else:
            body = {"status": "UP"}
        return 200, body, "application/json"

    if lowered_path in {"/wp-admin", "/wp-login.php", "/admin"}:
        return (
            200,
            "<!doctype html><html><head><title>Admin Login</title></head>"
            "<body><main><h1>Sign in</h1><form method=\"post\">"
            "<input name=\"username\"><input name=\"password\" type=\"password\">"
            "<button type=\"submit\">Sign in</button></form></main>"
            "</body></html>",
            "text/html",
        )

    if lowered_path in {"/api", "/api/v1", "/v1", "/v2"}:
        return (
            200,
            {
                "status": "ok",
                "version": "1.0.0",
                "endpoints": [
                    "/api/v1/chat/completions",
                    "/graphql",
                    "/api/v1/models",
                ],
            },
            "application/json",
        )

    return 404, {"detail": "Not Found"}, "application/json"


def _response_shape(path: str, status_code: int) -> str:
    lowered_path = ("/" + path.lstrip("/")).casefold().rstrip("/") or "/"
    if lowered_path in {"/api/v1/chat/completions", "/v1/chat/completions"}:
        return "decoy-chat-completion"
    if lowered_path in {"/api/v1/models", "/v1/models"}:
        return "decoy-model-list"
    if lowered_path == "/graphql":
        return "decoy-graphql"
    if lowered_path in {"/openapi.json", "/swagger.json"}:
        return "decoy-openapi"
    if lowered_path == "/.git/config":
        return "decoy-git-config"
    if lowered_path in _CREDENTIAL_PATHS:
        return "decoy-credentials"
    if lowered_path.startswith("/actuator"):
        return "decoy-actuator"
    if lowered_path in {"/wp-admin", "/wp-login.php", "/admin"}:
        return "decoy-login"
    if lowered_path in {"/api", "/api/v1", "/v1", "/v2"}:
        return "decoy-api-index"
    return "decoy-not-found" if status_code == 404 else "decoy-generic"


def install_trap(app: FastAPI, recorder: HoneypotRecorder) -> None:
    """Register the trap after all real application routes."""

    @app.api_route(
        "/{full_path:path}",
        methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"],
        include_in_schema=False,
    )
    async def honeypot_catch_all(request: Request, full_path: str) -> Response:
        path = f"/{full_path}"
        query = dict(request.query_params)
        matched, category, signature = _classify_scanner(
            request.headers.get("user-agent", ""),
            path,
            request.method,
            query,
        )
        if not matched:
            return JSONResponse({"detail": "Not Found"}, status_code=404)

        status_code, body, content_type = decoy_responses(path, request.method)
        recorder.record(
            source_ip=request.headers.get("cf-connecting-ip")
            or (request.client.host if request.client else "unknown"),
            user_agent=request.headers.get("user-agent", ""),
            method=request.method,
            path=path,
            query_keys=list(query),
            category=category,
            matched_signature=signature,
            status_code=status_code,
            response_shape=_response_shape(path, status_code),
        )
        if isinstance(body, dict):
            return JSONResponse(body, status_code=status_code)
        return Response(body, status_code=status_code, media_type=content_type)
