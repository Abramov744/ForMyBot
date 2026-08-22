#!/usr/bin/env python3
"""
Funding Fee Daily Report — Aster + Bybit + Lighter → Telegram
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
    now_msk = datetime.now(MSK)
    today_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    start_msk = today_msk - timedelta(days=1)
    end_msk = today_msk
    return int(start_msk.timestamp() * 1000), int(end_msk.timestamp() * 1000)


# ── Загрузка секретов ─────────────────────────────────────────────────────────

def load_secrets() -> dict:
    """Читает секреты из переменных окружения (GitHub Secrets)."""
    result = {
        "user": os.environ["ASTER_USER_ADDRESS"],
        "signer": os.environ["ASTER_SIGNER_ADDRESS"],
        "signer_private_key": os.environ["ASTER_SIGNER_PRIVATE_KEY"],
        "telegram_token": os.environ["TELEGRAM_BOT_TOKEN"],
        "telegram_chat_id": os.environ["TELEGRAM_CHAT_ID"],
    }

    # Bybit — опционально
    bybit_api_key = os.environ.get("BYBIT_API_KEY")
    bybit_api_secret = os.environ.get("BYBIT_API_SECRET")
    if bybit_api_key and bybit_api_secret:
        result["bybit_api_key"] = bybit_api_key
        result["bybit_api_secret"] = bybit_api_secret

    # Lighter — опционально
    lighter_account_index = os.environ.get("LIGHTER_ACCOUNT_INDEX")
    lighter_auth_token = os.environ.get("LIGHTER_AUTH_TOKEN")
    if lighter_account_index and lighter_auth_token:
        result["lighter_account_index"] = lighter_account_index
        result["lighter_auth_token"] = lighter_auth_token

    return result


# ── Aster EIP-712 подпись ─────────────────────────────────────────────────────

_TYPED_DATA_TEMPLATE = {
    "types": {
        "EIP712Domain": [
            {"name": "name", "type": "string"},
            {"name": "version", "type": "string"},
            {"name": "chainId", "type": "uint256"},
            {"name": "verifyingContract", "type": "address"},
        ],
        "Message": [{"name": "msg", "type": "string"}],
    },
    "primaryType": "Message",
    "domain": {
        "name": "AsterSignTransaction",
        "version": "1",
        "chainId": 1666,
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
            "startTime": str(cur_start),
            "endTime": str(end_ms),
            "limit": str(limit),
            "nonce": str(nonce),
            "user": user,
            "signer": signer,
        }
        param_str = urllib.parse.urlencode(params)
        sig = _aster_sign(param_str, private_key)
        url = f"https://fapi.asterdex.com/fapi/v3/income?{param_str}&signature={sig}"

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

def _bybit_sign(api_key: str, api_secret: str,
                 timestamp: str, recv_window: str, query_string: str) -> str:
    payload = f"{timestamp}{api_key}{recv_window}{query_string}"
    return hmac.new(api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def fetch_bybit(api_key: str, api_secret: str,
                 start_ms: int, end_ms: int) -> list:
    """GET /v5/account/transaction-log, type=SETTLEMENT, с пагинацией."""
    base_url = "https://api.bybit.com"
    recv_window = "5000"
    all_records = []
    cursor = None

    while True:
        timestamp = str(int(time.time() * 1000))
        params: dict = {
            "accountType": "UNIFIED",
            "category": "linear",
            "type": "SETTLEMENT",
            "startTime": str(start_ms),
            "endTime": str(end_ms),
            "limit": "50",
        }
        if cursor:
            params["cursor"] = cursor

        query_string = urllib.parse.urlencode(params)
        sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, query_string)
        headers = {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-SIGN": sig,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

        resp = requests.get(
            f"{base_url}/v5/account/transaction-log",
            params=params,
            headers=headers,
            timeout=30,
        )
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
# Авторизация: используется read-only auth-токен (создаётся один раз на
# https://app.lighter.xyz/read-only-tokens/, срок действия можно выбрать на годы
# вперёд). Токен передаётся как есть в заголовке Authorization — никакой
# подписи запросов на лету (в отличие от Aster) не требуется.
#
# Эндпоинты (https://mainnet.zklighter.elliot.ai):
#   GET /api/v1/orderBookDetails  — публичный, даёт список рынков (market_id -> symbol)
#   GET /api/v1/positionFunding   — приватный (Authorization), сами выплаты funding
#
# ВАЖНО: пример timestamp в схеме Lighter — 10-значное число (секунды unix,
# не миллисекунды, как у Aster/Bybit). Ниже start_ms/end_ms конвертируются
# в секунды. Если после первого реального запуска отчёт по Lighter окажется
# пустым или, наоборот, вернёт данные за неверный период — стоит распечатать
# сырой ответ (raise/print) и свериться: возможно, сервер всё же ждёт миллисекунды.

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
    end_s = end_ms // 1000

    all_records, limit, cursor = [], 100, None
    headers = {"authorization": auth_token}

    while True:
        params: dict = {
            "account_index": account_index,
            "limit": str(limit),
            "side": "all",
            "start_timestamp": str(start_s),
            "end_timestamp": str(end_s),
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
            item["asset"] = "USDC"  # Lighter торгуется с расчётами в USDC
        all_records.extend(items)

        cursor = data.get("next_cursor") or None
        if not cursor or not items:
            break

    return all_records


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
        sym = r.get(symbol_field, "UNKNOWN")
        asset = r.get(asset_field, "USDT")
        income = float(r.get(income_field, 0))
        by_symbol[sym] += income
        totals[asset] += income

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
                  lighter_records: list | None = None, lighter_error: str | None = None) -> str:
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

    # Итог по всем биржам
    if bybit_totals or aster_totals or lighter_totals:
        combined.append("")
        combined.append("💰 Итого по всем биржам:")
        all_assets: set = set(aster_totals) | set(bybit_totals) | set(lighter_totals)
        for asset in sorted(all_assets):
            total = (
                aster_totals.get(asset, 0.0)
                + bybit_totals.get(asset, 0.0)
                + lighter_totals.get(asset, 0.0)
            )
            emoji = "🟢" if total >= 0 else "🔴"
            combined.append(f"  {emoji} {asset}: {total:+.4f}")

    return "\n".join(combined)


# ── Отправка в Telegram ───────────────────────────────────────────────────────

def send_telegram(token: str, chat_id: str, text: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={"chat_id": chat_id, "text": text}, timeout=30)
    resp.raise_for_status()


# ── Точка входа ───────────────────────────────────────────────────────────────

def main():
    secrets = load_secrets()
    start_ms, end_ms = previous_day_msk_ms()

    # Aster
    aster_records, aster_error = None, None
    try:
        aster_records = fetch_aster(
            user=secrets["user"],
            signer=secrets["signer"],
            private_key=secrets["signer_private_key"],
            start_ms=start_ms,
            end_ms=end_ms,
        )
    except Exception as e:
        aster_error = str(e)
        print(f"[Aster] Ошибка: {e}")

    # Bybit (опционально)
    bybit_records, bybit_error = None, None
    if "bybit_api_key" in secrets:
        try:
            bybit_records = fetch_bybit(
                api_key=secrets["bybit_api_key"],
                api_secret=secrets["bybit_api_secret"],
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except Exception as e:
            bybit_error = str(e)
            print(f"[Bybit] Ошибка: {e}")

    # Lighter (опционально)
    lighter_records, lighter_error = None, None
    if "lighter_account_index" in secrets:
        try:
            lighter_records = fetch_lighter(
                account_index=secrets["lighter_account_index"],
                auth_token=secrets["lighter_auth_token"],
                start_ms=start_ms,
                end_ms=end_ms,
            )
        except Exception as e:
            lighter_error = str(e)
            print(f"[Lighter] Ошибка: {e}")

    message = build_report(
        start_ms, end_ms,
        aster_records, aster_error,
        bybit_records, bybit_error,
        lighter_records, lighter_error,
    )

    send_telegram(secrets["telegram_token"], secrets["telegram_chat_id"], message)
    print("Отправлено в Telegram:")
    print(message)


if __name__ == "__main__":
    main()
