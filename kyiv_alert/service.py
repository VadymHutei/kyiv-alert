import logging
from http.client import HTTPException
import sqlite3
import time
from pathlib import Path

from .notifier import DeliveryError, render_notification


logger = logging.getLogger(__name__)


class Service:
    def __init__(self, config, source, store, notifier):
        self.config = config
        self.source = source
        self.store = store
        self.notifier = notifier
        self.source_failures = 0
        self.delivery_failures = 0
        self.next_delivery_at = self.store.retry_not_before()
        self.last_level = None

    def tick(self, now=None):
        use_clock = now is None
        try:
            snapshot = self.source.fetch()
        except (OSError, ValueError, HTTPException) as error:
            self.source_failures += 1
            # Log categories, not request URLs that might contain secrets.
            logger.warning("Source unavailable or invalid (%s), failure=%d; state retained",
                           type(error).__name__, self.source_failures)
            return False
        now = time.time() if now is None else now
        if self.source_failures:
            logger.info("Source recovered after %d failures", self.source_failures)
            self.source_failures = 0
        kind = self.store.observe(snapshot, now, self.config.notification_max_age)
        if self.last_level != snapshot.level or kind:
            logger.info("Kyiv snapshot accepted: level=%s event=%s", snapshot.level, kind or "none")
        self.last_level = snapshot.level
        if not kind:
            logger.debug("Snapshot checked: Kyiv level=%s", snapshot.level)
        event = self.store.pending()
        if event is not None and now >= self.next_delivery_at:
            text = render_notification(event)
            if self.config.demo_mode:
                text = "[ТЕСТ — симульовані дані]\n" + text
            try:
                self.notifier.send(event["id"], text)
            except OSError as error:
                self.delivery_failures += 1
                delay = min(300, 5 * 2 ** min(self.delivery_failures - 1, 6))
                if isinstance(error, DeliveryError) and error.retry_after is not None:
                    delay = max(delay, error.retry_after)
                self.next_delivery_at = (time.time() if use_clock else now) + delay
                self.store.defer_delivery(self.next_delivery_at)
                detail = str(error) if isinstance(error, DeliveryError) else type(error).__name__
                logger.warning("Notification failed (%s); retry in %ss", detail, delay)
            else:
                self.store.mark_delivered(event["id"], now)
                self.delivery_failures = 0
                self.next_delivery_at = 0
        return True

    def run(self, stop):
        logger.info("Started; city=Kyiv notifier=%s poll_interval=%ss",
                    self.config.notifier, self.config.poll_interval)
        while not stop.is_set():
            self.tick()
            # Start-to-start is at least the configured interval, including errors.
            stop.wait(self.config.poll_interval)
        logger.info("Stopped")


def is_healthy(config, now=None):
    now = time.time() if now is None else now
    try:
        path = Path(config.state_path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(path, uri=True, timeout=1)
        try:
            row = connection.execute("SELECT observed_at FROM state WHERE id=1").fetchone()
            return row is not None and 0 <= now - row[0] <= config.health_max_age
        finally:
            connection.close()
    except sqlite3.Error:
        return False
