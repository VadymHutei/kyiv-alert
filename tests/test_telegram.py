import io
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import replace
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from kyiv_alert.__main__ import main
from kyiv_alert.config import Config
from kyiv_alert.notifier import (
    ConsoleNotifier, DeliveryError, TelegramNotifier, create_notifier,
)
from kyiv_alert.service import Service
from kyiv_alert.source import Snapshot
from kyiv_alert.store import Store


TOKEN = "123456:" + "fake_token_for_tests"
CHAT = "-1001234567890"
TELEGRAM_ENV = {"NOTIFIER": "telegram", "TELEGRAM_BOT_TOKEN": TOKEN,
                "TELEGRAM_CHAT_ID": CHAT}


class Response(io.BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(json.dumps(body).encode() if not isinstance(body, bytes) else body)


class TelegramTests(unittest.TestCase):
    def setUp(self):
        self.notifier = TelegramNotifier(TOKEN, CHAT, timeout=3)

    def test_post_plain_unicode_message_and_confirm_delivery(self):
        response = Response({"ok": True, "result": {"message_id": 7}})
        with patch.object(self.notifier._opener, "open", return_value=response) as send:
            with self.assertLogs("kyiv_alert.notifier", level="INFO") as logs:
                self.notifier.send(42, "🔴 Київ <тест> & перевірка")
        request = send.call_args.args[0]
        self.assertEqual(request.full_url, f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        self.assertEqual(request.method, "POST")
        body = json.loads(request.data)
        self.assertEqual(body["chat_id"], CHAT)
        self.assertEqual(body["text"], "🔴 Київ <тест> & перевірка")
        self.assertNotIn("parse_mode", body)
        self.assertNotIn("message_thread_id", body)
        self.assertEqual(send.call_args.kwargs["timeout"], 3)
        self.assertIn("event_id=42", " ".join(logs.output))
        self.assertNotIn(TOKEN, " ".join(logs.output))

    def test_optional_topic(self):
        self.notifier.message_thread_id = 123
        with patch.object(self.notifier._opener, "open", return_value=Response(
                {"ok": True, "result": {"message_id": 1}})) as send:
            self.notifier.send(1, "test")
        self.assertEqual(json.loads(send.call_args.args[0].data)["message_thread_id"], 123)

    def test_http_429_propagates_retry_after_without_response_secrets(self):
        body = {"ok": False, "error_code": 429, "description": TOKEN,
                "parameters": {"retry_after": 600}}
        error = HTTPError(self.notifier._url, 429, TOKEN, {}, Response(body))
        with patch.object(self.notifier._opener, "open", side_effect=error):
            with self.assertRaises(DeliveryError) as caught:
                self.notifier.send(1, "test")
        self.assertEqual(caught.exception.retry_after, 600)
        self.assertNotIn(TOKEN, str(caught.exception))
        self.assertNotIn(TOKEN, repr(caught.exception))

    def test_api_level_error_in_successful_http_response(self):
        response = Response({"ok": False, "error_code": 429,
                             "parameters": {"retry_after": 60}})
        with patch.object(self.notifier._opener, "open", return_value=response):
            with self.assertRaises(DeliveryError) as caught:
                self.notifier.send(1, "test")
        self.assertEqual(caught.exception.retry_after, 60)

    def test_invalid_http_body_still_reports_safe_status(self):
        error = HTTPError(self.notifier._url, 401, TOKEN, {}, Response(b"<html>" + TOKEN.encode()))
        with patch.object(self.notifier._opener, "open", side_effect=error):
            with self.assertRaises(DeliveryError) as caught:
                self.notifier.send(1, "test")
        self.assertIn("401", str(caught.exception))
        self.assertIn("check bot token", str(caught.exception))
        self.assertNotIn(TOKEN, str(caught.exception))

    def test_errors_do_not_leak_url_or_token(self):
        for error in (URLError(self.notifier._url), TimeoutError(TOKEN), RemoteDisconnected(TOKEN)):
            with self.subTest(error=type(error).__name__):
                with patch.object(self.notifier._opener, "open", side_effect=error):
                    with self.assertRaises(DeliveryError) as caught:
                        self.notifier.send(1, "test")
                self.assertNotIn(TOKEN, str(caught.exception))
                self.assertIsNone(caught.exception.retry_after)

    def test_malformed_responses_are_not_acknowledged(self):
        for body in (b"invalid JSON", b"\xff", b"x" * (64 * 1024 + 1), [],
                     {"ok": True}, {"ok": "true", "result": {"message_id": 1}},
                     {"ok": True, "result": {"message_id": True}},
                     {"ok": False, "error_code": TOKEN}):
            with self.subTest(body_type=type(body).__name__):
                with patch.object(self.notifier._opener, "open", return_value=Response(body)):
                    with self.assertRaises(DeliveryError) as caught:
                        self.notifier.send(1, "test")
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_invalid_retry_after_values_are_ignored(self):
        for value in ("60", True, -1, 0, None, 2**63):
            error = TelegramNotifier._error(429, {"parameters": {"retry_after": value}})
            self.assertIsNone(error.retry_after)


class TelegramConfigTests(unittest.TestCase):
    def config(self, values):
        with patch.dict(os.environ, values, clear=True):
            return Config.from_env()

    def test_console_remains_default_without_credentials(self):
        self.assertIsInstance(create_notifier(self.config({})), ConsoleNotifier)

    def test_telegram_factory_and_optional_topic(self):
        config = self.config({**TELEGRAM_ENV, "TELEGRAM_MESSAGE_THREAD_ID": "123"})
        notifier = create_notifier(config)
        self.assertIsInstance(notifier, TelegramNotifier)
        self.assertEqual(notifier.message_thread_id, 123)
        self.assertNotIn(TOKEN, repr(config))
        self.assertNotIn(CHAT, repr(config))

    def test_public_channel_username(self):
        config = self.config({**TELEGRAM_ENV, "TELEGRAM_CHAT_ID": "@test_channel"})
        self.assertEqual(config.telegram_chat_id, "@test_channel")

    def test_missing_or_invalid_settings_fail_without_echoing_values(self):
        for values in (
            {"NOTIFIER": "invalid"},
            {"NOTIFIER": "telegram"},
            {**TELEGRAM_ENV, "TELEGRAM_BOT_TOKEN": "secret/invalid"},
            {**TELEGRAM_ENV, "TELEGRAM_CHAT_ID": ""},
            {**TELEGRAM_ENV, "TELEGRAM_CHAT_ID": "0"},
            {**TELEGRAM_ENV, "TELEGRAM_CHAT_ID": "-0"},
            {**TELEGRAM_ENV, "TELEGRAM_CHAT_ID": "invalid"},
            {**TELEGRAM_ENV, "TELEGRAM_MESSAGE_THREAD_ID": "-1"},
            {**TELEGRAM_ENV, "TELEGRAM_MESSAGE_THREAD_ID": "0"},
            {**TELEGRAM_ENV, "TELEGRAM_MESSAGE_THREAD_ID": "invalid"},
        ):
            with self.subTest(keys=list(values)), self.assertRaises(ValueError) as caught:
                self.config(values)
            self.assertNotIn(TOKEN, str(caught.exception))
            self.assertNotIn("secret/invalid", str(caught.exception))


class RetryTests(unittest.TestCase):
    def test_rate_limit_survives_new_state_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(Config(), state_path=Path(directory) / "state.sqlite3")
            source = type("Source", (), {"fetch": lambda _: Snapshot("red")})()
            notifier = TelegramNotifier(TOKEN, CHAT)
            store = Store(config.state_path)
            try:
                service = Service(config, source, store, notifier)
                limited = Response({"ok": False, "error_code": 429,
                                    "parameters": {"retry_after": 600}})
                with patch.object(notifier._opener, "open", return_value=limited) as send:
                    service.tick(100)
                    # New state must not bypass a server-wide rate limit.
                    source.fetch = lambda: Snapshot("none")
                    service.tick(110)
                    self.assertEqual(send.call_count, 1)
                self.assertEqual(store.retry_not_before(), 700)
                self.assertEqual(store.pending()["level"], "none")
                store.close()
                store = Store(config.state_path)
                service = Service(config, source, store, notifier)
                with patch.object(notifier._opener, "open", return_value=Response(
                        {"ok": True, "result": {"message_id": 7}})) as send:
                    service.tick(699)
                    self.assertEqual(send.call_count, 0)
                    service.tick(700)
                    self.assertEqual(send.call_count, 1)
                self.assertIsNone(store.pending())
                self.assertEqual(store.retry_not_before(), 0)
            finally:
                store.close()

    def test_existing_database_is_upgraded_without_losing_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            with sqlite3.connect(path) as old:
                old.execute("CREATE TABLE state (id INTEGER PRIMARY KEY, level TEXT, since TEXT, observed_at REAL)")
                old.execute("INSERT INTO state VALUES (1, 'red', NULL, 100)")
            old.close()
            store = Store(path)
            try:
                self.assertEqual(store.retry_not_before(), 0)
                self.assertIsNone(store.observe(Snapshot("red"), 110))
                self.assertIsNone(store.pending())
            finally:
                store.close()


class TestMessageCommandTests(unittest.TestCase):
    def test_test_message_does_not_read_source_or_open_state(self):
        with patch.dict(os.environ, TELEGRAM_ENV, clear=True):
            with patch("sys.argv", ["kyiv_alert", "--test-notification"]):
                with patch("kyiv_alert.__main__.create_notifier") as factory:
                    with patch("kyiv_alert.__main__.Store") as store:
                        with patch("kyiv_alert.__main__.NeptunSource") as source:
                            self.assertEqual(main(), 0)
                    store.assert_not_called()
                    source.assert_not_called()
                message = factory.return_value.send.call_args.args[1]
                self.assertIn("[ТЕСТ]", message)

    def test_failure_exits_nonzero_without_leaking_credentials(self):
        with patch.dict(os.environ, TELEGRAM_ENV, clear=True):
            with patch("sys.argv", ["kyiv_alert", "--test-notification"]):
                with patch("kyiv_alert.__main__.create_notifier") as factory:
                    factory.return_value.send.side_effect = DeliveryError("Telegram network error")
                    with self.assertLogs(level="ERROR") as logs:
                        self.assertEqual(main(), 1)
                self.assertNotIn(TOKEN, " ".join(logs.output))


class LocalHttpTests(unittest.TestCase):
    def test_real_http_requests_success_rate_limit_and_redirect_rejection(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                received.append((self.path, body))
                if self.path == "/redirect":
                    self.send_response(302)
                    self.send_header("Location", "/trap")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                limited = self.path == "/rate-limit"
                reply = ({"ok": False, "parameters": {"retry_after": 45}}
                         if limited else {"ok": True, "result": {"message_id": 1}})
                data = json.dumps(reply).encode()
                self.send_response(429 if limited else 200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                received.append((self.path, None))
                self.send_error(500)

            def log_message(self, *_):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        notifier = TelegramNotifier(TOKEN, CHAT)
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            notifier._url = base + "/success"
            notifier.send(1, "Київ: перевірка")
            self.assertEqual(received[-1][1]["text"], "Київ: перевірка")
            notifier._url = base + "/rate-limit"
            with self.assertRaises(DeliveryError) as caught:
                notifier.send(2, "test")
            self.assertEqual(caught.exception.retry_after, 45)
            notifier._url = base + "/redirect"
            with self.assertRaises(DeliveryError):
                notifier.send(3, "test")
            self.assertEqual([path for path, _ in received],
                             ["/success", "/rate-limit", "/redirect"])
        finally:
            server.shutdown()
            thread.join(timeout=2)
            server.server_close()


if __name__ == "__main__":
    unittest.main()
