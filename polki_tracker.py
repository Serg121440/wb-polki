#!/usr/bin/env python3
"""
polki_tracker.py — ежедневный трекер позиций в карусели «Рекомендуем» на WB.

Запуск:  python polki_tracker.py
Cron:    0 2 * * * /path/to/venv/bin/python /path/to/polki_tracker.py >> /var/log/wb_polki.log 2>&1
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import base64
import json
import tempfile

import gspread
import requests
from dotenv import load_dotenv
from google.oauth2.service_account import Credentials

import config

# ---------------------------------------------------------------------------
# Инициализация
# ---------------------------------------------------------------------------
load_dotenv()

MSK = timezone(timedelta(hours=3))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("wb_polki.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Telegram-алерт
# ---------------------------------------------------------------------------
def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        log.warning("TELEGRAM_TOKEN или TELEGRAM_CHAT_ID не заданы — алерт пропущен")
        return
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
    except Exception as exc:
        log.error("Не удалось отправить Telegram-алерт: %s", exc)


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
def get_gsheet():
    sheet_id = os.getenv("SHEET_ID")
    if not sheet_id:
        raise EnvironmentError("SHEET_ID не задан в .env")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]

    # Вариант 1 (Docker / relaxdev.ru): JSON в base64 (надёжнее — без проблем с \n)
    sa_b64 = os.getenv("GOOGLE_SA_JSON_B64")
    sa_content = os.getenv("GOOGLE_SA_JSON_CONTENT")
    if sa_b64:
        info = json.loads(base64.b64decode(sa_b64).decode())
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    elif sa_content:
        # Фолбэк: если вставлен как обычный JSON
        info = json.loads(sa_content)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        # Вариант 2 (локальный запуск): путь к файлу ключа
        sa_path = os.getenv("GOOGLE_SA_JSON")
        if not sa_path:
            raise EnvironmentError("Задайте GOOGLE_SA_JSON_B64, GOOGLE_SA_JSON_CONTENT или GOOGLE_SA_JSON в .env")
        creds = Credentials.from_service_account_file(sa_path, scopes=scopes)

    gc = gspread.authorize(creds)
    return gc.open_by_key(sheet_id)


def read_watchlist(spreadsheet) -> list[dict]:
    """Читает вкладку «Полки_вход», возвращает список записей."""
    try:
        ws = spreadsheet.worksheet(config.INPUT_SHEET_NAME)
    except gspread.WorksheetNotFound:
        raise RuntimeError(
            f"Вкладка '{config.INPUT_SHEET_NAME}' не найдена. "
            "Создайте её вручную — см. README."
        )

    records = ws.get_all_records(expected_headers=["Категория", "SKU", "Бренд", "Наш"])
    result = []
    for row in records:
        sku_raw = str(row.get("SKU", "")).strip()
        if not sku_raw.isdigit():
            continue
        result.append(
            {
                "category": str(row["Категория"]).strip(),
                "sku": int(sku_raw),
                "brand": str(row.get("Бренд", "")).strip(),
                "is_ours": str(row.get("Наш", "")).strip().lower() in ("да", "yes", "1", "true"),
            }
        )
    log.info("Прочитано %d SKU из '%s'", len(result), config.INPUT_SHEET_NAME)
    return result


def ensure_log_sheet(spreadsheet):
    """Создаёт вкладку лога с шапкой, если её нет."""
    try:
        ws = spreadsheet.worksheet(config.LOG_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(config.LOG_SHEET_NAME, rows=10000, cols=len(config.LOG_HEADERS))
        ws.append_row(config.LOG_HEADERS, value_input_option="RAW")
        log.info("Создана вкладка '%s'", config.LOG_SHEET_NAME)
    return ws


def append_rows(ws, rows: list[list]) -> None:
    if not rows:
        return
    ws.append_rows(rows, value_input_option="RAW")
    log.info("Дописано %d строк в '%s'", len(rows), config.LOG_SHEET_NAME)


# ---------------------------------------------------------------------------
# WB API
# ---------------------------------------------------------------------------
def _get_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": config.USER_AGENT})
    return s


def fetch_recom(session: requests.Session, nm: int, dest: int) -> tuple[list[int], int]:
    """
    Запрашивает полку «Смотрите также» для карточки nm.
    Возвращает (список nmID до SHELF_DEPTH, фактическая длина).
    При ошибке возвращает ([], 0).
    """
    params = {**config.WB_RECOM_PARAMS, "query": nm}

    for attempt in range(1, config.RETRY_MAX + 1):
        try:
            resp = session.get(
                config.WB_RECOM_URL,
                params=params,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code == 429:
                wait = config.RETRY_BACKOFF ** attempt * 5
                log.warning("429 для nm=%d, ждём %.0fs", nm, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()

            data = resp.json()

            # Структура ответа u-recom.wb.ru: {"products": [...], "total": N}
            products = (
                data.get("products")
                or data.get("data", {}).get("products")
                or []
            )
            if not products and attempt == 1:
                log.debug("nm=%d: неожиданная схема ответа, ключи: %s", nm, list(data.keys()))

            ids = [int(p.get("id") or p.get("nmId") or 0) for p in products if p.get("id") or p.get("nmId")]
            ids = [x for x in ids if x]
            return ids[: config.SHELF_DEPTH], len(ids)

        except (requests.RequestException, ValueError) as exc:
            log.warning("Ошибка запроса nm=%d попытка %d/%d: %s", nm, attempt, config.RETRY_MAX, exc)
            if attempt < config.RETRY_MAX:
                time.sleep(config.RETRY_BACKOFF ** attempt)

    return [], 0


# ---------------------------------------------------------------------------
# Основная логика
# ---------------------------------------------------------------------------
def build_rows(watchlist: list[dict], now: datetime) -> list[list]:
    """
    Для каждого базового SKU запрашивает карусель и ищет в ней остальные SKU
    той же категории. Возвращает список строк для Google Sheets.
    """
    # Группируем watchlist по категориям
    by_category: dict[str, list[dict]] = {}
    for item in watchlist:
        by_category.setdefault(item["category"], []).append(item)

    session = _get_session()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    rows = []

    total_base = sum(len(v) for v in by_category.values())
    processed = 0

    for category, items in by_category.items():
        sku_map = {item["sku"]: item for item in items}

        for base_item in items:
            processed += 1
            base_sku = base_item["sku"]
            log.info("[%d/%d] Категория «%s», базовый SKU %d", processed, total_base, category, base_sku)

            ids, actual_len = fetch_recom(session, base_sku, config.DEST_MOSCOW)
            time.sleep(config.RATE_LIMIT_SLEEP)

            if actual_len == 0:
                log.warning("Пустая выдача для nm=%d — пропускаем", base_sku)
                continue

            # Ищем каждый другой SKU той же категории в выдаче
            for target_item in items:
                if target_item["sku"] == base_sku:
                    continue

                target_sku = target_item["sku"]
                try:
                    position = ids.index(target_sku) + 1  # 1-based
                except ValueError:
                    position = ">100"

                rows.append([
                    date_str,
                    time_str,
                    config.REGION_LABEL,
                    config.SHELF_TYPE,
                    category,
                    base_sku,
                    base_item["brand"],
                    "да" if base_item["is_ours"] else "нет",
                    target_sku,
                    target_item["brand"],
                    "да" if target_item["is_ours"] else "нет",
                    position,
                    actual_len,
                ])

    return rows


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------
def set_status(spreadsheet, status: str):
    """Пишет статус в панель управления."""
    try:
        ws = spreadsheet.worksheet("🚀 Управление")
        ws.update("D5", [[status]], value_input_option="RAW")
    except Exception as e:
        log.warning("Не удалось обновить статус: %s", e)


def main() -> int:
    now = datetime.now(MSK)
    log.info("=== wb_polki_tracker запуск %s ===", now.isoformat())

    try:
        spreadsheet = get_gsheet()
        set_status(spreadsheet, f"⏳ Запущен {now.strftime('%d.%m %H:%M')}")

        watchlist = read_watchlist(spreadsheet)

        if not watchlist:
            msg = "Watchlist пуст — вкладка 'Полки_вход' не заполнена или пуста."
            log.error(msg)
            set_status(spreadsheet, "❌ Watchlist пуст")
            send_telegram(f"⚠️ <b>wb_polki</b>: {msg}")
            return 1

        rows = build_rows(watchlist, now)

        if not rows:
            msg = "Не собрано ни одной строки — проверьте доступность WB API и watchlist."
            log.error(msg)
            set_status(spreadsheet, "❌ Нет данных от WB")
            send_telegram(f"⚠️ <b>wb_polki</b>: {msg}")
            return 1

        log_ws = ensure_log_sheet(spreadsheet)
        append_rows(log_ws, rows)

        done_msg = f"✅ Готово {now.strftime('%d.%m %H:%M')} — {len(rows)} строк"
        set_status(spreadsheet, done_msg)
        log.info("=== Готово: %d строк записано ===", len(rows))
        return 0

    except Exception as exc:
        log.exception("Критическая ошибка")
        try:
            set_status(get_gsheet(), f"❌ Ошибка: {type(exc).__name__}")
        except Exception:
            pass
        send_telegram(
            f"🚨 <b>wb_polki упал</b>\n"
            f"<code>{type(exc).__name__}: {exc}</code>"
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
