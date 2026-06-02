"""
scheduler.py — запускает polki_tracker.main() каждый день в заданное время МСК.
Этот файл — точка входа Docker-контейнера.
"""

import logging
import time
from datetime import datetime, timezone, timedelta

import schedule

import polki_tracker

log = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3))

RUN_TIME_MSK = "02:00"   # время запуска по МСК


def job():
    log.info("=== Планировщик: старт задачи ===")
    code = polki_tracker.main()
    if code != 0:
        log.error("polki_tracker завершился с кодом %d", code)


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-8s  %(message)s",
    )
    log.info("Планировщик запущен. Запуск трекера каждый день в %s МСК", RUN_TIME_MSK)

    # schedule работает в локальном времени процесса — запускаем контейнер с TZ=Europe/Moscow
    schedule.every().day.at(RUN_TIME_MSK).do(job)

    while True:
        schedule.run_pending()
        time.sleep(30)


if __name__ == "__main__":
    main()
