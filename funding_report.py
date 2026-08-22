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
