import json
import logging
import sys
from datetime import datetime, timezone
from http.client import HTTPException
from typing import Protocol
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zoneinfo import ZoneInfo


class Notifier(Protocol):
    def send(self, event_id: int, text: str) -> None: ...


class ConsoleNotifier:
    """A successful send means the notification was written to stdout."""
    def __init__(self, stream=None):
        self.stream = sys.stdout if stream is None else stream

    def send(self, event_id, text):
        # Write directly: logging handlers can swallow I/O errors, which would
        # incorrectly acknowledge a notification that never reached stdout.
        self.stream.write("NOTIFICATION " + json.dumps(
            {"event_id": event_id, "text": text}, ensure_ascii=False
        ) + "\n")
        self.stream.flush()


class DeliveryError(OSError):
    """Safe to log: never includes a request URL, token, or Telegram response body."""
    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Do not forward a bot token or message to an unexpected redirect target.
        return None


class TelegramNotifier:
    MAX_RESPONSE_BYTES = 64 * 1024

    def __init__(self, token, chat_id, timeout=5, message_thread_id=None):
        self._url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.timeout = timeout
        self.message_thread_id = message_thread_id
        self._opener = build_opener(NoRedirects())

    @staticmethod
    def _error(status, payload):
        retry_after = None
        if isinstance(payload, dict):
            parameters = payload.get("parameters")
            if isinstance(parameters, dict):
                value = parameters.get("retry_after")
                if type(value) is int and 0 < value <= 2**31 - 1:
                    retry_after = value
        advice = {
            400: "check chat ID, topic, and bot permissions",
            401: "check bot token",
            403: "check chat access and bot permissions",
            429: "rate limited",
        }.get(status, "delivery unavailable")
        return DeliveryError(f"Telegram error {status}: {advice}", retry_after)

    def _read_json(self, response):
        body = response.read(self.MAX_RESPONSE_BYTES + 1)
        if len(body) > self.MAX_RESPONSE_BYTES:
            raise ValueError("Response too large")
        return json.loads(body)

    def send(self, event_id, text):
        payload = {"chat_id": self.chat_id, "text": text,
                   "link_preview_options": {"is_disabled": True}}
        if self.message_thread_id is not None:
            payload["message_thread_id"] = self.message_thread_id
        request = Request(self._url, data=json.dumps(payload).encode("utf-8"),
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            try:
                with self._opener.open(request, timeout=self.timeout) as response:
                    status = response.status
                    result = self._read_json(response)
            except HTTPError as error:
                with error:
                    status = error.code
                    try:
                        result = self._read_json(error)
                    except (ValueError, OSError, HTTPException):
                        result = None
                raise self._error(status, result) from None
        except DeliveryError:
            raise
        except (OSError, HTTPException):
            raise DeliveryError("Telegram network error") from None
        except ValueError:
            raise DeliveryError("Telegram returned an invalid response") from None
        if status != 200 or not isinstance(result, dict) or result.get("ok") is not True:
            code = result.get("error_code", status) if isinstance(result, dict) else status
            # Do not include untrusted error values in logs.
            raise self._error(code if type(code) is int else status, result) from None
        message = result.get("result")
        if not isinstance(message, dict) or type(message.get("message_id")) is not int:
            raise DeliveryError("Telegram did not confirm a sent message")
        logging.getLogger(__name__).info("Telegram notification delivered: event_id=%s", event_id)


def create_notifier(config):
    if config.notifier == "telegram":
        return TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id,
                                config.http_timeout, config.telegram_message_thread_id)
    return ConsoleNotifier()


def render_notification(event, now, max_age):
    if event["kind"] == "sync" or now - event["created_at"] > max_age:
        status = {
            "red": "У Києві діє червоний рівень загрози.",
            "yellow": "У Києві немає червоного рівня. Жовтий рівень залишається.",
            "none": "У Києві немає червоного рівня загрози за даними джерела.",
        }[event["level"]]
        headline = "Поточний стан після затримки доставки: " + status
    elif event["kind"] == "active":
        headline = "🔴 У Києві діє червоний рівень загрози."
    elif event["kind"] == "started":
        headline = "🔴 У Києві виявлено червоний рівень загрози."
    else:
        headline = "Червоний рівень загрози в Києві знято за даними джерела."
        if event["level"] == "yellow":
            headline += " Жовтий рівень залишається."

    # Windows may lack the IANA database; Docker installs OS tzdata.
    try:
        local_zone = ZoneInfo("Europe/Kyiv")
        zone_label = "за Києвом"
    except KeyError:
        local_zone = timezone.utc
        zone_label = "UTC"
    checked_at = datetime.fromtimestamp(now, local_zone).strftime("%d.%m.%Y %H:%M:%S")
    lines = [headline, f"Перевірено: {checked_at} ({zone_label})."]
    if event["level"] == "red" and event["since"]:
        since = datetime.fromisoformat(event["since"]).astimezone(local_zone)
        lines.append(f"Початок за даними джерела: {since:%d.%m.%Y %H:%M:%S} ({zone_label}).")
    lines.extend([
        "Джерело: NEPTUN — https://neptun.in.ua/",
        "Інформаційний сервіс. Орієнтуйтеся на офіційні сигнали повітряної тривоги.",
    ])
    return "\n".join(lines)
