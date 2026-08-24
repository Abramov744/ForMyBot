#!/usr/bin/env python3
"""
Автоматический подбор цен открытия для калькулятора (calculator.py).

Для выбранной фьючерсной биржи + монеты берёт цену открытия ШОРТА из API
самой биржи (это данные о текущей открытой позиции — есть только пока
позиция открыта), а затем ищет цену открытия СПОТОВОЙ ноги (покупка той же
монеты) в истории сделок по всем ПОДключённым биржам, у которых есть спот
(Bybit/MEXC/Gate — Aster и Lighter спота не имеют вовсе), в окне времени
вокруг момента открытия шорта. Если совпадение не нашлось ни на одной
бирже — значение остаётся None, и в UI это поле нужно заполнить вручную
(ровно то поведение, которое и было явно согласовано).

Точность времени открытия шорта различается по биржам:
  - Bybit, MEXC — биржа отдаёт ТОЧНОЕ время открытия позиции (createdTime/
    createTime), окно поиска спот-сделки узкое (см. WINDOW_MINUTES_EXACT).
  - Aster, Gate, Lighter — точного времени открытия в API позиций нет,
    время оценивается по непрерывности начислений funding (тот же приём,
    что и в funding_report._trim_to_continuous_run для /positions) —
    точность на уровне интервала funding (обычно 8ч), поэтому окно поиска
    шире (см. WINDOW_MINUTES_APPROX) и это явно помечается в ответе.
"""

import concurrent.futures
import hmac
import hashlib
import threading
import time
import urllib.parse
from collections import defaultdict

import requests

from funding_report import (
    _aster_sign,
    _bybit_sign,
    _gate_sign,
    _get_proxies,
    _get_mexc_proxies,
    _get_gate_proxies,
    _fetch_in_windows,
    _trim_to_continuous_run,
    _EXCHANGE_WINDOW_DAYS,
    OPEN_POSITIONS_LOOKBACK_DAYS,
    fetch_aster,
    fetch_gate,
    fetch_lighter,
)

WINDOW_MINUTES_EXACT = 20      # Bybit/MEXC — точное время открытия
WINDOW_MINUTES_APPROX = 360    # Aster/Gate/Lighter — время оценено по funding
# WETH — доп. котировка специально для Uniswap (там купленная монета часто в паре
# с WETH, а не со стейблом); для CEX-бирж такая пара просто не найдётся и молча
# пропустится (см. _search_spot_entry) — это не ошибка, ищем в порядке приоритета.
QUOTE_CANDIDATES = ["USDT", "USDC", "WETH"]


# ── Извлечение базового актива из формата символа конкретной биржи ───────────

_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "BUSD")


def _base_asset(exchange: str, symbol: str) -> str:
    if exchange in ("mexc", "gate") and "_" in symbol:
        return symbol.split("_")[0]
    if exchange == "lighter":
        return symbol  # у Lighter symbol уже "голый" базовый актив
    for suf in _QUOTE_SUFFIXES:
        if symbol.endswith(suf) and len(symbol) > len(suf):
            return symbol[: -len(suf)]
    return symbol


# ── Цена/время открытия ШОРТА по данным позиции на фьючерсной бирже ──────────

def _aster_position_entry(secrets: dict, symbol: str) -> dict | None:
    nonce = int(time.time() * 1_000_000)
    params = {
        "timestamp": str(int(time.time() * 1000)), "nonce": str(nonce),
        "user": secrets["user"], "signer": secrets["signer"],
    }
    param_str = urllib.parse.urlencode(params)
    sig = _aster_sign(param_str, secrets["signer_private_key"])
    url = f"https://fapi.asterdex.com/fapi/v3/positionRisk?{param_str}&signature={sig}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        raise RuntimeError(f"Aster positionRisk error: {data}")
    for p in data:
        if p.get("symbol") == symbol and abs(float(p.get("positionAmt", 0))) > 0:
            return {
                "price": float(p["entryPrice"]),
                "qty": abs(float(p["positionAmt"])),
                "entry_time_ms": None,
                "time_is_exact": False,
            }
    return None


def _bybit_position_entry(secrets: dict, symbol: str) -> dict | None:
    base_url = "https://api.bybit.com"
    recv_window = "5000"
    proxies = _get_proxies()
    api_key = secrets["bybit_api_key"].strip()
    api_secret = secrets["bybit_api_secret"].strip()
    timestamp = str(int(time.time() * 1000))
    params_list = [("category", "linear"), ("symbol", symbol)]
    query_string = urllib.parse.urlencode(params_list)
    sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, query_string)
    headers = {
        "X-BAPI-API-KEY": api_key, "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": recv_window,
    }
    resp = requests.get(f"{base_url}/v5/position/list?{query_string}", headers=headers, timeout=30, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit position/list error {data.get('retCode')}: {data.get('retMsg')}")
    for p in data.get("result", {}).get("list", []):
        if p.get("symbol") == symbol and float(p.get("size", 0)) > 0:
            return {
                "price": float(p["avgPrice"]),
                "qty": float(p["size"]),
                "entry_time_ms": int(p["createdTime"]) if p.get("createdTime") else None,
                "time_is_exact": True,
            }
    return None


def _mexc_position_entry(secrets: dict, symbol: str) -> dict | None:
    from funding_report import _mexc_sign  # приватная HMAC-подпись contract API
    base_url = "https://api.mexc.com"
    api_key = secrets["mexc_api_key"].strip()
    api_secret = secrets["mexc_api_secret"].strip()
    timestamp = str(int(time.time() * 1000))
    sig = _mexc_sign(api_key, api_secret, timestamp, [])
    headers = {"ApiKey": api_key, "Request-Time": timestamp, "Signature": sig}
    resp = requests.get(f"{base_url}/api/v1/private/position/open_positions", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"MEXC open_positions error {data.get('code')}: {data.get('message') or data}")
    for p in data.get("data") or []:
        if p.get("symbol") == symbol and float(p.get("holdVol", 0)) > 0:
            price = p.get("openAvgPrice") or p.get("holdAvgPrice")
            return {
                "price": float(price),
                "qty": float(p["holdVol"]),
                "entry_time_ms": int(p["createTime"]) if p.get("createTime") else None,
                "time_is_exact": True,
            }
    return None


def _gate_position_entry(secrets: dict, symbol: str, settle: str = "usdt") -> dict | None:
    base_url = "https://api.gateio.ws"
    url_path = f"/api/v4/futures/{settle}/positions"
    proxies = _get_gate_proxies()
    api_key = secrets["gate_api_key"].strip()
    api_secret = secrets["gate_api_secret"].strip()
    sig, timestamp = _gate_sign(api_secret, "GET", url_path, "")
    headers = {"KEY": api_key, "Timestamp": timestamp, "SIGN": sig, "Accept": "application/json"}
    resp = requests.get(f"{base_url}{url_path}", headers=headers, timeout=30, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("label"):
        raise RuntimeError(f"Gate positions error {data.get('label')}: {data.get('message')}")
    for p in data if isinstance(data, list) else []:
        if p.get("contract") == symbol and float(p.get("size", 0)) != 0:
            return {
                "price": float(p["entry_price"]),
                "qty": abs(float(p["size"])),
                "entry_time_ms": None,
                "time_is_exact": False,
            }
    return None


def _lighter_position_entry(secrets: dict, symbol: str) -> dict | None:
    """
    Формат полей позиции у Lighter публично не задокументирован до конца —
    как и в funding_report.fetch_lighter_open_symbols, перебираем несколько
    вероятных вариантов имени поля с ценой входа. Если ни одно не подошло —
    возвращаем позицию без цены (qty при этом может быть известен), калькулятор
    в этом случае просто оставит поле цены фьючерса пустым для ручного ввода.
    """
    from funding_report import fetch_lighter_markets

    markets = fetch_lighter_markets()
    headers = {"authorization": secrets["lighter_auth_token"].strip()}
    params = {"by": "index", "value": secrets["lighter_account_index"], "active_only": "true"}
    resp = requests.get(
        "https://mainnet.zklighter.elliot.ai/api/v1/account",
        params=params, headers=headers, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 200) != 200:
        raise RuntimeError(f"Lighter account error: {data}")

    accounts = data.get("accounts", [data]) if "accounts" not in data else data["accounts"]
    for acc in accounts:
        for pos in acc.get("positions", []):
            market_id = pos.get("market_id", pos.get("market_index"))
            sym = markets.get(market_id, f"MARKET_{market_id}")
            if sym != symbol:
                continue
            size = float(pos.get("position", pos.get("size", pos.get("position_size", 0))) or 0)
            if size == 0:
                continue
            price = None
            for key in ("avg_entry_price", "entry_price", "avgEntryPrice", "entryPrice"):
                if pos.get(key) not in (None, ""):
                    price = float(pos[key])
                    break
            return {
                "price": price,
                "qty": abs(size),
                "entry_time_ms": None,
                "time_is_exact": False,
            }
    return None


_POSITION_ENTRY_FETCHERS = {
    "aster": _aster_position_entry,
    "bybit": _bybit_position_entry,
    "mexc": _mexc_position_entry,
    "gate": _gate_position_entry,
    "lighter": _lighter_position_entry,
}


# ── Оценка времени открытия по непрерывности funding (когда точного нет) ─────

def _infer_open_time_via_funding(secrets: dict, exchange: str, symbol: str) -> int | None:
    now_ms = int(time.time() * 1000)
    lookback_start_ms = now_ms - OPEN_POSITIONS_LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    window_days = _EXCHANGE_WINDOW_DAYS.get(exchange, 7)

    if exchange == "aster":
        fn = lambda s, e: fetch_aster(secrets["user"], secrets["signer"], secrets["signer_private_key"], s, e)
        time_field, time_unit = "time", "ms"
    elif exchange == "gate":
        fn = lambda s, e: fetch_gate(secrets["gate_api_key"], secrets["gate_api_secret"], s, e)
        time_field, time_unit = "time", "s"
    elif exchange == "lighter":
        fn = lambda s, e: fetch_lighter(secrets["lighter_account_index"], secrets["lighter_auth_token"], s, e)
        time_field, time_unit = "timestamp", "s"
    else:
        return None

    records = _fetch_in_windows(fn, window_days, lookback_start_ms, now_ms)
    records = [r for r in records if r.get("symbol") == symbol]
    trimmed = _trim_to_continuous_run(records, time_field=time_field, time_unit=time_unit)
    if not trimmed:
        return None

    times_ms = []
    for r in trimmed:
        raw = r.get(time_field)
        if raw is None:
            continue
        t = int(float(raw))
        times_ms.append(t * 1000 if time_unit == "s" else t)
    return min(times_ms) if times_ms else None


# ── Поиск цены покупки на споте в истории сделок ──────────────────────────────

def _fetch_bybit_spot_trades(secrets: dict, symbol: str, start_ms: int, end_ms: int) -> list:
    base_url = "https://api.bybit.com"
    recv_window = "5000"
    proxies = _get_proxies()
    api_key = secrets["bybit_api_key"].strip()
    api_secret = secrets["bybit_api_secret"].strip()
    timestamp = str(int(time.time() * 1000))
    params_list = [
        ("category", "spot"), ("symbol", symbol),
        ("startTime", str(start_ms)), ("endTime", str(end_ms)), ("limit", "100"),
    ]
    query_string = urllib.parse.urlencode(params_list)
    sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, query_string)
    headers = {
        "X-BAPI-API-KEY": api_key, "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": recv_window,
    }
    resp = requests.get(f"{base_url}/v5/execution/list?{query_string}", headers=headers, timeout=30, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit execution/list error {data.get('retCode')}: {data.get('retMsg')}")
    items = data.get("result", {}).get("list", [])
    return [
        {"price": float(i["execPrice"]), "qty": float(i["execQty"]), "side": i.get("side")}
        for i in items if i.get("side") == "Buy"
    ]


def _mexc_spot_sign(api_secret: str, query_string: str) -> str:
    return hmac.new(api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()


def _fetch_mexc_spot_trades(secrets: dict, symbol: str, start_ms: int, end_ms: int) -> list:
    """
    Спот-API MEXC — отдельный от контрактного, со своей (Binance-совместимой)
    схемой подписи: HMAC-SHA256 от query-строки запроса секретным ключом,
    подпись добавляется в неё же как параметр signature. API-ключ — в заголовке
    X-MEXC-APIKEY (см. https://mexcdevelop.github.io/apidocs/spot_v3_en/).
    """
    api_key = secrets["mexc_api_key"].strip()
    api_secret = secrets["mexc_api_secret"].strip()
    params_list = [
        ("symbol", symbol), ("startTime", str(start_ms)), ("endTime", str(end_ms)),
        ("limit", "1000"), ("recvWindow", "10000"), ("timestamp", str(int(time.time() * 1000))),
    ]
    query_string = urllib.parse.urlencode(params_list)
    sig = _mexc_spot_sign(api_secret, query_string)
    headers = {"X-MEXC-APIKEY": api_key}
    resp = requests.get(
        f"https://api.mexc.com/api/v3/myTrades?{query_string}&signature={sig}",
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("code"):
        raise RuntimeError(f"MEXC myTrades error {data.get('code')}: {data.get('msg')}")
    return [
        {"price": float(t["price"]), "qty": float(t["qty"]), "side": "Buy" if t.get("isBuyer") else "Sell"}
        for t in data if t.get("isBuyer")
    ]


def _fetch_gate_spot_trades(secrets: dict, symbol: str, start_ms: int, end_ms: int) -> list:
    base_url = "https://api.gateio.ws"
    url_path = "/api/v4/spot/my_trades"
    proxies = _get_gate_proxies()
    api_key = secrets["gate_api_key"].strip()
    api_secret = secrets["gate_api_secret"].strip()
    params_list = [
        ("currency_pair", symbol), ("from", str(start_ms // 1000)),
        ("to", str(end_ms // 1000)), ("limit", "100"),
    ]
    query_string = urllib.parse.urlencode(params_list)
    sig, timestamp = _gate_sign(api_secret, "GET", url_path, query_string)
    headers = {"KEY": api_key, "Timestamp": timestamp, "SIGN": sig, "Accept": "application/json"}
    resp = requests.get(f"{base_url}{url_path}?{query_string}", headers=headers, timeout=30, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("label"):
        raise RuntimeError(f"Gate my_trades error {data.get('label')}: {data.get('message')}")
    return [
        {"price": float(t["price"]), "qty": float(t["amount"]), "side": t.get("side")}
        for t in (data if isinstance(data, list) else []) if t.get("side") == "buy"
    ]


# ── Uniswap (любая EVM-сеть) — поиск покупки через историю переводов кошелька ─
#
# У Uniswap/блокчейна нет REST-эндпоинта "история моих сделок по паре", как у
# Bybit/MEXC/Gate — вместо этого ищем сами по истории ERC-20-переводов кошелька:
# своп-покупка на Uniswap — это одна транзакция, в которой есть ОДНОВРЕМЕННО
# исходящий перевод quote-актива (продали USDC/USDT/WETH) и входящий перевод
# base-актива (получили купленную монету) на/с адреса самого кошелька. Что там
# происходило внутри (через какие пулы/хопы прошёл своп) — не важно, это видно
# по крайним звеньям цепочки, где стороной выступает сам кошелёк.
#
# Сеть заранее не фиксируем: пользователь торгует с одного кошелька на разных
# EVM-сетях, поэтому вместо хардкода конкретных сетей опрашиваем ВСЕ майннеты,
# которые поддерживает единый Etherscan V2 API (один и тот же ключ работает на
# всех — см. https://docs.etherscan.io/v2-migration), список сетей берём
# динамически из /v2/chainlist, чтобы не отставать от Etherscan вручную.

ETHERSCAN_BASE_URL = "https://api.etherscan.io/v2/api"
ETHERSCAN_CHAINLIST_URL = "https://api.etherscan.io/v2/chainlist"
# По этим словам в названии сети отсеиваем тестnetы — chainlist отдаёт их вперемешку
# с майннетами, а тестовые переводы для калькулятора не нужны и только тратят лимит.
_TESTNET_NAME_KEYWORDS = ("testnet", "sepolia", "goerli", "holesky", "devnet", "fuji", "mumbai", "chapel")

_evm_chainlist_cache: list = []
_evm_chainlist_cache_ts: float = 0.0
EVM_CHAINLIST_CACHE_TTL_S = 24 * 60 * 60  # список сетей почти не меняется — кэш на сутки
# Сети, которые уже ответили "не поддерживается на бесплатном тарифе" или
# "дневной лимит исчерпан" — запоминаем и больше НЕ дёргаем до следующего
# обновления кэша сетей (см. _fetch_evm_chainlist), а не долбим их впустую
# на каждый следующий поиск цены. Сбрасывается вместе с кэшем сетей — вдруг
# тариф апгрейднули или дневной лимит уже сбросился.
_evm_blocked_chains: set = set()
_UNSUPPORTED_CHAIN_MSG_KEYWORDS = ("not supported for this chain", "api limit reached")

_etherscan_rate_lock = threading.Lock()
_etherscan_last_call_ts = [0.0]
# Реальный лимит бесплатного тарифа Etherscan — 3 запроса/сек на ключ (НЕ 5, как
# можно было бы подумать по документации) — проверено на практике 24.08.2026: с
# интервалом 0.22с (~4.5/сек) регулярно ловили "Max calls per sec rate limit
# reached (3/сек)". Берём интервал с запасом ниже реального лимита.
ETHERSCAN_MIN_INTERVAL_S = 0.4


def _etherscan_get(params: dict, api_key: str) -> dict:
    with _etherscan_rate_lock:
        wait = ETHERSCAN_MIN_INTERVAL_S - (time.time() - _etherscan_last_call_ts[0])
        if wait > 0:
            time.sleep(wait)
        _etherscan_last_call_ts[0] = time.time()

    resp = requests.get(ETHERSCAN_BASE_URL, params={**params, "apikey": api_key}, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _fetch_evm_chainlist(api_key: str) -> list:
    """[(chain_id, chain_name), ...] — все майннет-сети, которые прямо сейчас
    поддерживает единый Etherscan V2 API. Кэшируется на сутки в памяти процесса.

    ВАЖНО: у /v2/chainlist другой формат ответа, чем у остальных Etherscan-
    эндпоинтов (module=.../action=...) — тут НЕТ обёртки status/message,
    сразу {"comments": ..., "totalcount": N, "result": [...]}. Проверено
    вручную реальным запросом 24.08.2026 — не полагаемся на документацию,
    которая этот нюанс не описывала. Статус конкретной сети внутри result[]
    (число, не строка) — 0=Offline/1=Ok/2=Degraded согласно "comments" в
    самом ответе; пропускаем только офлайновые (0), Degraded всё ещё
    опрашиваем — деградация обычно означает задержку индексации, а не
    недоступность API."""
    global _evm_chainlist_cache, _evm_chainlist_cache_ts, _evm_blocked_chains
    now = time.time()
    if _evm_chainlist_cache and now - _evm_chainlist_cache_ts < EVM_CHAINLIST_CACHE_TTL_S:
        return _evm_chainlist_cache

    resp = requests.get(ETHERSCAN_CHAINLIST_URL, params={"apikey": api_key}, timeout=20)
    resp.raise_for_status()
    data = resp.json()
    result = data.get("result")
    if not isinstance(result, list):
        raise RuntimeError(f"Etherscan chainlist: неожиданный формат ответа: {data}")

    chains = []
    for c in result:
        name = str(c.get("chainname", ""))
        try:
            chain_id = int(c.get("chainid"))
        except (TypeError, ValueError):
            continue
        try:
            chain_status = int(c.get("status", 1) or 0)
        except (TypeError, ValueError):
            chain_status = 1
        if chain_status == 0:
            continue
        if any(kw in name.lower() for kw in _TESTNET_NAME_KEYWORDS):
            continue
        chains.append((chain_id, name))

    if not chains:
        raise RuntimeError(f"Etherscan chainlist вернул пустой список сетей: {data}")

    _evm_chainlist_cache = chains
    _evm_chainlist_cache_ts = now
    _evm_blocked_chains = set()
    return chains


def _fetch_uniswap_chain_transfers(chain_id: int, api_key: str, wallet: str) -> list:
    """До 1000 последних ERC-20-переводов кошелька на конкретной сети, без
    диапазона блоков — фильтруем по времени уже на своей стороне. Это на один
    запрос дешевле на КАЖДУЮ из ~50+ сетей, чем сначала переводить время в номер
    блока отдельным вызовом; для обычного кошелька 1000 последних переводов с
    запасом покрывают любое разумное окно поиска (если у кошелька на конкретной
    сети активность настолько высокая, что нужная сделка вышла за пределы
    последних 1000 переводов — она не найдётся, это осознанный компромисс ради
    скорости при опросе полусотни сетей разом)."""
    data = _etherscan_get({
        "module": "account", "action": "tokentx", "chainid": chain_id,
        "address": wallet, "sort": "desc", "offset": "1000", "page": "1",
    }, api_key)
    status = str(data.get("status", ""))
    message = str(data.get("message", ""))
    if status != "1" and message != "No transactions found":
        raise RuntimeError(f"Etherscan tokentx error (chain {chain_id}): {message} {data.get('result')}")
    result = data.get("result")
    return result if isinstance(result, list) else []


UNISWAP_QUOTE_CANDIDATES = ("USDT", "USDC", "WETH")


def _fetch_uniswap_spot_trades_all_quotes(secrets: dict, base_asset: str, start_ms: int, end_ms: int) -> dict:
    """{quote_asset: [trades...]} — ОДИН проход по всем EVM-сетям сразу, с
    проверкой всех кандидатов котировки (USDT/USDC/WETH) внутри каждой найденной
    транзакции. Раньше (до 24.08.2026) каждая сеть сканировалась ОТДЕЛЬНО под
    каждую quote-кандидатуру из общего цикла в _search_spot_entry — то есть одни
    и те же ~60 сетей опрашивались трижды на один поиск цены. Совершенно лишнее:
    одна и та же выборка из 1000 последних переводов кошелька уже содержит
    переводы во всех котируемых активах сразу, поэтому сканируем один раз и
    сортируем результат по тому, какая котировка нашлась."""
    wallet = secrets.get("uniswap_wallet_address")
    api_key = secrets.get("etherscan_api_key")
    if not wallet or not api_key:
        return {}
    wallet_lower = wallet.lower()
    start_s, end_s = start_ms // 1000, end_ms // 1000

    try:
        chains = _fetch_evm_chainlist(api_key)
    except Exception as e:
        print(f"[entry_price/uniswap] Не удалось получить список EVM-сетей: {e}")
        return {}

    def _scan_chain(chain_id: int, chain_name: str) -> list:
        if chain_id in _evm_blocked_chains:
            return []  # уже знаем, что сеть недоступна на текущем тарифе/лимите — не дёргаем зря
        try:
            transfers = _fetch_uniswap_chain_transfers(chain_id, api_key, wallet)
        except Exception as e:
            msg = str(e).lower()
            if any(kw in msg for kw in _UNSUPPORTED_CHAIN_MSG_KEYWORDS):
                # Ограничение тарифа Etherscan, не наша ошибка и не временный сбой —
                # запоминаем сеть и больше не опрашиваем её до обновления кэша сетей.
                _evm_blocked_chains.add(chain_id)
                print(f"[entry_price/uniswap/{chain_name}] Сеть недоступна на текущем тарифе Etherscan — больше не опрашивается в этом кэш-цикле ({e})")
            else:
                # "Max calls per sec" и подобное — транзиентное, оставляем на
                # следующую попытку без пометки как заблокированной сети.
                print(f"[entry_price/uniswap/{chain_name}] Ошибка запроса переводов: {e}")
            return []

        by_hash: dict = defaultdict(list)
        for t in transfers:
            ts = int(t.get("timeStamp", 0) or 0)
            if start_s <= ts <= end_s:
                by_hash[t.get("hash")].append(t)

        found = []
        for group in by_hash.values():
            base_in = None
            quote_out_by_symbol: dict = {}
            for t in group:
                sym = (t.get("tokenSymbol") or "").upper()
                try:
                    decimals = int(t.get("tokenDecimal", 18) or 18)
                    amount = int(t.get("value", "0")) / (10 ** decimals)
                except (TypeError, ValueError):
                    continue
                if sym == base_asset.upper() and (t.get("to") or "").lower() == wallet_lower:
                    base_in = amount
                elif sym in UNISWAP_QUOTE_CANDIDATES and (t.get("from") or "").lower() == wallet_lower:
                    quote_out_by_symbol[sym] = amount
            if not base_in or base_in <= 0:
                continue
            for quote_sym, quote_out in quote_out_by_symbol.items():
                found.append({
                    "quote": quote_sym, "price": quote_out / base_in,
                    "qty": base_in, "side": "Buy", "chain": chain_name,
                })
        return found

    all_trades = []
    # Сети опрашиваем параллельно потоками (I/O-bound — сеть, не CPU), но реальная
    # скорость всё равно упирается в общий rate-limit из _etherscan_get: пул тут
    # просто даёт запросам не ждать друг друга впустую на сетевых задержках.
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_scan_chain, cid, cname) for cid, cname in chains]
        for f in concurrent.futures.as_completed(futures):
            all_trades.extend(f.result())

    by_quote: dict = defaultdict(list)
    for t in all_trades:
        by_quote[t["quote"]].append(t)
    return dict(by_quote)


_SPOT_TRADE_FETCHERS = {
    "bybit": (lambda secrets, symbol, s, e: _fetch_bybit_spot_trades(secrets, symbol, s, e), lambda base, quote: f"{base}{quote}"),
    "mexc":  (lambda secrets, symbol, s, e: _fetch_mexc_spot_trades(secrets, symbol, s, e), lambda base, quote: f"{base}{quote}"),
    "gate":  (lambda secrets, symbol, s, e: _fetch_gate_spot_trades(secrets, symbol, s, e), lambda base, quote: f"{base}_{quote}"),
    # Uniswap сюда намеренно не входит — сканируется отдельно ОДИН раз на все
    # котировки сразу через _fetch_uniswap_spot_trades_all_quotes (см.
    # _search_spot_entry), а не через этот реестр из (fetch_fn, symbol_fn) на
    # каждую котировку, как у CEX-бирж.
}

_SPOT_EXCHANGE_SECRET_KEY = {
    "bybit": "bybit_api_key",
    "mexc": "mexc_api_key",
    "gate": "gate_api_key",
}


def _search_spot_entry(secrets: dict, base_asset: str, center_ms: int, window_minutes: int) -> dict | None:
    start_ms = center_ms - window_minutes * 60 * 1000
    end_ms = center_ms + window_minutes * 60 * 1000

    # Uniswap сканируется ОДИН раз на все котировки сразу (а не по разу на
    # каждую котировку внутри цикла ниже, как CEX-биржи) — иначе на каждый
    # поиск цены заново прогоняются все ~60 EVM-сетей по три раза подряд.
    uniswap_by_quote: dict = {}
    if "uniswap_wallet_address" in secrets:
        try:
            uniswap_by_quote = _fetch_uniswap_spot_trades_all_quotes(secrets, base_asset, start_ms, end_ms)
        except Exception as e:
            print(f"[entry_price/uniswap] Ошибка поиска спот-сделок: {e}")

    for quote in QUOTE_CANDIDATES:
        matches = []  # (exchange, [trades])
        for exchange, (fetch_fn, symbol_fn) in _SPOT_TRADE_FETCHERS.items():
            key = _SPOT_EXCHANGE_SECRET_KEY.get(exchange)
            if not key or key not in secrets:
                continue
            spot_symbol = symbol_fn(base_asset, quote)
            try:
                trades = fetch_fn(secrets, spot_symbol, start_ms, end_ms)
            except Exception as e:
                print(f"[entry_price/{exchange}] Ошибка поиска спот-сделок {spot_symbol}: {e}")
                continue
            if trades:
                matches.append((exchange, trades))

        uniswap_trades = uniswap_by_quote.get(quote)
        if uniswap_trades:
            matches.append(("uniswap", uniswap_trades))

        if matches:
            total_qty = sum(t["qty"] for _, trades in matches for t in trades)
            total_notional = sum(t["qty"] * t["price"] for _, trades in matches for t in trades)
            if total_qty <= 0:
                continue
            vwap = total_notional / total_qty
            exchanges_used = sorted({ex for ex, _ in matches})
            trade_count = sum(len(trades) for _, trades in matches)
            return {
                "price": vwap,
                "quote_asset": quote,
                "exchanges": exchanges_used,
                "trade_count": trade_count,
            }

    return None


# ── Итоговая функция для калькулятора ─────────────────────────────────────────

def get_entry_price_suggestion(secrets: dict, exchange: str, symbol: str) -> dict:
    """
    {
      "qty": float | None,
      "futures_entry_price": float | None,
      "futures_price_source": "position" | "unavailable",
      "spot_entry_price": float | None,
      "spot_price_exchanges": [str] | None,
      "spot_price_trade_count": int | None,
      "entry_time_ms": int | None,
      "entry_time_is_exact": bool,
      "notes": [str],
    }
    Ничего не кидает наружу при отсутствии данных — только заполняет notes.
    """
    notes: list[str] = []
    fetcher = _POSITION_ENTRY_FETCHERS.get(exchange)
    if fetcher is None:
        raise ValueError(f"Неизвестная биржа: {exchange}")

    try:
        pos = fetcher(secrets, symbol)
    except Exception as e:
        notes.append(f"Не удалось получить данные позиции с биржи: {e}")
        pos = None

    if pos is None:
        notes.append("Позиция с таким символом сейчас не открыта на бирже — авто-подбор цен недоступен, заполните вручную.")
        return {
            "qty": None, "futures_entry_price": None, "futures_price_source": "unavailable",
            "spot_entry_price": None, "spot_price_exchanges": None, "spot_price_trade_count": None,
            "entry_time_ms": None, "entry_time_is_exact": False, "notes": notes,
        }

    entry_time_ms = pos.get("entry_time_ms")
    time_is_exact = bool(pos.get("time_is_exact"))
    if entry_time_ms is None:
        try:
            entry_time_ms = _infer_open_time_via_funding(secrets, exchange, symbol)
        except Exception as e:
            notes.append(f"Не удалось оценить время открытия по истории funding: {e}")
        if entry_time_ms is not None:
            notes.append("Время открытия оценено приблизительно по непрерывности funding (не точное) — окно поиска спот-сделки расширено.")
        else:
            notes.append("Не удалось определить даже приблизительное время открытия — спот-цену придётся ввести вручную.")

    if pos.get("price") is None:
        notes.append("Биржа не отдала цену входа фьючерса в ожидаемом формате — заполните вручную.")

    spot_result = None
    if entry_time_ms is not None:
        window = WINDOW_MINUTES_EXACT if time_is_exact else WINDOW_MINUTES_APPROX
        base_asset = _base_asset(exchange, symbol)
        try:
            spot_result = _search_spot_entry(secrets, base_asset, entry_time_ms, window)
        except Exception as e:
            notes.append(f"Ошибка поиска спот-сделки: {e}")
        if spot_result is None:
            notes.append(
                f"Покупка {base_asset} на споте не найдена ни на одной подключённой спот-бирже "
                f"в окне ±{window} мин вокруг открытия шорта — введите цену спота вручную."
            )

    return {
        "qty": pos.get("qty"),
        "futures_entry_price": pos.get("price"),
        "futures_price_source": "position" if pos.get("price") is not None else "unavailable",
        "spot_entry_price": spot_result["price"] if spot_result else None,
        "spot_price_exchanges": spot_result["exchanges"] if spot_result else None,
        "spot_price_trade_count": spot_result["trade_count"] if spot_result else None,
        "entry_time_ms": entry_time_ms,
        "entry_time_is_exact": time_is_exact,
        "notes": notes,
    }
