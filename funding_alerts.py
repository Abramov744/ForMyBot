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

ГОДОВАЯ СТАВКА (APR) — периодичность выплат сильно различается по биржам
(1ч/4ч/8ч в зависимости от биржи и даже конкретного символа), поэтому
сравнивать сами ставки "за выплату" между биржами напрямую нельзя — нужно
привести к общему знаменателю. get_predicted_rate() возвращает интервал
выплат вместе со ставкой (третий элемент кортежа), и annualize() считает
APR = ставка_за_выплату × (24 / интервал_в_часах) × 365. Источник интервала
у каждой биржи свой (см. комментарии над каждым _fetch_*_predicted_rate):
  - Bybit, MEXC, Gate — отдают интервал прямо в том же ответе, что и саму
    ставку (fundingIntervalHour / collectCycle / funding_interval) —
    ничего дополнительно запрашивать не нужно.
  - Aster — интервал не в premiumIndex, а в отдельном публичном (без
    подписи) эндпоинте /fapi/v1/fundingInfo, который отдаёт его только для
    символов с НЕстандартным интервалом (документированное поведение
    Binance-совместимых API) — для всех остальных подразумевается дефолт
    8ч. Список кэшируется в памяти процесса на ASTER_FUNDING_INTERVALS_
    CACHE_TTL_S, чтобы не дёргать лишний раз при каждом символе.
  - Lighter — единственная биржа, где funding зафиксирован протоколом на
    уровне "раз в час" для вообще всех рынков (не варьируется по символам,
    задокументировано в docs.lighter.xyz/perpetual-futures/funding) —
    поэтому просто константа, без похода в API за этим значением.
Для Aster/Bybit/MEXC/Gate, если по какой-то причине поле интервала не
пришло в ответе, используется задокументированный дефолт биржи (8ч почти
везде) — это единственное разумное поведение при отсутствии данных, но
из-за этого APR в таком случае может быть немного неточным именно для
символов с нестандартным интервалом; такое расхождение проявится только
если сама биржа перестанет отдавать это поле.
"""

import html
import os
import time
from datetime import datetime, timezone

import requests

import aster_interval_history
from balances import _mexc_account_assets_raw, _gate_futures_account_raw
from entry_price import _fetch_mexc_contract_size
from liquidation_alerts import (
    _bybit_liquidation_positions,
    _aster_liquidation_positions,
    _kucoin_liquidation_positions,
    _gate_liquidation_positions,
    _mexc_liquidation_positions,
    _lighter_liquidation_positions,
    _mexc_fair_price,
)
from funding_report import (
    MSK,
    load_secrets,
    send_telegram_broadcast,
    fetch_aster_open_symbols,
    fetch_bybit_open_symbols,
    fetch_lighter_open_symbols,
    fetch_lighter_markets,
    fetch_lighter_mark_prices,
    fetch_mexc_open_symbols,
    fetch_gate_open_symbols,
    fetch_kucoin_open_symbols,
    _cached_raw_positions,
    _lighter_positions_raw,
    _lighter_account_raw,
    _mexc_positions_raw,
    _gate_positions_raw,
    _get_proxies,
    _get_mexc_proxies,
    _get_gate_proxies,
    _get_kucoin_proxies,
)

ALERT_CHECK_INTERVAL_MINUTES = float(os.environ.get("ALERT_CHECK_INTERVAL_MINUTES", "60"))
# Порог в ДОЛЯХ (не в процентах): 0.0 -> алерт при любой ставке < 0
FUNDING_ALERT_THRESHOLD = float(os.environ.get("FUNDING_ALERT_THRESHOLD", "0.0"))

EXCHANGE_LABELS = {
    "aster": "Aster", "bybit": "Bybit", "lighter": "Lighter",
    "mexc": "MEXC", "gate": "Gate", "kucoin": "KuCoin",
}

# Дефолтный интервал выплат (часы), используется только если биржа не
# отдала его явно в ответе — см. докстринг модуля, раздел "ГОДОВАЯ СТАВКА".
ASTER_DEFAULT_FUNDING_INTERVAL_HOURS = 8.0
BYBIT_DEFAULT_FUNDING_INTERVAL_HOURS = 8.0
MEXC_DEFAULT_FUNDING_INTERVAL_HOURS = 8.0
GATE_DEFAULT_FUNDING_INTERVAL_HOURS = 8.0
# KuCoin отдаёт granularity (интервал) в каждом ответе — используется как
# дефолт только если поле неожиданно отсутствует (не должно происходить по
# документации, но остальные биржи в этом файле тоже на этот случай имеют
# дефолт, см. остальные константы выше).
KUCOIN_DEFAULT_FUNDING_INTERVAL_HOURS = 8.0
# У Lighter funding зафиксирован протоколом на "раз в час" для всех рынков
# без исключений — не запрашивается через API, см. докстринг модуля.
LIGHTER_FUNDING_INTERVAL_HOURS = 1.0


# ── Список сейчас открытых позиций по всем подключённым биржам ───────────────

def get_open_positions(secrets: dict) -> tuple[dict, set]:
    """
    ({"aster": ["BTCUSDT", ...], "bybit": [...], ...}, {биржи_с_ошибкой_запроса})
    — только подключённые биржи.

    ВТОРОЙ элемент — множество бирж, у которых запрос списка открытых
    позиций В ЭТОМ ВЫЗОВЕ завершился ошибкой (транзиентный сбой сети/прокси/
    временная ошибка API) — раньше это никак не отличалось от "открытых
    позиций на бирже действительно нет" (биржа просто не попадала в
    result). На практике это привело к реальному багу: sltp_alerts.py
    сравнивает список открытых позиций с предыдущим проходом, чтобы
    обнаружить закрытие — один разовый сетевой сбой на, скажем, MEXC
    заставлял результат выглядеть как "на MEXC открытых позиций теперь 0",
    что sltp_alerts.py читал как "ВСЕ открытые на MEXC позиции только что
    закрылись" — ложный алерт о закрытии в Telegram И ложная запись даты
    закрытия в Google Sheet для позиции, которая на самом деле всё ещё
    открыта (см. историю бага — реальный случай с MEXC BTW_USDT). Теперь
    вызывающий код (см. sltp_alerts.check_closed_positions) может явно
    пропустить сравнение для биржи, чьё текущее состояние на самом деле
    неизвестно, а не спутать это с "позиций нет".
    """
    result: dict = {}
    failed: set = set()

    if "user" in secrets:
        try:
            symbols = fetch_aster_open_symbols(
                secrets["user"], secrets["signer"], secrets["signer_private_key"],
            )
            result["aster"] = sorted(symbols)
        except Exception as e:
            print(f"[alerts/aster] Не удалось получить открытые позиции: {e}")
            failed.add("aster")

    if "bybit_api_key" in secrets:
        try:
            open_times = fetch_bybit_open_symbols(secrets["bybit_api_key"], secrets["bybit_api_secret"])
            result["bybit"] = sorted(open_times.keys())
        except Exception as e:
            print(f"[alerts/bybit] Не удалось получить открытые позиции: {e}")
            failed.add("bybit")

    if "lighter_account_index" in secrets:
        try:
            symbols = fetch_lighter_open_symbols(secrets["lighter_account_index"], secrets["lighter_auth_token"])
            result["lighter"] = sorted(symbols)
        except Exception as e:
            print(f"[alerts/lighter] Не удалось получить открытые позиции: {e}")
            failed.add("lighter")

    if "mexc_api_key" in secrets:
        try:
            open_times = fetch_mexc_open_symbols(secrets["mexc_api_key"], secrets["mexc_api_secret"])
            result["mexc"] = sorted(open_times.keys())
        except Exception as e:
            print(f"[alerts/mexc] Не удалось получить открытые позиции: {e}")
            failed.add("mexc")

    if "gate_api_key" in secrets:
        try:
            symbols = fetch_gate_open_symbols(secrets["gate_api_key"], secrets["gate_api_secret"])
            result["gate"] = sorted(symbols)
        except Exception as e:
            print(f"[alerts/gate] Не удалось получить открытые позиции: {e}")
            failed.add("gate")

    if "kucoin_api_key" in secrets:
        try:
            open_times = fetch_kucoin_open_symbols(
                secrets["kucoin_api_key"], secrets["kucoin_api_secret"], secrets["kucoin_api_passphrase"],
            )
            result["kucoin"] = sorted(open_times.keys())
        except Exception as e:
            print(f"[alerts/kucoin] Не удалось получить открытые позиции: {e}")
            failed.add("kucoin")

    return result, failed


# ── Прогнозная ставка на следующую выплату — публичные эндпоинты бирж ────────

_aster_funding_intervals_cache: dict = {}
_aster_funding_intervals_cache_ts: float = 0.0
# Сами интервалы у биржи меняются крайне редко (это не рыночные данные,
# а конфигурация символа), поэтому кэш держим долго — час.
ASTER_FUNDING_INTERVALS_CACHE_TTL_S = 3600


def _fetch_aster_funding_intervals() -> dict:
    """
    symbol -> fundingIntervalHours, ТОЛЬКО для символов с нестандартным
    интервалом (см. докстринг модуля) — публичный эндпоинт без подписи.
    Для символа, которого нет в результате, действует дефолт 8ч
    (ASTER_DEFAULT_FUNDING_INTERVAL_HOURS) — это задокументированное
    поведение Binance-совместимого API, не догадка.
    """
    global _aster_funding_intervals_cache, _aster_funding_intervals_cache_ts
    now = time.time()
    if _aster_funding_intervals_cache and now - _aster_funding_intervals_cache_ts <= ASTER_FUNDING_INTERVALS_CACHE_TTL_S:
        return _aster_funding_intervals_cache

    resp = requests.get("https://fapi.asterdex.com/fapi/v1/fundingInfo", timeout=15)
    resp.raise_for_status()
    data = resp.json()
    _aster_funding_intervals_cache = {
        item["symbol"]: float(item["fundingIntervalHours"])
        for item in data
        if item.get("symbol") and item.get("fundingIntervalHours") is not None
    }
    _aster_funding_intervals_cache_ts = now
    return _aster_funding_intervals_cache


def _fetch_aster_predicted_rate(symbol: str) -> tuple[float, int | None, float]:
    resp = requests.get(
        "https://fapi.asterdex.com/fapi/v3/premiumIndex",
        params={"symbol": symbol}, timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, list):
        data = data[0] if data else {}
    next_ms = int(data["nextFundingTime"]) if data.get("nextFundingTime") else None

    try:
        interval_hours = _fetch_aster_funding_intervals().get(
            symbol, ASTER_DEFAULT_FUNDING_INTERVAL_HOURS,
        )
    except Exception as e:
        print(f"[rates/aster] Не удалось получить fundingInfo, беру дефолт {ASTER_DEFAULT_FUNDING_INTERVAL_HOURS:g}ч: {e}")
        interval_hours = ASTER_DEFAULT_FUNDING_INTERVAL_HOURS

    return float(data["lastFundingRate"]), next_ms, interval_hours


def _fetch_bybit_predicted_rate(symbol: str) -> tuple[float, int | None, float]:
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
    # fundingIntervalHour — прямо в тикере (см. докстринг модуля), в тех же
    # единицах (часах), что нам и нужно, без похода в instruments-info.
    interval_hours = (
        float(item["fundingIntervalHour"])
        if item.get("fundingIntervalHour")
        else BYBIT_DEFAULT_FUNDING_INTERVAL_HOURS
    )
    return float(item["fundingRate"]), next_ms, interval_hours


def _fetch_mexc_predicted_rate(symbol: str) -> tuple[float, int | None, float]:
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
    # collectCycle — интервал в часах прямо в том же ответе (см. докстринг модуля).
    interval_hours = (
        float(d["collectCycle"]) if d.get("collectCycle") else MEXC_DEFAULT_FUNDING_INTERVAL_HOURS
    )
    return float(d["fundingRate"]), next_ms, interval_hours


def _fetch_gate_predicted_rate(symbol: str, settle: str = "usdt") -> tuple[float, int | None, float]:
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
    # funding_interval — в СЕКУНДАХ (см. докстринг модуля), переводим в часы.
    funding_interval_s = data.get("funding_interval")
    interval_hours = (
        float(funding_interval_s) / 3600.0 if funding_interval_s else GATE_DEFAULT_FUNDING_INTERVAL_HOURS
    )
    return float(rate), next_ms, interval_hours


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


def _fetch_lighter_predicted_rate(symbol: str) -> tuple[float, int | None, float]:
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
            return float(item["rate"]) / 8.0, None, LIGHTER_FUNDING_INTERVAL_HOURS
    raise RuntimeError(f"Lighter: ставка по {symbol} не найдена в ответе funding-rates")


def _fetch_kucoin_predicted_rate(symbol: str) -> tuple[float, int | None, float]:
    """
    GET /api/v1/funding-rate/{symbol}/current — публичный (без ключей)
    эндпоинт KuCoin Futures. Поля ответа (value/fundingTime/granularity)
    подтверждены офиц. SDK ccxt (kucoin.py,
    futuresPublicGetFundingRateSymbolCurrent). granularity — интервал в МС
    (не фиксирован на 8ч, как у остальных бирж выше, КуCoin документирует
    право менять его по символу — используется как есть, без округления к
    ближайшему "стандартному" значению).
    """
    resp = requests.get(
        f"https://api-futures.kucoin.com/api/v1/funding-rate/{symbol}/current",
        timeout=15, proxies=_get_kucoin_proxies(),
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != "200000":
        raise RuntimeError(f"KuCoin funding-rate/current error {data.get('code')}: {data.get('msg')}")
    d = data.get("data") or {}
    next_ms = int(d["fundingTime"]) if d.get("fundingTime") else None
    granularity_ms = d.get("granularity")
    interval_hours = float(granularity_ms) / 3_600_000.0 if granularity_ms else KUCOIN_DEFAULT_FUNDING_INTERVAL_HOURS
    return float(d["value"]), next_ms, interval_hours


_PREDICTED_RATE_FETCHERS = {
    "aster": _fetch_aster_predicted_rate,
    "bybit": _fetch_bybit_predicted_rate,
    "mexc": _fetch_mexc_predicted_rate,
    "gate": _fetch_gate_predicted_rate,
    "lighter": _fetch_lighter_predicted_rate,
    "kucoin": _fetch_kucoin_predicted_rate,
}


def get_predicted_rate(exchange: str, symbol: str) -> tuple[float, int | None, float]:
    """
    (rate_as_fraction, next_funding_time_ms_or_None, funding_interval_hours).
    Кидает исключение при ошибке.
    """
    fetcher = _PREDICTED_RATE_FETCHERS.get(exchange)
    if fetcher is None:
        raise ValueError(f"Неизвестная биржа: {exchange}")
    return fetcher(symbol)


def annualize(rate: float, interval_hours: float) -> float:
    """
    Ставка "за выплату" -> ставка годовых, в ПРОЦЕНТАХ (не долях).
    Периодичность выплат разная по биржам и символам (1ч/4ч/8ч), поэтому
    сравнивать ставки за выплату между собой напрямую нельзя — см. раздел
    "ГОДОВАЯ СТАВКА" в докстринге модуля.
    """
    if not interval_hours or interval_hours <= 0:
        return float("nan")
    payments_per_year = (24.0 / interval_hours) * 365.0
    return rate * payments_per_year * 100.0


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
    chat_ids = secrets["telegram_chat_ids"]

    open_positions, failed_exchanges = get_open_positions(secrets)
    seen_keys = set()

    for exchange, symbols in open_positions.items():
        for symbol in symbols:
            key = (exchange, symbol)
            seen_keys.add(key)
            try:
                rate, next_ms, interval_hours = get_predicted_rate(exchange, symbol)
            except Exception as e:
                print(f"[alerts/{exchange}/{symbol}] Ошибка получения прогнозной ставки: {e}")
                continue

            # Побочный эффект: копим историю ФАКТИЧЕСКИ действующего интервала
            # funding по Aster (см. aster_interval_history.py про то, зачем) —
            # этот цикл и так уже раз в ALERT_CHECK_INTERVAL_MINUTES получает
            # актуальный interval_hours для каждого открытого символа, лишних
            # запросов к бирже это не добавляет.
            if exchange == "aster":
                aster_interval_history.record_observation(symbol, interval_hours)

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
                send_telegram_broadcast(token, chat_ids, text)
                print(f"[alerts] Отправлен алерт: {exchange} {symbol} {rate:+.6f}")

            state[key] = is_negative

    # Позиции, которые за это время закрылись, больше не отслеживаем —
    # иначе при повторном открытии той же пары состояние "уже алертили"
    # может ложно сохраниться из прошлого раза. НО: для бирж, чей запрос
    # списка позиций в этом проходе завершился ошибкой (failed_exchanges),
    # их символы просто отсутствуют в seen_keys — не потому что реально
    # закрылись, а потому что мы не знаем их текущее состояние (см.
    # докстринг get_open_positions про историю этого бага) — не удаляем их
    # state, иначе временный сбой сбрасывает edge-trigger память и может
    # породить лишний повторный алерт по уже известной отрицательной ставке.
    for key in list(state.keys()):
        if key not in seen_keys and key[0] not in failed_exchanges:
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

def _lighter_margin_base(secrets: dict) -> float:
    """
    Поддерживаемая маржа счёта Lighter — тот же смысл (весь реальный
    капитал счёта, НЕ считая floating PnL), что и у MEXC/Gate, см.
    margin_alerts.py. Для Lighter это одно готовое поле аккаунта —
    collateral (ПОДТВЕРЖДЕНО офиц. docs SDK, см. funding_report.
    _lighter_account_raw) — не нужно ничего складывать, как у MEXC.
    """
    accounts = _cached_raw_positions("lighter:account", lambda: _lighter_account_raw(
        secrets["lighter_account_index"], secrets["lighter_auth_token"],
    ))
    return sum(float(acc.get("collateral", 0) or 0) for acc in accounts)


def _lighter_margin_pnl_positions(secrets: dict) -> list:
    """
    Для Lighter — entry/mark как и в liquidation_alerts.
    _lighter_liquidation_positions, но ДОПОЛНИТЕЛЬНО unrealized_pnl — как
    и Gate, Lighter отдаёт долларовый PnL позиции ПРЯМО в ответе (поле
    unrealized_pnl, тот же самый, что уже используется в balances.
    fetch_lighter_balance), не нужно вычислять через qty*price_diff, как
    для MEXC.
    """
    markets = fetch_lighter_markets()
    mark_prices = fetch_lighter_mark_prices()
    positions = _cached_raw_positions("lighter", lambda: _lighter_positions_raw(
        secrets["lighter_account_index"], secrets["lighter_auth_token"],
    ))
    out = []
    for pos in positions:
        size = float(pos.get("position", pos.get("size", pos.get("position_size", 0))) or 0)
        if size == 0:
            continue
        sign = pos.get("sign")
        is_short = (int(sign) < 0) if sign is not None else (size < 0)
        if not is_short:
            continue  # у бота только шорты (см. CLAUDE.md)
        entry = None
        for key in ("avg_entry_price", "entry_price", "avgEntryPrice", "entryPrice"):
            if pos.get(key) not in (None, ""):
                entry = float(pos[key])
                break
        if entry is None:
            continue
        market_id = pos.get("market_id", pos.get("market_index"))
        symbol = markets.get(market_id, f"MARKET_{market_id}")
        mark = mark_prices.get(symbol)
        if mark is None:
            continue
        out.append({
            "symbol": symbol, "entry_price": entry, "mark_price": mark,
            "pnl_dollar": float(pos.get("unrealized_pnl", 0) or 0),
        })
    return out


def _mexc_margin_base(secrets: dict) -> float:
    """
    Поддерживаемая маржа фьючерсного счёта MEXC — та же формула, что и в
    margin_alerts.py (проверена на реальных сырых данных счёта
    пользователя, см. докстринг margin_alerts.py): margin_base =
    cashBalance + positionMargin — оба поля из ответа account/assets, НЕ
    зависящие от unrealized PnL (это весь реальный капитал на фьюч-счёте:
    свободные деньги + маржа, уже заложенная под открытую позицию).
    Суммируется по всем валютам счёта (на практике почти всегда только
    USDT, см. README) — отдельный сырой фетчер _mexc_account_assets_raw
    переиспользуется через импорт, не копируется.
    """
    raw = _mexc_account_assets_raw(secrets["mexc_api_key"], secrets["mexc_api_secret"])
    return sum(
        float(a.get("cashBalance", 0) or 0) + float(a.get("positionMargin", 0) or 0)
        for a in raw
    )


def _mexc_margin_pnl_positions(secrets: dict) -> list:
    """
    Для MEXC — entry/mark как и в _mexc_liquidation_positions, но
    ДОПОЛНИТЕЛЬНО реальный ДОЛЛАРОВЫЙ нереализованный PnL позиции
    (pnl_dollar), чтобы в /rates показывать его как % от маржи счёта, а не
    % от цены входа (явная просьба пользователя: с плечом небольшое
    движение цены может быть большим процентом от маржи, и наоборот —
    голый "% от цены" этого не отражает).

    holdVol у MEXC — количество КОНТРАКТОВ, не монет, поэтому для
    долларового PnL нужно домножить на contractSize конкретного символа
    (entry_price._fetch_mexc_contract_size — тот же хелпер, что уже
    используется в short_position_tracker для перевода holdVol в монеты).
    Позиция — шорт (у бота только шорты, см. CLAUDE.md), поэтому прибыль,
    когда цена ниже входа: pnl_dollar = qty_coins * (entry - mark).
    """
    items = _cached_raw_positions("mexc", lambda: _mexc_positions_raw(
        secrets["mexc_api_key"], secrets["mexc_api_secret"],
    ))
    out = []
    for p in items:
        if str(p.get("positionType")) == "1" or float(p.get("holdVol", 0) or 0) <= 0:
            continue  # positionType 1 == long (см. докстринг liquidation_alerts.py)
        entry = p.get("openAvgPrice") or p.get("holdAvgPrice")
        if not entry:
            continue
        symbol = p["symbol"]
        try:
            mark = _mexc_fair_price(symbol)
        except Exception as e:
            print(f"[rates/mexc] Не удалось получить текущую цену {symbol}: {e}")
            continue
        contract_size = _fetch_mexc_contract_size(symbol)
        qty_coins = float(p["holdVol"]) * contract_size
        entry = float(entry)
        pnl_dollar = qty_coins * (entry - mark)
        out.append({"symbol": symbol, "entry_price": entry, "mark_price": mark, "pnl_dollar": pnl_dollar})
    return out


def _gate_margin_base(secrets: dict, settle: str = "usdt") -> float:
    """
    Поддерживаемая маржа фьючерсного счёта Gate — та же формула, что и в
    margin_alerts.py (см. её докстринг): margin_base = total (уже сам
    сумма position_margin+order_margin+available, не зависит от
    unrealised_pnl) — ПОДТВЕРЖДЕНО по офиц. докам Gate API v4. Отдельный
    сырой фетчер _gate_futures_account_raw переиспользуется через импорт.
    """
    data = _gate_futures_account_raw(secrets["gate_api_key"], secrets["gate_api_secret"], settle)
    return float(data.get("total", 0) or 0)


def _gate_margin_pnl_positions(secrets: dict, settle: str = "usdt") -> list:
    """
    Для Gate — entry/mark как и в _gate_liquidation_positions, но
    ДОПОЛНИТЕЛЬНО unrealised_pnl — Gate, в отличие от MEXC, отдаёт
    долларовый PnL позиции ПРЯМО в ответе positions (поле unrealised_pnl,
    подтверждено офиц. докам), поэтому не нужно домножать qty на разницу
    цен самостоятельно — проще и надёжнее, чем для MEXC.
    """
    items = _cached_raw_positions(f"gate:{settle}", lambda: _gate_positions_raw(
        secrets["gate_api_key"], secrets["gate_api_secret"], settle,
    ))
    out = []
    for p in items:
        size = float(p.get("size", 0) or 0)
        if size >= 0:
            continue  # у бота только шорты (см. CLAUDE.md)
        entry, mark = p.get("entry_price"), p.get("mark_price")
        if not entry or not mark:
            continue
        out.append({
            "symbol": p["contract"],
            "entry_price": float(entry),
            "mark_price": float(mark),
            "pnl_dollar": float(p.get("unrealised_pnl", 0) or 0),
        })
    return out


# Цена входа/текущая/ЛИКВИДАЦИИ для всех шести бирж — те же position-
# фетчеры, что уже написаны и проверены для liquidation_alerts.py
# (переиспользуются через импорт, не копируются заново). Раньше здесь не
# было MEXC/Gate/Lighter (у них был отдельный путь без liq_price, когда
# таблицы ещё не было) — теперь нужны ВСЕ восемь полей сразу (включая цену
# ликвидации) в каждой строке таблицы /rates, так что это снова общий
# источник для всех шести, а _MARGIN_PNL_FETCHERS ниже используется
# ДОПОЛНИТЕЛЬНО (для PnL/% от маржи), не вместо.
_PRICE_FETCHERS = {
    "bybit": _bybit_liquidation_positions,
    "aster": _aster_liquidation_positions,
    "gate": _gate_liquidation_positions,
    "kucoin": _kucoin_liquidation_positions,
    "mexc": _mexc_liquidation_positions,
    "lighter": _lighter_liquidation_positions,
}

# Биржи, для которых /rates показывает % от поддерживаемой МАРЖИ СЧЁТА
# вместо % от цены входа (явная просьба пользователя: сначала MEXC, затем
# Gate и Lighter — "сделай всё то же самое"). Каждая запись —
# (pnl_positions_fetcher, margin_base_fetcher); структура рассчитана на
# дальнейшее расширение на другие биржи без переделки build_predicted_
# rates_report.
_MARGIN_PNL_FETCHERS = {
    "mexc": (_mexc_margin_pnl_positions, _mexc_margin_base),
    "gate": (_gate_margin_pnl_positions, _gate_margin_base),
    "lighter": (_lighter_margin_pnl_positions, _lighter_margin_base),
}


# Заголовки таблицы /rates (build_predicted_rates_report) и то, как
# выравнивается каждый столбец — текстовые столбцы (монета/биржа) влево,
# числовые вправо (обычный вид таблицы, числа читаются по правому краю).
_RATES_TABLE_HEADERS = ["Монета", "Биржа", "Ставка", "APR", "PnL", "%Маржи", "Цена", "Ликв."]
_RATES_TABLE_LEFT_ALIGN = {0, 1}


def _rates_table_row(symbol, exchange_label, rate, apr, pnl_dollar, margin_pct, mark, liq) -> list:
    return [
        symbol,
        exchange_label,
        f"{rate * 100:+.3f}%",
        f"{apr:+.1f}%",
        f"{pnl_dollar:+.2f}" if pnl_dollar is not None else "—",
        f"{margin_pct:+.1f}%" if margin_pct is not None else "—",
        f"{mark:g}" if mark is not None else "—",
        f"{liq:g}" if liq is not None else "—",
    ]


def _render_monospace_table(headers: list, rows: list) -> str:
    """
    Моноширинная HTML-таблица (<pre>...</pre>) — Telegram выравнивает
    пробелами только внутри <pre>/<code> (моноширинный шрифт), в обычном
    тексте колонки "расползлись" бы, потому что шрифт пропорциональный.
    Ширина каждого столбца — по самой длинной ячейке в нём (включая
    заголовок); текстовые столбцы (индексы из _RATES_TABLE_LEFT_ALIGN)
    выравниваются влево, числовые — вправо. html.escape на每 ячейку — на
    случай, если в данных (символ/биржа — внешние данные бирж, не наш
    ввод) окажется "<"/">"/"&", которые сломали бы HTML-разметку.
    """
    widths = [
        max(len(headers[i]), *(len(row[i]) for row in rows)) if rows else len(headers[i])
        for i in range(len(headers))
    ]

    def fmt_row(cells: list) -> str:
        parts = []
        for i, cell in enumerate(cells):
            padded = cell.ljust(widths[i]) if i in _RATES_TABLE_LEFT_ALIGN else cell.rjust(widths[i])
            parts.append(html.escape(padded))
        return "  ".join(parts)

    lines = [fmt_row(headers), "  ".join("-" * w for w in widths)]
    lines.extend(fmt_row(row) for row in rows)
    return "<pre>" + "\n".join(lines) + "</pre>"


def build_predicted_rates_report(secrets: dict) -> str:
    """
    ТАБЛИЦА по всем открытым сейчас позициям — по явной просьбе
    пользователя заменяет прежний текстовый список с разбивкой по биржам:
    одна строка на (биржа, символ), столбцы Монета/Биржа/Ставка/APR/PnL/
    %Маржи/Цена/Ликв. (именно в этом порядке и составе запрошены явно).
    Рендерится как моноширинный блок <pre> (см. _render_monospace_table) —
    без этого в Telegram колонки, выровненные пробелами, не совпадали бы
    по вертикали (пропорциональный шрифт вне <pre>/<code>), поэтому вызов
    send_telegram ниже по цепочке (bot_poll.send_predicted_rates_report)
    должен передавать parse_mode="HTML".

    В отличие от build_open_positions_report в funding_report.py (это уже
    НАЧИСЛЕННЫЙ funding с момента открытия позиции), здесь — чего ждать на
    СЛЕДУЮЩУЮ выплату. Переиспользует get_open_positions()/get_predicted_rate()
    — те же функции, что и check_funding_alerts(), поэтому если логика
    получения прогнозной ставки когда-нибудь изменится, эта команда
    останется согласована с алертами автоматически.

    "Ставка" — за одну выплату (знак — платите вы или вам), "APR" —
    приведённая к году (funding_alerts.annualize, т.к. периодичность
    выплат разная по биржам/символам и ставки "за выплату" между собой
    напрямую не сравнить, см. докстринг модуля).

    "PnL"/"%Маржи" заполнены только для MEXC/Gate/Lighter (см.
    _MARGIN_PNL_FETCHERS выше) — там есть формула поддерживаемой маржи
    счёта (явная просьба пользователя, сначала MEXC, затем "сделай всё то
    же самое" для Gate и Lighter). Для Bybit/Aster/KuCoin поддерживаемая
    маржа не отслеживается — в этих столбцах "—", а не выдуманное число.

    "Цена"/"Ликв." — для ВСЕХ шести бирж, через те же position-фетчеры,
    что уже написаны и проверены для liquidation_alerts.py (_PRICE_FETCHERS
    выше, переиспользуются через импорт, не копируются заново) — позиция
    пропускается из этих двух столбцов (не из таблицы целиком), если биржа
    не отдала валидную цену ликвидации для неё (см. докстринги конкретных
    _*_liquidation_positions про когда так бывает, например кросс-маржа).

    Строки отсортированы по ставке за выплату (сначала самое отрицательное
    — то, что спишут в первую очередь), как и в прежнем текстовом отчёте.
    """
    open_positions, failed_exchanges = get_open_positions(secrets)

    header_lines = []
    if failed_exchanges:
        labels = ", ".join(EXCHANGE_LABELS.get(ex, ex) for ex in sorted(failed_exchanges))
        header_lines.append(f"⚠️ Не удалось получить список открытых позиций: {labels} — по ним данных в таблице нет (это не значит, что там нет позиций).")

    if not any(open_positions.values()):
        msg = "Сейчас нет ни одной открытой позиции ни на одной подключённой бирже (либо не удалось получить данные — см. предупреждение выше)."
        return "\n\n".join(header_lines + [msg]) if header_lines else msg

    table_rows = []
    sort_rates = []  # параллельно table_rows — ставка за выплату, для сортировки
    errors = []      # (exchange, symbol, текст_ошибки)

    for exchange, symbols in open_positions.items():
        if not symbols:
            continue

        # Цена входа/текущая/ликвидации — одним запросом на всю биржу (не
        # по одному на символ); ошибка здесь не должна ронять саму ставку
        # ниже — просто столбцы Цена/Ликв. останутся с "—".
        price_by_symbol = {}
        price_fetcher = _PRICE_FETCHERS.get(exchange)
        if price_fetcher:
            try:
                price_by_symbol = {p["symbol"]: p for p in price_fetcher(secrets)}
            except Exception as e:
                print(f"[rates/{exchange}] Не удалось получить цену/ликвидацию: {e}")

        # PnL/% от маржи — только для MEXC/Gate/Lighter (см. докстринг).
        pnl_by_symbol = {}
        margin_base = None
        if exchange in _MARGIN_PNL_FETCHERS:
            pnl_fetcher, margin_base_fetcher = _MARGIN_PNL_FETCHERS[exchange]
            try:
                margin_base = margin_base_fetcher(secrets)
            except Exception as e:
                print(f"[rates/{exchange}] Не удалось получить маржу счёта: {e}")
            try:
                pnl_by_symbol = {p["symbol"]: p for p in pnl_fetcher(secrets)}
            except Exception as e:
                print(f"[rates/{exchange}] Не удалось получить PnL позиций: {e}")

        for symbol in symbols:
            try:
                rate, next_ms, interval_hours = get_predicted_rate(exchange, symbol)
            except Exception as e:
                errors.append((exchange, symbol, str(e)))
                continue

            apr = annualize(rate, interval_hours)
            price_info = price_by_symbol.get(symbol)
            mark = price_info["mark_price"] if price_info else None
            liq = price_info.get("liq_price") if price_info else None

            pnl_info = pnl_by_symbol.get(symbol)
            pnl_dollar = pnl_info["pnl_dollar"] if pnl_info else None
            margin_pct = (pnl_dollar / margin_base * 100) if (pnl_dollar is not None and margin_base) else None

            table_rows.append(_rates_table_row(
                symbol, EXCHANGE_LABELS.get(exchange, exchange), rate, apr, pnl_dollar, margin_pct, mark, liq,
            ))
            sort_rates.append(rate)

    if not table_rows:
        lines = header_lines + ["Не удалось получить прогнозную ставку ни по одной позиции."]
        for exchange, symbol, err in errors:
            lines.append(f"⚠️ {EXCHANGE_LABELS.get(exchange, exchange)} {symbol}: не удалось получить ставку ({err})")
        return "\n".join(lines)

    # Сначала самые отрицательные ставки (то, что спишут) — см. докстринг.
    table_rows = [row for _, row in sorted(zip(sort_rates, table_rows), key=lambda x: x[0])]

    result = list(header_lines)
    result.append("🔮 Прогнозная ставка funding по открытым позициям (на следующую выплату):")
    result.append(_render_monospace_table(_RATES_TABLE_HEADERS, table_rows))
    for exchange, symbol, err in errors:
        result.append(f"⚠️ {EXCHANGE_LABELS.get(exchange, exchange)} {symbol}: не удалось получить ставку ({err})")

    return "\n".join(result)


if __name__ == "__main__":
    alert_loop()
