import sqlite3
from pathlib import Path


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        with self.connection:
            self.connection.executescript("""
                CREATE TABLE IF NOT EXISTS state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    level TEXT NOT NULL,
                    since TEXT,
                    observed_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    level TEXT NOT NULL,
                    since TEXT,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    delivered_at REAL
                );
            """)

    def close(self):
        self.connection.close()

    def observe(self, snapshot, now, resync_after=300):
        """Persist the state and its notification in the same transaction."""
        with self.connection:
            previous = self.connection.execute("SELECT * FROM state WHERE id = 1").fetchone()
            kind = None
            if previous is None:
                if snapshot.level == "red":
                    kind = "active"
            elif previous["level"] != snapshot.level:
                if snapshot.level == "red":
                    kind = "started"
                elif previous["level"] == "red":
                    kind = "ended"

            if previous is not None and now - previous["observed_at"] > resync_after:
                kind = "sync"

            # Only send the latest state, never an obsolete pending start/end.
            if previous is not None and (previous["level"] != snapshot.level or kind == "sync"):
                pending = self.pending()
                if pending is not None and kind is None:
                    kind = "sync"
                self.connection.execute(
                    "UPDATE outbox SET status = 'superseded' WHERE status = 'pending'"
                )
            self.connection.execute(
                """INSERT INTO state VALUES (1, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET level=excluded.level,
                       since=excluded.since, observed_at=excluded.observed_at""",
                (snapshot.level, snapshot.since, now),
            )
            if kind:
                self.connection.execute(
                    "INSERT INTO outbox (kind, level, since, created_at) VALUES (?, ?, ?, ?)",
                    (kind, snapshot.level, snapshot.since, now),
                )
            # Keep a week of delivery records, plus all pending messages.
            self.connection.execute(
                "DELETE FROM outbox WHERE status != 'pending' AND created_at < ?",
                (now - 7 * 86400,),
            )
        return kind

    def pending(self):
        return self.connection.execute(
            "SELECT * FROM outbox WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()

    def mark_delivered(self, event_id, now):
        with self.connection:
            self.connection.execute(
                "UPDATE outbox SET status = 'sent', delivered_at = ? WHERE id = ?",
                (now, event_id),
            )
