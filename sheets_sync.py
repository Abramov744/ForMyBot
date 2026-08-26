#!/usr/bin/env python3
"""
Автозаполнение суммарного funding по ОТКРЫТЫМ СЕЙЧАС позициям в столбец P
таблицы "Учет арбитража фандинга" (Google Sheet) — вместо ручного ввода.

Формат таблицы — реальный, как есть у пользователя, этим скриптом не
создаётся и не меняется:
  B — монета (человекочитаемое имя, может содержать пометки вроде
      "(spot/perp)", "(пул)" — берётся текст ДО первой открывающей скобки)
  C — статус позиции, формула вида
      `=IF(ISBLANK(D),,IF(AND(...),"done","active"))`. Читается как ГОТОВОЕ
      вычисленное значение через Sheets API (эта формула здесь не
      воспроизводится и не пересчитывается) — так поведение остаётся
      согласованным, если пользователь когда-нибудь поправит саму формулу.
  O — Биржа 2 ("SHORT"-нога сделки — именно по ней начисляется funding,
      её и заполняет этот скрипт).
  P — Прибыль по Бирже 2. Именно сюда для АКТИВНЫХ (открытых) позиций
      пользователь сейчас вручную вписывает суммарный funding с момента
      открытия — это и автоматизирует скрипт. Для строк со статусом
      "done" P не трогается — там уже финальное значение сделки.

Переиспользует funding_report.fetch_all_time_open_positions() — ТУ ЖЕ
функцию, что уже считает "funding с момента открытия текущей позиции" для
команды /positions в Telegram-боте (там же и разница в точности между
биржами: Bybit/MEXC — по точному createdTime, Aster/Lighter/Gate — по
непрерывности начислений funding). Если бы этот скрипт запрашивал funding
как-то иначе — легко было бы разойтись в цифрах с ботом (см. CLAUDE.md и
хендофф про риск "двух мест, считающих одну и ту же метрику").

Сопоставление строки таблицы ↔ открытой позиции на бирже — по (биржа,
базовый актив), а не по свободному тексту из B напрямую: текст в B — это
то, что человек напечатал руками ("ZRO( (spot/perp)", "CC (пул)" и т.п.),
а не биржевой символ. Базовый актив из символа биржи (например, "ZROUSDT"
на Bybit или "ZRO_USDT" на Gate) извлекается уже существующей
entry_price._base_asset — тем же кодом, что и автоподбор цен в
калькуляторе, логика не дублируется заново.

Автоматизация работает ТОЛЬКО для строк, где Биржа 2 — одна из 5,
поддерживаемых ботом (Aster/Bybit/Lighter/MEXC/Gate). У остальных бирж,
которые встречаются в старых строках таблицы (paradex, edgex, apex,
kucoin, hyper и т.п.), бот не имеет API-доступа — такие строки скрипт
просто пропускает, не трогая ячейку (как и было при ручном вводе).

Если совпадение по (биржа, базовый актив) неоднозначно — сейчас на бирже
нет ни одной открытой позиции с таким активом, или подошло больше одной —
ячейка тоже НЕ трогается и это печатается в лог: лучше оставить как есть
для ручной проверки, чем один раз молча угадать неправильно в финансовой
таблице.

Переменные окружения (плюс обычные ASTER_*/BYBIT_*/LIGHTER_*/MEXC_*/GATE_*
из funding_report.load_secrets() — они тоже нужны, скрипт дёргает те же
fetch_*):
  GOOGLE_SHEET_ID              — id таблицы (из её URL, между /d/ и /edit)
  GOOGLE_SERVICE_ACCOUNT_JSON  — JSON-ключ сервисного аккаунта целиком,
                                  одной строкой (секрет со значением, не
                                  путь к файлу)
  GOOGLE_SHEET_TAB             — опционально, имя вкладки; по умолчанию
                                  берётся первая (единственная) вкладка
  SHEET_SYNC_INTERVAL_MINUTES  — как часто синхронизировать в фоновом
                                  цикле (см. sheet_sync_loop), по
                                  умолчанию 60

Расчёт суммарного funding для КАЖДОЙ сматченной позиции идёт через
calculator.fetch_funding_history()/calculator.sum_by_asset() — ровно те же
функции, что вызывает веб-калькулятор (app.py:/api/calculate) для поля
"funding_income_total", когда там выбрана конкретная монета. Это
сознательный выбор: рассчитывать funding как-то иначе (даже эквивалентным
на вид кодом) означало бы второе место, независимо считающее ту же
метрику, — оно рано или поздно разойдётся с калькулятором при следующем
фиксе логики в одном месте, но не в другом (ровно так уже разъезжались
бот и калькулятор до PR с фиксом обрезки по времени открытия — см.
хендофф). Здесь этого не происходит: fetch_funding_history() с указанным
symbol сам вызывает внутри тот же _trim_to_current_open_position(), что и
калькулятор в вебе, — код в буквальном смысле один и тот же, а не
переписанный заново.

Единственное, что делает этот скрипт САМ (а не просто вызывает
calculator.py) — подбирает НАЧАЛО периода для fetch_funding_history():
- Bybit/MEXC отдают точное время открытия позиции — используется оно
  (fetch_bybit_open_symbols/fetch_mexc_open_symbols); без этого пришлось
  бы гонять недельные окна вглубь всех 180 дней просто чтобы тут же
  обрезать почти всё обратно внутри fetch_funding_history.
- Aster/Lighter/Gate точного времени не дают — берётся тот же
  OPEN_POSITIONS_LOOKBACK_DAYS-lookback (180 дней), что уже используют и
  бот, и калькулятор в аналогичной ситуации — это не отдельное решение, а
  переиспользование той же константы.
"""

import json
import os
import re
import time

import gspread

from calculator import fetch_funding_history, sum_by_asset
from entry_price import _base_asset
from funding_report import (
    OPEN_POSITIONS_LOOKBACK_DAYS,
    fetch_aster_open_symbols,
    fetch_bybit_open_symbols,
    fetch_gate_open_symbols,
    fetch_lighter_open_symbols,
    fetch_mexc_open_symbols,
    load_secrets,
)

# Биржи с точным временем открытия позиции (используется как старт периода
# напрямую) — те же две, что и в calculator._trim_to_current_open_position.
_EXACT_TIME_EXCHANGES = ("bybit", "mexc")
# Биржи без точного времени — старт периода берётся по OPEN_POSITIONS_LOOKBACK_DAYS.
_CONTINUOUS_EXCHANGES = ("aster", "lighter", "gate")
SUPPORTED_EXCHANGES = _EXACT_TIME_EXCHANGES + _CONTINUOUS_EXCHANGES

# Данные начинаются с этой строки (см. структуру таблицы: строки 1-7 —
# инструкции/заголовки/шапка). Верхняя граница диапазона не задаётся —
# читаем "до конца листа" (открытый диапазон в A1-нотации), чтобы не
# привязываться к текущему числу строк и не обрезать будущие новые сделки.
_FIRST_DATA_ROW = 8

# Смещения столбцов внутри читаемого диапазона B{row}:O{row} (0 = B, ...).
_COL_COIN = 0        # B — монета
_COL_STATUS = 1      # C — статус (active/done/пусто)
_COL_EXCHANGE2 = 13  # O — Биржа 2


def _extract_coin_base(label: str) -> str:
    """'ZRO( (spot/perp)' -> 'ZRO', 'CC (пул)' -> 'CC', 'COAI' -> 'COAI'."""
    return re.split(r"\(", label)[0].strip().upper()


def build_open_symbol_index(secrets: dict) -> dict:
    """
    {exchange: {symbol: open_time_ms}} для Bybit/MEXC (точное время),
    {exchange: {symbol: None}} для Aster/Lighter/Gate (точного времени нет).
    Единая форма (всегда dict symbol->значение) — чтобы дальше по коду не
    ветвиться на dict/set. Биржи без настроенных секретов просто
    отсутствуют в результате (как и everywhere else в проекте).
    """
    index: dict = {}

    if "user" in secrets:
        try:
            symbols = fetch_aster_open_symbols(
                secrets["user"], secrets["signer"], secrets["signer_private_key"],
            )
            index["aster"] = {s: None for s in symbols}
        except Exception as e:
            print(f"[sheets_sync/aster] Не удалось получить открытые позиции: {e}", flush=True)

    if "bybit_api_key" in secrets:
        try:
            index["bybit"] = fetch_bybit_open_symbols(secrets["bybit_api_key"], secrets["bybit_api_secret"])
        except Exception as e:
            print(f"[sheets_sync/bybit] Не удалось получить открытые позиции: {e}", flush=True)

    if "lighter_account_index" in secrets:
        try:
            symbols = fetch_lighter_open_symbols(secrets["lighter_account_index"], secrets["lighter_auth_token"])
            index["lighter"] = {s: None for s in symbols}
        except Exception as e:
            print(f"[sheets_sync/lighter] Не удалось получить открытые позиции: {e}", flush=True)

    if "mexc_api_key" in secrets:
        try:
            index["mexc"] = fetch_mexc_open_symbols(secrets["mexc_api_key"], secrets["mexc_api_secret"])
        except Exception as e:
            print(f"[sheets_sync/mexc] Не удалось получить открытые позиции: {e}", flush=True)

    if "gate_api_key" in secrets:
        try:
            symbols = fetch_gate_open_symbols(secrets["gate_api_key"], secrets["gate_api_secret"])
            index["gate"] = {s: None for s in symbols}
        except Exception as e:
            print(f"[sheets_sync/gate] Не удалось получить открытые позиции: {e}", flush=True)

    return index


def match_symbol(exchange: str, coin_base: str, open_symbol_index: dict) -> tuple[str | None, str]:
    """
    Ищет среди ОТКРЫТЫХ СЕЙЧАС позиций на бирже ровно один символ с таким
    базовым активом. Возвращает (native_symbol_или_None, причина_для_лога).
    """
    symbols_on_exchange = open_symbol_index.get(exchange, {})
    matches = [sym for sym in symbols_on_exchange if _base_asset(exchange, sym) == coin_base]
    if len(matches) == 1:
        return matches[0], "ok"
    if len(matches) == 0:
        return None, "сейчас нет открытой позиции с таким активом на этой бирже"
    return None, f"неоднозначно — подошло несколько символов: {matches}"


def compute_funding_total(secrets: dict, exchange: str, symbol: str, open_time_ms: int | None) -> float:
    """
    Собственно расчёт — целиком через calculator.py (см. докстринг модуля):
    fetch_funding_history() сам обрезает записи до текущей открытой позиции
    (calculator._trim_to_current_open_position), sum_by_asset() суммирует
    ровно так же, как и funding_income_total в /api/calculate. Этот скрипт
    только выбирает старт периода, чтобы не тянуть лишнее.
    """
    now_ms = int(time.time() * 1000)
    if exchange in _EXACT_TIME_EXCHANGES and open_time_ms is not None:
        start_ms = open_time_ms
    else:
        start_ms = now_ms - OPEN_POSITIONS_LOOKBACK_DAYS * 24 * 60 * 60 * 1000

    records = fetch_funding_history(secrets, exchange, start_ms, now_ms, symbol=symbol)
    return sum(sum_by_asset(records, exchange).values())


def sync_once(secrets: dict) -> None:
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    tab_name = os.environ.get("GOOGLE_SHEET_TAB")

    gc = gspread.service_account_from_dict(creds_info)
    sh = gc.open_by_key(sheet_id)
    ws = sh.worksheet(tab_name) if tab_name else sh.get_worksheet(0)

    open_symbol_index = build_open_symbol_index(secrets)

    rows = ws.get(f"B{_FIRST_DATA_ROW}:O")  # открытый диапазон — до конца листа

    updates = []
    skipped = []
    for offset, row in enumerate(rows):
        row_number = _FIRST_DATA_ROW + offset

        coin = row[_COL_COIN].strip() if len(row) > _COL_COIN and row[_COL_COIN] else ""
        if not coin:
            continue  # пустая строка-заготовка без данных — не трогаем

        status = row[_COL_STATUS].strip().lower() if len(row) > _COL_STATUS and row[_COL_STATUS] else ""
        if status != "active":
            continue  # закрытые позиции не трогаем — там уже финальное значение

        exchange_raw = (
            row[_COL_EXCHANGE2].strip().lower()
            if len(row) > _COL_EXCHANGE2 and row[_COL_EXCHANGE2] else ""
        )
        if exchange_raw not in SUPPORTED_EXCHANGES:
            skipped.append((row_number, coin, exchange_raw, "биржа не поддерживается ботом"))
            continue

        coin_base = _extract_coin_base(coin)
        symbol, reason = match_symbol(exchange_raw, coin_base, open_symbol_index)
        if symbol is None:
            skipped.append((row_number, coin, exchange_raw, reason))
            continue

        try:
            open_time_ms = open_symbol_index[exchange_raw][symbol]
            value = compute_funding_total(secrets, exchange_raw, symbol, open_time_ms)
        except Exception as e:
            skipped.append((row_number, coin, exchange_raw, f"ошибка расчёта funding: {e}"))
            continue

        updates.append({"range": f"P{row_number}", "values": [[round(value, 4)]]})

    if updates:
        ws.batch_update(updates)  # raw=True по умолчанию — числа пишутся как есть, не как формулы

    print(f"[sheets_sync] Обновлено ячеек P: {len(updates)}.", flush=True)
    for row_number, coin, exchange_raw, reason in skipped:
        print(f"[sheets_sync] Пропущена строка {row_number} ({coin!r}, биржа={exchange_raw!r}): {reason}", flush=True)


SHEET_SYNC_INTERVAL_MINUTES = float(os.environ.get("SHEET_SYNC_INTERVAL_MINUTES", "60"))


def sheet_sync_loop(secrets: dict) -> None:
    """
    Бесконечный цикл: раз в SHEET_SYNC_INTERVAL_MINUTES минут выполняет
    sync_once(). Запускается фоновым потоком из app.py — тем же паттерном,
    что и funding_alerts.alert_loop (ошибка на одной итерации не должна
    останавливать цикл целиком, следующая попытка — через тот же интервал).
    """
    interval_s = max(60.0, SHEET_SYNC_INTERVAL_MINUTES * 60)
    print(f"[sheets_sync] Запущен цикл синхронизации с Google Sheet каждые "
          f"{SHEET_SYNC_INTERVAL_MINUTES:.0f} мин.", flush=True)
    while True:
        try:
            sync_once(secrets)
        except Exception as e:
            print(f"[sheets_sync] Ошибка цикла синхронизации: {e}", flush=True)
        time.sleep(interval_s)


def main():
    """Разовый запуск из командной строки — для ручной проверки/отладки."""
    secrets = load_secrets()
    sync_once(secrets)


if __name__ == "__main__":
    main()
