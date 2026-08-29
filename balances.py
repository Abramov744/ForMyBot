#!/usr/bin/env python3
"""
Сводный баланс по всем биржам/кошелькам/DeFi-протоколам — используется
веб-страницей /balances и командой /balance в Telegram-боте.

Отличие от funding_report.py: там — история funding и открытые ПОЗИЦИИ на
фьючерсах (нужные для самой стратегии), здесь — сколько денег СЕЙЧАС лежит
на каждом источнике целиком, включая споt-баланс (туда обычно уходит долгая
часть капитала — лонг-хедж) и DeFi-позиции, не имеющие отношения к funding.

Источники и как берётся баланс:
  - Bybit  — Unified Trading Account, totalEquity (споt и деривативы уже
    объединены биржей в один баланс, в USD).
  - MEXC   — отдельно фьючерсный (тот же API, что и funding_report.fetch_mexc)
    и спот-баланс (другая подпись запроса, см. fetch_mexc_spot_balance).
  - Gate   — отдельно фьючерсный и спот-баланс, обе подписи запросов — тот же
    HMAC-SHA512-механизм, что уже используется в funding_report._gate_sign.
  - Aster  — фьючерсный баланс (споta у Aster нет вовсе, см. README).
  - Lighter — баланс на счету (споta нет, только перпетуалы).
  - Rabby wallet + Aave + Fluid + любой другой DeFi-протокол на кошельке —
    через DeBank Open API (https://pro-openapi.debank.com), платно
    (AccessKey с cloud.debank.com). Решили так вместо on-chain-запросов
    напрямую (было в первой версии этого файла, см. git-историю) — DeBank
    сам знает про Fluid и любой другой протокол на кошельке, тогда как
    on-chain-вариант требовал бы для каждого протокола отдельно разбирать
    его ABI (для Fluid — сложные вложенные структуры, недокументированные
    до конца даже в официальной доке, см. обсуждение в PR).

Оценка стоимости в USD для спот-балансов бирж (не для кошелька — тут её уже
считает сам DeBank) берётся с бесплатного публичного CoinGecko API (без
ключа, с кэшем на 5 минут); ошибки/лимиты этого API НЕ должны ронять
остальную часть отчёта — то, что не удалось оценить, просто показывается
без цены, а не исключается молча и не оценивается наугад.
"""

import hashlib
import hmac
import os
import time
import urllib.parse

import requests

from funding_report import (
    LIGHTER_BASE_URL,
    _aster_sign,
    _bybit_sign,
    _gate_sign,
    _get_gate_proxies,
    _get_mexc_proxies,
    _get_proxies,
    _mexc_sign,
)


# ── Цены в USD (CoinGecko, бесплатный публичный API без ключа) ────────────────

COINGECKO_BASE_URL = "https://api.coingecko.com/api/v3"

# Стейблкоины считаем по номиналу без обращения к CoinGecko вообще — так
# надёжнее (бесплатный API не всегда знает контракт конкретного бриджнутого
# варианта USDT/USDC на менее популярной сети) и не тратит скудный лимит
# бесплатного тарифа на заведомый результат.
STABLECOIN_SYMBOLS = {"USDT", "USDC", "USDE", "FDUSD", "DAI", "TUSD", "BUSD", "USDD", "PYUSD", "USD1"}

# Символ спот-баланса биржи → id монеты в CoinGecko. Только самые ходовые
# монеты — то, что реально может лежать в споте у этого бота (долгосрочный
# лонг-хедж под шорт на фьючерсах). Монета не из списка и не стейблкоин —
# просто показывается в отчёте без оценки в USD, а не угадывается.
SYMBOL_TO_COINGECKO_ID = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana", "BNB": "binancecoin",
    "XRP": "ripple", "DOGE": "dogecoin", "ADA": "cardano", "AVAX": "avalanche-2",
    "LINK": "chainlink", "DOT": "polkadot", "TRX": "tron", "LTC": "litecoin",
    "ARB": "arbitrum", "OP": "optimism", "SUI": "sui", "TON": "the-open-network",
    "NEAR": "near", "ATOM": "cosmos", "APT": "aptos", "FIL": "filecoin",
    "INJ": "injective-protocol", "WLD": "worldcoin-wld", "PEPE": "pepe", "SHIB": "shiba-inu",
}

_coingecko_cache: dict = {}          # cache_key -> {id_or_addr_lower: price_usd}
_coingecko_cache_ts: dict = {}       # cache_key -> время последнего успешного запроса
COINGECKO_CACHE_TTL_S = 300          # баланс не нужно оценивать точнее раза в 5 минут


def _coingecko_get(path: str, params: dict) -> dict | None:
    try:
        resp = requests.get(f"{COINGECKO_BASE_URL}{path}", params=params, timeout=20)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"[balances/coingecko] Ошибка запроса {path}: {e}")
        return None


def _coingecko_simple_prices(ids: set) -> dict:
    """{coingecko_id: price_usd}. Кэш на COINGECKO_CACHE_TTL_S, один батч-запрос
    на все id сразу. При ошибке/лимите бесплатного API — просто возвращает то,
    что было в кэше (может быть пусто), без исключения."""
    if not ids:
        return {}
    cache_key = "simple:" + ",".join(sorted(ids))
    now = time.time()
    if cache_key in _coingecko_cache and now - _coingecko_cache_ts[cache_key] < COINGECKO_CACHE_TTL_S:
        return _coingecko_cache[cache_key]

    data = _coingecko_get("/simple/price", {"ids": ",".join(sorted(ids)), "vs_currencies": "usd"})
    prices = {cid: float(v["usd"]) for cid, v in (data or {}).items() if "usd" in v}
    if prices:
        _coingecko_cache[cache_key] = prices
        _coingecko_cache_ts[cache_key] = now
        return prices
    return _coingecko_cache.get(cache_key, {})


# ── Биржи ──────────────────────────────────────────────────────────────────────

def fetch_bybit_balance(api_key: str, api_secret: str) -> float:
    """GET /v5/account/wallet-balance, accountType=UNIFIED → totalEquity.
    Unified Trading Account у Bybit уже объединяет спот и деривативы в один
    баланс, поэтому это единственное число покрывает обе ноги сразу."""
    base_url = "https://api.bybit.com"
    recv_window = "5000"
    proxies = _get_proxies()
    api_key, api_secret = api_key.strip(), api_secret.strip()

    timestamp = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode([("accountType", "UNIFIED")])
    sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, query_string)
    headers = {
        "X-BAPI-API-KEY": api_key, "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": recv_window,
    }
    resp = requests.get(
        f"{base_url}/v5/account/wallet-balance?{query_string}",
        headers=headers, timeout=30, proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit wallet-balance error {data.get('retCode')}: {data.get('retMsg')}")
    lst = data.get("result", {}).get("list", [])
    return float(lst[0].get("totalEquity", 0) or 0) if lst else 0.0


def fetch_mexc_futures_balance(api_key: str, api_secret: str) -> float:
    """GET /api/v1/private/account/assets — тот же контрактный (фьючерсный)
    API и та же подпись, что и funding_report.fetch_mexc. Суммирует поле
    equity по всем валютам счёта (аккаунт USDT-маржинальный, см. README, —
    практически всегда будет только запись USDT)."""
    base_url = "https://api.mexc.com"
    api_key, api_secret = api_key.strip(), api_secret.strip()
    timestamp = str(int(time.time() * 1000))
    sig = _mexc_sign(api_key, api_secret, timestamp, [])
    headers = {"ApiKey": api_key, "Request-Time": timestamp, "Signature": sig}

    resp = requests.get(
        f"{base_url}/api/v1/private/account/assets",
        headers=headers, timeout=30, proxies=_get_mexc_proxies(),
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"MEXC account/assets error {data.get('code')}: {data.get('message') or data}")
    return sum(float(a.get("equity", 0) or 0) for a in (data.get("data") or []))


def fetch_mexc_spot_balance(api_key: str, api_secret: str) -> dict:
    """
    GET /api/v3/account — СПОТ-API MEXC, у него ДРУГАЯ схема подписи, чем у
    фьючерсного (см. funding_report._mexc_sign и комментарий там же про
    разницу схем): query_string подписывается как есть (timestamp+recvWindow,
    без сортировки параметров по алфавиту), signature = HMAC-SHA256(secret,
    query_string), передаётся в query, ключ — в заголовке X-MEXC-APIKEY.

    НЕ ПРОВЕРЕНО на реальном аккаунте (в отличие от остальных функций этого
    файла, которые переиспользуют уже боевые схемы подписи из
    funding_report.py) — перед тем как полагаться на число, сверьте с
    балансом в приложении MEXC хотя бы один раз.

    Возвращает {ASSET: free+locked} по всем ненулевым балансам.
    """
    base_url = "https://api.mexc.com"
    api_key, api_secret = api_key.strip(), api_secret.strip()
    timestamp = str(int(time.time() * 1000))
    query_string = f"timestamp={timestamp}&recvWindow=5000"
    sig = hmac.new(api_secret.encode("utf-8"), query_string.encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {"X-MEXC-APIKEY": api_key}

    resp = requests.get(
        f"{base_url}/api/v3/account?{query_string}&signature={sig}",
        headers=headers, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if "balances" not in data:
        raise RuntimeError(f"MEXC spot account error: {data}")

    out = {}
    for b in data["balances"]:
        amount = float(b.get("free", 0) or 0) + float(b.get("locked", 0) or 0)
        if amount > 0:
            out[b["asset"]] = amount
    return out


def fetch_gate_futures_balance(api_key: str, api_secret: str, settle: str = "usdt") -> float:
    """GET /api/v4/futures/{settle}/accounts → поле total. Та же подпись
    (funding_report._gate_sign), что и остальные Gate-запросы."""
    base_url = "https://api.gateio.ws"
    url_path = f"/api/v4/futures/{settle}/accounts"
    api_key, api_secret = api_key.strip(), api_secret.strip()

    sig, timestamp = _gate_sign(api_secret, "GET", url_path, "")
    headers = {"KEY": api_key, "Timestamp": timestamp, "SIGN": sig, "Accept": "application/json"}

    resp = requests.get(f"{base_url}{url_path}", headers=headers, timeout=30, proxies=_get_gate_proxies())
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("label"):
        raise RuntimeError(f"Gate futures accounts error {data.get('label')}: {data.get('message')}")
    return float(data.get("total", 0) or 0)


def fetch_gate_spot_balance(api_key: str, api_secret: str) -> dict:
    """GET /api/v4/spot/accounts — та же подпись, что и futures/accounts
    (Gate API v4 подписывает GET без тела одинаково для всех продуктов).
    Возвращает {CURRENCY: available+locked} по ненулевым балансам."""
    base_url = "https://api.gateio.ws"
    url_path = "/api/v4/spot/accounts"
    api_key, api_secret = api_key.strip(), api_secret.strip()

    sig, timestamp = _gate_sign(api_secret, "GET", url_path, "")
    headers = {"KEY": api_key, "Timestamp": timestamp, "SIGN": sig, "Accept": "application/json"}

    resp = requests.get(f"{base_url}{url_path}", headers=headers, timeout=30, proxies=_get_gate_proxies())
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("label"):
        raise RuntimeError(f"Gate spot accounts error {data.get('label')}: {data.get('message')}")

    out = {}
    for a in (data if isinstance(data, list) else []):
        amount = float(a.get("available", 0) or 0) + float(a.get("locked", 0) or 0)
        if amount > 0:
            out[a["currency"]] = amount
    return out


def fetch_aster_balance(user: str, signer: str, private_key: str) -> float:
    """GET /fapi/v3/balance — тот же стиль запроса (nonce+подпись через
    funding_report._aster_sign), что и остальные Aster-эндпоинты; версия
    пути (v3) взята той же, что уже подтверждена рабочей для /fapi/v3/income
    и /fapi/v3/positionRisk у этой биржи (форк API Binance Futures). Спота у
    Aster нет вовсе (см. README) — это весь баланс аккаунта."""
    nonce = int(time.time() * 1_000_000)
    params = {
        "timestamp": str(int(time.time() * 1000)),
        "nonce": str(nonce),
        "user": user,
        "signer": signer,
    }
    param_str = urllib.parse.urlencode(params)
    sig = _aster_sign(param_str, private_key)
    url = f"https://fapi.asterdex.com/fapi/v3/balance?{param_str}&signature={sig}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        raise RuntimeError(f"Aster balance error: {data}")
    return sum(float(a.get("balance", 0) or 0) for a in data if a.get("asset") == "USDT")


def fetch_lighter_balance(account_index: str) -> float:
    """
    GET /api/v1/account?by=index&value={account_index} — тот же эндпоинт,
    что и funding_report.fetch_lighter_open_symbols использует для списка
    позиций; здесь читаем из ТОГО ЖЕ ответа общую стоимость счёта.

    НЕ ПРОВЕРЕНО на реальном аккаунте: имя поля с итоговой стоимостью счёта
    в документации Lighter не описано полностью (см. тот же комментарий в
    funding_report.py), поэтому перебираются несколько вероятных вариантов
    ключа — сверьте результат с приложением Lighter, прежде чем полагаться
    на число.
    """
    resp = requests.get(
        f"{LIGHTER_BASE_URL}/api/v1/account",
        params={"by": "index", "value": account_index}, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 200) != 200:
        raise RuntimeError(f"Lighter account error: {data}")

    accounts = data.get("accounts", [data]) if "accounts" not in data else data["accounts"]
    for acc in accounts:
        for key in ("collateral", "portfolio_value", "account_value", "total_asset_value", "available_balance"):
            if acc.get(key) is not None:
                try:
                    return float(acc[key])
                except (TypeError, ValueError):
                    continue
    return 0.0


# ── Rabby wallet + DeFi-протоколы (DeBank Open API) ────────────────────────────
#
# https://pro-openapi.debank.com — платный API (AccessKey с cloud.debank.com,
# 14 дней бесплатного триала, дальше — предоплаченные "units"). Взамен даёт
# ОДНИМ запросом баланс кошелька по всем сетям (шире, чем бесплатный тариф
# Etherscan — там часть сетей вроде Base/BNB/OP недоступна без платной
# подписки) и ЛЮБЫЕ DeFi-протоколы на кошельке разом — Aave, Fluid, что
# угодно ещё в будущем — без необходимости отдельно разбирать ABI каждого
# протокола (см. обсуждение in PR про то, почему on-chain-вариант для Fluid
# не поехал).
#
# ВНИМАНИЕ: имена полей ответа (amount/price/chain/symbol у all_token_list;
# stats.net_usd_value/asset_usd_value/debt_usd_value у
# all_complex_protocol_list) взяты из широко растиражированной схемы DeBank
# Open API — множество открытых проектов её повторяют один в один, — но НЕ
# сверены вживую с реальным ответом (в этой среде разработки нет доступа ни
# к документации DeBank, ни к тестовому запросу с реальным ключом). Сверьте
# первый реальный ответ с ожидаемой структурой, прежде чем полагаться на
# цифры (см. также аналогичную пометку у fetch_mexc_spot_balance выше).

DEBANK_BASE_URL = "https://pro-openapi.debank.com/v1"


def _debank_get(path: str, params: dict, access_key: str) -> dict:
    resp = requests.get(
        f"{DEBANK_BASE_URL}{path}", params=params,
        headers={"AccessKey": access_key}, timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_debank_wallet_balance(wallet: str, access_key: str) -> dict:
    """
    GET /user/all_token_list?id=&is_all=true — баланс кошелька по всем сетям,
    которые знает DeBank. amount/price в ответе уже в человекочитаемом виде
    (не сырые единицы контракта, decimals уже применены) — в отличие от
    Etherscan-эндпоинтов, отдельно запрашивать цену через CoinGecko не нужно.

    Возвращает {"chains": {chain_slug: [{"symbol":, "amount":, "price_usd":}, ...]},
                 "total_usd": float}.
    """
    tokens = _debank_get("/user/all_token_list", {"id": wallet, "is_all": "true"}, access_key)
    if not isinstance(tokens, list):
        raise RuntimeError(f"DeBank all_token_list: неожиданный ответ: {tokens}")

    chains: dict = {}
    total_usd = 0.0
    for t in tokens:
        try:
            amount = float(t.get("amount", 0) or 0)
        except (TypeError, ValueError):
            continue
        if amount <= 0:
            continue
        price = t.get("price")
        try:
            price = float(price) if price is not None else None
        except (TypeError, ValueError):
            price = None

        chain = t.get("chain", "?")
        chains.setdefault(chain, []).append({
            "symbol": t.get("optimized_symbol") or t.get("symbol") or "?",
            "amount": amount,
            "price_usd": price,
        })
        if price is not None:
            total_usd += amount * price

    return {"chains": chains, "total_usd": total_usd}


def fetch_debank_protocol_positions(wallet: str, access_key: str) -> dict:
    """
    GET /user/all_complex_protocol_list?id= — ВСЕ DeFi-протоколы, которые
    DeBank видит у кошелька (Aave/Fluid/что угодно ещё — без хардкода
    конкретных протоколов, в отличие от прежнего on-chain-варианта только
    под Aave). net_usd_value внутри каждого portfolio_item уже учитывает
    обеспечение минус долг для протоколов с плечом (лендинг/vault).

    Возвращает {protocol_id: {"name":, "chain":, "net_usd": float}} — только
    протоколы с ненулевой позицией.
    """
    protocols = _debank_get("/user/all_complex_protocol_list", {"id": wallet}, access_key)
    if not isinstance(protocols, list):
        raise RuntimeError(f"DeBank all_complex_protocol_list: неожиданный ответ: {protocols}")

    result = {}
    for p in protocols:
        net_usd = sum(
            float(item.get("stats", {}).get("net_usd_value", 0) or 0)
            for item in p.get("portfolio_item_list", [])
        )
        if abs(net_usd) < 1e-9:
            continue
        result[p.get("id") or p.get("name", "?")] = {
            "name": p.get("name", "?"),
            "chain": p.get("chain", "?"),
            "net_usd": net_usd,
        }
    return result


def load_wallet_secrets() -> dict:
    """
    RABBY_WALLET_ADDRESS — если не задан, но задан UNISWAP_WALLET_ADDRESS
    (entry_price.py), используется он: по смыслу это обычно один и тот же
    EVM-кошелёк (Rabby), которым пользователь торгует на Uniswap и держит
    DeFi-позиции. DEBANK_ACCESS_KEY — отдельный платный ключ с
    cloud.debank.com (не путать с ETHERSCAN_API_KEY — тот по-прежнему нужен
    entry_price.py для поиска цены входа на Uniswap, к балансам отношения
    больше не имеет).
    """
    wallet = os.environ.get("RABBY_WALLET_ADDRESS") or os.environ.get("UNISWAP_WALLET_ADDRESS")
    access_key = os.environ.get("DEBANK_ACCESS_KEY")
    if wallet and access_key:
        return {"wallet_address": wallet, "debank_access_key": access_key}
    return {}


# ── Сборка сводного отчёта по всем источникам сразу ────────────────────────────

def fetch_all_balances(secrets: dict) -> dict:
    """
    Опрашивает все настроенные источники. Ошибка одного источника не мешает
    остальным (тот же принцип, что и funding_report.fetch_all) — каждый
    источник в результате отдельно помечен либо значением, либо ошибкой.

    Возвращает {"exchanges": {name: {"value": float|None, "error": str|None}},
                 "wallet": {...}|None, "protocols": {...}|None, "total_usd": float}
    """
    exchanges: dict = {}
    total = 0.0

    def _try(name, fn):
        nonlocal total
        try:
            value = fn()
            exchanges[name] = {"value": value, "error": None}
            total += value
        except Exception as e:
            print(f"[balances/{name}] Ошибка: {e}")
            exchanges[name] = {"value": None, "error": str(e)}

    _try("aster", lambda: fetch_aster_balance(secrets["user"], secrets["signer"], secrets["signer_private_key"]))

    if "bybit_api_key" in secrets:
        _try("bybit", lambda: fetch_bybit_balance(secrets["bybit_api_key"], secrets["bybit_api_secret"]))

    if "lighter_account_index" in secrets:
        _try("lighter", lambda: fetch_lighter_balance(secrets["lighter_account_index"]))

    if "mexc_api_key" in secrets:
        def _mexc_total():
            futures = fetch_mexc_futures_balance(secrets["mexc_api_key"], secrets["mexc_api_secret"])
            spot = fetch_mexc_spot_balance(secrets["mexc_api_key"], secrets["mexc_api_secret"])
            spot_usd = sum(
                amt * (1.0 if sym.upper() in STABLECOIN_SYMBOLS else _coingecko_simple_prices(
                    {SYMBOL_TO_COINGECKO_ID[sym.upper()]}
                ).get(SYMBOL_TO_COINGECKO_ID.get(sym.upper()), 0.0))
                for sym, amt in spot.items()
                if sym.upper() in STABLECOIN_SYMBOLS or sym.upper() in SYMBOL_TO_COINGECKO_ID
            )
            return futures + spot_usd
        _try("mexc", _mexc_total)

    if "gate_api_key" in secrets:
        def _gate_total():
            futures = fetch_gate_futures_balance(secrets["gate_api_key"], secrets["gate_api_secret"])
            spot = fetch_gate_spot_balance(secrets["gate_api_key"], secrets["gate_api_secret"])
            spot_usd = sum(
                amt * (1.0 if cur.upper() in STABLECOIN_SYMBOLS else _coingecko_simple_prices(
                    {SYMBOL_TO_COINGECKO_ID[cur.upper()]}
                ).get(SYMBOL_TO_COINGECKO_ID.get(cur.upper()), 0.0))
                for cur, amt in spot.items()
                if cur.upper() in STABLECOIN_SYMBOLS or cur.upper() in SYMBOL_TO_COINGECKO_ID
            )
            return futures + spot_usd
        _try("gate", _gate_total)

    wallet_secrets = load_wallet_secrets()
    wallet_result = None
    protocols_result = None
    if wallet_secrets:
        address = wallet_secrets["wallet_address"]
        access_key = wallet_secrets["debank_access_key"]
        try:
            wallet_result = fetch_debank_wallet_balance(address, access_key)
            total += wallet_result["total_usd"]
        except Exception as e:
            print(f"[balances/wallet] Ошибка: {e}")
            wallet_result = {"error": str(e)}

        try:
            protocols_result = fetch_debank_protocol_positions(address, access_key)
            total += sum(v["net_usd"] for v in protocols_result.values())
        except Exception as e:
            print(f"[balances/protocols] Ошибка: {e}")
            protocols_result = {"error": str(e)}

    return {
        "exchanges": exchanges,
        "wallet": wallet_result,
        "protocols": protocols_result,
        "total_usd": total,
    }


# ── Текстовый отчёт для Telegram (/balance) ─────────────────────────────────────

EXCHANGE_LABELS = {
    "aster": "Aster", "bybit": "Bybit", "lighter": "Lighter",
    "mexc": "MEXC", "gate": "Gate",
}


def build_balances_report(result: dict) -> str:
    """Тот же result, что отдаёт /api/balances на веб-странице — здесь просто
    коротко в текст для Telegram (подробная разбивка по сетям/токенам кошелька
    удобнее смотрится на странице /balances, чем длинным сообщением в чате)."""
    lines = ["💰 Сводный баланс"]

    lines.append("")
    for key, v in result["exchanges"].items():
        label = EXCHANGE_LABELS.get(key, key)
        if v["error"]:
            lines.append(f"{label}: ❌ {v['error']}")
        else:
            lines.append(f"{label}: ${v['value']:,.2f}")

    wallet = result.get("wallet")
    if wallet is None:
        lines.append("Rabby wallet: не настроено (RABBY_WALLET_ADDRESS/DEBANK_ACCESS_KEY)")
    elif "error" in wallet:
        lines.append(f"Rabby wallet: ❌ {wallet['error']}")
    else:
        lines.append(f"Rabby wallet: ${wallet['total_usd']:,.2f}")

    protocols = result.get("protocols")
    if protocols is None:
        lines.append("DeFi-протоколы: не настроено")
    elif "error" in protocols:
        lines.append(f"DeFi-протоколы: ❌ {protocols['error']}")
    elif not protocols:
        lines.append("DeFi-протоколы: открытых позиций не найдено")
    else:
        for p in protocols.values():
            lines.append(f"{p['name']} ({p['chain']}): ${p['net_usd']:,.2f}")

    lines.append("")
    lines.append(f"Итого: ${result['total_usd']:,.2f}")
    return "\n".join(lines)
