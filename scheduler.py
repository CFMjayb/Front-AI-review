"""EDOM scheduler — runs pipeline every N hours, digest every Monday at 7am."""
import logging
import os
import signal
import sys
import time

from dotenv import load_dotenv
load_dotenv(override=True)

import schedule

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

RUN_INTERVAL_HOURS = int(os.environ.get("RUN_INTERVAL_HOURS", "4"))


def _run_pipeline():
    logger.info("Scheduled pipeline run starting")
    try:
        from pipeline import run_pipeline
        run_pipeline()
    except Exception as exc:
        logger.error(f"Scheduled pipeline run failed: {exc}")


def _run_digest():
    logger.info("Scheduled digest run starting")
    try:
        from digest import run_digest
        run_digest()
    except Exception as exc:
        logger.error(f"Scheduled digest run failed: {exc}")


def _run_briefing():
    logger.info("Scheduled chief-of-staff briefing starting")
    try:
        from cos.briefing import run_briefing
        run_briefing()
    except Exception as exc:
        logger.error(f"Scheduled briefing run failed: {exc}")


def main():
    logger.info(f"EDOM scheduler starting — pipeline every {RUN_INTERVAL_HOURS}h, "
                f"briefing daily 06:00, digest Mondays 07:00")

    schedule.every(RUN_INTERVAL_HOURS).hours.do(_run_pipeline)
    schedule.every().day.at("06:00").do(_run_briefing)
    schedule.every().monday.at("07:00").do(_run_digest)

    def _stop(signum, frame):
        logger.info("Scheduler stopping")
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while True:
        schedule.run_pending()
        time.sleep(60)


if __name__ == "__main__":
    main()
