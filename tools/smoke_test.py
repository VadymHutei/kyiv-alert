"""Exercise actual containers in an isolated Compose project; requires a built image."""
import json
import subprocess
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
READ_STATE = """
import json, sqlite3
c = sqlite3.connect('file:/data/state.sqlite3?mode=ro', uri=True)
c.row_factory = sqlite3.Row
state = c.execute('SELECT * FROM state').fetchone()
events = c.execute('SELECT * FROM outbox ORDER BY id').fetchall()
print(json.dumps({'state': dict(state) if state else None,
                  'events': [dict(row) for row in events]}))
c.close()
"""


def main():
    project = "kyiv-alert-smoke-" + uuid.uuid4().hex[:8]
    base = ["docker", "compose", "-p", project,
            "-f", "compose.yaml", "-f", "compose.mock.yaml"]

    def compose(*args, check=True):
        result = subprocess.run(base + list(args), cwd=ROOT, capture_output=True,
                                text=True, encoding="utf-8", timeout=90)
        if check and result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
        return result

    def read():
        result = compose("exec", "-T", "alert", "python", "-c", READ_STATE)
        return json.loads(result.stdout)

    def set_mode(mode):
        compose("exec", "-T", "mock-api", "python", "tools/mock_api.py", "set", mode)

    def wait_for(level, count, after=0):
        deadline = time.monotonic() + 25
        last = None
        while time.monotonic() < deadline:
            last = read()
            state = last["state"]
            sent = [event for event in last["events"] if event["status"] == "sent"]
            if state and state["level"] == level and len(sent) == count and state["observed_at"] > after:
                return last
            time.sleep(1)
        raise AssertionError(f"Expected level={level}, sent={count}; got {last}")

    try:
        print(f"Starting isolated project {project}", flush=True)
        compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "60")
        wait_for("none", 0)
        set_mode("red")
        initial = wait_for("red", 1)
        # Observe a second unchanged snapshot: there must be no duplicate.
        before_restart = wait_for("red", 1, initial["state"]["observed_at"])
        compose("restart", "alert")
        after_restart = wait_for("red", 1, before_restart["state"]["observed_at"])
        print("PASS red onset, repeated snapshot, persistent restart", flush=True)

        for mode in ("invalid", "error"):
            set_mode(mode)
            time.sleep(6)
            state = read()
            assert state["state"]["level"] == "red", state
            assert len(state["events"]) == 1, state
            assert state["state"]["observed_at"] == after_restart["state"]["observed_at"], state
        print("PASS invalid JSON shape / HTTP 503 preserve red state", flush=True)

        set_mode("yellow")
        wait_for("yellow", 2)
        set_mode("off")
        wait_for("none", 2)
        set_mode("oblast-red")
        current = read()["state"]["observed_at"]
        wait_for("none", 2, current)
        print("PASS downgrade notification, yellow-only silence, oblast exclusion", flush=True)

        set_mode("red")
        wait_for("red", 3)
        set_mode("off")
        wait_for("none", 4)
        logs = compose("logs", "--no-color", "alert").stdout
        # Restart logs include old output, but unchanged states must not add events.
        assert logs.count("NOTIFICATION ") == 4, logs
        assert "[ТЕСТ" in logs, logs
        assert "Жовтий рівень залишається" in logs, logs
        compose("exec", "-T", "alert", "python", "-m", "kyiv_alert", "--healthcheck")
        print("PASS red removal, four console notifications, healthy service", flush=True)
    except BaseException:
        print(compose("logs", "--no-color", "--tail", "30", check=False).stdout, flush=True)
        raise
    finally:
        # Only this unique test project's containers and volumes are removed.
        compose("down", "--volumes", "--remove-orphans", check=False)
        print(f"Cleaned up {project}", flush=True)


if __name__ == "__main__":
    main()
