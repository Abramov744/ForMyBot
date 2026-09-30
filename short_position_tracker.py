#!/usr/bin/env python3
"""
Ежечасное отслеживание НОВЫХ открытых шорт-позиций по всем 6 биржам
(Aster/Bybit/KuCoin/Lighter/MEXC/Gate) и автозапись в Google Sheet, когда
на споте (на любой из подключённых спот-бирж + Uniswap, если настроен)
найдена покупка той же монеты на тот же объём — автоматизирует ручной
ввод B (монета в формате "МОНЕТА(spot/perp)") и D (дата и время открытия
шорта), которые пользователь иначе вписывает сам в момент открытия сделки
в таблицу "Учет арбитража фандинга". Заодно пишется и O (биржа шорта) —
без него не работали бы ни автосинк funding в sheets_sync.sync_once()
(ищет строку по паре биржа+монета именно в колонке O), ни защита от
повторной записи ОДНОЙ И ТОЙ ЖЕ сделки на следующей ежечасной проверке
(см. _already_logged ниже) — решение согласовано с пользователем явно.

В ТУ ЖЕ строку дополнительно пишутся (согласовано с пользователем явно):
  - W — объём, купленный на споте (spot_result["qty"] из
    entry_price._search_spot_entry — уже суммарный объём по всем найденным
    спот-сделкам, если покупка была не одним ордером);
  - X — средняя цена покупки на споте (spot_result["price"] — та же
    функция уже считает VWAP по всем найденным спот-сделкам сразу,
    отдельно взвешивать не нужно, см. её докстринг в entry_price.py);
  - Y — средняя цена входа шорта на фьючерсе (поле "price" в словаре
    позиции каждой _fetch_*_open_shorts — те же поля avgPrice/entryPrice/
    entry_price/openAvgPrice/avgEntryPrice, что уже проверены и
    используются в entry_price._*_position_entry для автоподбора цены в
    калькуляторе, здесь просто ещё и извлекаются в список открытых
    шортов, а не только при точечном запросе по одному символу);
  - M — название спот-биржи(-бирж), где нашлась встречная покупка, в
    формате "Биржа spot" (например, "MEXC spot") — spot_result["exchanges"]
    из entry_price._search_spot_entry уже содержит список всех бирж, чьи
    сделки вошли в VWAP по X; если их несколько — через запятую.
  Если цену определить не удалось (поле отсутствует в ответе биржи) —
  соответствующая ячейка просто не пишется (остаётся пустой для ручного
  заполнения), а не заполняется нулём/угадыванием.

КАК ОПРЕДЕЛЯЕТСЯ "НОВЫЙ" ШОРТ: отдельное состояние между запусками НЕ
хранится (Railway может перезапустить процесс на редеплое — любое
in-memory состояние потерялось бы и либо пропустило бы реальную сделку,
либо продублировало строку после рестарта). Вместо этого при КАЖДОЙ
проверке для каждой текущей открытой шорт-позиции на бирже ищется
существующая АКТИВНАЯ строка в таблице с той же парой (биржа, монета) —
если такая уже есть, сделка уже записана. Новая строка создаётся, только
если активной строки под эту пару ещё нет — так идемпотентность держится
на самой таблице (источник истины), а не на памяти процесса, и переживает
рестарт без дублей и без пропусков.

СТОРОНА ПОЗИЦИИ (шорт vs лонг) — по знаку объёма/полю side, ПОДТВЕРЖДЕНО
по официальным источникам для пяти бирж из шести:
  - Bybit  — поле side == "Sell" (офиц. v5 API).
  - MEXC   — поле positionType: "1" — long, иначе — short (офиц. SDK ccxt,
    mexc.py: rawSide == '1' -> 'long' иначе 'short').
  - Gate   — знак поля size: < 0 — шорт (офиц. документация Gate API v4 —
    "Positive number means long, negative number means short").
  - Aster  — знак поля positionAmt: < 0 — шорт (та же конвенция Binance
    Futures API, форком которого является Aster — используется и в
    остальных местах проекта, где это поле уже читается).
  - KuCoin — знак поля currentQty: < 0 — шорт (офиц. SDK ccxt, kucoin.py
    parse_position: currentQty > 0 -> 'long', < 0 -> 'short'; подтверждено
    и официальным KuCoin Futures New User Guide).
  - Lighter — поле "sign" в офиц. SDK (elliottech/lighter-python,
    AccountPosition) описано ТОЛЬКО как "int", без документированных
    значений — используется по аналогии со всеми остальными биржами
    (отрицательное = шорт), это ПРЕДПОЛОЖЕНИЕ, не подтверждённый факт.
    Если после деплоя на Lighter появится строка для позиции, которая на
    самом деле лонг (или наоборот — не появится для реального шорта) —
    это первое место для проверки, см. также README.

ПОИСК ПОКУПКИ НА СПОТЕ — переиспользует entry_price._search_spot_entry
(та же функция, что уже делает автоподбор цены для калькулятора) БЕЗ
ИЗМЕНЕНИЙ в её логике поиска (те же окна WINDOW_MINUTES_EXACT/APPROX, тот
же список спот-бирж + Uniswap) — добавлена только проверка ОБЪЁМА
(_qty_matches, см. QTY_TOLERANCE), которой раньше не было (калькулятору
для автоподбора цены проверять объём не нужно, только цену саму по себе).

ДОПУЩЕНИЕ О ЧАСОВОМ ПОЯСЕ: время в столбец D пишется по МСК (UTC+3) — та
же зона, что используется во всём проекте для дневных периодов (см.
funding_report.MSK) — предполагается, что и сама Google-таблица настроена
на МСК (иначе отображаемое время разойдётся с реальным на разницу
часовых поясов). Пишется строкой вида "ГГГГ-ММ-ДД ЧЧ:ММ:СС" через
value_input_option=USER_ENTERED — Google Sheets сам распознаёт такой
формат как дату независимо от локали таблицы (в отличие от неоднозначных
ДД/ММ/ГГГГ или ММ/ДД/ГГГГ) и хранит как настоящее дата-время, не текст.
"""

import os
import time
from datetime import datetime

import gspread

from entry_price import (
    WINDOW_MINUTES_EXACT,
    WINDOW_MINUTES_APPROX,
    _base_asset,
    _fetch_mexc_contract_size,
    _infer_open_time_via_funding,
    _search_spot_entry,
)
from funding_report import (
    MSK,
    _cached_raw_positions,
    _aster_positions_raw,
    _bybit_positions_raw,
    _gate_positions_raw,
    _kucoin_positions_raw,
    _lighter_positions_raw,
    _mexc_positions_raw,
    fetch_lighter_markets,
    load_secrets,
)
from sheets_sync import (
    _COL_COIN,
    _COL_EXCHANGE2,
    _COL_STATUS,
    _FIRST_DATA_ROW,
    _extract_coin_base,
    _open_worksheet,
)

# Допуск на расхождение объёма шорта и спот-покупки — согласовано с
# пользователем явно (±1%, с запасом на округление лотов/минимальный шаг
# ордера и комиссии в монете на разных биржах, но достаточно жёстко, чтобы
# не принять за одну сделку два случайных независимых ордера).
QTY_TOLERANCE = 0.01

# Отображаемые названия спот-бирж для столбца M — только те, что вообще
# могут оказаться в spot_result["exchanges"] (см. entry_price._SPOT_TRADE_
# FETCHERS + Uniswap отдельно), не общий EXCHANGE_LABELS из funding_alerts/
# funding_chart — там нет kucoin/uniswap, потому что они не участвуют в
# фандинг-отчётах/алертах, только в поиске спот-цены.
_SPOT_EXCHANGE_LABELS = {
    "bybit": "Bybit", "mexc": "MEXC", "gate": "Gate",
    "kucoin": "KuCoin", "uniswap": "Uniswap",
}


# ── Открытые ШОРТ-позиции по каждой бирже: [{"symbol", "qty", "entry_time_ms", "time_is_exact"}, ...] ──
#
# Не переиспользует funding_report.fetch_*_open_symbols напрямую — те
# возвращают ВСЕ позиции (long и short вперемешку, без объёма) в форме
# {symbol: time_ms}, заточенной под расчёт funding, где сторона позиции не
# важна. Здесь нужна именно сторона и объём, поэтому свой разбор ответа —
# тот же паттерн, что entry_price.py уже использует для _*_position_entry
# (отдельные функции под конкретную задачу поверх одних и тех же API). Сам
# HTTP-запрос при этом НЕ дублируется: каждая функция ниже берёт сырой
# список позиций через funding_report._cached_raw_positions — тот же общий
# короткоживущий кэш, что использует и fetch_*_open_symbols, и
# liquidation_alerts.py (см. докстринг _cached_raw_positions в
# funding_report.py про то, зачем — раньше три модуля независимо делали
# один и тот же запрос).

def _fetch_bybit_open_shorts(secrets: dict) -> list:
    items = _cached_raw_positions("bybit", lambda: _bybit_positions_raw(
        secrets["bybit_api_key"], secrets["bybit_api_secret"],
    ))
    return [
        {
            "symbol": p["symbol"], "qty": float(p["size"]),
            "entry_time_ms": int(p["createdTime"]) if p.get("createdTime") else None,
            "time_is_exact": True,
            "price": float(p["avgPrice"]) if p.get("avgPrice") else None,
        }
        for p in items
        if p.get("side") == "Sell" and float(p.get("size", 0)) > 0
    ]


def _fetch_mexc_open_shorts(secrets: dict) -> list:
    items = _cached_raw_positions("mexc", lambda: _mexc_positions_raw(
        secrets["mexc_api_key"], secrets["mexc_api_secret"],
    ))
    out = []
    for p in items:
        if str(p.get("positionType")) == "1" or float(p.get("holdVol", 0)) <= 0:
            continue  # positionType 1 == long (см. докстринг модуля)
        contract_size = _fetch_mexc_contract_size(p["symbol"])  # holdVol — в контрактах, не в монетах
        price = p.get("openAvgPrice") or p.get("holdAvgPrice")
        out.append({
            "symbol": p["symbol"], "qty": float(p["holdVol"]) * contract_size,
            "entry_time_ms": int(p["createTime"]) if p.get("createTime") else None,
            "time_is_exact": True,
            "price": float(price) if price else None,
        })
    return out


def _fetch_gate_open_shorts(secrets: dict, settle: str = "usdt") -> list:
    items = _cached_raw_positions(f"gate:{settle}", lambda: _gate_positions_raw(
        secrets["gate_api_key"], secrets["gate_api_secret"], settle,
    ))
    out = []
    for p in items:
        size = float(p.get("size", 0) or 0)
        if size < 0:  # отрицательный size == шорт (см. докстринг модуля)
            price = p.get("entry_price")
            out.append({
                "symbol": p["contract"], "qty": abs(size), "entry_time_ms": None, "time_is_exact": False,
                "price": float(price) if price else None,
            })
    return out


def _fetch_aster_open_shorts(secrets: dict) -> list:
    items = _cached_raw_positions("aster", lambda: _aster_positions_raw(
        secrets["user"], secrets["signer"], secrets["signer_private_key"],
    ))
    out = []
    for p in items:
        amt = float(p.get("positionAmt", 0) or 0)
        if amt < 0:  # отрицательный positionAmt == шорт (конвенция Binance Futures, см. докстринг модуля)
            price = p.get("entryPrice")
            out.append({
                "symbol": p["symbol"], "qty": abs(amt), "entry_time_ms": None, "time_is_exact": False,
                "price": float(price) if price else None,
            })
    return out


def _fetch_lighter_open_shorts(secrets: dict) -> list:
    markets = fetch_lighter_markets()
    positions = _cached_raw_positions("lighter", lambda: _lighter_positions_raw(
        secrets["lighter_account_index"], secrets["lighter_auth_token"],
    ))
    out = []
    for pos in positions:
        market_id = pos.get("market_id", pos.get("market_index"))
        symbol = markets.get(market_id, f"MARKET_{market_id}")
        size = float(pos.get("position", pos.get("size", pos.get("position_size", 0))) or 0)
        if size == 0:
            continue
        sign = pos.get("sign")
        # "sign" не задокументирован официально — используется как знак
        # (отрицательное = шорт) по аналогии с остальными биржами, если
        # поле есть; иначе как fallback берётся знак самого "position"
        # (см. докстринг модуля про непроверенность этого места).
        is_short = (int(sign) < 0) if sign is not None else (size < 0)
        if is_short:
            price = None
            for key in ("avg_entry_price", "entry_price", "avgEntryPrice", "entryPrice"):
                if pos.get(key) not in (None, ""):
                    price = float(pos[key])
                    break
            out.append({
                "symbol": symbol, "qty": abs(size), "entry_time_ms": None, "time_is_exact": False,
                "price": price,
            })
    return out


def _fetch_kucoin_open_shorts(secrets: dict) -> list:
    items = _cached_raw_positions("kucoin", lambda: _kucoin_positions_raw(
        secrets["kucoin_api_key"], secrets["kucoin_api_secret"], secrets["kucoin_api_passphrase"],
    ))
    out = []
    for p in items:
        qty = float(p.get("currentQty", 0) or 0)
        if p.get("isOpen") and qty < 0:  # отрицательный currentQty == шорт (см. докстринг модуля)
            price = p.get("avgEntryPrice")
            out.append({
                "symbol": p["symbol"], "qty": abs(qty),
                "entry_time_ms": int(p["openingTimestamp"]) if p.get("openingTimestamp") else None,
                "time_is_exact": True,
                "price": float(price) if price else None,
            })
    return out


# {exchange: (fetch_fn, ключ_секрета_для_проверки_что_биржа_настроена)}
_OPEN_SHORTS_FETCHERS = {
    "aster": (_fetch_aster_open_shorts, "user"),
    "bybit": (_fetch_bybit_open_shorts, "bybit_api_key"),
    "mexc": (_fetch_mexc_open_shorts, "mexc_api_key"),
    "gate": (_fetch_gate_open_shorts, "gate_api_key"),
    "lighter": (_fetch_lighter_open_shorts, "lighter_account_index"),
    "kucoin": (_fetch_kucoin_open_shorts, "kucoin_api_key"),
}


def _qty_matches(short_qty: float, spot_qty: float) -> bool:
    if short_qty <= 0:
        return False
    return abs(spot_qty - short_qty) <= short_qty * QTY_TOLERANCE


def _already_logged(rows: list, exchange: str, coin_base: str) -> bool:
    """Есть ли уже АКТИВНАЯ строка в таблице для этой пары (биржа, монета)
    — см. докстринг модуля про то, почему это единственный источник
    идемпотентности (без отдельного состояния между запусками)."""
    for row in rows:
        coin = row[_COL_COIN].strip() if len(row) > _COL_COIN and row[_COL_COIN] else ""
        if not coin:
            continue
        status = row[_COL_STATUS].strip().lower() if len(row) > _COL_STATUS and row[_COL_STATUS] else ""
        if status != "active":
            continue
        exch = row[_COL_EXCHANGE2].strip().lower() if len(row) > _COL_EXCHANGE2 and row[_COL_EXCHANGE2] else ""
        if exch == exchange and _extract_coin_base(coin) == coin_base:
            return True
    return False


def _format_open_time_msk(entry_time_ms: int) -> str:
    """Serial-дату Google Sheets сам не считаем (см. докстринг модуля про
    value_input_option=USER_ENTERED) — просто форматируем строку в
    МСК-времени, а парсит её уже сама таблица при записи."""
    return datetime.fromtimestamp(entry_time_ms / 1000, tz=MSK).strftime("%Y-%m-%d %H:%M:%S")


def check_for_new_shorts(secrets: dict) -> None:
    """
    Один проход: по каждой настроенной бирже — список открытых шортов,
    для каждого, которого ещё нет в таблице как активной строки, — поиск
    покупки на споте с совпадающим объёмом, и если нашлось — новая строка
    (B/D/O). Ничего не пишет и тихо выходит, если Google Sheets не
    настроены (тот же принцип, что и в sheets_sync.record_position_close).
    """
    if not (os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")):
        return

    ws = _open_worksheet()
    rows = ws.get(f"B{_FIRST_DATA_ROW}:O")  # тот же открытый диапазон, что и в sheets_sync.sync_once

    # next_row — НЕ _FIRST_DATA_ROW + len(rows) (было так до фикса, см.
    # историю бага): открытый диапазон "B{FIRST}:O" тянет из Google Sheets
    # ЗНАЧЕНИЯ, а не только "реально введённые пользователем данные" — если
    # в столбце C (формула статуса) или любом другом столбце между B и O
    # формула протянута заранее на много строк вперёд и возвращает "" для
    # ещё не используемых строк, Sheets API всё равно отдаёт эту "" как
    # значение этой строки, и len(rows) считает такие строки тоже —
    # реальный случай: следующая строка уехала на #200 вместо места сразу
    # после последней настоящей сделки (#107), потому что между ними
    # формула в C была протянута на сотню строк вперёд. Вместо этого ищем
    # ПОСЛЕДНЮЮ строку с непустой колонкой B (монета) — тот же признак
    # "это настоящая строка", что уже используется везде в этом файле и в
    # sheets_sync.py (_already_logged, sync_once, record_position_close).
    last_used_offset = -1
    for offset, row in enumerate(rows):
        if len(row) > _COL_COIN and row[_COL_COIN].strip():
            last_used_offset = offset
    next_row = _FIRST_DATA_ROW + last_used_offset + 1

    updates = []
    new_rows_count = 0
    for exchange, (fetch_fn, required_key) in _OPEN_SHORTS_FETCHERS.items():
        if required_key not in secrets:
            continue
        try:
            shorts = fetch_fn(secrets)
        except Exception as e:
            print(f"[short_position_tracker/{exchange}] Не удалось получить открытые позиции: {e}", flush=True)
            continue

        # Печатается ВСЕГДА, даже когда новых строк не появится — иначе по
        # логу нельзя отличить "открытых шортов правда нет" от "получение
        # списка позиций молча сломано" (в обоих случаях новых строк 0).
        symbols_note = f" ({', '.join(s['symbol'] for s in shorts)})" if shorts else ""
        print(f"[short_position_tracker/{exchange}] открытых шортов сейчас: {len(shorts)}{symbols_note}", flush=True)

        for short in shorts:
            symbol = short["symbol"]
            coin_base = _base_asset(exchange, symbol)

            if _already_logged(rows, exchange, coin_base):
                print(f"[short_position_tracker/{exchange}] {symbol}: уже записано активной строкой в таблице, пропуск.", flush=True)
                continue

            entry_time_ms = short["entry_time_ms"]
            time_is_exact = short["time_is_exact"]
            if entry_time_ms is None:
                try:
                    entry_time_ms = _infer_open_time_via_funding(secrets, exchange, symbol)
                except Exception as e:
                    print(f"[short_position_tracker/{exchange}] Не удалось оценить время открытия {symbol}: {e}", flush=True)
            approx_note = ""
            if entry_time_ms is None:
                # Не удалось определить даже приблизительно (например, самая
                # первая проверка после открытия — funding ещё не начислялся
                # ни разу) — пишем время ОБНАРУЖЕНИЯ, это не время открытия,
                # честно помечаем это в логе.
                entry_time_ms = int(time.time() * 1000)
                approx_note = " (время открытия не определено, записано время обнаружения ботом)"

            window = WINDOW_MINUTES_EXACT if time_is_exact else WINDOW_MINUTES_APPROX
            try:
                spot_result = _search_spot_entry(secrets, coin_base, entry_time_ms, window)
            except Exception as e:
                print(f"[short_position_tracker/{exchange}] Ошибка поиска спот-покупки {coin_base}: {e}", flush=True)
                continue

            if spot_result is None:
                print(f"[short_position_tracker/{exchange}] {symbol}: шорт {short['qty']:g} — покупка на споте "
                      f"не найдена ни на одной бирже в окне ±{window} мин, строка не создана.", flush=True)
                continue

            if not _qty_matches(short["qty"], spot_result["qty"]):
                print(f"[short_position_tracker/{exchange}] {symbol}: объём не совпал — шорт {short['qty']:g}, "
                      f"на споте куплено {spot_result['qty']:g} (допуск ±{QTY_TOLERANCE:.0%}), строка не создана.", flush=True)
                continue

            row_number = next_row
            next_row += 1
            new_rows_count += 1
            updates.append({"range": f"B{row_number}", "values": [[f"{coin_base}(spot/perp)"]]})
            updates.append({"range": f"D{row_number}", "values": [[_format_open_time_msk(entry_time_ms)]]})
            updates.append({"range": f"O{row_number}", "values": [[exchange]]})
            updates.append({"range": f"W{row_number}", "values": [[round(spot_result["qty"], 8)]]})
            updates.append({"range": f"X{row_number}", "values": [[round(spot_result["price"], 8)]]})
            spot_exchanges_label = ", ".join(
                f"{_SPOT_EXCHANGE_LABELS.get(ex, ex)} spot" for ex in spot_result["exchanges"]
            )
            updates.append({"range": f"M{row_number}", "values": [[spot_exchanges_label]]})

            futures_price = short.get("price")
            if futures_price is not None:
                updates.append({"range": f"Y{row_number}", "values": [[round(futures_price, 8)]]})
            else:
                # Биржа не отдала цену входа в ожидаемом поле — оставляем Y
                # пустой для ручного заполнения, а не пишем 0/угадываем
                # (тот же принцип, что и везде в проекте с недостающими данными).
                print(f"[short_position_tracker/{exchange}] {symbol}: не удалось определить среднюю цену "
                      f"входа фьючерса — Y{row_number} оставлена пустой.", flush=True)

            print(f"[short_position_tracker] Новая строка {row_number}: {exchange}/{coin_base}, "
                  f"объём шорта {short['qty']:g} ≈ спот {spot_result['qty']:g}{approx_note}, "
                  f"цена спота {spot_result['price']:g} ({spot_exchanges_label}), "
                  f"цена фьючерса {futures_price if futures_price is not None else '?'}.",
                  flush=True)

    if updates:
        ws.batch_update(updates, value_input_option=gspread.utils.ValueInputOption.user_entered)
    print(f"[short_position_tracker] Проверка завершена, новых строк: {new_rows_count}.", flush=True)


SHORT_POSITION_CHECK_INTERVAL_MINUTES = float(os.environ.get("SHORT_POSITION_CHECK_INTERVAL_MINUTES", "60"))


def short_position_check_loop(secrets: dict) -> None:
    """Бесконечный цикл: раз в SHORT_POSITION_CHECK_INTERVAL_MINUTES минут
    выполняет check_for_new_shorts(). Тот же паттерн фонового потока, что и
    funding_alerts.alert_loop/sheets_sync.sheet_sync_loop — ошибка на одной
    итерации не должна останавливать цикл целиком."""
    interval_s = max(60.0, SHORT_POSITION_CHECK_INTERVAL_MINUTES * 60)
    print(f"[short_position_tracker] Запущен цикл проверки новых шорт-позиций каждые "
          f"{SHORT_POSITION_CHECK_INTERVAL_MINUTES:.0f} мин.", flush=True)
    while True:
        try:
            check_for_new_shorts(secrets)
        except Exception as e:
            print(f"[short_position_tracker] Ошибка цикла проверки: {e}", flush=True)
        time.sleep(interval_s)


def main():
    """Разовый запуск из командной строки — для ручной проверки/отладки."""
    secrets = load_secrets()
    check_for_new_shorts(secrets)


if __name__ == "__main__":
    main()
