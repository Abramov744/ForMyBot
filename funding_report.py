#!/usr/bin/env python3
"""
Funding Fee Daily Report — Aster + Bybit + Lighter + MEXC + Gate → Telegram
Период: предыдущие календарные сутки по МСК (00:00–00:00 МСК).
Запуск: ежедневно в 04:00 UTC (07:00 МСК).
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timezone, timedelta

import requests
from eth_account import Account
from eth_account.messages import encode_typed_data


# ── Timezone ──────────────────────────────────────────────────────────────────

MSK = timezone(timedelta(hours=3))


def previous_day_msk_ms():
    """Возвращает (start_ms, end_ms) предыдущих суток по МСК в миллисекундах UTC."""
    now_msk   = datetime.now(MSK)
    today_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    start_msk = today_msk - timedelta(days=1)
    end_msk   = today_msk
    return int(start_msk.timestamp() * 1000), int(end_msk.timestamp() * 1000)


# ── Загрузка секретов ─────────────────────────────────────────────────────────

def load_secrets() -> dict:
    """Читает секреты из переменных окружения (GitHub Secrets)."""
    result = {
        "user":               os.environ["ASTER_USER_ADDRESS"],
        "signer":             os.environ["ASTER_SIGNER_ADDRESS"],
        "signer_private_key": os.environ["ASTER_SIGNER_PRIVATE_KEY"],
        "telegram_token":     os.environ["TELEGRAM_BOT_TOKEN"],
        "telegram_chat_id":   os.environ["TELEGRAM_CHAT_ID"],
    }
    # Bybit — опционально
    bybit_api_key    = os.environ.get("BYBIT_API_KEY")
    bybit_api_secret = os.environ.get("BYBIT_API_SECRET")
    if bybit_api_key and bybit_api_secret:
        result["bybit_api_key"]    = bybit_api_key
        result["bybit_api_secret"] = bybit_api_secret

    # Lighter — опционально
    lighter_account_index = os.environ.get("LIGHTER_ACCOUNT_INDEX")
    lighter_auth_token    = os.environ.get("LIGHTER_AUTH_TOKEN")
    if lighter_account_index and lighter_auth_token:
        result["lighter_account_index"] = lighter_account_index
        result["lighter_auth_token"]    = lighter_auth_token

    # MEXC — опционально
    mexc_api_key    = os.environ.get("MEXC_API_KEY")
    mexc_api_secret = os.environ.get("MEXC_API_SECRET")
    if mexc_api_key and mexc_api_secret:
        result["mexc_api_key"]    = mexc_api_key
        result["mexc_api_secret"] = mexc_api_secret

    # Gate — опционально
    gate_api_key    = os.environ.get("GATE_API_KEY")
    gate_api_secret = os.environ.get("GATE_API_SECRET")
    if gate_api_key and gate_api_secret:
        result["gate_api_key"]    = gate_api_key
        result["gate_api_secret"] = gate_api_secret

    # Uniswap (Arbitrum + Base) — опционально, только для поиска спот-цены
    # входа в entry_price.py (не биржа со своим API, а чтение истории
    # ERC-20-переводов кошелька через Etherscan). Один и тот же кошелёк
    # используется на обеих сетях — так подтвердил пользователь.
    uniswap_wallet_address = os.environ.get("UNISWAP_WALLET_ADDRESS")
    etherscan_api_key      = os.environ.get("ETHERSCAN_API_KEY")
    if uniswap_wallet_address and etherscan_api_key:
        result["uniswap_wallet_address"] = uniswap_wallet_address
        result["etherscan_api_key"]      = etherscan_api_key

    return result


# ── Aster EIP-712 подпись ─────────────────────────────────────────────────────

_TYPED_DATA_TEMPLATE = {
    "types": {
        "EIP712Domain": [
            {"name": "name",              "type": "string"},
            {"name": "version",           "type": "string"},
            {"name": "chainId",           "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "Message": [{"name": "msg", "type": "string"}],
    },
    "primaryType": "Message",
    "domain": {
        "name":              "AsterSignTransaction",
        "version":           "1",
        "chainId":           1666,
        "verifyingContract": "0x0000000000000000000000000000000000000000",
    },
    "message": {"msg": ""},
}


def _aster_sign(param_str: str, private_key: str) -> str:
    td = json.loads(json.dumps(_TYPED_DATA_TEMPLATE))
    td["message"]["msg"] = param_str
    signable = encode_typed_data(full_message=td)
    return Account.sign_message(signable, private_key=private_key).signature.hex()


# ── Получение данных с Aster ──────────────────────────────────────────────────

def fetch_aster(user: str, signer: str, private_key: str,
                start_ms: int, end_ms: int) -> list:
    """GET /fapi/v3/income, incomeType=FUNDING_FEE, с пагинацией."""
    all_records, limit, cur_start = [], 1000, start_ms
    while True:
        nonce = int(time.time() * 1_000_000)
        params = {
            "incomeType": "FUNDING_FEE",
            "startTime":  str(cur_start),
            "endTime":    str(end_ms),
            "limit":      str(limit),
            "nonce":      str(nonce),
            "user":       user,
            "signer":     signer,
        }
        param_str = urllib.parse.urlencode(params)
        sig  = _aster_sign(param_str, private_key)
        url  = f"https://fapi.asterdex.com/fapi/v3/income?{param_str}&signature={sig}"
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict):
            raise RuntimeError(f"Aster API error: {data}")
        if not data:
            break
        all_records.extend(data)
        if len(data) < limit:
            break
        cur_start = int(data[-1]["time"]) + 1
        if cur_start >= end_ms:
            break
    return all_records


def fetch_aster_open_symbols(user: str, signer: str, private_key: str) -> set:
    """GET /fapi/v3/positionRisk — возвращает набор символов с ненулевой позицией."""
    nonce = int(time.time() * 1_000_000)
    params = {
        "timestamp": str(int(time.time() * 1000)),
        "nonce":     str(nonce),
        "user":      user,
        "signer":    signer,
    }
    param_str = urllib.parse.urlencode(params)
    sig = _aster_sign(param_str, private_key)
    url = f"https://fapi.asterdex.com/fapi/v3/positionRisk?{param_str}&signature={sig}"
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        raise RuntimeError(f"Aster positionRisk error: {data}")
    return {p["symbol"] for p in data if abs(float(p.get("positionAmt", 0))) > 0}


# ── Получение данных с Bybit ──────────────────────────────────────────────────

def _get_proxies() -> dict | None:
    """
    Читает прокси из окружения для запросов к Bybit.
    Приоритет: BYBIT_PROXY → HTTPS_PROXY → HTTP_PROXY.
    Поддерживаемые форматы значения:
      - http://user:pass@host:port
      - http://host:port
      - host:port:user:pass   → конвертируется в http://user:pass@host:port
      - host:port             → добавляется схема http://
    """
    proxy = (
        os.environ.get("BYBIT_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not proxy:
        return None

    if not proxy.startswith(("http://", "https://")):
        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            proxy = f"http://{user}:{pwd}@{host}:{port}"
        else:
            proxy = f"http://{proxy}"

    return {"http": proxy, "https": proxy}


def _bybit_sign(api_key: str, api_secret: str,
                timestamp: str, recv_window: str, query_string: str) -> str:
    payload = f"{timestamp}{api_key}{recv_window}{query_string}"
    return hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def fetch_bybit(api_key: str, api_secret: str,
                start_ms: int, end_ms: int) -> list:
    """GET /v5/account/transaction-log, type=SETTLEMENT, с пагинацией."""
    base_url    = "https://api.bybit.com"
    recv_window = "5000"
    all_records = []
    cursor      = None
    proxies     = _get_proxies()
    if proxies:
        print(f"[Bybit DEBUG] запрос через прокси: {proxies['https'].split('@')[-1]}")

    # На случай, если в GitHub-секрете затесался пробел/перевод строки
    api_key    = api_key.strip()
    api_secret = api_secret.strip()

    while True:
        timestamp = str(int(time.time() * 1000))

        # Параметры в фиксированном порядке — list of tuples
        params_list = [
            ("accountType", "UNIFIED"),
            ("category",    "linear"),
            ("type",        "SETTLEMENT"),
            ("startTime",   str(start_ms)),
            ("endTime",     str(end_ms)),
            ("limit",       "50"),
        ]
        if cursor:
            params_list.append(("cursor", cursor))

        query_string = urllib.parse.urlencode(params_list)

        # payload для HMAC — ровно то, что подписываем
        payload = f"{timestamp}{api_key}{recv_window}{query_string}"
        b_payload = payload.encode("utf-8")

        # Считаем подпись
        sig = hmac.new(api_secret.encode("utf-8"), b_payload, hashlib.sha256).hexdigest()

        headers = {
            "X-BAPI-API-KEY":     api_key,
            "X-BAPI-SIGN":        sig,
            "X-BAPI-TIMESTAMP":   timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

        url = f"{base_url}/v5/account/transaction-log?{query_string}"
        resp = requests.get(
            url,
            headers=headers,
            timeout=30,
            proxies=proxies,
        )
        if not resp.ok:
            print(f"[Bybit DEBUG] HTTP {resp.status_code}, тело ответа: {resp.text}")
        resp.raise_for_status()
        data = resp.json()

        if data.get("retCode", 0) != 0:
            raise RuntimeError(f"Bybit API error {data.get('retCode')}: {data.get('retMsg')}")

        items = data.get("result", {}).get("list", [])
        all_records.extend(items)

        cursor = data.get("result", {}).get("nextPageCursor") or None
        if not cursor or not items:
            break

    return all_records


def fetch_bybit_open_symbols(api_key: str, api_secret: str) -> dict:
    """
    GET /v5/position/list, category=linear, settleCoin=USDT.
    Возвращает {symbol: created_time_ms} — Bybit отдаёт точное время
    открытия позиции в поле createdTime, поэтому здесь (в отличие от
    большинства других бирж) можно посчитать funding именно "с момента
    открытия", а не приближённо через фиксированную глубину поиска.
    """
    base_url = "https://api.bybit.com"
    recv_window = "5000"
    proxies = _get_proxies()
    api_key, api_secret = api_key.strip(), api_secret.strip()

    timestamp = str(int(time.time() * 1000))
    params_list = [("category", "linear"), ("settleCoin", "USDT"), ("limit", "200")]
    query_string = urllib.parse.urlencode(params_list)
    sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, query_string)
    headers = {
        "X-BAPI-API-KEY":     api_key,
        "X-BAPI-SIGN":        sig,
        "X-BAPI-TIMESTAMP":   timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
    }
    resp = requests.get(f"{base_url}/v5/position/list?{query_string}", headers=headers, timeout=30, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit position/list error {data.get('retCode')}: {data.get('retMsg')}")

    items = data.get("result", {}).get("list", [])
    return {
        p["symbol"]: int(p["createdTime"])
        for p in items if float(p.get("size", 0)) > 0
    }


# ── Получение данных с Lighter ────────────────────────────────────────────────
#
# Авторизация: read-only auth-токен (создаётся один раз на
# https://app.lighter.xyz/read-only-tokens/, срок действия можно выбрать на годы
# вперёд). Токен передаётся как есть в заголовке Authorization — никакой
# подписи запросов на лету (в отличие от Aster) не требуется.
#
# Эндпоинты (https://mainnet.zklighter.elliot.ai):
#   GET /api/v1/orderBookDetails  — публичный, даёт список рынков (market_id -> symbol)
#   GET /api/v1/positionFunding   — приватный (Authorization), сами выплаты funding

LIGHTER_BASE_URL = "https://mainnet.zklighter.elliot.ai"


def fetch_lighter_markets() -> dict:
    """GET /api/v1/orderBookDetails — публичный эндпоинт, market_id -> symbol."""
    resp = requests.get(
        f"{LIGHTER_BASE_URL}/api/v1/orderBookDetails",
        params={"filter": "perp"},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    if data.get("code", 200) != 200:
        raise RuntimeError(f"Lighter orderBookDetails error: {data}")

    return {
        m["market_id"]: m["symbol"]
        for m in data.get("order_book_details", [])
    }


def fetch_lighter(account_index: str, auth_token: str,
                  start_ms: int, end_ms: int) -> list:
    """GET /api/v1/positionFunding, с курсорной пагинацией."""
    markets = fetch_lighter_markets()

    start_s = start_ms // 1000
    end_s   = end_ms // 1000

    all_records, limit, cursor = [], 100, None
    headers = {"authorization": auth_token.strip()}

    while True:
        params: dict = {
            "account_index":  account_index,
            "limit":          str(limit),
            "side":           "all",
            "start_timestamp": str(start_s),
            "end_timestamp":   str(end_s),
        }
        if cursor:
            params["cursor"] = cursor

        resp = requests.get(
            f"{LIGHTER_BASE_URL}/api/v1/positionFunding",
            params=params,
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code", 200) != 200:
            raise RuntimeError(f"Lighter API error: {data}")

        items = data.get("position_fundings", [])
        for item in items:
            item["symbol"] = markets.get(item.get("market_id"), f"MARKET_{item.get('market_id')}")
            item["asset"]  = "USDC"  # Lighter торгуется с расчётами в USDC
        all_records.extend(items)

        cursor = data.get("next_cursor") or None
        if not cursor or not items:
            break

    return all_records


def fetch_lighter_open_symbols(account_index: str, auth_token: str) -> set:
    """
    GET /api/v1/account?by=index&value={account_index}&active_only=true
    active_only=true просит сервер сразу отдать только рынки с реальной
    открытой позицией (а не просто те, где выставлялось плечо когда-то).
    Точные имена полей в ответе документированы не полностью, поэтому
    разбор сделан с запасом — пробуем несколько вероятных вариантов ключей.
    """
    markets = fetch_lighter_markets()
    headers = {"authorization": auth_token.strip()}
    params = {"by": "index", "value": account_index, "active_only": "true"}

    resp = requests.get(
        f"{LIGHTER_BASE_URL}/api/v1/account",
        params=params, headers=headers, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 200) != 200:
        raise RuntimeError(f"Lighter account error: {data}")

    accounts = data.get("accounts", [data]) if "accounts" not in data else data["accounts"]
    open_symbols = set()
    for acc in accounts:
        for pos in acc.get("positions", []):
            size = float(pos.get("position", pos.get("size", pos.get("position_size", 0))) or 0)
            if size == 0:
                continue
            market_id = pos.get("market_id", pos.get("market_index"))
            open_symbols.add(markets.get(market_id, f"MARKET_{market_id}"))
    return open_symbols


# ── Получение данных с MEXC ───────────────────────────────────────────────────
#
# Авторизация MEXC Futures отличается и от Bybit, и от Aster:
#   строка_параметров = отсортированные по алфавиту "key=value", склеенные через &
#   (пустая строка, если параметров нет)
#   target_string      = ApiKey + Request-Time(мс) + строка_параметров
#   Signature           = HMAC-SHA256(ApiSecret, target_string), hex
# Подпись передаётся не в query, а в заголовках: ApiKey, Request-Time, Signature.
#
# Эндпоинт: GET /api/v1/private/position/funding_records (база: https://api.mexc.com)

def _get_mexc_proxies() -> dict | None:
    """
    Прокси для запросов к MEXC — на случай, если GitHub Actions IP тоже
    попадёт под блокировку (как ранее было с Bybit). Читает MEXC_PROXY,
    затем общие HTTPS_PROXY/HTTP_PROXY. Форматы значения — как у Bybit-версии.
    """
    proxy = (
        os.environ.get("MEXC_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not proxy:
        return None

    if not proxy.startswith(("http://", "https://")):
        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            proxy = f"http://{user}:{pwd}@{host}:{port}"
        else:
            proxy = f"http://{proxy}"

    return {"http": proxy, "https": proxy}


def _mexc_sign(api_key: str, api_secret: str,
               timestamp: str, params_list: list) -> str:
    # Бизнес-параметры сортируются по алфавиту (dictionary order) по ключу
    sorted_params = sorted(params_list, key=lambda kv: kv[0])
    param_string = "&".join(f"{k}={v}" for k, v in sorted_params)
    target = f"{api_key}{timestamp}{param_string}"
    return hmac.new(api_secret.encode("utf-8"), target.encode("utf-8"), hashlib.sha256).hexdigest()


def fetch_mexc(api_key: str, api_secret: str,
               start_ms: int, end_ms: int) -> list:
    """GET /api/v1/private/position/funding_records, с постраничной пагинацией."""
    base_url    = "https://api.mexc.com"
    page_size   = 100
    all_records = []
    page_num    = 1
    proxies     = _get_mexc_proxies()
    if proxies:
        print(f"[MEXC DEBUG] запрос через прокси: {proxies['https'].split('@')[-1]}")

    api_key    = api_key.strip()
    api_secret = api_secret.strip()

    while True:
        timestamp = str(int(time.time() * 1000))

        params_list = [
            ("start_time", str(start_ms)),
            ("end_time",   str(end_ms)),
            ("page_num",   str(page_num)),
            ("page_size",  str(page_size)),
        ]

        sig = _mexc_sign(api_key, api_secret, timestamp, params_list)
        headers = {
            "ApiKey":       api_key,
            "Request-Time": timestamp,
            "Signature":    sig,
        }

        query_string = urllib.parse.urlencode(sorted(params_list, key=lambda kv: kv[0]))
        url = f"{base_url}/api/v1/private/position/funding_records?{query_string}"

        resp = requests.get(url, headers=headers, timeout=30, proxies=proxies)
        if not resp.ok:
            print(f"[MEXC DEBUG] HTTP {resp.status_code}, тело ответа: {resp.text}")
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success", False):
            raise RuntimeError(f"MEXC API error {data.get('code')}: {data.get('message') or data}")

        payload = data.get("data") or {}
        items = payload.get("resultList", [])
        for item in items:
            symbol = item.get("symbol", "")
            item["asset"] = symbol.split("_")[-1] if "_" in symbol else "USDT"
        all_records.extend(items)

        total_page = payload.get("totalPage", 1)
        if not items or page_num >= total_page:
            break
        page_num += 1

    return all_records


def fetch_mexc_open_symbols(api_key: str, api_secret: str) -> dict:
    """
    GET /api/v1/private/position/open_positions — уже отдаёт только открытые
    позиции. Возвращает {symbol: create_time_ms} — MEXC, как и Bybit, хранит
    точное время создания позиции (поле createTime), поэтому funding можно
    посчитать именно с этого момента, а не приближённо.
    """
    base_url = "https://api.mexc.com"
    api_key, api_secret = api_key.strip(), api_secret.strip()
    timestamp = str(int(time.time() * 1000))

    sig = _mexc_sign(api_key, api_secret, timestamp, [])
    headers = {"ApiKey": api_key, "Request-Time": timestamp, "Signature": sig}

    resp = requests.get(f"{base_url}/api/v1/private/position/open_positions", headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"MEXC open_positions error {data.get('code')}: {data.get('message') or data}")

    items = data.get("data") or []
    return {
        p["symbol"]: int(p["createTime"])
        for p in items if float(p.get("holdVol", 0)) > 0
    }


# ── Получение данных с Gate ───────────────────────────────────────────────────
#
# Авторизация Gate API v4 (своя, отличная от остальных):
#   payload_hash    = HexEncode(SHA512(тело_запроса))  — для GET тело пустое
#   signature_string = "Method\nURL\nQueryString\npayload_hash\nTimestamp"
#   SIGN             = HexEncode(HMAC-SHA512(api_secret, signature_string))
# Заголовки: KEY (api_key), Timestamp (unix-секунды), SIGN.
#
# Эндпоинт: GET /api/v4/futures/{settle}/account_book, type=fund — история
# начислений funding по фьючерсам. settle — валюта расчётов контракта;
# по умолчанию берём usdt (покрывает все USDT-M бессрочные контракты).

def _get_gate_proxies() -> dict | None:
    """Прокси для запросов к Gate — тот же паттерн, что и для Bybit/MEXC."""
    proxy = (
        os.environ.get("GATE_PROXY")
        or os.environ.get("HTTPS_PROXY")
        or os.environ.get("HTTP_PROXY")
    )
    if not proxy:
        return None

    if not proxy.startswith(("http://", "https://")):
        parts = proxy.split(":")
        if len(parts) == 4:
            host, port, user, pwd = parts
            proxy = f"http://{user}:{pwd}@{host}:{port}"
        else:
            proxy = f"http://{proxy}"

    return {"http": proxy, "https": proxy}


def _gate_sign(api_secret: str, method: str, url_path: str,
               query_string: str, payload: str = "") -> tuple[str, str]:
    timestamp = str(time.time())
    payload_hash = hashlib.sha512(payload.encode("utf-8")).hexdigest()
    signature_string = f"{method}\n{url_path}\n{query_string}\n{payload_hash}\n{timestamp}"
    sign = hmac.new(api_secret.encode("utf-8"), signature_string.encode("utf-8"), hashlib.sha512).hexdigest()
    return sign, timestamp


def fetch_gate(api_key: str, api_secret: str,
               start_ms: int, end_ms: int, settle: str = "usdt") -> list:
    """GET /api/v4/futures/{settle}/account_book, type=fund, с пагинацией offset/limit."""
    base_url = "https://api.gateio.ws"
    url_path = f"/api/v4/futures/{settle}/account_book"
    limit    = 1000
    offset   = 0
    all_records = []
    proxies  = _get_gate_proxies()
    if proxies:
        print(f"[Gate DEBUG] запрос через прокси: {proxies['https'].split('@')[-1]}")

    api_key    = api_key.strip()
    api_secret = api_secret.strip()

    start_s = start_ms // 1000
    end_s   = end_ms // 1000

    while True:
        params_list = [
            ("type",   "fund"),
            ("from",   str(start_s)),
            ("to",     str(end_s)),
            ("limit",  str(limit)),
            ("offset", str(offset)),
        ]
        query_string = urllib.parse.urlencode(params_list)

        sig, timestamp = _gate_sign(api_secret, "GET", url_path, query_string)
        headers = {
            "KEY":       api_key,
            "Timestamp": timestamp,
            "SIGN":      sig,
            "Accept":    "application/json",
        }

        url = f"{base_url}{url_path}?{query_string}"
        resp = requests.get(url, headers=headers, timeout=30, proxies=proxies)
        if not resp.ok:
            print(f"[Gate DEBUG] HTTP {resp.status_code}, тело ответа: {resp.text}")
        resp.raise_for_status()
        data = resp.json()

        if isinstance(data, dict) and data.get("label"):
            raise RuntimeError(f"Gate API error {data.get('label')}: {data.get('message')}")

        items = data if isinstance(data, list) else []
        for item in items:
            text = item.get("text", "")
            item["symbol"] = text.split(":")[0] if text else "UNKNOWN"
            item["asset"]  = settle.upper()
        all_records.extend(items)

        if len(items) < limit:
            break
        offset += limit

    return all_records


def fetch_gate_open_symbols(api_key: str, api_secret: str, settle: str = "usdt") -> set:
    """GET /api/v4/futures/{settle}/positions — набор контрактов с ненулевым размером."""
    base_url = "https://api.gateio.ws"
    url_path = f"/api/v4/futures/{settle}/positions"
    proxies = _get_gate_proxies()
    api_key, api_secret = api_key.strip(), api_secret.strip()

    sig, timestamp = _gate_sign(api_secret, "GET", url_path, "")
    headers = {"KEY": api_key, "Timestamp": timestamp, "SIGN": sig, "Accept": "application/json"}

    resp = requests.get(f"{base_url}{url_path}", headers=headers, timeout=30, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("label"):
        raise RuntimeError(f"Gate positions error {data.get('label')}: {data.get('message')}")

    items = data if isinstance(data, list) else []
    return {p["contract"] for p in items if float(p.get("size", 0)) != 0}


# ── Опрос всех подключённых бирж за произвольный период ──────────────────────

def fetch_all(secrets: dict, start_ms: int, end_ms: int) -> dict:
    """
    Опрашивает все биржи за период [start_ms, end_ms).
    Aster опрашивается всегда, остальные — только если для них есть секреты.
    Возвращает dict: {"aster": (records, error), "bybit": (...), ...}
    Ошибки отдельных бирж не прерывают опрос остальных.
    """
    results: dict = {}

    try:
        aster_records = fetch_aster(
            user        = secrets["user"],
            signer      = secrets["signer"],
            private_key = secrets["signer_private_key"],
            start_ms    = start_ms,
            end_ms      = end_ms,
        )
        results["aster"] = (aster_records, None)
    except Exception as e:
        print(f"[Aster] Ошибка: {e}")
        results["aster"] = (None, str(e))

    if "bybit_api_key" in secrets:
        try:
            records = fetch_bybit(
                api_key    = secrets["bybit_api_key"],
                api_secret = secrets["bybit_api_secret"],
                start_ms   = start_ms,
                end_ms     = end_ms,
            )
            results["bybit"] = (records, None)
        except Exception as e:
            print(f"[Bybit] Ошибка: {e}")
            results["bybit"] = (None, str(e))

    if "lighter_account_index" in secrets:
        try:
            records = fetch_lighter(
                account_index = secrets["lighter_account_index"],
                auth_token    = secrets["lighter_auth_token"],
                start_ms      = start_ms,
                end_ms        = end_ms,
            )
            results["lighter"] = (records, None)
        except Exception as e:
            print(f"[Lighter] Ошибка: {e}")
            results["lighter"] = (None, str(e))

    if "mexc_api_key" in secrets:
        try:
            records = fetch_mexc(
                api_key    = secrets["mexc_api_key"],
                api_secret = secrets["mexc_api_secret"],
                start_ms   = start_ms,
                end_ms     = end_ms,
            )
            results["mexc"] = (records, None)
        except Exception as e:
            print(f"[MEXC] Ошибка: {e}")
            results["mexc"] = (None, str(e))

    if "gate_api_key" in secrets:
        try:
            records = fetch_gate(
                api_key    = secrets["gate_api_key"],
                api_secret = secrets["gate_api_secret"],
                start_ms   = start_ms,
                end_ms     = end_ms,
            )
            results["gate"] = (records, None)
        except Exception as e:
            print(f"[Gate] Ошибка: {e}")
            results["gate"] = (None, str(e))

    return results


# ── Funding по всем открытым сейчас позициям (за всё доступное время) ────────

# Как далеко в прошлое "копаем" в поисках funding по текущим открытым
# позициям. Большинство бирж не дают запросить весь диапазон разом (см.
# _EXCHANGE_WINDOW_DAYS ниже) — приходится разбивать на окна, а раз так,
# разумно ограничить и общую глубину, чтобы не делать сотни запросов.
# Если позиция держится дольше — увеличьте это число.
OPEN_POSITIONS_LOOKBACK_DAYS = 180

# Максимальный диапазон дат в одном запросе истории funding — ограничение
# самой биржи (не наше). Bybit/Aster подтверждено документацией и/или
# реальной ошибкой 400; для Lighter точных цифр в доках нет, взято с запасом.
_EXCHANGE_WINDOW_DAYS = {
    "aster": 7,
    "bybit": 7,
    "lighter": 30,
    "mexc": 90,
    "gate": 30,
}

# Некоторые биржи (подтверждено для Gate реальной ошибкой 400 "from time
# exceeds 180-day limit") ограничивают не только диапазон ОДНОГО запроса,
# но и то, насколько глубоко в прошлое вообще можно заглянуть — независимо
# от OPEN_POSITIONS_LOOKBACK_DAYS. Берём на 1 день меньше официального
# лимита биржи "про запас": пока опрашиваются другие биржи (это может
# занять заметное время — например, десятки запросов к Bybit), реальное
# "сейчас" успевает сдвинуться вперёд, и без запаса легко вылезти за
# границу лимита по факту исполнения запроса, а не по факту его расчёта.
_EXCHANGE_MAX_LOOKBACK_DAYS = {
    "gate": 179,
}


def _fetch_in_windows(fetch_fn, window_days: int, start_ms: int, end_ms: int) -> list:
    """
    Вызывает fetch_fn(chunk_start_ms, chunk_end_ms) кусками не длиннее
    window_days дней подряд и объединяет результаты в один список.
    Нужно из-за ограничений бирж на максимальный диапазон дат в одном
    запросе истории (см. _EXCHANGE_WINDOW_DAYS).
    """
    window_ms = window_days * 24 * 60 * 60 * 1000
    all_records: list = []
    cur_start = start_ms
    while cur_start < end_ms:
        cur_end = min(cur_start + window_ms, end_ms)
        all_records.extend(fetch_fn(cur_start, cur_end))
        cur_start = cur_end
    return all_records


# Funding у перпетуалов на всех крупных биржах идёт не реже раза в 8 часов
# (стандартный интервал); чаще — бывает (1ч/4ч при повышенной волатильности),
# реже — нет. Поэтому разрыв заметно больше 8 часов между двумя соседними
# начислениями по одному символу — надёжный признак закрытия позиции в этот
# промежуток (funding не начисляется, когда позиции нет), а не просто смены
# частоты начислений (та бывает чаще, не реже, так что 8ч-порог её не ловит).
CONTINUOUS_FUNDING_GAP_HOURS = 16

# Отдельный, куда более щедрый порог — только для подстраховки Bybit/MEXC от
# "залипшего" createdTime (см. докстринг _trim_open_position_records). Здесь
# нам не нужно ловить закрытие позиции с точностью до одного пропущенного
# начисления (в отличие от Aster/Lighter/Gate) — реальный момент открытия и
# так известен точно; эта обрезка должна сработать только на действительно
# древнем "хвосте" (счёт на месяцы/годы), а не на паре часов задержки первой
# записи после настоящего открытия.
STALE_CREATEDTIME_GAP_HOURS = 72


def _trim_to_continuous_run(records: list, time_field: str, time_unit: str,
                             gap_threshold_hours: float = CONTINUOUS_FUNDING_GAP_HOURS,
                             debug_label: str | None = None) -> list:
    """
    Для записей funding ОДНОГО символа: сортирует по времени (от новых
    к старым) и оставляет только непрерывную цепочку начислений от самой
    последней записи назад. Как только разрыв между двумя соседними по
    времени начислениями превышает gap_threshold_hours — считаем, что до
    этого момента позиция была закрыта, и дальше в прошлое не берём.

    Порог фиксированный (не подстраивается под данные) — см. комментарий
    к CONTINUOUS_FUNDING_GAP_HOURS про то, почему это безопаснее адаптивного
    порога (не путает смену частоты начислений с закрытием позиции).

    time_unit: "ms" или "s" — единицы измерения значения в time_field.

    debug_label — если задан, при реальной обрезке (не при "и так всё
    подряд") печатает в лог, где именно нашёлся разрыв и сколько записей
    отрезано — чтобы расследовать похожие случаи не гаданием по скриншоту
    графика, а по факту из лога (см. историю с Bybit createdTime).
    """
    def _time_ms(r):
        raw = r.get(time_field)
        if raw is None:
            return None
        val = int(float(raw))
        return val * 1000 if time_unit == "s" else val

    timed = [(_time_ms(r), r) for r in records]
    timed = [(t, r) for t, r in timed if t is not None]
    if not timed:
        return []
    timed.sort(key=lambda x: x[0], reverse=True)  # от самых новых к старым
    if len(timed) == 1:
        return [timed[0][1]]

    threshold_ms = gap_threshold_hours * 60 * 60 * 1000
    result = [timed[0][1]]
    for i in range(len(timed) - 1):
        gap = timed[i][0] - timed[i + 1][0]
        if gap > threshold_ms:
            if debug_label:
                cut_after = datetime.fromtimestamp(timed[i][0] / 1000, tz=timezone.utc)
                cut_before = datetime.fromtimestamp(timed[i + 1][0] / 1000, tz=timezone.utc)
                print(
                    f"[trim/{debug_label}] Найден разрыв {gap / 3600000:.1f}ч "
                    f"(порог {gap_threshold_hours}ч) между {cut_before.strftime('%Y-%m-%d %H:%M')} UTC "
                    f"и {cut_after.strftime('%Y-%m-%d %H:%M')} UTC — оставляю только {len(result)} "
                    f"запись(ей) из {len(timed)} (более старые отрезаны)."
                )
            break
        result.append(timed[i + 1][1])
    return result


def _trim_open_position_records(records: list, symbol_field: str,
                                 time_field: str, time_unit: str,
                                 gap_threshold_hours: float = CONTINUOUS_FUNDING_GAP_HOURS,
                                 debug_label: str | None = None) -> list:
    """
    Группирует записи funding по символу и для каждого символа отдельно
    оставляет только непрерывную цепочку начислений (см. _trim_to_continuous_run).

    gap_threshold_hours по умолчанию — CONTINUOUS_FUNDING_GAP_HOURS (16ч),
    рассчитанный на биржи БЕЗ точного времени открытия (Aster, Lighter,
    Gate), где разрыв — единственный способ понять, что позиция была
    закрыта, и его нужно ловить как можно точнее.

    Для Bybit/MEXC, где createdTime и так даёт точное время открытия,
    вызывающий код передаёт сюда более щедрый порог (см. вызовы ниже) —
    там эта обрезка нужна только как подстраховка от "залипшего" (очень
    старого) createdTime, а не для определения момента открытия с нуля.
    Обычный 16-часовой порог для этой цели слишком строгий: любая мелкая
    задержка биржи с первым начислением после реального открытия (что
    случается на практике) ошибочно read-ается как "до этого позиции не
    было" и срезает законное начало истории.

    debug_label — см. _trim_to_continuous_run; здесь дополняется символом
    (например "Bybit/SIRENUSDT"), чтобы в логе было видно, какого именно
    символа касается срез.
    """
    by_symbol: dict = defaultdict(list)
    for r in records:
        by_symbol[r.get(symbol_field)].append(r)

    trimmed: list = []
    for symbol, sym_records in by_symbol.items():
        label = f"{debug_label}/{symbol}" if debug_label else None
        trimmed.extend(_trim_to_continuous_run(sym_records, time_field, time_unit, gap_threshold_hours, debug_label=label))
    return trimmed


def fetch_all_time_open_positions(secrets: dict) -> dict:
    """
    Для каждой подключённой биржи:
      1) получает список символов с открытой сейчас позицией;
      2) забирает funding fee и оставляет только записи по этим символам.

    Точность различается по биржам:
      - Bybit, MEXC — ТОЧНО по времени: биржа отдаёт время открытия
        позиции (createdTime/createTime), funding считается именно с
        этого момента.
      - Aster, Lighter, Gate — ТОЧНО по непрерывности начислений: биржа
        не отдаёт время открытия позиции в API, поэтому берётся история
        funding за последние OPEN_POSITIONS_LOOKBACK_DAYS дней (потолок
        глубины поиска) и из неё оставляется только непрерывная цепочка
        начислений от самого последнего назад — как только между двумя
        соседними по времени начислениями обнаруживается разрыв заметно
        больше обычного интервала, считаем, что до этого момента позиция
        была закрыта (см. _trim_to_continuous_run).

    Если на бирже сейчас нет ни одной открытой позиции — биржа просто
    не попадает в результат (как будто не была запрошена), чтобы не
    засорять отчёт пустыми секциями.
    Формат результата — тот же, что и у fetch_all(): {"aster": (records, error), ...}
    """
    now_ms = int(time.time() * 1000)
    lookback_start_ms = now_ms - OPEN_POSITIONS_LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    results: dict = {}

    if "user" in secrets:
        try:
            open_symbols = fetch_aster_open_symbols(
                user=secrets["user"], signer=secrets["signer"],
                private_key=secrets["signer_private_key"],
            )
            if open_symbols:
                records = _fetch_in_windows(
                    lambda s, e: fetch_aster(
                        user=secrets["user"], signer=secrets["signer"],
                        private_key=secrets["signer_private_key"], start_ms=s, end_ms=e,
                    ),
                    _EXCHANGE_WINDOW_DAYS["aster"], lookback_start_ms, now_ms,
                )
                records = [r for r in records if r.get("symbol") in open_symbols]
                records = _trim_open_position_records(
                    records, symbol_field="symbol", time_field="time", time_unit="ms",
                )
                results["aster"] = (records, None)
        except Exception as e:
            print(f"[Aster/positions] Ошибка: {e}")
            results["aster"] = (None, str(e))

    if "bybit_api_key" in secrets:
        try:
            open_times = fetch_bybit_open_symbols(secrets["bybit_api_key"], secrets["bybit_api_secret"])
            if open_times:
                # Bybit отдаёт точное время открытия — начинаем именно с него,
                # а не с общего lookback-окна (там, где оно раньше, разумеется).
                # НО: на практике createdTime не всегда сбрасывается при полном
                # закрытии и повторном открытии позиции по тому же символу и
                # может указывать на дату из далёкого прошлого (см. подстраховку
                # ниже) — если довериться ему слепо, отсюда же получаем и лавину
                # запросов к Bybit (по 7-дневным окнам вплоть до этой древней
                # даты), упирающуюся в лимиты/таймауты прокси ещё ДО того, как
                # подстраховка успевает что-то обрезать. Поэтому дополнительно
                # не уходим глубже общего lookback_start_ms — так же, как уже
                # сделано для Aster/Lighter/Gate.
                earliest_ms = min(open_times.values())
                fetch_start_ms = max(min(earliest_ms, now_ms), lookback_start_ms)
                print(
                    "[Bybit/positions] createdTime по символам: " +
                    ", ".join(
                        f"{sym}={datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
                        for sym, ts in open_times.items()
                    ) + f"; запрашиваю с {datetime.fromtimestamp(fetch_start_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
                )
                records = _fetch_in_windows(
                    lambda s, e: fetch_bybit(
                        api_key=secrets["bybit_api_key"], api_secret=secrets["bybit_api_secret"],
                        start_ms=s, end_ms=e,
                    ),
                    _EXCHANGE_WINDOW_DAYS["bybit"], fetch_start_ms, now_ms,
                )
                print(f"[Bybit/positions] Получено сырых записей: {len(records)}")
                records = [
                    r for r in records
                    if r.get("symbol") in open_times
                    and int(r.get("transactionTime", 0)) >= open_times[r["symbol"]]
                ]
                print(f"[Bybit/positions] После фильтра по символу+createdTime: {len(records)}")
                # Подстраховка сверх фильтра по createdTime: та же ситуация
                # ("залипшее" createdTime) может пропустить в результат старые,
                # не относящиеся к текущей позиции записи — обрезаем ещё и по
                # непрерывности начислений (тот же приём, что и для
                # Aster/Lighter/Gate, где createdTime вовсе не отдаётся). Если
                # он не понадобится (когда createdTime корректен), результат не
                # изменится, а если понадобится — вырежет ложную старую историю.
                records = _trim_open_position_records(
                    records, symbol_field="symbol", time_field="transactionTime", time_unit="ms",
                    gap_threshold_hours=STALE_CREATEDTIME_GAP_HOURS, debug_label="Bybit",
                )
                print(f"[Bybit/positions] После обрезки по непрерывности: {len(records)}")
                results["bybit"] = (records, None)
        except Exception as e:
            print(f"[Bybit/positions] Ошибка: {e}")
            results["bybit"] = (None, str(e))

    if "lighter_account_index" in secrets:
        try:
            open_symbols = fetch_lighter_open_symbols(secrets["lighter_account_index"], secrets["lighter_auth_token"])
            if open_symbols:
                records = _fetch_in_windows(
                    lambda s, e: fetch_lighter(
                        account_index=secrets["lighter_account_index"], auth_token=secrets["lighter_auth_token"],
                        start_ms=s, end_ms=e,
                    ),
                    _EXCHANGE_WINDOW_DAYS["lighter"], lookback_start_ms, now_ms,
                )
                records = [r for r in records if r.get("symbol") in open_symbols]
                records = _trim_open_position_records(
                    records, symbol_field="symbol", time_field="timestamp", time_unit="s",
                )
                results["lighter"] = (records, None)
        except Exception as e:
            print(f"[Lighter/positions] Ошибка: {e}")
            results["lighter"] = (None, str(e))

    if "mexc_api_key" in secrets:
        try:
            open_times = fetch_mexc_open_symbols(secrets["mexc_api_key"], secrets["mexc_api_secret"])
            if open_times:
                # Тот же потолок глубины, что и у Bybit выше, и по той же причине
                # (не доверяем createTime слепо на случай, если он "залип").
                earliest_ms = min(open_times.values())
                fetch_start_ms = max(min(earliest_ms, now_ms), lookback_start_ms)
                print(
                    "[MEXC/positions] createTime по символам: " +
                    ", ".join(
                        f"{sym}={datetime.fromtimestamp(ts / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
                        for sym, ts in open_times.items()
                    ) + f"; запрашиваю с {datetime.fromtimestamp(fetch_start_ms / 1000, tz=timezone.utc).strftime('%Y-%m-%d %H:%M')} UTC"
                )
                records = _fetch_in_windows(
                    lambda s, e: fetch_mexc(
                        api_key=secrets["mexc_api_key"], api_secret=secrets["mexc_api_secret"],
                        start_ms=s, end_ms=e,
                    ),
                    _EXCHANGE_WINDOW_DAYS["mexc"], fetch_start_ms, now_ms,
                )
                print(f"[MEXC/positions] Получено сырых записей: {len(records)}")
                records = [
                    r for r in records
                    if r.get("symbol") in open_times
                    and int(r.get("settleTime", 0)) >= open_times[r["symbol"]]
                ]
                print(f"[MEXC/positions] После фильтра по символу+createTime: {len(records)}")
                # Та же подстраховка, что и для Bybit выше — createTime теоретически
                # может оказаться таким же "залипшим" от старого открытия позиции.
                records = _trim_open_position_records(
                    records, symbol_field="symbol", time_field="settleTime", time_unit="ms",
                    gap_threshold_hours=STALE_CREATEDTIME_GAP_HOURS, debug_label="MEXC",
                )
                print(f"[MEXC/positions] После обрезки по непрерывности: {len(records)}")
                results["mexc"] = (records, None)
        except Exception as e:
            print(f"[MEXC/positions] Ошибка: {e}")
            results["mexc"] = (None, str(e))

    if "gate_api_key" in secrets:
        try:
            open_symbols = fetch_gate_open_symbols(secrets["gate_api_key"], secrets["gate_api_secret"])
            if open_symbols:
                # Свежее "сейчас" на момент непосредственно перед запросом — пока
                # отрабатывали другие биржи (особенно Bybit, там может быть
                # много запросов), время могло заметно сдвинуться вперёд.
                gate_now_ms = int(time.time() * 1000)
                gate_max_days = _EXCHANGE_MAX_LOOKBACK_DAYS.get("gate", OPEN_POSITIONS_LOOKBACK_DAYS)
                gate_lookback_days = min(OPEN_POSITIONS_LOOKBACK_DAYS, gate_max_days)
                gate_start_ms = gate_now_ms - gate_lookback_days * 24 * 60 * 60 * 1000

                records = _fetch_in_windows(
                    lambda s, e: fetch_gate(
                        api_key=secrets["gate_api_key"], api_secret=secrets["gate_api_secret"],
                        start_ms=s, end_ms=e,
                    ),
                    _EXCHANGE_WINDOW_DAYS["gate"], gate_start_ms, gate_now_ms,
                )
                records = [r for r in records if r.get("symbol") in open_symbols]
                records = _trim_open_position_records(
                    records, symbol_field="symbol", time_field="time", time_unit="s",
                )
                results["gate"] = (records, None)
        except Exception as e:
            print(f"[Gate/positions] Ошибка: {e}")
            results["gate"] = (None, str(e))

    return results


def build_open_positions_report(results: dict) -> str:
    """
    Аналог build_report(), но без привязки к датам — заголовок и структура
    под "суммарно по открытым сейчас позициям". Переиспользует _section_lines,
    поэтому агрегация по активам/символам ведётся тем же кодом, что и в
    обычном отчёте.
    """
    combined = [
        "📈 Funding по всем открытым позициям (с момента открытия каждой позиции)",
    ]
    field_map = {
        "aster":   ("Aster",   "income",  "symbol", "asset"),
        "bybit":   ("Bybit",   "funding", "symbol", "currency"),
        "lighter": ("Lighter", "change",  "symbol", "asset"),
        "mexc":    ("MEXC",    "funding", "symbol", "asset"),
        "gate":    ("Gate",    "change",  "symbol", "asset"),
    }

    all_totals: dict = defaultdict(float)
    any_section = False
    for key, (label, income_field, symbol_field, asset_field) in field_map.items():
        if key not in results:
            continue
        records, error = results[key]
        any_section = True
        combined.append("")
        lines, totals = _section_lines(label, records, error, income_field, symbol_field, asset_field)
        combined.extend(lines)
        for asset, val in totals.items():
            all_totals[asset] += val

    if not any_section:
        combined.append("Сейчас нет ни одной открытой позиции ни на одной подключённой бирже.")
        return "\n".join(combined)

    if all_totals:
        combined.append("")
        combined.append("💰 Итого по всем биржам:")
        for asset, val in sorted(all_totals.items()):
            emoji = "🟢" if val >= 0 else "🔴"
            combined.append(f"  {emoji} {asset}: {val:+.4f}")

    return "\n".join(combined)


# ── Формирование отчёта ───────────────────────────────────────────────────────

def _fmt_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _section_lines(exchange: str,
                   records: list | None,
                   error: str | None,
                   income_field: str,
                   symbol_field: str,
                   asset_field: str) -> tuple[list[str], dict]:
    """
    Возвращает (строки_секции, итоги_по_активам).
    error != None → секция об ошибке, пустой итог.
    """
    lines = [f"── {exchange} ──"]
    totals: dict = defaultdict(float)

    if error:
        lines.append(f"❌ Ошибка при получении данных")
        return lines, totals

    if not records:
        lines.append("Выплат за период не было")
        return lines, totals

    by_symbol: dict = defaultdict(float)
    for r in records:
        sym    = r.get(symbol_field, "UNKNOWN")
        asset  = r.get(asset_field,  "USDT")
        income = float(r.get(income_field, 0))
        by_symbol[sym]   += income
        totals[asset]    += income

    for sym, val in sorted(by_symbol.items(), key=lambda x: -abs(x[1])):
        emoji = "🟢" if val >= 0 else "🔴"
        lines.append(f"{emoji} {sym}: {val:+.4f}")

    for asset, val in sorted(totals.items()):
        emoji = "🟢" if val >= 0 else "🔴"
        lines.append(f"{emoji} Итого {exchange} ({asset}): {val:+.4f}")

    return lines, dict(totals)


def build_report(start_ms: int, end_ms: int,
                 aster_records: list | None, aster_error: str | None,
                 bybit_records: list | None, bybit_error: str | None,
                 lighter_records: list | None = None, lighter_error: str | None = None,
                 mexc_records: list | None = None, mexc_error: str | None = None,
                 gate_records: list | None = None, gate_error: str | None = None) -> str:

    header = [
        "📊 Отчёт по funding fee",
        f"Период: {_fmt_ms(start_ms)} — {_fmt_ms(end_ms)} UTC",
        "",
    ]

    aster_lines, aster_totals = _section_lines(
        "Aster", aster_records, aster_error,
        income_field="income", symbol_field="symbol", asset_field="asset",
    )

    combined: list[str] = header + aster_lines

    # Bybit — только если запрашивался
    bybit_totals: dict = {}
    if bybit_records is not None or bybit_error is not None:
        combined.append("")
        bybit_lines, bybit_totals = _section_lines(
            "Bybit", bybit_records, bybit_error,
            income_field="funding", symbol_field="symbol", asset_field="currency",
        )
        combined.extend(bybit_lines)

    # Lighter — только если запрашивался
    lighter_totals: dict = {}
    if lighter_records is not None or lighter_error is not None:
        combined.append("")
        lighter_lines, lighter_totals = _section_lines(
            "Lighter", lighter_records, lighter_error,
            income_field="change", symbol_field="symbol", asset_field="asset",
        )
        combined.extend(lighter_lines)

    # MEXC — только если запрашивался
    mexc_totals: dict = {}
    if mexc_records is not None or mexc_error is not None:
        combined.append("")
        mexc_lines, mexc_totals = _section_lines(
            "MEXC", mexc_records, mexc_error,
            income_field="funding", symbol_field="symbol", asset_field="asset",
        )
        combined.extend(mexc_lines)

    # Gate — только если запрашивался
    gate_totals: dict = {}
    if gate_records is not None or gate_error is not None:
        combined.append("")
        gate_lines, gate_totals = _section_lines(
            "Gate", gate_records, gate_error,
            income_field="change", symbol_field="symbol", asset_field="asset",
        )
        combined.extend(gate_lines)

    # Итог по всем биржам
    if bybit_totals or aster_totals or lighter_totals or mexc_totals or gate_totals:
        combined.append("")
        combined.append("💰 Итого по всем биржам:")
        all_assets: set = (
            set(aster_totals) | set(bybit_totals) | set(lighter_totals)
            | set(mexc_totals) | set(gate_totals)
        )
        for asset in sorted(all_assets):
            total = (
                aster_totals.get(asset, 0.0)
                + bybit_totals.get(asset, 0.0)
                + lighter_totals.get(asset, 0.0)
                + mexc_totals.get(asset, 0.0)
                + gate_totals.get(asset, 0.0)
            )
            emoji = "🟢" if total >= 0 else "🔴"
            combined.append(f"  {emoji} {asset}: {total:+.4f}")

    return "\n".join(combined)


# ── Отправка в Telegram ───────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url  = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=30)
    resp.raise_for_status()


# ── Точка входа ───────────────────────────────────────────────────────────────

def main():
    secrets  = load_secrets()
    start_ms, end_ms = previous_day_msk_ms()

    results = fetch_all(secrets, start_ms, end_ms)

    message = build_report(
        start_ms, end_ms,
        *results.get("aster",   (None, None)),
        *results.get("bybit",   (None, None)),
        *results.get("lighter", (None, None)),
        *results.get("mexc",    (None, None)),
        *results.get("gate",    (None, None)),
    )

    send_telegram(secrets["telegram_token"], secrets["telegram_chat_id"], message)
    print("Отправлено в Telegram:")
    print(message)


if __name__ == "__main__":
    main()
