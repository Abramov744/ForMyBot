#!/usr/bin/env python3
"""
Логика калькулятора комиссий за финансирование (funding).

Стратегия, под которую считается калькулятор: заработок только с шорта —
монета покупается на споте (лонг) и одновременно шортится на фьючерсах,
funding получаете/платите только по фьючерсной ноге. Калькулятор считает
"чистый" результат такой сделки за период:

  net = funding_income - opening_fees + spread_effect

  funding_income  — сумма фактических выплат funding по фьючерсной позиции
                     за выбранный период (берётся из истории биржи —
                     переиспользует те же fetch_* функции, что и отчёты бота).
  opening_fees     — комиссии биржи за ОТКРЫТИЕ обеих ног (спот-покупка +
                     фьючерс-шорт), taker % * объём каждой ноги.
  spread_effect    — ценовой спред между ценой исполнения фьючерса и споta
                      в момент открытия: (цена_фьючерса - цена_спота) * qty.
                      Точных исторических цен спреда биржи не отдают, поэтому
                      цены исполнения обеих ног вводятся вручную — как
                      фактически исполнились ваши ордера при открытии.

Комиссии и цены исполнения — всегда ручной ввод/переопределение в UI;
значения по умолчанию ниже — это ТОЛЬКО отправная точка (стандартные
taker-ставки без VIP-скидок на момент написания), их нужно проверять
под конкретный аккаунт.
"""

from collections import defaultdict

from funding_report import (
    fetch_aster, fetch_bybit, fetch_lighter, fetch_mexc, fetch_gate,
    fetch_aster_open_symbols, fetch_bybit_open_symbols, fetch_lighter_open_symbols,
    fetch_mexc_open_symbols, fetch_gate_open_symbols,
    _fetch_in_windows, _EXCHANGE_WINDOW_DAYS, _trim_to_continuous_run,
)

# Для КАЖДОГО символа биржа хранит funding-историю без разбивки на "эта
# позиция" / "предыдущая позиция по тому же символу" — если монету уже
# открывали и закрывали раньше в пределах выбранного периода, её funding
# тоже попадёт в выборку по символу. bot_poll/funding_report для отчёта
# "по открытым позициям" (fetch_all_time_open_positions) от этого защищён —
# он обрезает историю каждого символа до момента, когда ТЕКУЩАЯ позиция
# была открыта (точно — по createTime/transactionTime у Bybit/MEXC,
# приближённо — по непрерывности начислений у Aster/Gate/Lighter, см.
# _trim_to_continuous_run). Раньше калькулятор такой обрезки не делал вовсе
# и просто суммировал все funding-записи по символу за весь период, который
# выбрал пользователь (по умолчанию — последние 30 дней) — из-за этого при
# автоподборе цены для монеты, которую открывали/закрывали больше одного
# раза за это время, калькулятор мог показать сумму БОЛЬШЕ, чем реальный
# funding текущей позиции, хотя бот в Telegram считал верно. Информация
# ниже воспроизводит именно ту логику, что уже используется в отчёте по
# открытым позициям — чтобы оба места считали одинаково.
_EXACT_OPEN_TIME_FIELDS = {
    "bybit": "transactionTime",
    "mexc": "settleTime",
}
_CONTINUOUS_OPEN_TIME_FIELDS = {
    "aster": ("time", "ms"),
    "lighter": ("timestamp", "s"),
    "gate": ("time", "s"),
}


def _trim_to_current_open_position(secrets: dict, exchange: str, symbol: str, records: list) -> list:
    """Оставляет только funding-записи, относящиеся к ТЕКУЩЕЙ открытой
    позиции по символу — если позиция сейчас не открыта на бирже, обрезка
    не применяется вовсе (в этом случае считаем, что пользователь осознанно
    смотрит историческую сумму за произвольный период для уже закрытой
    сделки, и её трогать не нужно)."""
    try:
        if exchange in _EXACT_OPEN_TIME_FIELDS:
            fetch_open = {"bybit": fetch_bybit_open_symbols, "mexc": fetch_mexc_open_symbols}[exchange]
            api_key_field, api_secret_field = {
                "bybit": ("bybit_api_key", "bybit_api_secret"),
                "mexc": ("mexc_api_key", "mexc_api_secret"),
            }[exchange]
            if api_key_field not in secrets:
                return records
            open_times = fetch_open(secrets[api_key_field], secrets[api_secret_field])
            open_time = open_times.get(symbol)
            if open_time is None:
                return records  # сейчас не открыта — обрезка не применяется
            time_field = _EXACT_OPEN_TIME_FIELDS[exchange]
            return [r for r in records if int(r.get(time_field, 0) or 0) >= open_time]

        if exchange in _CONTINUOUS_OPEN_TIME_FIELDS:
            fetch_open = {
                "aster": lambda: fetch_aster_open_symbols(
                    secrets["user"], secrets["signer"], secrets["signer_private_key"],
                ),
                "lighter": lambda: fetch_lighter_open_symbols(
                    secrets["lighter_account_index"], secrets["lighter_auth_token"],
                ),
                "gate": lambda: fetch_gate_open_symbols(secrets["gate_api_key"], secrets["gate_api_secret"]),
            }.get(exchange)
            required_key = {"aster": "user", "lighter": "lighter_account_index", "gate": "gate_api_key"}[exchange]
            if required_key not in secrets or fetch_open is None:
                return records
            open_symbols = fetch_open()
            if symbol not in open_symbols:
                return records  # сейчас не открыта — обрезка не применяется
            time_field, time_unit = _CONTINUOUS_OPEN_TIME_FIELDS[exchange]
            return _trim_to_continuous_run(records, time_field, time_unit)
    except Exception as e:
        # Не удалось проверить статус позиции — лучше вернуть НЕобрезанные
        # данные (старое поведение), чем уронить весь расчёт калькулятора
        # из-за побочного запроса.
        print(f"[calculator] Не удалось обрезать funding до времени открытия позиции ({exchange}/{symbol}): {e}")
        return records

    return records

EXCHANGE_LABELS = {
    "aster": "Aster", "bybit": "Bybit", "lighter": "Lighter",
    "mexc": "MEXC", "gate": "Gate",
}

# income_field/symbol_field/asset_field — те же имена полей, что и в
# funding_report._section_lines/build_report, специально не переименовывались,
# чтобы не разъезжаться с остальным кодом бота.
EXCHANGE_FIELDS = {
    "aster":   {"income": "income",  "symbol": "symbol", "asset": "asset"},
    "bybit":   {"income": "funding", "symbol": "symbol", "asset": "currency"},
    "lighter": {"income": "change",  "symbol": "symbol", "asset": "asset"},
    "mexc":    {"income": "funding", "symbol": "symbol", "asset": "asset"},
    "gate":    {"income": "change",  "symbol": "symbol", "asset": "asset"},
}

# Стандартные (не-VIP, tier 0) taker-ставки в % — только дефолт для UI.
DEFAULT_FUTURES_TAKER_FEE_PCT = {
    "aster": 0.04,
    "bybit": 0.055,
    "mexc": 0.02,
    "gate": 0.05,
    "lighter": 0.0,
}
# Спот-нога может исполняться на другой бирже, чем фьючерс — единого
# дефолта по бирже тут нет, берём типичную ставку большинства spot-бирж.
DEFAULT_SPOT_TAKER_FEE_PCT = 0.10


class CalculatorError(Exception):
    pass


def fetch_funding_history(secrets: dict, exchange: str, start_ms: int, end_ms: int,
                           symbol: str | None = None) -> list:
    """Сырые записи funding по бирже за период, опционально отфильтрованные по символу."""
    window_days = _EXCHANGE_WINDOW_DAYS.get(exchange, 7)

    if exchange == "aster":
        if "user" not in secrets:
            raise CalculatorError("Aster не подключён (нет секретов)")
        fn = lambda s, e: fetch_aster(
            secrets["user"], secrets["signer"], secrets["signer_private_key"], s, e,
        )
    elif exchange == "bybit":
        if "bybit_api_key" not in secrets:
            raise CalculatorError("Bybit не подключён (нет секретов)")
        fn = lambda s, e: fetch_bybit(secrets["bybit_api_key"], secrets["bybit_api_secret"], s, e)
    elif exchange == "lighter":
        if "lighter_account_index" not in secrets:
            raise CalculatorError("Lighter не подключён (нет секретов)")
        fn = lambda s, e: fetch_lighter(secrets["lighter_account_index"], secrets["lighter_auth_token"], s, e)
    elif exchange == "mexc":
        if "mexc_api_key" not in secrets:
            raise CalculatorError("MEXC не подключён (нет секретов)")
        fn = lambda s, e: fetch_mexc(secrets["mexc_api_key"], secrets["mexc_api_secret"], s, e)
    elif exchange == "gate":
        if "gate_api_key" not in secrets:
            raise CalculatorError("Gate не подключён (нет секретов)")
        fn = lambda s, e: fetch_gate(secrets["gate_api_key"], secrets["gate_api_secret"], s, e)
    else:
        raise CalculatorError(f"Неизвестная биржа: {exchange}")

    records = _fetch_in_windows(fn, window_days, start_ms, end_ms)

    if symbol:
        field = EXCHANGE_FIELDS[exchange]["symbol"]
        records = [r for r in records if r.get(field) == symbol]
        # См. комментарий у _trim_to_current_open_position: без этого шага сюда
        # попал бы funding и от предыдущих (уже закрытых) открытий того же
        # символа внутри выбранного периода, а не только текущей позиции.
        records = _trim_to_current_open_position(secrets, exchange, symbol, records)

    return records


def sum_by_asset(records: list, exchange: str) -> dict:
    fields = EXCHANGE_FIELDS[exchange]
    totals: dict = defaultdict(float)
    for r in records:
        asset = r.get(fields["asset"], "USDT")
        totals[asset] += float(r.get(fields["income"], 0))
    return dict(totals)


def group_by_symbol(records: list, exchange: str) -> dict:
    fields = EXCHANGE_FIELDS[exchange]
    totals: dict = defaultdict(float)
    for r in records:
        sym = r.get(fields["symbol"], "UNKNOWN")
        totals[sym] += float(r.get(fields["income"], 0))
    return dict(sorted(totals.items(), key=lambda kv: -abs(kv[1])))


def group_by_day(records: list, exchange: str, time_field_candidates=("time", "transactionTime", "settleTime", "timestamp")) -> dict:
    """Дата (YYYY-MM-DD, UTC) -> сумма funding за день. Ищет поле времени по кандидатам,
    т.к. у разных бирж оно называется по-разному (см. funding_report.py)."""
    from datetime import datetime, timezone

    fields = EXCHANGE_FIELDS[exchange]
    totals: dict = defaultdict(float)
    for r in records:
        raw_t = None
        for f in time_field_candidates:
            if r.get(f) is not None:
                raw_t = r.get(f)
                break
        if raw_t is None:
            continue
        t = int(float(raw_t))
        # У некоторых бирж (Lighter, Gate) время в секундах, у остальных — в мс.
        if t < 10**12:
            t *= 1000
        day = datetime.fromtimestamp(t / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        totals[day] += float(r.get(fields["income"], 0))
    return dict(sorted(totals.items()))


def list_open_symbols(secrets: dict) -> dict:
    """{"aster": [...], ...} — только для подключённых бирж, пусто вместо ошибки."""
    result: dict = {}

    if "user" in secrets:
        try:
            result["aster"] = sorted(fetch_aster_open_symbols(
                secrets["user"], secrets["signer"], secrets["signer_private_key"],
            ))
        except Exception as e:
            print(f"[calculator] Aster open symbols error: {e}")

    if "bybit_api_key" in secrets:
        try:
            result["bybit"] = sorted(fetch_bybit_open_symbols(
                secrets["bybit_api_key"], secrets["bybit_api_secret"],
            ).keys())
        except Exception as e:
            print(f"[calculator] Bybit open symbols error: {e}")

    if "lighter_account_index" in secrets:
        try:
            result["lighter"] = sorted(fetch_lighter_open_symbols(
                secrets["lighter_account_index"], secrets["lighter_auth_token"],
            ))
        except Exception as e:
            print(f"[calculator] Lighter open symbols error: {e}")

    if "mexc_api_key" in secrets:
        try:
            result["mexc"] = sorted(fetch_mexc_open_symbols(
                secrets["mexc_api_key"], secrets["mexc_api_secret"],
            ).keys())
        except Exception as e:
            print(f"[calculator] MEXC open symbols error: {e}")

    if "gate_api_key" in secrets:
        try:
            result["gate"] = sorted(fetch_gate_open_symbols(
                secrets["gate_api_key"], secrets["gate_api_secret"],
            ))
        except Exception as e:
            print(f"[calculator] Gate open symbols error: {e}")

    return result


def list_connected_exchanges(secrets: dict) -> list:
    keys = []
    if "user" in secrets:
        keys.append("aster")
    if "bybit_api_key" in secrets:
        keys.append("bybit")
    if "lighter_account_index" in secrets:
        keys.append("lighter")
    if "mexc_api_key" in secrets:
        keys.append("mexc")
    if "gate_api_key" in secrets:
        keys.append("gate")
    return keys


def calculate(secrets: dict, exchange: str, symbol: str | None,
              start_ms: int, end_ms: int,
              qty: float, spot_entry_price: float, futures_entry_price: float,
              spot_fee_pct: float, futures_fee_pct: float) -> dict:
    if qty <= 0:
        raise CalculatorError("Объём позиции должен быть больше нуля")
    if spot_entry_price <= 0 or futures_entry_price <= 0:
        raise CalculatorError("Цены исполнения должны быть больше нуля")
    if end_ms <= start_ms:
        raise CalculatorError("Конец периода должен быть позже начала")

    records = fetch_funding_history(secrets, exchange, start_ms, end_ms, symbol=symbol)

    income_by_asset = sum_by_asset(records, exchange)
    funding_income_total = sum(income_by_asset.values())

    notional_spot = qty * spot_entry_price
    notional_futures = qty * futures_entry_price
    opening_fees = notional_spot * (spot_fee_pct / 100.0) + notional_futures * (futures_fee_pct / 100.0)

    # Спред при входе: если фьючерс продан (шорт открыт) дороже, чем куплен
    # спот — это доход, зафиксированный на входе (базис); если дешевле — расход.
    spread_effect = notional_futures - notional_spot

    net_total = funding_income_total - opening_fees + spread_effect

    return {
        "exchange": exchange,
        "exchange_label": EXCHANGE_LABELS.get(exchange, exchange),
        "symbol": symbol,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "records_count": len(records),
        "funding_income_by_asset": income_by_asset,
        "funding_income_total": funding_income_total,
        "by_symbol": group_by_symbol(records, exchange) if not symbol else None,
        "by_day": group_by_day(records, exchange),
        "notional_spot": notional_spot,
        "notional_futures": notional_futures,
        "opening_fees": opening_fees,
        "spread_effect": spread_effect,
        "net_total": net_total,
    }
