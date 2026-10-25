import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    alerts_url: str = field(default="https://neptun.in.ua/api/v1/alerts", repr=False)
    poll_interval: float = 10
    http_timeout: float = 5
    state_path: Path = Path("data/state.sqlite3")
    notification_max_age: float = 300
    health_max_age: float = 120
    demo_mode: bool = False
    notifier: str = "console"
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = field(default="", repr=False)
    telegram_message_thread_id: int | None = None

    @classmethod
    def from_env(cls):
        def number(name, default):
            try:
                value = float(os.environ.get(name, default))
            except ValueError:
                raise ValueError(f"{name} must be a finite positive number") from None
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a finite positive number")
            return value

        url = os.environ.get("ALERTS_URL", cls.alerts_url)
        parts = urlsplit(url)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError("ALERTS_URL must be an HTTP(S) URL")
        if parts.username or parts.password:
            raise ValueError("Do not put credentials in ALERTS_URL")
        interval = number("POLL_INTERVAL_SECONDS", 10)
        if interval < 5:
            raise ValueError("POLL_INTERVAL_SECONDS must be at least 5")
        demo_mode = os.environ.get("DEMO_MODE", "false").lower()
        if demo_mode not in {"true", "false"}:
            raise ValueError("DEMO_MODE must be true or false")
        notifier = os.environ.get("NOTIFIER", "console").strip().lower()
        if notifier not in {"console", "telegram"}:
            raise ValueError("NOTIFIER must be console or telegram")
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        thread = os.environ.get("TELEGRAM_MESSAGE_THREAD_ID", "").strip()
        thread_id = None
        if notifier == "telegram":
            if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
                raise ValueError("TELEGRAM_BOT_TOKEN is required and must be a bot token")
            if not (re.fullmatch(r"-?[0-9]+", chat_id) and int(chat_id) != 0
                    or re.fullmatch(r"@[A-Za-z0-9_]+", chat_id)):
                raise ValueError("TELEGRAM_CHAT_ID must be a nonzero chat ID or @username")
            if thread:
                if not re.fullmatch(r"[0-9]+", thread) or int(thread) <= 0:
                    raise ValueError("TELEGRAM_MESSAGE_THREAD_ID must be a positive integer")
                thread_id = int(thread)
        return cls(
            alerts_url=url,
            poll_interval=interval,
            http_timeout=number("HTTP_TIMEOUT_SECONDS", 5),
            state_path=Path(os.environ.get("STATE_PATH", "data/state.sqlite3")),
            notification_max_age=number("NOTIFICATION_MAX_AGE_SECONDS", 300),
            health_max_age=number("HEALTH_MAX_AGE_SECONDS", 120),
            demo_mode=demo_mode == "true",
            notifier=notifier,
            telegram_bot_token=token,
            telegram_chat_id=chat_id,
            telegram_message_thread_id=thread_id,
        )
