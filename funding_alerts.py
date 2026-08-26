#!/usr/bin/env python3
"""
Алерты об отрицательном ПРОГНОЗНОМ funding rate по открытым позициям.

Идея: каждые ALERT_CHECK_INTERVAL_MINUTES минут (по умолчанию 60) для
каждой подключённой биржи запрашивается список сейчас открытых позиций
(переиспользует fetch_*_open_symbols из funding_report.py — то же самое,
что уже используется для /positions), а затем для каждого символа —
ПРОГНОЗНАЯ ставка на СЛЕДУЮЩУЮ выплату funding (публичные, не требующие
подписи эндпоинты бирж). Если ставка отрицательная — по вашей стратегии
(шорт на фьючерсах + лонг на споте, доход только с шорта) это означает,
что следующую выплату вы заплатите, а не получите — бот присылает алерт
в Telegram ДО момента списания.

Отрицательная ставка не превращается в поток одинаковых сообщений: алерт
шлётся только на "переходе" из неотрицательной ставки в отрицательную
(edge-triggered per (биржа, символ)). Как только ставка возвращается
к неотрицательной — состояние сбрасывается и следующий уход в минус
снова пришлёт алерт. Состояние хранится в памяти процесса, поэтому
переживает только пока жив процесс (перезапуск на Railway — состояние
обнуляется, возможен один лишний алерт сразу после деплоя, если ставка
в этот момент уже отрицательная — это осознанный компромисс ради простоты).

ВАЖНО про источники прогнозной ставки — не все биржи одинаково честны:
  - Aster, Bybit, MEXC — эндпоинт отдаёт именно ставку, которая будет
    применена к СЛЕДУЮЩЕЙ выплате (подтверждено документацией/полем
    nextFundingTime/nextSettleTime рядом с этим же значением).
  - Gate — есть отдельное поле funding_rate_indicative именно для этого
    (funding_rate — это уже применённая, историческая ставка).
  - Lighter — публичный эндпоинт funding-rates не документирует явно,
    прогноз это или последняя расчётная ставка; используется как есть,
    это лучшее, что доступно публично.

Те же get_open_positions()/get_predicted_rate() используются и для
build_predicted_rates_report() — отчёта по запросу для команды /rates в
Telegram-боте (см. bot_poll.py), не только для фонового цикла алертов.
"""

import os
import time
from datetime import datetime, timezone

import requests

from funding_report import (
    MSK,
    load_secrets,
    send_telegram,
    fetch_aster_open_symbols,
    fetch_bybit_open_symbols,
    fetch_lighter_open_symbols,
    fetch_lighter_markets,
    fetch_mexc_open_symbols,
    fetch_gate_open_symbols,
    _get_proxies,
    _get_mexc_proxies,
    _get_gate_proxies,
)

ALERT_CHECK_INTERVAL_MINUTES = float(os.environ.get("ALERT_CHECK_INTERVAL_MINUTES", "60"))
# Порог в ДОЛЯХ (не в процентах): 0.0 -> алерт при любой ставке < 0
FUNDING_ALERT_THRESHOLD = float(os.environ.get("FUNDING_ALERT_THRESHOLD", "0.0"))

EXCHANGE_LABELS = {
    "aster": "Aster", "bybit": "Bybit", "lighter": "Lighter",
    "mexc": "MEXC", "gate": "Gate",
}


# ── Список сейчас открытых позиций по всем подключённым биржам ───────────────

def get_open_positions(secrets: dict) -> dict:
    """{"aster": ["BTCUSDT", ...], "bybit": [...], ...} — только подключённые биржи."""
    result: dict = {}

    if "user" in secrets:
        try:
            symbols = fetch_aster_open_symbols(
                secrets["user"], secrets["signer"], secrets["signer_private_key"],
            )
            result["aster"] = sorted(symbols)
        except Exception as e:
            print(f"[alerts/aster] Не удалось получить открытые позиции: {e}")

    if "bybit_api_key" in secrets:
        try:
            open_times = fetch_bybit_open_symbols(secrets["bybit_api_key"], secrets["bybit_api_secret"])
            result["bybit"] = sorted(open_times.keys())
        except Exception as e:
            print(f"[alerts/bybit] Не удалось получить открытые позиции: {e}")

    if "lighter_account_index" in secrets:
        try:
            symbols = fetch_lighter_open_symbols(secrets["lighter_account_index"], secrets["lighter_auth_token"])
            result["lighter"] = sorted(symbols)
        except Exception as e:
            print(f"[alerts/lighter] Не удалось получить открытые позиции: {e}")

    if "mexc_api_key" in secrets:
        try:
            open_times = fetch_mexc_open_symbols(secrets["mexc_api_key"], secrets["mexc_api_secret"])
            result["mexc"] = sorted(open_times.keys())
        except Exception as e:
            print(f"[alerts/mexc] Не удалось получить открытые позиции: {e}")

    if "gate_api_key" in secrets:
        try:
            symbols = fetch_gate_open_symbols(secrets["gate_api_key"], secrets["gate_api_secret"])
            result["gate"] = sorted(symbols)
        except Exception as e:
            print(f"[alerts/gate] Не удалось получить открытые позиции: {e}")

    return result


# ── Прогнозная ставка на следующую выплату — публичные эндпоинты бирж ────────

def _fetch_aster_predicted_rate(symbol: str) -> tuple[float, int | None]:
    resp = requests.get(
        "https://fapi.asterdex.com/fapi/v3/premiumIndex",
        params={"symbol": symbol}, timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        data = data[0] if data else {}
    next_ms = int(data["nextFundingTime"]) if data.get("nextFundingTime") else None
    return float(data["lastFundingRate"]), next_ms


def _fetch_bybit_predicted_rate(symbol: str) -> tuple[float, int | None]:
    proxies = _get_proxies()
    resp = requests.get(
        "https://api.bybit.com/v5/market/tickers",
        params={"category": "linear", "symbol": symbol}, timeout=15, proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit tickers error {data.get('retCode')}: {data.get('retMsg')}")
    items = data.get("result", {}).get("list", [])
    if not items:
        raise RuntimeError(f"Bybit: нет данных тикера по {symbol}")
    item = items[0]
    next_ms = int(item["nextFundingTime"]) if item.get("nextFundingTime") else None
    return float(item["fundingRate"]), next_ms


def _fetch_mexc_predicted_rate(symbol: str) -> tuple[float, int | None]:
    proxies = _get_mexc_proxies()
    resp = requests.get(
        f"https://api.mexc.com/api/v1/contract/funding_rate/{symbol}",
        timeout=15, proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"MEXC funding_rate error {data.get('code')}: {data.get('message') or data}")
    d = data.get("data") or {}
    next_ms = int(d["nextSettleTime"]) if d.get("nextSettleTime") else None
    return float(d["fundingRate"]), next_ms


def _fetch_gate_predicted_rate(symbol: str, settle: str = "usdt") -> tuple[float, int | None]:
    proxies = _get_gate_proxies()
    resp = requests.get(
        f"https://api.gateio.ws/api/v4/futures/{settle}/contracts/{symbol}",
        timeout=15, proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("label"):
        raise RuntimeError(f"Gate contract error {data.get('label')}: {data.get('message')}")
    rate = data.get("funding_rate_indicative")
    if rate in (None, ""):
        rate = data.get("funding_rate")
    next_apply = data.get("funding_next_apply")
    next_ms = int(next_apply) * 1000 if next_apply else None
    return float(rate), next_ms


_lighter_markets_cache: dict = {}
_lighter_markets_cache_ts: float = 0.0
LIGHTER_MARKETS_CACHE_TTL_S = 300


def _lighter_symbol_to_market_id(symbol: str) -> int | None:
    global _lighter_markets_cache, _lighter_markets_cache_ts
    now = time.time()
    if not _lighter_markets_cache or now - _lighter_markets_cache_ts > LIGHTER_MARKETS_CACHE_TTL_S:
        _lighter_markets_cache = fetch_lighter_markets()
        _lighter_markets_cache_ts = now
    for market_id, sym in _lighter_markets_cache.items():
        if sym == symbol:
            return market_id
    return None


def _fetch_lighter_predicted_rate(symbol: str) -> tuple[float, int | None]:
    market_id = _lighter_symbol_to_market_id(symbol)
    resp = requests.get("https://mainnet.zklighter.elliot.ai/api/v1/funding-rates", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 200) != 200:
        raise RuntimeError(f"Lighter funding-rates error: {data}")
    for item in data.get("funding_rates", []):
        if item.get("exchange") == "lighter" and (
            item.get("market_id") == market_id or item.get("symbol") == symbol
        ):
            return float(item["rate"]), None
    raise RuntimeError(f"Lighter: ставка по {symbol} не найдена в ответе funding-rates")


_PREDICTED_RATE_FETCHERS = {
    "aster": _fetch_aster_predicted_rate,
    "bybit": _fetch_bybit_predicted_rate,
    "mexc": _fetch_mexc_predicted_rate,
    "gate": _fetch_gate_predicted_rate,
    "lighter": _fetch_lighter_predicted_rate,
}


def get_predicted_rate(exchange: str, symbol: str) -> tuple[float, int | None]:
    """(rate_as_fraction, next_funding_time_ms_or_None). Кидает исключение при ошибке."""
    fetcher = _PREDICTED_RATE_FETCHERS.get(exchange)
    if fetcher is None:
        raise ValueError(f"Неизвестная биржа: {exchange}")
    return fetcher(symbol)


# ── Проверка и отправка алертов ───────────────────────────────────────────────

def _fmt_next_time(next_ms: int | None) -> str:
    if not next_ms:
        return "время неизвестно"
    dt_msk = datetime.fromtimestamp(next_ms / 1000, tz=timezone.utc).astimezone(MSK)
    return dt_msk.strftime("%Y-%m-%d %H:%M МСК")


def check_funding_alerts(secrets: dict, state: dict) -> None:
    """
    Один проход проверки: обновляет state на месте, шлёт алерты в Telegram
    при переходе ставки по (exchange, symbol) из неотрицательной в отрицательную.
    """
    token = secrets["telegram_token"]
    chat_id = secrets["telegram_chat_id"]

    open_positions = get_open_positions(secrets)
    seen_keys = set()

    for exchange, symbols in open_positions.items():
        for symbol in symbols:
            key = (exchange, symbol)
            seen_keys.add(key)
            try:
                rate, next_ms = get_predicted_rate(exchange, symbol)
            except Exception as e:
                print(f"[alerts/{exchange}/{symbol}] Ошибка получения прогнозной ставки: {e}")
                continue

            was_negative = state.get(key, False)
            is_negative = rate < FUNDING_ALERT_THRESHOLD

            if is_negative and not was_negative:
                label = EXCHANGE_LABELS.get(exchange, exchange)
                text = (
                    f"⚠️ Отрицательный прогнозный фандинг по открытой позиции\n"
                    f"{label} {symbol}: {rate * 100:+.4f}% на следующую выплату "
                    f"({_fmt_next_time(next_ms)})\n"
                    f"По вашей схеме (шорт на фьючерсах) эту выплату вы заплатите, а не получите."
                )
                try:
                    send_telegram(token, chat_id, text)
                    print(f"[alerts] Отправлен алерт: {exchange} {symbol} {rate:+.6f}")
                except Exception as e:
                    print(f"[alerts] Не удалось отправить алерт в Telegram: {e}")

            state[key] = is_negative

    # Позиции, которые за это время закрылись, больше не отслеживаем —
    # иначе при повторном открытии той же пары состояние "уже алертили"
    # может ложно сохраниться из прошлого раза.
    for key in list(state.keys()):
        if key not in seen_keys:
            del state[key]


def alert_loop(secrets: dict | None = None) -> None:
    """Бесконечный цикл проверки раз в ALERT_CHECK_INTERVAL_MINUTES минут."""
    if secrets is None:
        secrets = load_secrets()
    state: dict = {}
    interval_s = max(60.0, ALERT_CHECK_INTERVAL_MINUTES * 60)
    print(f"[alerts] Запущен цикл проверки фандинга каждые {ALERT_CHECK_INTERVAL_MINUTES:.0f} мин.", flush=True)

    while True:
        try:
            check_funding_alerts(secrets, state)
        except Exception as e:
            print(f"[alerts] Ошибка цикла проверки: {e}", flush=True)
        time.sleep(interval_s)


# ── Отчёт по запросу: прогнозная ставка для команды /rates в Telegram ────────

def build_predicted_rates_report(secrets: dict) -> str:
    """
    В отличие от build_open_positions_report в funding_report.py (это уже
    НАЧИСЛЕННЫЙ funding с момента открытия позиции), здесь — чего ждать на
    СЛЕДУЮЩУЮ выплату. Переиспользует get_open_positions()/get_predicted_rate()
    — те же функции, что и check_funding_alerts(), поэтому если логика
    получения прогнозной ставки когда-нибудь изменится (например, уточнится
    источник для Lighter, см. докстринг модуля), эта команда останется
    согласована с алертами автоматически, а не разъедется как отдельная
    копия того же самого.
    """
    lines = ["🔮 Прогнозная ставка funding по открытым позициям (на следующую выплату)"]
    open_positions = get_open_positions(secrets)

    if not any(open_positions.values()):
        lines.append("")
        lines.append("Сейчас нет ни одной открытой позиции ни на одной подключённой бирже.")
        return "\n".join(lines)

    any_rate = False
    for exchange, symbols in open_positions.items():
        if not symbols:
            continue
        label = EXCHANGE_LABELS.get(exchange, exchange)
        lines.append("")
        lines.append(f"── {label} ──")

        rows = []    # (rate, symbol, next_ms) — успешно полученные ставки
        errors = []  # (symbol, текст_ошибки)
        for symbol in symbols:
            try:
                rate, next_ms = get_predicted_rate(exchange, symbol)
                rows.append((rate, symbol, next_ms))
                any_rate = True
            except Exception as e:
                errors.append((symbol, str(e)))

        # Сначала самые отрицательные (то, что заплатите) — так самое
        # срочное сразу видно вверху секции, а не теряется среди строк.
        for rate, symbol, next_ms in sorted(rows, key=lambda x: x[0]):
            emoji = "🟢" if rate >= 0 else "🔴"
            lines.append(f"{emoji} {symbol}: {rate * 100:+.4f}% ({_fmt_next_time(next_ms)})")

        for symbol, err in errors:
            lines.append(f"⚠️ {symbol}: не удалось получить ставку ({err})")

    if not any_rate:
        lines.append("")
        lines.append("Не удалось получить прогнозную ставку ни по одной позиции.")

    return "\n".join(lines)


if __name__ == "__main__":
    alert_loop()
