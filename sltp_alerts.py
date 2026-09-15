#!/usr/bin/env python3
"""
Алерт о закрытии открытой фьючерсной позиции — независимо от причины
(вручную, по стоп-лоссу, по тейк-профиту, по ликвидации) шлётся всегда;
дополнительно, где это можно определить надёжно, уточняется сама причина
(SL/TP). В отличие от funding_alerts.py — тот про ставку funding, этот про
сам факт закрытия позиции. Дополнительно, при каждом обнаруженном закрытии,
в Google Sheet (см. sheets_sync.record_position_close) записывается:
  - столбец F — дата закрытия, тот же "снимок времени" из ячейки F6, что
    пользователь копирует туда вручную при закрытии сделки;
  - столбец AE — СРЕДНЕВЗВЕШЕННАЯ (VWAP) цена закрытия ФЬЮЧЕРСА по всем
    исполнениям в окне CLOSE_PRICE_LOOKBACK_MINUTES, а не последнее из них
    — пользователь подтвердил, что иногда закрывает позицию ЧАСТЯМИ, растя-
    нутыми по времени (см. _CLOSE_PRICE_FETCHERS — не все 5 бирж поддер-
    живаются, см. её докстринг про Lighter);
  - столбец AD — СРЕДНЕВЗВЕШЕННАЯ цена закрытия на СПОТЕ, все продажи того
    же актива в том же окне по всем подключённым спот-биржам сразу
    (симметрично поиску покупки при открытии позиции, см. entry_price.
    _search_spot_exit).
Каждая из трёх записей — независимая попытка: отсутствие цены на одной
бирже (не нашлась продажа/не удалось определить цену закрытия фьючерса) не
блокирует запись остальных двух.

КАК РАБОТАЕТ: каждые SLTP_CHECK_INTERVAL_MINUTES минут (по умолчанию 2 —
заметно чаще, чем funding-алерты, т.к. тут речь о реальном закрытии
позиции, а не о ставке, которая просто накапливается) сверяем список сейчас
открытых позиций (funding_alerts.get_open_positions — та же функция, что
уже используется для остальных алертов и для команды /positions) с тем,
что было на предыдущей итерации. Если символ на бирже был открыт, а теперь
пропал — позиция закрылась, и это САМО ПО СЕБЕ уже повод для алерта на
любой из пяти бирж (сравнение списков открытых позиций работает одинаково
надёжно везде — тут не нужно лезть в историю ордеров конкретной биржи).

ВАЖНО: биржа, у которой запрос списка позиций в конкретном проходе
завершился ошибкой (временный сбой сети/прокси/API), НЕ участвует в
сравнении в этом проходе — это не то же самое, что "открытых позиций на
ней нет" (см. get_open_positions в funding_alerts.py и докстринг
check_closed_positions ниже). Раньше эти два случая не различались, из-за
чего единичный сетевой сбой на одной бирже выглядел как одновременное
закрытие ВСЕХ открытых на ней позиций — ложный алерт в Telegram и ложная
запись даты закрытия в Google Sheet для всё ещё открытой связки (реальный
случай на живом аккаунте: MEXC BTW_USDT).

УТОЧНЕНИЕ ПРИЧИНЫ (SL/TP) — не одинаковое для всех пяти, и это осознанно:
  - Bybit — надёжно: GET /v5/execution/list отдаёт поле stopOrderType прямо
    в каждом исполнении ("StopLoss"/"TakeProfit"/"PartialStopLoss"/
    "PartialTakeProfit") — прямой, официально задокументированный сигнал,
    ничего вычислять/угадывать не нужно.
  - Aster — надёжно: GET /fapi/v1/allOrders (Binance-совместимый API)
    отдаёт origType ("STOP_MARKET"/"STOP"/"TAKE_PROFIT_MARKET"/
    "TAKE_PROFIT") для исполненного ордера — тоже прямой сигнал.
  - MEXC, Gate, Lighter — причина НЕ уточняется в этой версии (см. историю
    решения ниже), но сам факт закрытия по-прежнему алертится — просто без
    пометки SL/TP:
      * MEXC — условные (Plan) ордера живут в отдельном API, официальный
        формат ответа истории исполненных план-ордеров не задокументирован
        достаточно точно, чтобы писать код не вслепую (в отличие от,
        скажем, MEXC funding_rate/history, который проверен по документации
        построчно).
      * Gate — есть price_triggered_orders со статусом finished, но внутри
        одной finished-записи нет прямого поля "это был TP" / "это был SL"
        — различить можно только косвенно (сравнением цены закрытия с ценой
        входа), а входа у уже закрытой позиции мы не знаем без отдельного
        кэширования на каждой итерации, чего в этой версии нет.
      * Lighter — публичного REST-эндпоинта для истории ордеров аккаунта с
        полем типа ордера (ORDER_TYPE_STOP_LOSS/ORDER_TYPE_TAKE_PROFIT,
        такое значение есть в SDK при СОЗДАНИИ ордера) в документации не
        нашлось вообще.
    Если реально словите SL/TP на одной из этих трёх бирж и захотите видеть
    пометку и там — пришлите, что произошло (биржа/символ/время/цена
    закрытия), тогда можно будет прицельно доработать именно уточнение
    причины, не трогая сам факт алерта — он уже работает для всех пяти.
"""

import os
import time
import urllib.parse

import requests

from funding_report import (
    _get_proxies, _bybit_sign, _aster_sign, _mexc_sign, _gate_sign,
    _get_mexc_proxies, _get_gate_proxies,
    load_secrets, send_telegram_broadcast,
)
from funding_alerts import get_open_positions, EXCHANGE_LABELS
from entry_price import _base_asset, _search_spot_exit
from sheets_sync import record_position_close

SLTP_CHECK_INTERVAL_MINUTES = float(os.environ.get("SLTP_CHECK_INTERVAL_MINUTES", "2"))

# Насколько глубоко ищем историю ордеров назад от текущего момента при
# обнаружении закрытия — с запасом относительно интервала проверки: позиция
# могла закрыться сразу ПОСЛЕ предыдущей проверки, а не прямо перед текущей.
# Используется для уточнения причины (SL/TP) — там нужен только ПОСЛЕДНИЙ
# исполненный ордер, окно не обязано быть широким.
SLTP_LOOKBACK_MINUTES = max(15.0, SLTP_CHECK_INTERVAL_MINUTES * 3)

# Отдельное, более широкое окно — специально для цены закрытия (AD/AE, см.
# _CLOSE_PRICE_FETCHERS и _search_spot_exit ниже): пользователь подтвердил,
# что иногда закрывает позицию ЧАСТЯМИ, растянутыми по времени — цена
# закрытия должна быть средневзвешенной (VWAP) по ВСЕМ филлам в этом окне,
# а не последним филлом. SLTP_LOOKBACK_MINUTES для этого маловат (рассчитан
# на "разница между двумя проверками", а не "сколько может растянуться
# ручное частичное закрытие").
CLOSE_PRICE_LOOKBACK_MINUTES = float(os.environ.get("CLOSE_PRICE_LOOKBACK_MINUTES", "360"))  # 6 часов


# ── Bybit: /v5/execution/list, поле stopOrderType ────────────────────────────

def _bybit_close_reason(secrets: dict, symbol: str) -> tuple | None:
    """
    (kind, price) где kind — "SL" или "TP", price — цена исполнения (может
    быть None), либо None, если условный ордер не нашёлся в окне поиска
    (значит закрытие было обычным/ручным/ликвидацией, либо случилось раньше
    окна — в обоих случаях лучше промолчать, чем гадать).
    """
    proxies = _get_proxies()
    api_key = secrets["bybit_api_key"].strip()
    api_secret = secrets["bybit_api_secret"].strip()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(SLTP_LOOKBACK_MINUTES * 60 * 1000)
    recv_window = "5000"
    timestamp = str(now_ms)
    params_list = [
        ("category", "linear"), ("symbol", symbol),
        ("startTime", str(start_ms)), ("endTime", str(now_ms)), ("limit", "50"),
    ]
    query_string = urllib.parse.urlencode(params_list)
    sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, query_string)
    headers = {
        "X-BAPI-API-KEY": api_key, "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": recv_window,
    }
    resp = requests.get(
        f"https://api.bybit.com/v5/execution/list?{query_string}",
        headers=headers, timeout=15, proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit execution/list error {data.get('retCode')}: {data.get('retMsg')}")

    items = data.get("result", {}).get("list", [])
    # Bybit отдаёт исполнения от новых к старым — берём первое совпадение.
    for item in items:
        stop_type = item.get("stopOrderType", "UNKNOWN")
        price = float(item["execPrice"]) if item.get("execPrice") else None
        if stop_type in ("StopLoss", "PartialStopLoss"):
            return ("SL", price)
        if stop_type in ("TakeProfit", "PartialTakeProfit"):
            return ("TP", price)
    return None


# ── Aster: /fapi/v1/allOrders, поле origType ──────────────────────────────────

def _aster_close_reason(secrets: dict, symbol: str) -> tuple | None:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(SLTP_LOOKBACK_MINUTES * 60 * 1000)
    nonce = int(time.time() * 1_000_000)
    params = {
        "symbol": symbol, "startTime": str(start_ms), "endTime": str(now_ms), "limit": "50",
        "timestamp": str(now_ms), "nonce": str(nonce),
        "user": secrets["user"], "signer": secrets["signer"],
    }
    param_str = urllib.parse.urlencode(params)
    sig = _aster_sign(param_str, secrets["signer_private_key"])
    resp = requests.get(
        f"https://fapi.asterdex.com/fapi/v1/allOrders?{param_str}&signature={sig}",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        raise RuntimeError(f"Aster allOrders error: {data}")

    # От новых к старым по времени обновления ордера — берём первое совпадение.
    for order in sorted(data, key=lambda o: -int(o.get("updateTime", 0))):
        if order.get("status") != "FILLED":
            continue
        orig_type = order.get("origType") or order.get("type") or ""
        price = float(order["avgPrice"]) if order.get("avgPrice") else None
        if orig_type in ("STOP_MARKET", "STOP"):
            return ("SL", price)
        if orig_type in ("TAKE_PROFIT_MARKET", "TAKE_PROFIT"):
            return ("TP", price)
    return None


# Только биржи, где сигнал SL/TP задокументирован прямо и однозначно —
# см. докстринг модуля про MEXC/Gate/Lighter.
_CLOSE_REASON_FETCHERS = {
    "bybit": _bybit_close_reason,
    "aster": _aster_close_reason,
}


def _vwap(qty_price_pairs) -> float | None:
    """Средневзвешенная по объёму цена по списку (qty, price) — общий
    хелпер для всех _*_close_price ниже и переиспользуется по той же логике,
    что уже в entry_price._search_spot_trade (там же VWAP по спот-сделкам)."""
    pairs = list(qty_price_pairs)
    total_qty = sum(qty for qty, _ in pairs)
    if total_qty <= 0:
        return None
    return sum(qty * price for qty, price in pairs) / total_qty


# ── Цена закрытия фьючерса (столбец AE в Google Sheet) ───────────────────────
#
# Отдельные функции от _*_close_reason выше, а не переиспользование уже
# полученного там списка исполнений — тот же паттерн, что уже используется в
# кодовой базе для "свой (но однострочный, не более) вызов того же
# эндпоинта под конкретную задачу" (см. докстринг short_position_tracker.py
# про entry_price._*_position_entry). Цена нужна ВСЕГДА при закрытии
# (независимо от причины — SL/TP/вручную), а _*_close_reason возвращает
# None целиком, если это не SL/TP — тогда бы цена терялась вместе с
# причиной.
#
# СРЕДНЕВЗВЕШЕННАЯ (VWAP), не последний филл — пользователь подтвердил, что
# иногда закрывает позицию частями. Окно поиска — CLOSE_PRICE_LOOKBACK_MINUTES
# (шире, чем у _*_close_reason выше, см. её докстринг).
#
# СТОРОНА ИСПОЛНЕНИЯ (закрытие шорта = покупка, в отличие от входа в шорт —
# продажи) фильтруется, только там, где для этого есть НАДЁЖНОЕ явное поле:
#   - Bybit — поле side ("Buy"/"Sell") прямо в каждой записи execution/list,
#     задокументировано официально.
#   - Aster — поле side ("BUY"/"SELL") в каждом ордере, тот же
#     Binance-совместимый формат, что и остальные поля этого эндпоинта.
#   - MEXC, Gate — НЕ фильтруется: у MEXC сторона сделки — числовой код 1-4
#     (открытие/закрытие лонга/шорта), однозначного маппинга кодов 2/4 в
#     официальной документации не нашлось; у Gate в ответе my_trades нет
#     явного поля стороны (см. докстринг _gate_close_price). Поэтому для
#     этих двух бирж окно поиска НЕ расширено так же сильно, как для
#     Bybit/Aster — усредняются ВСЕ найденные за это время сделки по
#     символу, с осознанным риском случайно захватить сделку открытия
#     позиции, если окно всё же дотянется до неё (при обычном использовании
#     бота — маловероятно: одна связка на символ, открыта-подержана-
#     закрыта, без повторной торговли тем же символом в узком окне).

def _bybit_close_price(secrets: dict, symbol: str) -> float | None:
    proxies = _get_proxies()
    api_key = secrets["bybit_api_key"].strip()
    api_secret = secrets["bybit_api_secret"].strip()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(CLOSE_PRICE_LOOKBACK_MINUTES * 60 * 1000)
    recv_window = "5000"
    timestamp = str(now_ms)
    params_list = [
        ("category", "linear"), ("symbol", symbol),
        ("startTime", str(start_ms)), ("endTime", str(now_ms)), ("limit", "100"),
    ]
    query_string = urllib.parse.urlencode(params_list)
    sig = _bybit_sign(api_key, api_secret, timestamp, recv_window, query_string)
    headers = {
        "X-BAPI-API-KEY": api_key, "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": timestamp, "X-BAPI-RECV-WINDOW": recv_window,
    }
    resp = requests.get(
        f"https://api.bybit.com/v5/execution/list?{query_string}",
        headers=headers, timeout=15, proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("retCode", 0) != 0:
        raise RuntimeError(f"Bybit execution/list error {data.get('retCode')}: {data.get('retMsg')}")
    items = data.get("result", {}).get("list", [])
    closes = [i for i in items if i.get("side") == "Buy" and i.get("execPrice") and i.get("execQty")]
    return _vwap([(float(i["execQty"]), float(i["execPrice"])) for i in closes])


def _aster_close_price(secrets: dict, symbol: str) -> float | None:
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(CLOSE_PRICE_LOOKBACK_MINUTES * 60 * 1000)
    nonce = int(time.time() * 1_000_000)
    params = {
        "symbol": symbol, "startTime": str(start_ms), "endTime": str(now_ms), "limit": "500",
        "timestamp": str(now_ms), "nonce": str(nonce),
        "user": secrets["user"], "signer": secrets["signer"],
    }
    param_str = urllib.parse.urlencode(params)
    sig = _aster_sign(param_str, secrets["signer_private_key"])
    resp = requests.get(
        f"https://fapi.asterdex.com/fapi/v1/allOrders?{param_str}&signature={sig}",
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict):
        raise RuntimeError(f"Aster allOrders error: {data}")
    closes = [
        o for o in data
        if o.get("status") == "FILLED" and o.get("side") == "BUY" and o.get("avgPrice") and o.get("executedQty")
    ]
    return _vwap([(float(o["executedQty"]), float(o["avgPrice"])) for o in closes])


def _mexc_close_price(secrets: dict, symbol: str) -> float | None:
    """
    GET /api/v1/private/order/list/order_deals/v3 — история исполненных
    сделок (deals) по символу. Путь и поля ответа (price, vol, timestamp)
    ПОДТВЕРЖДЕНЫ по офиц. SDK ccxt (ccxt/mexc.py: fetchMyTrades для
    контрактных рынков вызывает contractPrivateGetOrderListOrderDealsV3 —
    этот же путь; parseTrade читает оттуда price/vol/timestamp/side и т.д.).
    Сторону сделки НЕ фильтруем — см. докстринг раздела выше.
    """
    base_url = "https://api.mexc.com"
    api_key = secrets["mexc_api_key"].strip()
    api_secret = secrets["mexc_api_secret"].strip()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(SLTP_LOOKBACK_MINUTES * 60 * 1000)
    timestamp = str(now_ms)
    params_list = [
        ("symbol", symbol), ("page_num", "1"), ("page_size", "50"),
        ("start_time", str(start_ms)), ("end_time", str(now_ms)),
    ]
    sig = _mexc_sign(api_key, api_secret, timestamp, params_list)
    headers = {"ApiKey": api_key, "Request-Time": timestamp, "Signature": sig}
    query_string = urllib.parse.urlencode(sorted(params_list, key=lambda kv: kv[0]))
    resp = requests.get(
        f"{base_url}/api/v1/private/order/list/order_deals/v3?{query_string}",
        headers=headers, timeout=15, proxies=_get_mexc_proxies(),
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"MEXC order_deals error {data.get('code')}: {data.get('message') or data}")
    deals = [d for d in (data.get("data") or []) if d.get("price") and d.get("vol")]
    return _vwap([(float(d["vol"]), float(d["price"])) for d in deals])


def _gate_close_price(secrets: dict, symbol: str, settle: str = "usdt") -> float | None:
    """
    GET /api/v4/futures/{settle}/my_trades — история исполненных сделок по
    контракту. Путь/параметры и поля ответа (price, size, create_time_ms)
    ПОДТВЕРЖДЕНЫ по офиц. SDK ccxt (ccxt/gate.py: приватный futures-
    эндпоинт '{settle}/my_trades') и по WebSocket-схеме Gate для того же
    потока сделок (та же модель полей). Явного поля стороны нет (size —
    это объём сделки, не подписанное изменение позиции, как у объекта
    позиции в других местах этого кода, — соответственно, "минус = шорт"
    здесь применять нельзя без риска ошибиться), поэтому сторону НЕ
    фильтруем — см. докстринг раздела выше.
    """
    base_url = "https://api.gateio.ws"
    url_path = f"/api/v4/futures/{settle}/my_trades"
    proxies = _get_gate_proxies()
    api_key = secrets["gate_api_key"].strip()
    api_secret = secrets["gate_api_secret"].strip()
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - int(SLTP_LOOKBACK_MINUTES * 60 * 1000)
    params_list = [
        ("contract", symbol), ("from", str(start_ms // 1000)),
        ("to", str(now_ms // 1000)), ("limit", "50"),
    ]
    query_string = urllib.parse.urlencode(params_list)
    sig, timestamp = _gate_sign(api_secret, "GET", url_path, query_string)
    headers = {"KEY": api_key, "Timestamp": timestamp, "SIGN": sig, "Accept": "application/json"}
    resp = requests.get(f"{base_url}{url_path}?{query_string}", headers=headers, timeout=15, proxies=proxies)
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("label"):
        raise RuntimeError(f"Gate my_trades error {data.get('label')}: {data.get('message')}")
    trades = [t for t in (data if isinstance(data, list) else []) if t.get("price") and t.get("size")]
    return _vwap([(abs(float(t["size"])), float(t["price"])) for t in trades])


# Lighter намеренно не включён — как и для _CLOSE_REASON_FETCHERS, публичного
# документированного эндпоинта истории сделок/ордеров аккаунта не нашлось
# (см. докстринг модуля). Если понадобится — присылайте реальный случай
# (символ/время закрытия), тогда можно будет доработать прицельно.
_CLOSE_PRICE_FETCHERS = {
    "bybit": _bybit_close_price,
    "aster": _aster_close_price,
    "mexc": _mexc_close_price,
    "gate": _gate_close_price,
}


def _fmt_alert(exchange: str, symbol: str, reason: tuple | None) -> str:
    label = EXCHANGE_LABELS.get(exchange, exchange)
    if reason is None:
        # Причина не определена (обычное/ручное закрытие, ликвидация, либо
        # биржа не поддерживает уточнение причины — см. докстринг модуля) —
        # сам факт закрытия всё равно известен точно, шлём его как есть.
        return f"⚪️ Позиция {label} {symbol} закрыта"
    kind, price = reason
    emoji = "🛑" if kind == "SL" else "🎯"
    kind_ru = "стоп-лоссу" if kind == "SL" else "тейк-профиту"
    price_part = f" по цене {price:g}" if price else ""
    return f"{emoji} Позиция {label} {symbol} закрыта по {kind_ru}{price_part}"


def check_closed_positions(secrets: dict, prev_open: dict) -> dict:
    """
    Один проход: сравнивает prev_open ({exchange: set(symbols)} с прошлой
    итерации) с текущим состоянием открытых позиций, шлёт алерт при
    ЛЮБОМ обнаруженном закрытии — на любой из пяти бирж. Если биржа входит
    в _CLOSE_REASON_FETCHERS (Bybit/Aster), дополнительно пытается уточнить
    причину (SL/TP) и добавить её в текст; для остальных бирж, а также если
    уточнить не удалось (ошибка запроса или закрытие действительно было
    обычным/ручным), алерт всё равно уходит — просто без пометки причины.
    Возвращает новое состояние для следующей итерации.

    ВАЖНО (реальный баг, найденный на живом аккаунте — ложный алерт "MEXC
    BTW_USDT закрыта" и ложная запись даты закрытия в Google Sheet для
    связки, которая на самом деле оставалась открытой): get_open_positions
    возвращает ВТОРЫМ элементом failed_exchanges — биржи, у которых запрос
    списка позиций в ЭТОМ проходе завершился ошибкой (временный сбой сети/
    прокси/API). Раньше это никак не отличалось от "на бирже открытых
    позиций сейчас нет" — current_open.get(exchange, []) в обоих случаях
    давал пустой список, из-за чего единичный сетевой сбой на бирже
    выглядел как одновременное закрытие ВСЕХ открытых на ней позиций.
    Теперь для таких бирж сравнение в этом проходе просто пропускается —
    прежнее состояние переносится в результат как есть, без диффа.
    """
    token = secrets["telegram_token"]
    chat_ids = secrets["telegram_chat_ids"]
    now_ms = int(time.time() * 1000)  # центр окна поиска цены/продажи на споте при закрытии (см. ниже)

    current_open, failed_exchanges = get_open_positions(secrets)  # {exchange: [symbols]}
    if failed_exchanges:
        print(f"[sltp] Запрос списка открытых позиций не удался: {sorted(failed_exchanges)} — "
              f"эти биржи в этом проходе не сравниваются с прошлым состоянием (временный сбой "
              f"API не должен приниматься за закрытие всех позиций на бирже).")

    # Проходим по объединению бирж из обоих состояний, а не только по
    # _CLOSE_REASON_FETCHERS — иначе MEXC/Gate/Lighter вообще выпали бы
    # из отслеживания закрытий.
    for exchange in set(prev_open) | set(current_open):
        if exchange in failed_exchanges:
            continue  # текущее состояние неизвестно — не диффим и не алертим (см. докстринг выше)

        prev_symbols = prev_open.get(exchange, set())
        current_symbols = set(current_open.get(exchange, []))
        closed_symbols = prev_symbols - current_symbols

        for symbol in closed_symbols:
            reason = None
            fetcher = _CLOSE_REASON_FETCHERS.get(exchange)
            if fetcher:
                try:
                    reason = fetcher(secrets, symbol)
                except Exception as e:
                    print(f"[sltp/{exchange}/{symbol}] Не удалось определить причину закрытия: {e}")
                    reason = None  # не блокирует отправку алерта о самом факте закрытия

            text = _fmt_alert(exchange, symbol, reason)
            send_telegram_broadcast(token, chat_ids, text)
            print(f"[sltp] Отправлен алерт: {exchange} {symbol} причина={reason}")

            # Цена закрытия фьючерса (столбец AE) — только для бирж из
            # _CLOSE_PRICE_FETCHERS (см. её докстринг про Lighter). Ошибка
            # не блокирует ни алерт (уже отправлен), ни запись остального.
            futures_close_price = None
            price_fetcher = _CLOSE_PRICE_FETCHERS.get(exchange)
            if price_fetcher:
                try:
                    futures_close_price = price_fetcher(secrets, symbol)
                    if futures_close_price is None:
                        print(f"[sltp/{exchange}/{symbol}] Цена закрытия фьючерса не найдена "
                              f"в окне поиска.")
                except Exception as e:
                    print(f"[sltp/{exchange}/{symbol}] Не удалось получить цену закрытия фьючерса: {e}")

            # Цена закрытия на споте (столбец AD) — ищем ПРОДАЖУ того же
            # актива в том же окне, по всем подключённым спот-биржам сразу
            # (симметрично поиску покупки при открытии, см. entry_price.
            # _search_spot_exit). Если не нашлась — не гадаем, оставляем
            # пустой, как и для остальных полей в этой таблице.
            spot_close_price = None
            try:
                base_asset = _base_asset(exchange, symbol)
                spot_exit = _search_spot_exit(secrets, base_asset, now_ms, CLOSE_PRICE_LOOKBACK_MINUTES)
                if spot_exit:
                    spot_close_price = spot_exit["price"]
                else:
                    print(f"[sltp/{exchange}/{symbol}] Продажа на споте не найдена ни на одной бирже "
                          f"в окне ±{CLOSE_PRICE_LOOKBACK_MINUTES:.0f} мин.")
            except Exception as e:
                print(f"[sltp/{exchange}/{symbol}] Ошибка поиска продажи на споте: {e}")

            # Запись в Google Sheet — отдельным try/except: если она упадёт
            # (таблица не подключена, неоднозначное совпадение строки и
            # т.п.), это не должно повлиять на уже отправленный
            # Telegram-алерт и не должно останавливать обработку остальных
            # закрывшихся позиций в этом же проходе.
            try:
                record_position_close(exchange, symbol, spot_close_price, futures_close_price)
            except Exception as e:
                print(f"[sltp/{exchange}/{symbol}] Не удалось записать данные закрытия в Google Sheet: {e}")

    # Для бирж с успешным запросом — новое состояние из current_open. Для
    # бирж из failed_exchanges — состояние НЕ трогаем, переносим prev_open
    # как есть (см. докстринг выше): иначе, если биржа отвалится на
    # несколько проходов подряд, а затем восстановится, следующее сравнение
    # пойдёт с пустого состояния и упустит реальные закрытия, случившиеся,
    # пока биржа была недоступна, — то же самое, только менее заметное,
    # проявление одной и той же ошибки "нет данных = позиций нет".
    new_state = {exchange: set(symbols) for exchange, symbols in current_open.items()}
    for exchange in failed_exchanges:
        if exchange in prev_open:
            new_state[exchange] = prev_open[exchange]
    return new_state


def sltp_alert_loop(secrets: dict | None = None) -> None:
    """Бесконечный цикл проверки раз в SLTP_CHECK_INTERVAL_MINUTES минут."""
    if secrets is None:
        secrets = load_secrets()
    prev_open: dict = {}
    interval_s = max(30.0, SLTP_CHECK_INTERVAL_MINUTES * 60)
    print(f"[sltp] Запущен цикл проверки закрытий по SL/TP каждые {SLTP_CHECK_INTERVAL_MINUTES:.0f} мин.", flush=True)

    first_run = True
    while True:
        try:
            if first_run:
                # На первом проходе только заполняем состояние — иначе все
                # позиции, открытые ДО старта бота, покажутся "закрывшимися"
                # прямо на первой итерации и породят ложные алерты.
                initial_open, _ = get_open_positions(secrets)
                prev_open = {ex: set(syms) for ex, syms in initial_open.items()}
                first_run = False
            else:
                prev_open = check_closed_positions(secrets, prev_open)
        except Exception as e:
            print(f"[sltp] Ошибка цикла проверки: {e}", flush=True)
        time.sleep(interval_s)


if __name__ == "__main__":
    sltp_alert_loop()
