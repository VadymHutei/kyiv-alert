import json
import sys
from datetime import datetime, timezone
from typing import Protocol
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
