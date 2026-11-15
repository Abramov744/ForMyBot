#!/usr/bin/env python3
"""Суточная история APR в отдельной вкладке Google Sheets."""

import json
import os
import time
from datetime import date, datetime, timedelta

import gspread

import kucoin_symbol_history
from funding_report import MSK, fetch_all
from sheets_sync import _with_sheets_retry

_APR_SHEET_TAB = "apr_history"
_HEADER = ["date_msk", "apr_percent", "roi_percent", "income_usdt", "margin_usdt", "recorded_at_msk"]


def read_margin_l4() -> float:
    """Читает текущее значение маржи из L4 основной вкладки таблицы."""
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    client = gspread.service_account_from_dict(creds_info)
    spreadsheet = client.open_by_key(sheet_id)
    tab_name = os.environ.get("GOOGLE_SHEET_TAB", "").strip()
    worksheet = spreadsheet.worksheet(tab_name) if tab_name else spreadsheet.sheet1
    value = worksheet.acell("L4", value_render_option="UNFORMATTED_VALUE").value
    margin = float(value)
    if margin <= 0:
        raise ValueError("ячейка L4 должна содержать положительную маржу")
    return margin


def stablecoin_income(results: dict) -> tuple[float, str | None]:
    """Суммирует funding в USDT и USDC с курсом 1:1."""
    fields = {
        "aster": ("income", "asset"),
        "bybit": ("funding", "currency"),
        "lighter": ("change", "asset"),
        "gate": ("change", "asset"),
        "kucoin": ("funding", "settleCurrency"),
        "dydx": ("payment", "asset"),
        "htx": ("payment", "asset"),
    }
    total = 0.0
    failed = []
    for exchange, (income_field, asset_field) in fields.items():
        records, error = results.get(exchange, (None, None))
        if error:
            failed.append(exchange)
            continue
        for record in records or []:
            if str(record.get(asset_field, "")).upper() in ("USDT", "USDC"):
                total += float(record.get(income_field, 0) or 0)
    return total, (", ".join(failed) if failed else None)


def daily_metrics(results: dict) -> tuple[float, float, float, float]:
    """Возвращает доход, маржу L4, дневной ROI и простой годовой APR."""
    income, incomplete = stablecoin_income(results)
    if incomplete:
        raise RuntimeError(f"нет данных по биржам: {incomplete}")
    margin = read_margin_l4()
    roi = income / margin * 100
    return income, margin, roi, roi * 365


def _open_worksheet():
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    client = gspread.service_account_from_dict(creds_info)
    spreadsheet = client.open_by_key(sheet_id)
    try:
        return spreadsheet.worksheet(_APR_SHEET_TAB)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(title=_APR_SHEET_TAB, rows=1000, cols=len(_HEADER))
        worksheet.append_row(_HEADER, value_input_option="RAW")
        return worksheet


def load_apr_history() -> list[dict]:
    worksheet = _with_sheets_retry(_open_worksheet)
    rows = _with_sheets_retry(worksheet.get_all_values)
    history = []
    for row in rows[1:]:
        if len(row) < 3:
            continue
        try:
            history.append({
                "date_msk": datetime.strptime(row[0], "%Y-%m-%d").date(),
                "apr_percent": float(row[1]),
                "roi_percent": float(row[2]),
            })
        except (TypeError, ValueError):
            continue
    return sorted(history, key=lambda item: item["date_msk"])


def record_daily_apr(
    report_date: date, income: float, margin: float, roi: float, apr: float
) -> bool:
    """Записывает один APR-снимок на дату; повторный запуск не дублирует строку."""
    worksheet = _with_sheets_retry(_open_worksheet)
    rows = _with_sheets_retry(worksheet.get_all_values)
    date_key = report_date.isoformat()
    if any(row and row[0] == date_key for row in rows[1:]):
        return False
    captured_at = datetime.now(MSK).isoformat(timespec="seconds")
    row = [date_key, apr, roi, income, margin, captured_at]
    _with_sheets_retry(worksheet.append_row, row, value_input_option="RAW")
    return True


def capture_yesterday_apr(secrets: dict, report_date: date | None = None) -> bool:
    """Собирает данные за полные вчерашние сутки и сохраняет APR-снимок."""
    if report_date is None:
        report_date = datetime.now(MSK).date() - timedelta(days=1)

    # Проверяем идемпотентность до дорогих запросов к биржам.
    history = load_apr_history()
    if any(item["date_msk"] == report_date for item in history):
        print(f"[apr_history] APR за {report_date} уже записан; пропускаю.")
        return False

    start = datetime(report_date.year, report_date.month, report_date.day, tzinfo=MSK)
    end = start + timedelta(days=1)
    kucoin_symbols = (
        kucoin_symbol_history.load_known_symbols()
        if "kucoin_api_key" in secrets else set()
    )
    results = fetch_all(
        secrets,
        int(start.timestamp() * 1000),
        int(end.timestamp() * 1000),
        kucoin_symbols=kucoin_symbols,
    )
    income, margin, roi, apr = daily_metrics(results)
    created = record_daily_apr(report_date, income, margin, roi, apr)
    if created:
        print(f"[apr_history] За {report_date}: ROI {roi:.4f}%, APR {apr:.2f}% записан.")
    return created


def daily_apr_loop(secrets: dict) -> None:
    """Каждый день в 00:01 МСК рассчитывает APR за предыдущие сутки."""
    now = datetime.now(MSK)
    next_run = now.replace(hour=0, minute=1, second=0, microsecond=0)
    if next_run <= now:
        next_run += timedelta(days=1)
    print("[apr_history] Ежедневный расчёт APR запланирован на 00:01 МСК.", flush=True)

    while True:
        seconds = (next_run - datetime.now(MSK)).total_seconds()
        if seconds > 0:
            time.sleep(seconds)
        report_date = (next_run - timedelta(days=1)).date()
        while True:
            try:
                capture_yesterday_apr(secrets, report_date)
                break
            except Exception as exc:
                print(f"[apr_history] Ошибка расчёта за {report_date}: {exc}; повтор через 5 минут.", flush=True)
                time.sleep(300)
        next_run += timedelta(days=1)
