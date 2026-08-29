#!/usr/bin/env python3
"""
Сводный баланс по всем биржам/кошелькам/DeFi-протоколам — используется
веб-страницей /balances и командой /balance в Telegram-боте.

Отличие от funding_report.py: там — история funding и открытые ПОЗИЦИИ на
фьючерсах (нужные для самой стратегии), здесь — сколько денег СЕЙЧАС лежит
на каждом источнике целиком, включая споt-баланс (туда обычно уходит долгая
часть капитала — лонг-хедж) и DeFi-позиции, не имеющие отношения к funding.

Источники и как берётся баланс:
  - Bybit  — Unified Trading Account (totalEquity) + Funding-кошелёк + Earn
    (Flexible Savings/стейкинг) + Spot Grid Bot + крипто-займы (net) — у
    Bybit деньги могут лежать в ПЯТИ разных местах одновременно, и каждое
    требует своего запроса (см. докстринги fetch_bybit_*_balance — там же и
    разница в степени уверенности: Unified/Funding проверены на практике,
    Grid Bot и крипто-займы — экспериментально, документация Bybit не даёт
    точных полей ответа).
  - MEXC   — отдельно фьючерсный (тот же API, что и funding_report.fetch_mexc)
    и спот-баланс (другая подпись запроса, см. fetch_mexc_spot_balance).
  - Gate   — отдельно фьючерсный и спот-баланс, обе подписи запросов — тот же
    HMAC-SHA512-механизм, что уже используется в funding_report._gate_sign.
  - Aster  — баланс кошелька + нереализованный PnL открытых позиций
    (positionRisk, поле unRealizedProfit — тот же эндпоинт и то же поле,
    что использует Binance Futures API, форком которого является Aster;
    без PnL баланс не совпадал с реальным при открытых позициях). Споta у
    Aster нет вовсе, см. README.
  - Lighter — collateral (обеспечение) + сумма unrealized_pnl по всем
    открытым позициям — по документации Lighter Total Account Value =
    Collateral + Unrealized PnL, а готового поля с суммой в API нет, считаем
    сами (см. fetch_lighter_balance).
  - Aave v3 — ON-CHAIN, чистая стоимость позиции (обеспечение минус долг)
    через view-функцию Pool.getUserAccountData(address).

Rabby wallet (сканирование ERC-20/нативных балансов по всем EVM-сетям через
Etherscan) и Fluid сознательно НЕ мониторятся:
  - Rabby wallet — полный скан ~60 сетей на каждый вызов был слишком
    медленным (десятки секунд) и на реальном аккаунте ронял отправку в
    Telegram (см. send_telegram — сообщение либо не успевало уложиться в
    таймаут, либо превышало лимит длины сообщения Telegram из-за списка
    "без оценки" по мелкой пыли на редких сетях). Платный DeBank Open API
    решал бы это одним быстрым запросом, но признан слишком дорогим — решили
    вообще убрать сканирование кошелька из мониторинга, а не чинить
    наполовину.
  - Fluid — у него нет простого REST API для чтения позиций, только
    on-chain resolver-контракты со сложным, не до конца документированным
    ABI (вложенные структуры) — разбирать их вслепую для финансового
    инструмента рискованно (см. историю обсуждения в PR).

Оценка стоимости в USD для не-стейблкоинов на споте берётся с бесплатного
публичного CoinGecko API (без ключа, с кэшем на 5 минут) — единственный
источник цен в проекте без отдельного API-ключа под аккаунт пользователя,
поэтому ошибки/лимиты этого API НЕ должны ронять остальную часть отчёта: при
неудаче соответствующая монета просто не попадает в сумму, а не оценивается
наугад (см. price_spot_balances_usd).
"""

import hashlib
import hmac
import json
import os
import time
import urllib.parse

import requests

from entry_price import _etherscan_get  # переиспользуется только для Aave (eth_call через Etherscan proxy)
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

# Символ → id монеты в CoinGecko. Курируемый список самых ходовых монет —
# гарантированно верное соответствие тикер→id (у CoinGecko тикеры НЕ уникальны
# — разных монет с одинаковым символом сотни, поэтому для топовых монет не
# полагаемся на поиск по общему списку ниже, а фиксируем явно). Расширяется
# полным списком монет CoinGecko при необходимости, см. _coingecko_symbol_to_id.
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


_coingecko_coin_list_cache: list | None = None
_coingecko_coin_list_ts = 0.0
COINGECKO_COIN_LIST_TTL_S = 24 * 60 * 60  # полный список тикеров почти не меняется — кэш на сутки


def _coingecko_symbol_to_id(symbol: str) -> str | None:
    """
    Тикер → coingecko-id. Раньше это был только SYMBOL_TO_COINGECKO_ID
    (~24 самых ходовых монет) — любая монета не из этого списка молча
    выпадала из суммы баланса (это и оказалось причиной, почему у MEXC
    "не все суммы учтены": на споте держится что-то за пределами курируемого
    списка). Теперь при промахе по курируемому списку дополнительно ищем по
    ПОЛНОМУ списку монет CoinGecko (/coins/list, кэш на сутки — список тикеров
    почти не меняется). У CoinGecko тикеры не уникальны (сотни монет с
    одинаковым символом) — берём первое совпадение; для по-настоящему топовых
    монет коллизии не страшны, они всегда есть в курируемом списке выше и до
    полного списка дело не доходит.
    """
    symbol = symbol.upper()
    if symbol in SYMBOL_TO_COINGECKO_ID:
        return SYMBOL_TO_COINGECKO_ID[symbol]

    global _coingecko_coin_list_cache, _coingecko_coin_list_ts
    now = time.time()
    if _coingecko_coin_list_cache is None or now - _coingecko_coin_list_ts > COINGECKO_COIN_LIST_TTL_S:
        data = _coingecko_get("/coins/list", {})
        if isinstance(data, list):
            _coingecko_coin_list_cache = data
            _coingecko_coin_list_ts = now
        elif _coingecko_coin_list_cache is None:
            return None  # не удалось скачать список, и кэша ещё нет — сдаёмся

    for c in _coingecko_coin_list_cache:
        if str(c.get("symbol", "")).upper() == symbol:
            return c.get("id")
    return None


def price_spot_balances_usd(balances: dict) -> float:
    """
    {ASSET: amount} → суммарная оценка в USD. Стейблкоины — по номиналу без
    обращения к CoinGecko (см. STABLECOIN_SYMBOLS), остальное — батч-запросом
    через _coingecko_symbol_to_id/_coingecko_simple_prices. Монета, для
    которой цена не нашлась вообще нигде — просто не попадает в сумму (не
    оценивается наугад), но с полным списком тикеров CoinGecko такое должно
    быть редкостью — используется MEXC/Gate-спотом (fetch_all_balances) и
    Bybit Funding-кошельком.
    """
    total = 0.0
    ids_by_symbol = {}
    for sym, amt in balances.items():
        if sym.upper() in STABLECOIN_SYMBOLS:
            total += amt
            continue
        cid = _coingecko_symbol_to_id(sym)
        if cid:
            ids_by_symbol[sym] = cid

    if ids_by_symbol:
        prices = _coingecko_simple_prices(set(ids_by_symbol.values()))
        for sym, cid in ids_by_symbol.items():
            price = prices.get(cid)
            if price is not None:
                total += balances[sym] * price

    return total


# ── Биржи ──────────────────────────────────────────────────────────────────────

def _bybit_signed_get(path: str, params_list: list, api_key: str, api_secret: str) -> dict:
    base_url = "https://api.bybit.com"
    recv_window = "5000"
    api_key, api_secret = api_key.strip(), api_secret.strip()

    timestamp = str(int(time.time() * 1000))
    query_string = urllib.parse.urlencode(params_list)
    sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, query_string)
    headers = {
        "X-BAPI-API-KEY": api_key, "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": recv_window,
    }
    resp = requests.get(f"{base_url}{path}?{query_string}", headers=headers, timeout=30, proxies=_get_proxies())
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit {path} error {data.get('retCode')}: {data.get('retMsg')}")
    return data


def _bybit_signed_post(path: str, body: dict, api_key: str, api_secret: str) -> dict:
    """
    POST-вариант _bybit_signed_get — нужен для Trading Bot API (список ботов
    и детали конкретного бота отдаются только через POST). Формула подписи
    та же самая (funding_report._bybit_sign): вместо query_string подписывается
    JSON-тело запроса КАК ЕСТЬ (та же строка, что отправляется в body) —
    поэтому важно сериализовать json.dumps один раз и переиспользовать
    строку и для подписи, и для самого запроса.
    """
    base_url = "https://api.bybit.com"
    recv_window = "5000"
    api_key, api_secret = api_key.strip(), api_secret.strip()

    timestamp = str(int(time.time() * 1000))
    body_str = json.dumps(body)
    sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, body_str)
    headers = {
        "X-BAPI-API-KEY": api_key, "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json",
    }
    resp = requests.post(f"{base_url}{path}", headers=headers, data=body_str, timeout=30, proxies=_get_proxies())
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit {path} error {data.get('retCode')}: {data.get('retMsg')}")
    return data


def fetch_bybit_earn_balance(api_key: str, api_secret: str) -> tuple[float, list]:
    """
    GET /v5/earn/position, отдельно category=FlexibleSaving и category=OnChain
    (стейкинг) — category обязателен, одним запросом оба сразу не получить.
    Поле amount — принципал позиции в единицах самой монеты (не в USD),
    оцениваем через price_spot_balances_usd. Накопленные, но ещё не
    зачисленные проценты (claimableYield и т.п.) сознательно не считаем —
    это не деньги "на счету", а то, что можно ЗАБРАТЬ, разумно исключить,
    чтобы не завышать баланс тем, чего ещё физически нет.

    Возвращает (сумма, [текст ошибки по каждой упавшей категории]) — раньше
    ошибка только печаталась в лог Railway и была не видна ни на /balances,
    ни в Telegram, из-за чего расследовать "почему 0" можно было только по
    логам. Теперь текст ошибки идёт вместе с числом дальше в отчёт.
    """
    coins: dict = {}
    errors = []
    for category in ("FlexibleSaving", "OnChain"):
        try:
            data = _bybit_signed_get("/v5/earn/position", [("category", category)], api_key, api_secret)
        except Exception as e:
            errors.append(f"{category}: {e}")
            continue
        for pos in data.get("result", {}).get("list", []):
            coin = pos.get("coin")
            try:
                amount = float(pos.get("amount", 0) or 0)
            except (TypeError, ValueError):
                continue
            if coin and amount > 0:
                coins[coin] = coins.get(coin, 0.0) + amount
    return price_spot_balances_usd(coins), errors


def fetch_bybit_grid_bot_balance(api_key: str, api_secret: str) -> tuple[float, str]:
    """
    Средства в запущенном Spot Grid Bot — ОТДЕЛЬНЫЙ от Unified/Funding пул:
    при создании бота Bybit сам переводит нужную сумму ИЗ Funding-кошелька
    под управление бота, поэтому без этого запроса такие деньги не видны
    вообще нигде в остальном балансе (см. README).

    ЭКСПЕРИМЕНТАЛЬНО, ниже уверенность, чем у остального кода в этом файле:
    официальная документация Bybit не даёт готового примера ответа с суммой
    инвестиций бота — только что она вообще где-то в структуре ответа
    детального эндпоинта есть. Поэтому: 1) список запущенных ботов — через
    POST /v5/botsummary/list-all-bots (status=0 — только запущенные), тип
    BOT_TYPE_ENUM_GRID_SPOT — фьючерсные грид/мартингейл-боты НЕ считаем: их
    маржа остаётся частью Unified Trading Account (totalEquity), в отличие
    от спот-бота; 2) для каждого найденного бота — POST
    /v5/grid/query-grid-detail с его gridId; 3) сумма инвестиций ищется
    перебором нескольких вероятных имён поля (тот же приём, что и для
    Lighter в fetch_lighter_balance).

    Возвращает (сумма, диагностическая заметка) — заметка заполняется
    ВСЕГДА, не только при ошибке: для этой экспериментальной части "0 и всё
    в порядке" ничем не отличается от "0 из-за того, что не совпало
    угаданное имя поля" без явного объяснения, что именно было найдено —
    именно это и не получилось различить в первый раз (см. историю PR:
    "Spot Grid Bot: $0.00" без ошибки — а был ли бот вообще найден,
    оставалось неизвестным).
    """
    try:
        summary = _bybit_signed_post(
            "/v5/botsummary/list-all-bots",
            {"status": 0, "page": 0, "limit": 50, "type": "BOT_TYPE_ENUM_GRID_SPOT"},
            api_key, api_secret,
        )
    except Exception as e:
        return 0.0, f"не удалось получить список ботов: {e}"

    bots = summary.get("result", {}).get("bots", [])
    if not bots:
        return 0.0, "запущенных Spot Grid ботов не найдено (status=0 — если бот на паузе, а не полностью остановлен, он может не попасть в этот список)"

    total_usd = 0.0
    matched_count = 0
    unmatched_ids = []
    detail_errors = []
    key_dumps = []  # реальные ключи ответа там, где угаданное имя поля не совпало —
                     # чтобы в следующий раз не гадать имя поля, а взять точное
    for bot in bots:
        grid = bot.get("grid", {}) or {}
        grid_id = grid.get("grid_id") or grid.get("gridId")
        if not grid_id:
            unmatched_ids.append("без grid_id в ответе списка")
            if len(key_dumps) < 2:
                inner_keys = f", ключи grid: {sorted(grid.keys())}" if grid else " (ключа grid нет вовсе)"
                key_dumps.append(f"ключи бота: {sorted(bot.keys())}{inner_keys}")
            continue
        try:
            detail = _bybit_signed_post(
                "/v5/grid/query-grid-detail", {"gridId": grid_id}, api_key, api_secret,
            )
        except Exception as e:
            detail_errors.append(f"{grid_id}: {e}")
            continue

        result = detail.get("result", {}) or {}
        d = result.get("detail", result)
        matched = False
        for key in ("totalInvestment", "investment", "totalInvestAmount", "investAmount", "quoteInvestment"):
            if d.get(key) is not None:
                try:
                    total_usd += float(d[key])
                    matched_count += 1
                    matched = True
                    break
                except (TypeError, ValueError):
                    continue
        if not matched:
            unmatched_ids.append(str(grid_id))
            if len(key_dumps) < 2:
                key_dumps.append(f"ключи query-grid-detail для {grid_id}: {sorted(d.keys())}")

    notes = [f"найдено ботов: {len(bots)}"]
    if matched_count:
        notes.append(f"сумма прочитана у {matched_count}")
    if unmatched_ids:
        notes.append(f"не удалось прочитать сумму у {len(unmatched_ids)} ({', '.join(unmatched_ids)}) — вероятно, неверное имя поля в query-grid-detail")
    if key_dumps:
        notes.append(" | ".join(key_dumps))
    if detail_errors:
        notes.append(f"ошибка деталей: {'; '.join(detail_errors)}")
    return total_usd, "; ".join(notes)


def _bybit_extract_loan_position(pos: dict, collateral_coins: dict, debt_coins: dict) -> tuple[bool, bool]:
    """Разбирает одну позицию крипто-займа в collateral_coins/debt_coins
    (мутирует их на месте). Возвращает (распознан_ли_залог, распознан_ли_долг)
    ОТДЕЛЬНО — раньше это был один общий флаг "распознано хоть что-то",
    из-за чего "распознан залог, но не распознан долг" (или наоборот)
    выглядело как полный успех, хотя реальная сумма всё равно была неверной
    (см. реальный ответ: "найдено позиций: 2, суммы прочитаны у 1" — какая
    именно половина не прочиталась, было не видно)."""
    collateral_matched = False
    for key in ("collateralCoin", "collateralCurrency"):
        coin = pos.get(key)
        if coin:
            for amt_key in ("collateralAmount", "collateralQty", "collateralBalance"):
                if pos.get(amt_key) is not None:
                    try:
                        collateral_coins[coin] = collateral_coins.get(coin, 0.0) + float(pos[amt_key])
                        collateral_matched = True
                    except (TypeError, ValueError):
                        pass
                    break
            break
    debt_matched = False
    for key in ("loanCoin", "loanCurrency"):
        coin = pos.get(key)
        if coin:
            for amt_key in ("totalDebt", "loanAmount", "debtAmount", "liability"):
                if pos.get(amt_key) is not None:
                    try:
                        debt_coins[coin] = debt_coins.get(coin, 0.0) + float(pos[amt_key])
                        debt_matched = True
                    except (TypeError, ValueError):
                        pass
                    break
            break
    return collateral_matched, debt_matched


def fetch_bybit_crypto_loan_balance(api_key: str, api_secret: str) -> tuple[float, str]:
    """
    Крипто-займы (залоговое кредитование) — чистая стоимость (залог минус
    долг). У Bybit это ТРИ разных продукта с разными эндпоинтами (общий,
    гибкий, фиксированный срок) — опрашиваются все три, у "общего" (common)
    без параметров, у гибкого (flexible) — список активных займов по
    монетам, у займа с фиксированным сроком (fixed) он один на orderId,
    поэтому без списка активных orderId'ов его отдельно не опросить —
    сознательно пропущен, если есть именно такой займ, чистая позиция будет
    занижена (см. заметку в отчёте, если сумма расходится с реальной).

    ЭКСПЕРИМЕНТАЛЬНО, как и fetch_bybit_grid_bot_balance выше: официальная
    документация не даёт примера JSON-ответа с полями залога/долга, поэтому
    имена полей перебираются по нескольким вероятным вариантам. Залог и долг
    в ответе — предположительно по каждой монете отдельно (не сразу в USD),
    оцениваются через price_spot_balances_usd.

    Возвращает (сумма, диагностическая заметка) — см. докстринг
    fetch_bybit_grid_bot_balance про то, почему заметка нужна даже когда
    ошибки формально нет.
    """
    collateral_coins: dict = {}
    debt_coins: dict = {}
    positions_found = 0
    collateral_matched_count = 0
    debt_matched_count = 0
    endpoint_errors = []
    key_dumps = []  # реальные ключи позиции там, где залог или долг не распознались

    def _pull(path: str, params_list: list):
        nonlocal positions_found, collateral_matched_count, debt_matched_count
        try:
            data = _bybit_signed_get(path, params_list, api_key, api_secret)
        except Exception as e:
            endpoint_errors.append(f"{path}: {e}")
            return
        result = data.get("result", {}) or {}
        positions = result.get("list") if isinstance(result.get("list"), list) else ([result] if result else [])
        for pos in positions:
            if not isinstance(pos, dict) or not pos:
                continue
            positions_found += 1
            collateral_ok, debt_ok = _bybit_extract_loan_position(pos, collateral_coins, debt_coins)
            if collateral_ok:
                collateral_matched_count += 1
            if debt_ok:
                debt_matched_count += 1
            if (not collateral_ok or not debt_ok) and len(key_dumps) < 2:
                key_dumps.append(f"{path}: {sorted(pos.keys())}")

    _pull("/v5/crypto-loan-common/position", [])
    _pull("/v5/crypto-loan-flexible/ongoing-coin", [])

    net = price_spot_balances_usd(collateral_coins) - price_spot_balances_usd(debt_coins)

    if not positions_found and not endpoint_errors:
        note = "открытых займов не найдено (common + flexible; фиксированный срок — не опрашивается, см. докстринг)"
    else:
        notes = []
        if positions_found:
            notes.append(
                f"найдено позиций: {positions_found}, залог распознан у {collateral_matched_count}, "
                f"долг распознан у {debt_matched_count}"
            )
        if key_dumps:
            notes.append(" | ".join(key_dumps))
        if endpoint_errors:
            notes.append("; ".join(endpoint_errors))
        note = "; ".join(notes)
    return net, note


def fetch_bybit_balance(api_key: str, api_secret: str) -> dict:
    """
    Unified Trading Account (totalEquity, /v5/account/wallet-balance) +
    Funding-кошелёк + Earn (Flexible Savings/стейкинг) + Spot Grid Bot +
    крипто-займы (net) — у Bybit это ПЯТЬ разных мест, где могут лежать
    деньги, и каждое требует своего запроса (см. докстринги отдельных
    fetch_bybit_*_balance выше — там же и разница в степени уверенности:
    Unified/Funding проверены на практике, Earn документирован уверенно,
    Grid Bot и крипто-займы — экспериментально).

    Возвращает {"total": float, "parts": {"unified":, "funding":, "earn":,
    "grid_bot":, "crypto_loan":: {"value": float, "error": str|None}}} —
    раньше это была просто float, и ошибка любой отдельной части была видна
    только в логах Railway (fetch_all_balances/build_balances_report/
    /api/balances показывали только итоговую сумму). Разбивка по частям
    нужна именно для того, чтобы разобраться, ПОЧЕМУ конкретная часть дала 0
    — не хватает прав у API-ключа (Bybit выдаёт права на Earn/Bot/Loan
    отдельными переключателями от прав на Wallet/Trade, которых достаточно
    для остального бота), реальный ответ API отличается от того, что здесь
    предполагалось, или там действительно пусто — не отличить одно от
    другого без текста ошибки под рукой.
    """
    unified = _bybit_signed_get(
        "/v5/account/wallet-balance", [("accountType", "UNIFIED")], api_key, api_secret,
    )
    lst = unified.get("result", {}).get("list", [])
    unified_usd = float(lst[0].get("totalEquity", 0) or 0) if lst else 0.0
    parts = {"unified": {"value": unified_usd, "error": None}}

    try:
        funding = _bybit_signed_get(
            "/v5/asset/transfer/query-account-coins-balance", [("accountType", "FUND")], api_key, api_secret,
        )
        funding_coins = {
            c["coin"]: float(c.get("walletBalance", 0) or 0)
            for c in funding.get("result", {}).get("balance", [])
            if float(c.get("walletBalance", 0) or 0) > 0
        }
        parts["funding"] = {"value": price_spot_balances_usd(funding_coins), "error": None}
    except Exception as e:
        parts["funding"] = {"value": 0.0, "error": str(e)}

    try:
        earn_usd, earn_errors = fetch_bybit_earn_balance(api_key, api_secret)
        parts["earn"] = {"value": earn_usd, "error": "; ".join(earn_errors) if earn_errors else None}
    except Exception as e:
        parts["earn"] = {"value": 0.0, "error": str(e)}

    # Grid Bot и крипто-займы — экспериментальные части: диагностическая
    # заметка (note) заполняется ВСЕГДА, а не только при ошибке (см.
    # докстринги fetch_bybit_grid_bot_balance/fetch_bybit_crypto_loan_balance)
    # — "error" тут используется только если сама функция неожиданно упала
    # исключением мимо своего внутреннего try/except.
    try:
        grid_usd, grid_note = fetch_bybit_grid_bot_balance(api_key, api_secret)
        parts["grid_bot"] = {"value": grid_usd, "error": None, "note": grid_note}
    except Exception as e:
        parts["grid_bot"] = {"value": 0.0, "error": str(e), "note": None}

    try:
        loan_usd, loan_note = fetch_bybit_crypto_loan_balance(api_key, api_secret)
        parts["crypto_loan"] = {"value": loan_usd, "error": None, "note": loan_note}
    except Exception as e:
        parts["crypto_loan"] = {"value": 0.0, "error": str(e), "note": None}

    for name, p in parts.items():
        if p["error"]:
            print(f"[balances/bybit/{name}] {p['error']}")
        elif p.get("note"):
            print(f"[balances/bybit/{name}] {p['note']}")

    total = sum(p["value"] for p in parts.values())
    return {"total": total, "parts": parts}


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


def _aster_signed_get(path: str, user: str, signer: str, private_key: str) -> list:
    nonce = int(time.time() * 1_000_000)
    params = {
        "timestamp": str(int(time.time() * 1000)),
        "nonce": str(nonce),
        "user": user,
        "signer": signer,
    }
    param_str = urllib.parse.urlencode(params)
    sig = _aster_sign(param_str, private_key)
    resp = requests.get(f"https://fapi.asterdex.com{path}?{param_str}&signature={sig}", timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        raise RuntimeError(f"Aster {path} error: {data}")
    return data


def fetch_aster_balance(user: str, signer: str, private_key: str) -> float:
    """
    GET /fapi/v3/balance (кошелёк) + GET /fapi/v3/positionRisk (нереализованный
    PnL открытых позиций, поле unRealizedProfit) — тот же стиль запроса
    (nonce+подпись через funding_report._aster_sign) и та же версия пути
    (v3), что уже подтверждена рабочей для /fapi/v3/income в funding_report.py
    (форк API Binance Futures).

    Без PnL баланс не совпадал с реальным при открытых позициях — "balance"
    в /fapi/v3/balance это кошелёк БЕЗ учёта текущего нереализованного
    результата по открытым позициям (см. также аналогичный фикс для Lighter
    в fetch_lighter_balance). Поле unRealizedProfit — то же самое, что
    использует официальный Binance Futures API (developers.binance.com,
    Position Information V2/V3) — отдельно на реальном ответе именно Aster
    не сверено, но positionAmt/symbol из этого же эндпоинта уже используются
    в funding_report.fetch_aster_open_symbols и совпадают с Binance 1:1.
    Спота у Aster нет вовсе (см. README).
    """
    wallet = _aster_signed_get("/fapi/v3/balance", user, signer, private_key)
    wallet_usd = sum(float(a.get("balance", 0) or 0) for a in wallet if a.get("asset") == "USDT")

    positions = _aster_signed_get("/fapi/v3/positionRisk", user, signer, private_key)
    unrealized_pnl = sum(float(p.get("unRealizedProfit", 0) or 0) for p in positions)

    return wallet_usd + unrealized_pnl


def fetch_lighter_balance(account_index: str) -> float:
    """
    GET /api/v1/account?by=index&value={account_index}&active_only=true —
    тот же эндпоинт, что и funding_report.fetch_lighter_open_symbols
    использует для списка позиций.

    ПОДТВЕРЖДЕНО по официальной документации полей SDK (elliottech/lighter-
    python, docs/Account.md и docs/AccountPosition.md, дословный список
    полей на 29.08.2026): на уровне аккаунта есть только collateral —
    обеспечение БЕЗ учёта результата по открытым позициям, готового поля
    "итог с учётом PnL" в API нет. При этом по документации Lighter (Total
    Account Value = Collateral + Unrealized PnL) полный баланс — это
    collateral ПЛЮС unrealized_pnl каждой открытой позиции (поле
    AccountPosition.unrealized_pnl) — раньше считался только collateral,
    поэтому PnL открытых позиций не попадал в баланс. Реальный ответ Lighter
    напрямую не проверялся (доступа к его API из этой среды разработки не
    было) — имена полей взяты из офиц. SDK, но однократно сверить с
    приложением Lighter после деплоя всё равно стоит.
    """
    resp = requests.get(
        f"{LIGHTER_BASE_URL}/api/v1/account",
        params={"by": "index", "value": account_index, "active_only": "true"}, timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 200) != 200:
        raise RuntimeError(f"Lighter account error: {data}")

    accounts = data.get("accounts", [data]) if "accounts" not in data else data["accounts"]
    total = 0.0
    for acc in accounts:
        try:
            total += float(acc.get("collateral", 0) or 0)
        except (TypeError, ValueError):
            pass
        for pos in acc.get("positions", []):
            try:
                total += float(pos.get("unrealized_pnl", 0) or 0)
            except (TypeError, ValueError):
                continue
    return total


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


# ── Секреты для этого модуля (отдельно от funding_report.load_secrets —
#    те же переменные окружения, но опциональные и специфичные для балансов) ──

def load_wallet_secrets() -> dict:
    """
    Адрес кошелька и Etherscan-ключ — сейчас нужны только для Aave (баланс
    самого кошелька/Rabby wallet больше не сканируется, см. докстринг модуля).
    RABBY_WALLET_ADDRESS — если не задан, но задан UNISWAP_WALLET_ADDRESS
    (entry_price.py), используется он: по смыслу это обычно один и тот же
    EVM-кошелёк, которым пользователь торгует на Uniswap и держит Aave-позицию.
    ETHERSCAN_API_KEY — общий с entry_price.py.
    """
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
                 "aave": {...}|None, "total_usd": float}
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
        # Bybit — особый случай: fetch_bybit_balance отдаёт не число, а разбивку
        # по пяти частям (unified/funding/earn/grid_bot/crypto_loan) — чтобы
        # ошибка КОНКРЕТНОЙ части была видна в /balances и в Telegram, а не
        # только в логах Railway (см. докстринг fetch_bybit_balance).
        try:
            bybit = fetch_bybit_balance(secrets["bybit_api_key"], secrets["bybit_api_secret"])
            exchanges["bybit"] = {"value": bybit["total"], "error": None, "parts": bybit["parts"]}
            total += bybit["total"]
        except Exception as e:
            print(f"[balances/bybit] Ошибка: {e}")
            exchanges["bybit"] = {"value": None, "error": str(e)}

    if "lighter_account_index" in secrets:
        _try("lighter", lambda: fetch_lighter_balance(secrets["lighter_account_index"]))

    if "mexc_api_key" in secrets:
        def _mexc_total():
            futures = fetch_mexc_futures_balance(secrets["mexc_api_key"], secrets["mexc_api_secret"])
            spot = fetch_mexc_spot_balance(secrets["mexc_api_key"], secrets["mexc_api_secret"])
            return futures + price_spot_balances_usd(spot)
        _try("mexc", _mexc_total)

    if "gate_api_key" in secrets:
        def _gate_total():
            futures = fetch_gate_futures_balance(secrets["gate_api_key"], secrets["gate_api_secret"])
            spot = fetch_gate_spot_balance(secrets["gate_api_key"], secrets["gate_api_secret"])
            return futures + price_spot_balances_usd(spot)
        _try("gate", _gate_total)

    wallet_secrets = load_wallet_secrets()
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
        "aave": aave_result,
        "total_usd": total,
    }


# ── Текстовый отчёт для Telegram (/balance) ─────────────────────────────────────

EXCHANGE_LABELS = {
    "aster": "Aster", "bybit": "Bybit", "lighter": "Lighter",
    "mexc": "MEXC", "gate": "Gate",
}
_BYBIT_PART_LABELS = {
    "unified": "Unified", "funding": "Funding", "earn": "Earn",
    "grid_bot": "Spot Grid Bot", "crypto_loan": "Крипто-займы (net)",
}
_AAVE_CHAIN_NAMES = {1: "Ethereum", 42161: "Arbitrum One", 8453: "Base"}


def build_balances_report(result: dict) -> str:
    """Тот же result, что отдаёт /api/balances на веб-странице — здесь просто
    коротко в текст для Telegram."""
    lines = ["💰 Сводный баланс"]

    lines.append("")
    for key, v in result["exchanges"].items():
        label = EXCHANGE_LABELS.get(key, key)
        if v["error"]:
            lines.append(f"{label}: ❌ {v['error']}")
        else:
            lines.append(f"{label}: ${v['value']:,.2f}")

        if key == "bybit" and v.get("parts"):
            for part_name, p in v["parts"].items():
                part_label = _BYBIT_PART_LABELS.get(part_name, part_name)
                if p["error"]:
                    lines.append(f"  · {part_label}: ${p['value']:,.2f} ⚠️ {p['error']}")
                elif p.get("note"):
                    lines.append(f"  · {part_label}: ${p['value']:,.2f} ℹ️ {p['note']}")
                else:
                    lines.append(f"  · {part_label}: ${p['value']:,.2f}")

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

    lines.append("")
    lines.append(f"Итого: ${result['total_usd']:,.2f}")
    return "\n".join(lines)
