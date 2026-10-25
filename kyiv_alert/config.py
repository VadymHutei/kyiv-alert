import math
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Config:
    alerts_url: str = "https://neptun.in.ua/api/v1/alerts"
    poll_interval: float = 10
    http_timeout: float = 5
    state_path: Path = Path("data/state.sqlite3")
    notification_max_age: float = 300
    health_max_age: float = 120
    demo_mode: bool = False

    @classmethod
    def from_env(cls):
        def number(name, default):
            value = float(os.environ.get(name, default))
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
        return cls(
            alerts_url=url,
            poll_interval=interval,
            http_timeout=number("HTTP_TIMEOUT_SECONDS", 5),
            state_path=Path(os.environ.get("STATE_PATH", "data/state.sqlite3")),
            notification_max_age=number("NOTIFICATION_MAX_AGE_SECONDS", 300),
            health_max_age=number("HEALTH_MAX_AGE_SECONDS", 120),
            demo_mode=demo_mode == "true",
        )
