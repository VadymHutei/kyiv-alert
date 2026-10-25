import json
from dataclasses import dataclass
from datetime import datetime
from urllib.request import Request, urlopen


CITY_KEYS = frozenset({"м. київ", "м.київ", "київ", "місто київ"})
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class InvalidSnapshot(ValueError):
    pass


@dataclass(frozen=True)
class Snapshot:
    level: str  # none, yellow, red
    since: str | None = None


def parse_snapshot(payload):
    """Only absence in a structurally valid full snapshot means no city alert."""
    if not isinstance(payload, dict):
        raise InvalidSnapshot("Expected a JSON object")
    city = []
    for field in ("raions", "oblasts"):
        records = payload.get(field)
        if not isinstance(records, list):
            raise InvalidSnapshot(f"Missing or invalid {field} array")
        for record in records:
            if not isinstance(record, dict):
                raise InvalidSnapshot("Alert entry must be an object")
            key = record.get("key")
            if not isinstance(key, str) or not key.strip():
                raise InvalidSnapshot("Alert entry must have a nonempty key")
            if key.strip().casefold() not in CITY_KEYS:
                continue
            level = record.get("level")
            if not isinstance(level, str) or level not in {"red", "yellow"}:
                raise InvalidSnapshot("Unknown or missing Kyiv threat level")
            since = record.get("since")
            if since is not None:
                try:
                    parsed = datetime.fromisoformat(since)
                    if parsed.tzinfo is None:
                        raise ValueError("Timestamp needs a timezone")
                except (TypeError, ValueError):
                    raise InvalidSnapshot("Invalid Kyiv since timestamp") from None
            city.append(Snapshot(level, since))
    if not city:
        return Snapshot("none")
    # A yellow record must never hide a concurrent red record.
    return next((item for item in city if item.level == "red"), city[0])


class NeptunSource:
    def __init__(self, url, timeout):
        self.url = url
        self.timeout = timeout

    def fetch(self):
        request = Request(self.url, headers={
            "Accept": "application/json",
            "User-Agent": "kyiv-alert/0.1",
        })
        with urlopen(request, timeout=self.timeout) as response:
            if response.status != 200:
                raise InvalidSnapshot(f"Unexpected HTTP status {response.status}")
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise InvalidSnapshot("Response exceeds size limit")
        return parse_snapshot(json.loads(body))
