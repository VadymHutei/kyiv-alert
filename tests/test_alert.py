import io
import json
import os
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from kyiv_alert.config import Config
from kyiv_alert.notifier import ConsoleNotifier, render_notification
from kyiv_alert.service import Service, is_healthy
from kyiv_alert.source import InvalidSnapshot, Snapshot, parse_snapshot
from kyiv_alert.store import Store


def payload(level="red", key="м. київ", field="oblasts"):
    data = {"raions": [], "oblasts": []}
    data[field] = [{"key": key, "level": level, "since": "2026-10-25T12:00:00Z"}]
    return data


class ParserTests(unittest.TestCase):
    def test_kyiv_in_both_collections(self):
        for field in ("raions", "oblasts"):
            with self.subTest(field=field):
                self.assertEqual(parse_snapshot(payload(field=field)).level, "red")

    def test_city_does_not_include_oblast_or_districts(self):
        for key in ("київська", "броварський", "бучанський", "львівська"):
            self.assertEqual(parse_snapshot(payload(key=key)).level, "none")

    def test_empty_snapshot_means_none(self):
        self.assertEqual(parse_snapshot({"raions": [], "oblasts": []}), Snapshot("none"))

    def test_red_wins_over_yellow(self):
        data = payload("yellow")
        data["raions"] = payload("red")["oblasts"]
        self.assertEqual(parse_snapshot(data).level, "red")

    def test_invalid_data_does_not_mean_end(self):
        examples = [None, [], {}, {"raions": [], "oblasts": None},
                    {"raions": [None], "oblasts": []},
                    {"raions": [{}], "oblasts": []}]
        for level in (None, "green", [], {}):
            examples.append(payload(level))
        invalid_time = payload()
        invalid_time["oblasts"][0]["since"] = "yesterday"
        examples.append(invalid_time)
        for data in examples:
            with self.subTest(data=data), self.assertRaises(InvalidSnapshot):
                parse_snapshot(data)


class FakeSource:
    value = Snapshot("none")

    def fetch(self):
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class Recorder:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send(self, event_id, text):
        if self.fail:
            raise OSError("simulated delivery failure")
        self.messages.append((event_id, text))


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = replace(Config(), state_path=Path(self.temp.name) / "state.sqlite3")
        self.store = Store(self.config.state_path)
        self.source = FakeSource()
        self.notifier = Recorder()
        self.service = Service(self.config, self.source, self.store, self.notifier)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def tick(self, level, now):
        self.source.value = level if isinstance(level, Exception) else Snapshot(level)
        return self.service.tick(now)

    def test_start_repeat_downgrade_and_end(self):
        for time, level in enumerate(("none", "yellow", "red", "red", "yellow", "none")):
            self.tick(level, 100 + time)
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertTrue(self.notifier.messages[0][1].startswith("🔴 Тривога"))
        self.assertTrue(self.notifier.messages[1][1].startswith("🟡 Відбій червоного рівня"))

    def test_red_to_absent(self):
        self.tick("red", 100)
        self.tick("none", 110)
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertTrue(self.notifier.messages[1][1].startswith("🟢 Відбій"))

    def test_first_start_during_red_does_not_invent_start_time(self):
        self.tick("red", 100)
        self.assertIn("Початок: невідомо", self.notifier.messages[0][1])

    def test_restart_does_not_repeat_red(self):
        self.tick("red", 100)
        self.store.close()
        self.store = Store(self.config.state_path)
        self.service = Service(self.config, self.source, self.store, self.notifier)
        self.tick("red", 110)
        self.assertEqual(len(self.notifier.messages), 1)

    def test_source_error_preserves_state_and_suppresses_delivery(self):
        self.notifier.fail = True
        self.tick("red", 100)
        for time, error in enumerate((InvalidSnapshot("bad"), TimeoutError(), OSError())):
            self.assertFalse(self.tick(error, 110 + time))
        self.assertEqual(self.store.pending()["level"], "red")
        self.assertEqual(self.store.connection.execute("SELECT level FROM state").fetchone()[0], "red")
        self.assertEqual(len(self.notifier.messages), 0)

    def test_pending_delivery_survives_restart(self):
        self.notifier.fail = True
        self.tick("red", 100)
        event_id = self.store.pending()["id"]
        self.store.close()
        self.store = Store(self.config.state_path)
        self.service = Service(self.config, self.source, self.store, self.notifier)
        self.notifier.fail = False
        self.tick("red", 110)
        self.assertEqual(self.notifier.messages[0][0], event_id)
        self.assertIsNone(self.store.pending())

    def test_obsolete_start_is_not_sent_after_recovery(self):
        self.notifier.fail = True
        self.tick("red", 100)
        self.notifier.fail = False
        self.tick("none", 110)
        self.assertEqual(len(self.notifier.messages), 1)
        self.assertTrue(self.notifier.messages[0][1].startswith("🟢 Відбій"))
        statuses = [r[0] for r in self.store.connection.execute("SELECT status FROM outbox ORDER BY id")]
        self.assertEqual(statuses, ["superseded", "sent"])

    def test_long_source_outage_sends_current_state(self):
        self.tick("red", 100)
        self.tick(TimeoutError(), 200)
        self.tick("red", 500)
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertTrue(self.notifier.messages[-1][1].startswith("🔴 Тривога"))
        self.assertIn("Початок: невідомо", self.notifier.messages[-1][1])

    def test_delivery_backoff(self):
        self.notifier.fail = True
        self.tick("red", 100)
        self.notifier.fail = False
        self.tick("red", 104)
        self.assertEqual(len(self.notifier.messages), 0)
        self.tick("red", 105)
        self.assertEqual(len(self.notifier.messages), 1)

    def test_health_uses_successful_observation_time(self):
        self.assertFalse(is_healthy(self.config, 100))
        self.tick("none", 100)
        self.assertTrue(is_healthy(self.config, 110))
        self.tick(TimeoutError(), 240)
        self.assertFalse(is_healthy(self.config, 240))

    def test_delayed_end_keeps_detection_time_instead_of_delivery_time(self):
        self.tick("red", 100)
        self.notifier.fail = True
        self.tick("none", 110)
        pending = self.store.pending()
        expected = render_notification(pending)
        self.notifier.fail = False
        self.tick("none", 200)
        self.assertEqual(self.notifier.messages[-1][1], expected)
        self.assertIsNone(self.store.pending())

    def test_simulated_notifications_are_explicitly_marked(self):
        self.service.config = replace(self.config, demo_mode=True)
        self.tick("red", 100)
        self.assertTrue(self.notifier.messages[0][1].startswith("[ТЕСТ"))

    def test_failed_end_is_updated_when_yellow_disappears(self):
        self.tick("red", 100)
        self.notifier.fail = True
        self.tick("yellow", 110)
        self.notifier.fail = False
        self.tick("none", 120)
        self.assertEqual(len(self.notifier.messages), 2)
        self.assertTrue(self.notifier.messages[-1][1].startswith("🟢 Відбій"))
        self.assertIn("Закінчення: невідомо", self.notifier.messages[-1][1])

    def test_enqueue_failure_rolls_back_state(self):
        self.store.observe(Snapshot("none"), 100)
        self.store.connection.execute("""
            CREATE TRIGGER reject_enqueue BEFORE INSERT ON outbox
            BEGIN SELECT RAISE(ABORT, 'simulated storage failure'); END;
        """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.observe(Snapshot("red"), 110)
        state = self.store.connection.execute("SELECT level, observed_at FROM state").fetchone()
        self.assertEqual(tuple(state), ("none", 100))
        self.assertIsNone(self.store.pending())


class MessageTimeTests(unittest.TestCase):
    def test_source_start_time_and_two_line_format(self):
        event = {"kind": "active", "level": "red", "since": "2026-10-25T12:00:00Z",
                 "created_at": datetime(2026, 10, 25, 14, tzinfo=timezone.utc).timestamp()}
        with patch("kyiv_alert.notifier.ZoneInfo", return_value=timezone(timedelta(hours=2))):
            text = render_notification(event)
        self.assertEqual(text, "🔴 Тривога в Києві\nПочаток: 25.10.2026 14:00")

    def test_end_uses_observed_time_not_yellow_since(self):
        event = {"kind": "ended", "level": "yellow", "since": "2026-10-25T01:00:00Z",
                 "created_at": datetime(2026, 10, 25, 12, 30, tzinfo=timezone.utc).timestamp()}
        with patch("kyiv_alert.notifier.ZoneInfo", return_value=timezone(timedelta(hours=2))):
            text = render_notification(event)
        self.assertEqual(text, "🟡 Відбій червоного рівня в Києві\nЗакінчення: ≈ 25.10.2026 14:30")

    def test_utc_fallback_is_labelled(self):
        event = {"kind": "started", "level": "red", "since": "2026-10-25T12:00:00Z",
                 "created_at": 100}
        with patch("kyiv_alert.notifier.ZoneInfo", side_effect=KeyError):
            text = render_notification(event)
        self.assertEqual(text, "🔴 Тривога в Києві\nПочаток: 25.10.2026 12:00 UTC")

    def test_recovery_does_not_invent_end_time(self):
        event = {"kind": "sync", "level": "none", "since": None, "created_at": 100}
        self.assertEqual(render_notification(event),
                         "🟢 Відбій червоного рівня в Києві\nЗакінчення: невідомо")


class ConsoleTests(unittest.TestCase):
    def test_output_is_one_json_record(self):
        stream = io.StringIO()
        ConsoleNotifier(stream).send(42, "Тест\nДругий рядок")
        lines = stream.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0].removeprefix("NOTIFICATION ")),
                         {"event_id": 42, "text": "Тест\nДругий рядок"})

    def test_output_error_is_not_acknowledged(self):
        stream = io.StringIO()
        stream.close()
        # Closed streams raise ValueError; actual stdout I/O failures raise OSError.
        with self.assertRaises(ValueError):
            ConsoleNotifier(stream).send(1, "test")

    def test_flush_failure_is_propagated(self):
        class BrokenStream(io.StringIO):
            def flush(self):
                raise OSError("stdout unavailable")
        with self.assertRaises(OSError):
            ConsoleNotifier(BrokenStream()).send(1, "test")


class ConfigTests(unittest.TestCase):
    def test_invalid_poll_intervals(self):
        for value in ("0", "4", "nan", "inf", "-1"):
            with self.subTest(value=value), patch.dict(os.environ, {"POLL_INTERVAL_SECONDS": value}):
                with self.assertRaises(ValueError):
                    Config.from_env()

    def test_health_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(Config(), state_path=Path(directory) / "missing.sqlite3")
            self.assertFalse(is_healthy(config))
            self.assertFalse(config.state_path.exists())


if __name__ == "__main__":
    unittest.main()
