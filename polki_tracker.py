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


def _norm_category(name: str) -> str:
    """Нормализованный ключ категории: без регистра, ё→е, без лишних пробелов."""
    return " ".join(name.lower().replace("ё", "е").split())


def read_vpr(spreadsheet) -> list[dict]:
    """
    Читает вкладку «БАЗА ВПР» — кураторскую карту конкурентов
    (article / Категория / ПП / Сцепить / Арт Дубль / Бренд).
    Пустые слоты (без артикула) пропускаются.
    """
    try:
        ws = spreadsheet.worksheet(config.VPR_SHEET_NAME)
    except gspread.WorksheetNotFound:
        log.warning("Вкладка '%s' не найдена — цены соберём только по '%s'",
                    config.VPR_SHEET_NAME, config.INPUT_SHEET_NAME)
        return []

    rows = ws.get_all_values()
    result = []
    for row in rows[1:]:
        if len(row) < 2:
            continue
        sku_raw = row[0].strip()
        if not sku_raw.isdigit():
            continue
        brand = row[5].strip() if len(row) > 5 else ""
        result.append(
            {
                "category": row[1].strip(),
                "sku": int(sku_raw),
                "brand": brand,
                "is_ours": brand.strip().lower() == config.OUR_BRAND.lower(),
            }
        )
    log.info("Прочитано %d SKU из '%s'", len(result), config.VPR_SHEET_NAME)
    return result


def read_price_universe(spreadsheet, watchlist: list[dict]) -> list[dict]:
    """
    Объединяет «Полки_вход» и «БАЗА ВПР» в единый список для сбора цен.
    Дедуплицирует по SKU: запись из «Полки_вход» приоритетнее (там выверены
    названия категорий и флаг «Наш»). Категории из «БАЗА ВПР» приводятся
    к эталонным названиям через нормализацию и CATEGORY_ALIASES.
    """
    canon = {_norm_category(item["category"]): item["category"] for item in watchlist}
    canon.update(
        {key: value for key, value in config.CATEGORY_ALIASES.items()}
    )

    universe: dict[int, dict] = {}
    for item in watchlist:
        universe[item["sku"]] = item

    added = 0
    for item in read_vpr(spreadsheet):
        item = {**item, "category": canon.get(_norm_category(item["category"]), item["category"])}
        if item["sku"] in universe:
            continue
        universe[item["sku"]] = item
        added += 1

    log.info(
        "Универсум цен: %d SKU (%d из '%s' + %d новых из '%s')",
        len(universe), len(watchlist), config.INPUT_SHEET_NAME,
        added, config.VPR_SHEET_NAME,
    )
    return list(universe.values())


def ensure_sheet(spreadsheet, name: str, headers: list[str]):
    """Создаёт вкладку с шапкой, если её нет."""
    try:
        ws = spreadsheet.worksheet(name)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(name, rows=10000, cols=len(headers))
        ws.append_row(headers, value_input_option="RAW")
        log.info("Создана вкладка '%s'", name)
    return ws


def ensure_log_sheet(spreadsheet):
    return ensure_sheet(spreadsheet, config.LOG_SHEET_NAME, config.LOG_HEADERS)


def append_rows(ws, rows: list[list], sheet_name: str = "") -> None:
    if not rows:
        return
    ws.append_rows(rows, value_input_option="RAW")
    log.info("Дописано %d строк в '%s'", len(rows), sheet_name or ws.title)


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


def _fetch_price_batch(session: requests.Session, batch: list[int]) -> dict[int, dict]:
    """Один запрос карточек. Возвращает {sku: {...}} или {} при ошибке."""
    params = {**config.WB_CARD_PARAMS, "nm": ";".join(str(s) for s in batch)}
    result: dict[int, dict] = {}

    for attempt in range(1, config.RETRY_MAX + 1):
        try:
            resp = session.get(
                config.WB_CARD_URL,
                params=params,
                timeout=config.REQUEST_TIMEOUT,
            )
            if resp.status_code == 429:
                wait = config.RETRY_BACKOFF ** attempt * 5
                log.warning("429 при запросе цен, ждём %.0fs", wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()

            data = resp.json()
            for p in data.get("products", []):
                sku = p.get("id")
                sizes = p.get("sizes") or []
                if not sku or not sizes:
                    continue
                price = sizes[0].get("price") or {}
                basic = price.get("basic", 0) / 100
                product = price.get("product", 0) / 100
                discount_pct = round((1 - product / basic) * 100, 1) if basic else 0
                result[sku] = {
                    "name": p.get("name", ""),
                    "basic": basic,
                    "product": product,
                    "discount_pct": discount_pct,
                }
            return result

        except (requests.RequestException, ValueError) as exc:
            log.warning("Ошибка запроса цен (%d SKU), попытка %d/%d: %s",
                        len(batch), attempt, config.RETRY_MAX, exc)
            if attempt < config.RETRY_MAX:
                time.sleep(config.RETRY_BACKOFF ** attempt)

    return result


def fetch_prices(session: requests.Session, skus: list[int]) -> dict[int, dict]:
    """
    Запрашивает карточки товаров пачками и возвращает цены.
    {sku: {"name": str, "basic": float, "product": float, "discount_pct": float}}

    Если крупная пачка не отдала ничего (WB режет длинный nm-список или
    падает на одном битом SKU) — дробим её на части и добираем остаток.
    Так один нерабочий артикул не обнуляет весь дневной сбор цен.
    Цена берётся из первого размера (sizes[0].price).
    """
    result: dict[int, dict] = {}

    def crawl(batch: list[int], chunk: int) -> None:
        for i in range(0, len(batch), chunk):
            part = batch[i : i + chunk]
            got = _fetch_price_batch(session, part)
            result.update(got)
            time.sleep(config.RATE_LIMIT_SLEEP)

            missing = [s for s in part if s not in result]
            if missing and chunk > 1:
                next_chunk = 1 if chunk <= 10 else 10
                log.info("Добираем %d SKU пачками по %d", len(missing), next_chunk)
                crawl(missing, next_chunk)

    crawl(skus, config.PRICE_BATCH_SIZE)

    log.info("Получены цены для %d/%d SKU", len(result), len(skus))
    missing = [s for s in skus if s not in result]
    if missing:
        log.warning("Без цены остались SKU: %s", ", ".join(str(s) for s in missing[:20]))
    return result


def build_price_rows(watchlist: list[dict], prices: dict[int, dict], now: datetime) -> list[list]:
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S")
    rows = []
    for item in watchlist:
        info = prices.get(item["sku"])
        if not info:
            continue
        rows.append([
            date_str,
            time_str,
            item["category"],
            item["sku"],
            item["brand"],
            "да" if item["is_ours"] else "нет",
            info["name"],
            info["basic"],
            info["product"],
            info["discount_pct"],
        ])
    return rows


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
# Сводка цен по категориям
# ---------------------------------------------------------------------------
def _arg_sep(spreadsheet) -> str:
    """Разделитель аргументов формулы зависит от локали таблицы (ru_RU → «;»)."""
    try:
        locale = spreadsheet.fetch_sheet_metadata()["properties"].get("locale", "")
    except Exception:
        locale = "ru_RU"
    return "," if locale.startswith("en") else ";"


def write_summary(spreadsheet, categories: list[str]) -> None:
    """
    Пересобирает вкладку «Цены_сводка»: по строке на категорию, метрики —
    формулами поверх «Цены_лог», поэтому сводка сама подтягивает свежий срез
    после каждого запуска парсера (и её видно/можно проверить прямо в таблице).

    Срез определяется парой Дата+Время последнего прогона: за один запуск все
    строки пишутся с одним временем, так что повторные запуски в один день
    не задваивают счёт конкурентов.
    """
    ws = ensure_sheet(spreadsheet, config.SUMMARY_SHEET_NAME, config.SUMMARY_HEADERS)
    if ws.col_count < 12:            # K/L — служебные ячейки со срезом
        ws.resize(cols=12)
    sep = _arg_sep(spreadsheet)

    def f(template: str) -> str:
        return template.replace("§", sep)

    log_ref = f"'{config.PRICE_SHEET_NAME}'"
    # Колонки «Цены_лог»: A Дата, B Время, C Категория, D SKU, F Наш, I Цена_итог
    date_col, time_col = f"{log_ref}!$A$2:$A", f"{log_ref}!$B$2:$B"
    cat_col, sku_col = f"{log_ref}!$C$2:$C", f"{log_ref}!$D$2:$D"
    own_col, price_col = f"{log_ref}!$F$2:$F", f"{log_ref}!$I$2:$I"

    # Нулевая цена = карточки нет в наличии, в статистику её не берём
    def flt(row: int, own: str) -> str:
        return (
            f"FILTER({price_col}§{cat_col}=$A{row}§{own_col}=\"{own}\""
            f"§{date_col}=$L$1§{time_col}=$L$2§{price_col}>0)"
        )

    def counts(row: int, extra: str = "") -> str:
        return (
            f"=COUNTIFS({cat_col}§$A{row}§{date_col}§$L$1§{time_col}§$L$2{extra})"
        )

    rows = []
    for idx, category in enumerate(categories, start=2):
        rows.append([
            category,
            f(f"=IFERROR(TEXTJOIN(\", \"§TRUE§UNIQUE(FILTER({sku_col}§{cat_col}=$A{idx}"
              f"§{own_col}=\"да\"§{date_col}=$L$1§{time_col}=$L$2)))§\"\")"),
            f(f"=IFERROR(MEDIAN({flt(idx, 'да')})§\"\")"),
            f(counts(idx, f"§{own_col}§\"нет\"§{price_col}§\">0\"")),
            f(f"=IFERROR(MIN({flt(idx, 'нет')})§\"\")"),
            f(f"=IFERROR(MEDIAN({flt(idx, 'нет')})§\"\")"),
            f(f"=IFERROR(MAX({flt(idx, 'нет')})§\"\")"),
            f(f"=IF(N($C{idx})=0§\"\"§IFERROR(ROUND(($C{idx}/$F{idx}-1)*100§1)§\"\"))"),
            f(f"=IF(N($C{idx})=0§\"\"§{counts(idx, f'§{price_col}§\"<\"&$C{idx}§{price_col}§\">0\"')[1:]}+1)"),
            f(counts(idx, f"§{price_col}§0")),
        ])

    ws.update(values=[config.SUMMARY_HEADERS], range_name="A1:J1", value_input_option="RAW")
    ws.update(
        values=rows,
        range_name=f"A2:J{len(rows) + 1}",
        value_input_option="USER_ENTERED",
    )

    # Срез, на который считается сводка (последний прогон парсера)
    ws.update(
        values=[
            ["Срез — дата:", f(f"=IFERROR(INDEX(SORT(UNIQUE(FILTER({date_col}§{date_col}<>\"\"))§1§FALSE)§1)§\"\")")],
            ["Срез — время:", f(f"=IFERROR(INDEX(SORT(UNIQUE(FILTER({time_col}§{date_col}=$L$1))§1§FALSE)§1)§\"\")")],
        ],
        range_name="K1:L2",
        value_input_option="USER_ENTERED",
    )

    # Чистим хвост от категорий, которых больше нет
    tail_start = len(rows) + 2
    if ws.row_count >= tail_start:
        ws.batch_clear([f"A{tail_start}:J{ws.row_count}"])

    try:
        ws.freeze(rows=1)
        ws.format("A1:J1", {"textFormat": {"bold": True}})
    except Exception as exc:
        log.warning("Не удалось оформить '%s': %s", config.SUMMARY_SHEET_NAME, exc)

    log.info("Сводка '%s' пересобрана: %d категорий", config.SUMMARY_SHEET_NAME, len(rows))


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


def collect_prices(spreadsheet, watchlist: list[dict], now: datetime) -> int:
    """
    Собирает цены по всем конкурентам («Полки_вход» + «БАЗА ВПР»),
    дописывает их в «Цены_лог» и пересобирает сводку по категориям.
    Возвращает число записанных строк.
    """
    universe = read_price_universe(spreadsheet, watchlist)
    session = _get_session()
    unique_skus = list({item["sku"] for item in universe})
    prices = fetch_prices(session, unique_skus)
    price_rows = build_price_rows(universe, prices, now)

    if not price_rows:
        log.warning("Не собрано ни одной цены")
        send_telegram("⚠️ <b>wb_polki</b>: цены не собраны — WB не отдал ни одной карточки")
        return 0

    price_ws = ensure_sheet(spreadsheet, config.PRICE_SHEET_NAME, config.PRICE_HEADERS)
    append_rows(price_ws, price_rows, config.PRICE_SHEET_NAME)

    coverage = len(prices) / len(unique_skus) if unique_skus else 0
    if coverage < config.PRICE_COVERAGE_ALERT:
        send_telegram(
            f"⚠️ <b>wb_polki</b>: цены получены только для "
            f"{len(prices)} из {len(unique_skus)} SKU ({coverage:.0%})"
        )

    # Порядок категорий: как в «Полки_вход», затем новые из «БАЗА ВПР»
    categories: list[str] = []
    for item in universe:
        if item["category"] and item["category"] not in categories:
            categories.append(item["category"])
    write_summary(spreadsheet, categories)

    return len(price_rows)


def main(prices_only: bool = False) -> int:
    now = datetime.now(MSK)
    log.info("=== wb_polki_tracker запуск %s (prices_only=%s) ===", now.isoformat(), prices_only)

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

        rows = []
        if not prices_only:
            rows = build_rows(watchlist, now)

            if not rows:
                msg = "Не собрано ни одной строки — проверьте доступность WB API и watchlist."
                log.error(msg)
                set_status(spreadsheet, "❌ Нет данных от WB")
                send_telegram(f"⚠️ <b>wb_polki</b>: {msg}")
                return 1

            log_ws = ensure_log_sheet(spreadsheet)
            append_rows(log_ws, rows, config.LOG_SHEET_NAME)

        # Цены собираем всегда — даже если полки не отработали
        price_count = collect_prices(spreadsheet, watchlist, now)

        done_msg = f"✅ Готово {now.strftime('%d.%m %H:%M')} — {len(rows)} строк, {price_count} цен"
        set_status(spreadsheet, done_msg)
        log.info("=== Готово: %d строк полок, %d строк цен ===", len(rows), price_count)
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
    # --prices-only — пересобрать только цены и сводку, без обхода полок
    sys.exit(main(prices_only="--prices-only" in sys.argv))
