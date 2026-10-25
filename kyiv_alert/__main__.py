import argparse
import logging
import signal
import sys
import threading
import time

from .config import Config
from .notifier import DeliveryError, create_notifier
from .service import Service, is_healthy
from .source import NeptunSource
from .store import Store


def main():
    parser = argparse.ArgumentParser(description="Kyiv red threat notifications")
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--healthcheck", action="store_true")
    actions.add_argument("--test-notification", action="store_true",
                         help="Send a clearly marked test message without polling or changing state")
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
    notifier = create_notifier(config)
    if args.test_notification:
        try:
            notifier.send(0, "[ТЕСТ] Kyiv Alert: перевірка доставки сповіщень.\n"
                             "Це тестове повідомлення, а не повідомлення про повітряну загрозу.")
        except OSError as error:
            detail = str(error) if isinstance(error, DeliveryError) else type(error).__name__
            logging.error("Test notification failed: %s", detail)
            return 1
        logging.info("Test notification delivered via %s", config.notifier)
        return 0
    stop = threading.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: stop.set())
    store = Store(config.state_path)
    try:
        Service(config, NeptunSource(config.alerts_url, config.http_timeout),
                store, notifier).run(stop)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
