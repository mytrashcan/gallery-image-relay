from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import web_app
from honeypot_trap import HoneypotRecorder, install_trap, looks_like_ai_scanner


class HoneypotTrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.log_path = Path(self.temp_dir.name) / "honeypot.jsonl"
        self.recorder = HoneypotRecorder(self.log_path)
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        install_trap(app, self.recorder)
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        self.temp_dir.cleanup()

    def _events(self) -> list[dict]:
        if not self.log_path.exists():
            return []
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_known_ai_user_agent_on_root_is_trapped(self) -> None:
        matched, category = looks_like_ai_scanner("GPTBot/1.2", "/", "GET", {})

        self.assertTrue(matched)
        self.assertEqual(category, "ai-crawler")

        response = self.client.get("/", headers={"User-Agent": "GPTBot/1.2"})

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["metadata"]["source"], "ai-honeypot")
        self.assertEqual(self._events()[0]["category"], "ai-crawler")

    def test_normal_browser_is_not_trapped_on_chat_completion_path(self) -> None:
        response = self.client.post(
            "/api/v1/chat/completions",
            headers={
                "User-Agent": (
                    "Mozilla/5.0 AppleWebKit/537.36 "
                    "Chrome/126.0.0.0 Safari/537.36"
                )
            },
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Not Found"})
        self.assertEqual(self._events(), [])

    def test_python_requests_on_env_is_credential_hunter(self) -> None:
        response = self.client.get(
            "/.env",
            headers={"User-Agent": "python-requests/2.32.3"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("honeypot", response.text)
        event = self._events()[0]
        self.assertEqual(event["category"], "credential-hunter")
        self.assertEqual(event["matched_signature"], "/.env")

    def test_chat_completion_decoy_has_choices(self) -> None:
        response = self.client.post(
            "/v1/chat/completions",
            headers={"User-Agent": "curl/8.7.1"},
        )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsInstance(body["choices"], list)
        self.assertTrue(body["choices"])
        self.assertEqual(body["metadata"]["source"], "ai-honeypot")

    def test_recorder_writes_required_jsonl_fields(self) -> None:
        self.recorder.record(
            source_ip="203.0.113.7",
            user_agent="GPTBot/1.2",
            method="GET",
            path="/api",
            query_keys=["token", "page", "page"],
            category="api-scanner",
            matched_signature="/api",
            status_code=200,
            response_shape="decoy-api-index",
        )

        event = self._events()[0]
        required_fields = {
            "timestamp",
            "event_id",
            "source_ip",
            "user_agent",
            "method",
            "path",
            "query_keys",
            "category",
            "matched_signature",
            "status_code",
            "response_shape",
        }
        self.assertTrue(required_fields.issubset(event))
        self.assertEqual(len(event["event_id"]), 32)
        self.assertEqual(event["query_keys"], ["page", "token"])

    def test_non_matching_browser_path_returns_404(self) -> None:
        response = self.client.get(
            "/definitely-not-a-real-page",
            headers={"User-Agent": "Mozilla/5.0 Firefox/128.0"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json(), {"detail": "Not Found"})
        self.assertEqual(self._events(), [])

    def test_human_crawlers_and_link_previews_are_never_trapped(self) -> None:
        protected_user_agents = (
            "Mozilla/5.0 Chrome/126.0.0.0 Safari/537.36",
            "Mozilla/5.0 Version/17.5 Safari/605.1.15",
            "Mozilla/5.0 Firefox/128.0",
            "Mozilla/5.0 Edg/126.0.0.0 Chrome/126.0.0.0",
            "Mozilla/5.0 SamsungBrowser/25.0 Chrome/121.0.0.0",
            "Discordbot/2.0",
            "facebookexternalhit/1.1",
            "KakaoTalk-Scrap/1.0",
            "TelegramBot (like TwitterBot)",
            "Mozilla/5.0 NAVER(inapp; search; 2000; 12.0.0)",
            "Googlebot/2.1 (+http://www.google.com/bot.html)",
            "bingbot/2.0",
            "Applebot/0.1",
        )

        for user_agent in protected_user_agents:
            with self.subTest(user_agent=user_agent):
                matched, category = looks_like_ai_scanner(
                    user_agent,
                    "/.env",
                    "GET",
                    {},
                )
                self.assertFalse(matched)
                self.assertEqual(category, "")

    def test_web_app_registers_trap_after_real_routes(self) -> None:
        static_dir = Path(self.temp_dir.name) / "web_static"
        with (
            patch.object(web_app.app_config, "web_static_dir", str(static_dir)),
            patch.object(web_app.app_config, "turnstile_secret", ""),
            patch.dict(os.environ, {"HONEYPOT_LOG_PATH": str(self.log_path)}),
        ):
            app = web_app.create_app()
            with TestClient(app) as client:
                real_response = client.get("/", headers={"User-Agent": "GPTBot/1.2"})
                decoy_response = client.get(
                    "/v1/models",
                    headers={"User-Agent": "GPTBot/1.2"},
                )

        self.assertEqual(app.routes[-1].path, "/{full_path:path}")
        self.assertEqual(real_response.status_code, 200)
        self.assertNotIn("ai-honeypot", real_response.text)
        self.assertEqual(decoy_response.status_code, 200)
        self.assertEqual(decoy_response.json()["metadata"]["source"], "ai-honeypot")
        self.assertEqual(decoy_response.headers["x-content-type-options"], "nosniff")
        self.assertEqual([event["path"] for event in self._events()], ["/v1/models"])


if __name__ == "__main__":
    unittest.main()
