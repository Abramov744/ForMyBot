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
  - Rabby wallet — ON-CHAIN, без API-ключа биржи: нативный газ-токен и
    ERC-20-токены кошелька, авто-обнаруженные через историю переводов (тот
    же Etherscan V2 API и тот же список сетей, что уже используется в
    entry_price.py для поиска цены входа на Uniswap).
  - Aave v3 — ON-CHAIN, чистая стоимость позиции (обеспечение минус долг)
    через view-функцию Pool.getUserAccountData(address).
  - Fluid  — ПОКА НЕ АВТОМАТИЗИРОВАН, см. докстринг fetch_fluid_balance.

Оценка стоимости в USD для того, что не биржа (кошелёк) берётся с
бесплатного публичного CoinGecko API (без ключа, с кэшем на 5 минут) —
это единственный источник цен в проекте, у которого нет отдельного API-ключа
для аккаунта пользователя, поэтому ошибки/лимиты этого API НЕ должны ронять
остальную часть отчёта: при неудаче соответствующая позиция просто
показывается "без оценки в USD", а не исключается молча и не пугает исключением.
"""

import hashlib
import hmac
import time
import urllib.parse

import requests

from entry_price import (
    _etherscan_get,
    _evm_blocked_chains,
    _fetch_chain_token_transfers,
    _fetch_evm_chainlist,
    _UNSUPPORTED_CHAIN_MSG_KEYWORDS,
)
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

# chain_id (Etherscan V2 chainid) → coingecko-id нативного газ-токена сети и
# platform-слаг для запроса цен ERC-20-токенов (/simple/token_price/{platform}).
# Только сети, в которых уверены (проверено на практике) — добавить новую
# сеть можно одной строкой; для сети не из списка кошелёк всё равно
# опрашивается (см. fetch_wallet_balance), просто ненулевой баланс на ней
# покажется без оценки в USD, а не с угаданной ценой.
CHAIN_PRICING = {
    1:     {"native_id": "ethereum",    "platform": "ethereum"},            # Ethereum
    42161: {"native_id": "ethereum",    "platform": "arbitrum-one"},        # Arbitrum One
    8453:  {"native_id": "ethereum",    "platform": "base"},                # Base
    10:    {"native_id": "ethereum",    "platform": "optimistic-ethereum"}, # OP Mainnet
    56:    {"native_id": "binancecoin", "platform": "binance-smart-chain"}, # BNB Smart Chain
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


def _coingecko_token_prices(platform: str, contract_addresses: list) -> dict:
    """{contract_address_lower: price_usd} для одной сети — один запрос
    покрывает сразу все переданные контракты (CoinGecko поддерживает список
    через запятую в contract_addresses)."""
    if not contract_addresses:
        return {}
    cache_key = f"token:{platform}:" + ",".join(sorted(a.lower() for a in contract_addresses))
    now = time.time()
    if cache_key in _coingecko_cache and now - _coingecko_cache_ts[cache_key] < COINGECKO_CACHE_TTL_S:
        return _coingecko_cache[cache_key]

    data = _coingecko_get(
        f"/simple/token_price/{platform}",
        {"contract_addresses": ",".join(contract_addresses), "vs_currencies": "usd"},
    )
    prices = {addr.lower(): float(v["usd"]) for addr, v in (data or {}).items() if "usd" in v}
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


# ── Rabby wallet (on-chain, Etherscan V2) ──────────────────────────────────────

def fetch_wallet_balance(wallet: str, etherscan_api_key: str) -> dict:
    """
    Баланс обычного EVM-кошелька (Rabby и т.п.) по ВСЕМ сетям, которые прямо
    сейчас поддерживает Etherscan V2 (тот же динамический список, что и в
    entry_price.py для поиска цены входа на Uniswap) — без хардкода списка
    сетей и без хардкода списка токенов:
      - нативный газ-токен каждой сети — action=balance;
      - ERC-20-токены — авто-обнаружение по истории последних 1000 переводов
        (entry_price._fetch_chain_token_transfers), затем action=tokenbalance
        по каждому найденному контракту. Это не гарантирует 100% полноту —
        тот же компромисс, что и в entry_price.py (см. комментарий там).

    Оценка в USD — через CoinGecko (см. CHAIN_PRICING/SYMBOL_TO_COINGECKO_ID
    выше); чего не смогли оценить — возвращается с price_usd=None, а не
    выбрасывается из отчёта и не оценивается наугад.

    Может занять до ~30 секунд (как и поиск цены на Uniswap в entry_price.py)
    — опрашиваются десятки сетей по очереди с троттлингом Etherscan.

    Возвращает {"chains": {chain_id: {"chain_name":, "native": {...}, "tokens": [...]}},
                 "total_usd": float, "unpriced": [...]}
    """
    try:
        chains = _fetch_evm_chainlist(etherscan_api_key)
    except Exception as e:
        raise RuntimeError(f"Не удалось получить список EVM-сетей: {e}")

    wallet_lower = wallet.lower()
    result_chains = {}
    total_usd = 0.0
    unpriced = []

    for chain_id, chain_name in chains:
        if chain_id in _evm_blocked_chains:
            continue
        try:
            chain_entry = _wallet_balance_on_chain(chain_id, chain_name, wallet_lower, etherscan_api_key)
        except Exception as e:
            msg = str(e).lower()
            if any(kw in msg for kw in _UNSUPPORTED_CHAIN_MSG_KEYWORDS):
                _evm_blocked_chains.add(chain_id)
            else:
                print(f"[balances/wallet/{chain_name}] Ошибка: {e}")
            continue

        if chain_entry is None:
            continue  # пустая сеть (нулевой нативный баланс и нет токенов) — не засоряем отчёт
        result_chains[chain_id] = chain_entry
        if chain_entry["native"]["price_usd"] is None:
            unpriced.append(f"{chain_entry['native']['symbol']} ({chain_name})")
        else:
            total_usd += chain_entry["native"]["amount"] * chain_entry["native"]["price_usd"]
        for t in chain_entry["tokens"]:
            if t["price_usd"] is None:
                unpriced.append(f"{t['symbol']} ({chain_name})")
            else:
                total_usd += t["amount"] * t["price_usd"]

    return {"chains": result_chains, "total_usd": total_usd, "unpriced": unpriced}


def _wallet_balance_on_chain(chain_id: int, chain_name: str, wallet_lower: str, api_key: str) -> dict | None:
    pricing = CHAIN_PRICING.get(chain_id)

    native_raw = _etherscan_get(
        {"module": "account", "action": "balance", "address": wallet_lower, "tag": "latest", "chainid": chain_id},
        api_key,
    )
    if str(native_raw.get("status", "")) != "1":
        raise RuntimeError(f"balance error: {native_raw}")
    native_amount = int(native_raw.get("result", "0") or "0") / 1e18

    tokens_meta = _discover_wallet_tokens(chain_id, api_key, wallet_lower)

    token_amounts = {}
    for contract, meta in tokens_meta.items():
        try:
            bal_raw = _etherscan_get(
                {
                    "module": "account", "action": "tokenbalance", "contractaddress": contract,
                    "address": wallet_lower, "tag": "latest", "chainid": chain_id,
                },
                api_key,
            )
            if str(bal_raw.get("status", "")) != "1":
                continue
            amount = int(bal_raw.get("result", "0") or "0") / (10 ** meta["decimals"])
        except Exception as e:
            print(f"[balances/wallet/{chain_name}] Не удалось получить баланс токена {meta['symbol']}: {e}")
            continue
        if amount > 0:
            token_amounts[contract] = amount

    if native_amount == 0 and not token_amounts:
        return None

    # Цены: нативный токен — по coingecko-id сети (если известен), остальное —
    # стейблкоины по номиналу, всё прочее — батчем по platform-слагу сети.
    native_price = None
    if pricing:
        native_price = _coingecko_simple_prices({pricing["native_id"]}).get(pricing["native_id"])

    non_stable_contracts = [
        addr for addr in token_amounts
        if tokens_meta[addr]["symbol"].upper() not in STABLECOIN_SYMBOLS
    ]
    token_prices = {}
    if pricing and non_stable_contracts:
        token_prices = _coingecko_token_prices(pricing["platform"], non_stable_contracts)

    tokens_out = []
    for addr, amount in token_amounts.items():
        symbol = tokens_meta[addr]["symbol"]
        if symbol.upper() in STABLECOIN_SYMBOLS:
            price = 1.0
        else:
            price = token_prices.get(addr.lower())
        tokens_out.append({"symbol": symbol, "amount": amount, "price_usd": price})

    return {
        "chain_name": chain_name,
        "native": {
            "symbol": "ETH" if pricing and pricing["native_id"] == "ethereum" else "?",
            "amount": native_amount,
            "price_usd": native_price,
        },
        "tokens": tokens_out,
    }


def _discover_wallet_tokens(chain_id: int, api_key: str, wallet: str) -> dict:
    """{contract_address_lower: {"symbol":, "decimals":}} по истории
    переводов кошелька — см. докстринг fetch_wallet_balance про неполноту."""
    transfers = _fetch_chain_token_transfers(chain_id, api_key, wallet)
    tokens = {}
    for t in transfers:
        addr = (t.get("contractAddress") or "").lower()
        if not addr:
            continue
        try:
            decimals = int(t.get("tokenDecimal", 18) or 18)
        except (TypeError, ValueError):
            decimals = 18
        tokens[addr] = {"symbol": t.get("tokenSymbol") or "?", "decimals": decimals}
    return tokens


# ── Aave v3 (on-chain, Pool.getUserAccountData) ────────────────────────────────

# Proxy-адрес контракта Pool (не Pool Implementation) — проверено по
# Etherscan/Arbiscan/BaseScan на 29.08.2026, см. ссылки в комментариях.
AAVE_V3_POOL = {
    1:     "0x87870Bca3F3fD6335C3F4ce8392D69350B4fA4E2",  # etherscan.io/address/0x87870bca3f3fd6335c3f4ce8392d69350b4fa4e2
    42161: "0x794a61358D6845594F94dc1DB02A252b5b4814aD",  # arbiscan.io/address/0x794a61358d6845594f94dc1db02a252b5b4814ad
    8453:  "0xA238Dd80C259a72e81d7e4664a9801593F98d1c5",  # basescan.org/address/0xa238dd80c259a72e81d7e4664a9801593f98d1c5
}

# keccak256("getUserAccountData(address)")[:4] — считается в коде через
# eth_utils (тот же keccak, что использует eth_account для подписи Aster),
# а не хардкодится строкой, чтобы не полагаться на память/угадывание точного
# 4-байтового селектора функции.
def _aave_selector() -> str:
    from eth_utils import keccak
    return keccak(text="getUserAccountData(address)")[:4].hex()


def fetch_aave_balance(wallet: str, etherscan_api_key: str) -> dict:
    """
    Чистая стоимость позиции на Aave v3 (обеспечение минус долг) по каждой
    сети из AAVE_V3_POOL — через view-функцию Pool.getUserAccountData(address),
    вызванную как eth_call через Etherscan V2 proxy (module=proxy&action=eth_call,
    тот же ключ и троттлинг, что и остальные Etherscan-запросы).

    totalCollateralBase/totalDebtBase у Aave v3 выражены в БАЗОВОЙ валюте
    оракула — на всех перечисленных сетях это USD с 8 знаками после запятой
    (см. IPriceOracleGetter.BASE_CURRENCY_UNIT в документации Aave), поэтому
    отдельно переводить в USD не нужно.

    Возвращает {chain_id: {"collateral_usd":, "debt_usd":, "net_usd":}} —
    только сети, где у кошелька есть ненулевая позиция.
    """
    call_data = "0x" + _aave_selector() + wallet.lower().replace("0x", "").rjust(64, "0")

    result = {}
    for chain_id, pool_address in AAVE_V3_POOL.items():
        try:
            raw = _etherscan_get(
                {
                    "module": "proxy", "action": "eth_call", "to": pool_address,
                    "data": call_data, "tag": "latest", "chainid": chain_id,
                },
                etherscan_api_key,
            )
            hex_result = raw.get("result")
            if not isinstance(hex_result, str) or not hex_result.startswith("0x") or len(hex_result) < 2 + 64 * 2:
                raise RuntimeError(f"неожиданный ответ eth_call: {raw}")
            body = hex_result[2:]
            total_collateral_base = int(body[0:64], 16)
            total_debt_base = int(body[64:128], 16)
        except Exception as e:
            print(f"[balances/aave] Сеть {chain_id}: {e}")
            continue

        if total_collateral_base == 0 and total_debt_base == 0:
            continue  # позиции на этой сети нет — не засоряем отчёт нулями

        result[chain_id] = {
            "collateral_usd": total_collateral_base / 1e8,
            "debt_usd": total_debt_base / 1e8,
            "net_usd": (total_collateral_base - total_debt_base) / 1e8,
        }
    return result


# ── Fluid ───────────────────────────────────────────────────────────────────────

def fetch_fluid_balance(wallet: str) -> None:
    """
    ПОКА НЕ АВТОМАТИЗИРОВАНО.

    У Fluid (Instadapp) нет простого REST API для чтения позиций пользователя
    — только on-chain resolver-контракты (FluidVaultResolver.positionsByUser,
    FluidLendingResolver и т.п.), и заранее не известно, каким именно
    продуктом Fluid пользуется владелец бота: простое лендинг (fToken —
    считался бы одним balanceOf + конвертацией курса, несложно) или
    вault/leverage-позиции (NFT-based, вложенные структуры в ABI, гораздо
    легче ошибиться с декодированием — а это именно тот случай, когда по
    CLAUDE.md лучше спросить, чем предполагать).

    Как только известно, какой именно продукт Fluid используется (и на какой
    сети) — дописать эту функцию по образцу fetch_aave_balance выше.
    """
    return None


# ── Секреты для этого модуля (отдельно от funding_report.load_secrets —
#    те же переменные окружения, но опциональные и специфичные для балансов) ──

def load_wallet_secrets() -> dict:
    """
    RABBY_WALLET_ADDRESS — если не задан, но задан UNISWAP_WALLET_ADDRESS
    (entry_price.py), используется он: по смыслу это обычно один и тот же
    EVM-кошелёк (Rabby), которым пользователь торгует на Uniswap и держит
    DeFi-позиции. ETHERSCAN_API_KEY — общий с entry_price.py.
    """
    import os
    wallet = os.environ.get("RABBY_WALLET_ADDRESS") or os.environ.get("UNISWAP_WALLET_ADDRESS")
    api_key = os.environ.get("ETHERSCAN_API_KEY")
    if wallet and api_key:
        return {"wallet_address": wallet, "etherscan_api_key": api_key}
    return {}


# ── Сборка сводного отчёта по всем источникам сразу ────────────────────────────

def fetch_all_balances(secrets: dict) -> dict:
    """
    Опрашивает все настроенные источники. Ошибка одного источника не мешает
    остальным (тот же принцип, что и funding_report.fetch_all) — каждый
    источник в результате отдельно помечен либо значением, либо ошибкой.

    Возвращает {"exchanges": {name: {"value": float|None, "error": str|None}},
                 "wallet": {...}|None, "aave": {...}|None, "fluid": None,
                 "total_usd": float}
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
    if wallet_secrets:
        try:
            wallet_result = fetch_wallet_balance(wallet_secrets["wallet_address"], wallet_secrets["etherscan_api_key"])
            total += wallet_result["total_usd"]
        except Exception as e:
            print(f"[balances/wallet] Ошибка: {e}")
            wallet_result = {"error": str(e)}

    aave_result = None
    if wallet_secrets:
        try:
            aave_result = fetch_aave_balance(wallet_secrets["wallet_address"], wallet_secrets["etherscan_api_key"])
            total += sum(v["net_usd"] for v in aave_result.values())
        except Exception as e:
            print(f"[balances/aave] Ошибка: {e}")
            aave_result = {"error": str(e)}

    return {
        "exchanges": exchanges,
        "wallet": wallet_result,
        "aave": aave_result,
        "fluid": None,  # см. fetch_fluid_balance — пока не автоматизировано
        "total_usd": total,
    }


# ── Текстовый отчёт для Telegram (/balance) ─────────────────────────────────────

EXCHANGE_LABELS = {
    "aster": "Aster", "bybit": "Bybit", "lighter": "Lighter",
    "mexc": "MEXC", "gate": "Gate",
}
_AAVE_CHAIN_NAMES = {1: "Ethereum", 42161: "Arbitrum One", 8453: "Base"}


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
        lines.append("Rabby wallet: не настроено (RABBY_WALLET_ADDRESS/ETHERSCAN_API_KEY)")
    elif "error" in wallet:
        lines.append(f"Rabby wallet: ❌ {wallet['error']}")
    else:
        lines.append(f"Rabby wallet: ${wallet['total_usd']:,.2f}")
        if wallet["unpriced"]:
            lines.append(f"  без оценки: {', '.join(wallet['unpriced'])}")

    aave = result.get("aave")
    if aave is None:
        lines.append("Aave v3: не настроено")
    elif "error" in aave:
        lines.append(f"Aave v3: ❌ {aave['error']}")
    elif not aave:
        lines.append("Aave v3: нет открытых позиций")
    else:
        for chain_id, p in aave.items():
            chain_name = _AAVE_CHAIN_NAMES.get(chain_id, str(chain_id))
            lines.append(f"Aave v3 ({chain_name}): ${p['net_usd']:,.2f} (обеспечение ${p['collateral_usd']:,.2f}, долг ${p['debt_usd']:,.2f})")

    lines.append("Fluid: ⚪️ не подключено (см. README)")

    lines.append("")
    lines.append(f"Итого: ${result['total_usd']:,.2f}")
    return "\n".join(lines)
