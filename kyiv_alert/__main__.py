import argparse
import logging
import signal
import sys
import threading
import time

from .config import Config
from .notifier import ConsoleNotifier
from .service import Service, is_healthy
from .source import NeptunSource
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Kyiv red threat notifications (console mode)")
    parser.add_argument("--healthcheck", action="store_true")
    args = parser.parse_args()
    logging.Formatter.converter = time.gmtime
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)sZ %(levelname)s %(name)s %(message)s")
    try:
        config = Config.from_env()
    except ValueError as error:
        parser.error(str(error))
    if args.healthcheck:
        return 0 if is_healthy(config) else 1
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    store = Store(config.state_path)
    try:
        Service(config, NeptunSource(config.alerts_url, config.http_timeout),
                store, ConsoleNotifier()).run(stop)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
