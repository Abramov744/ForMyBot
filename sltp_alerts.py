#!/usr/bin/env python3
"""
Алерт о закрытии открытой фьючерсной позиции — независимо от причины
(вручную, по стоп-лоссу, по тейк-профиту, по ликвидации) шлётся всегда;
дополнительно, где это можно определить надёжно, уточняется сама причина
(SL/TP). В отличие от funding_alerts.py — тот про ставку funding, этот про
сам факт закрытия позиции. Дополнительно, при каждом обнаруженном закрытии,
в Google Sheet (см. sheets_sync.record_position_close) записывается дата
закрытия в столбец F соответствующей строки — тот же "снимок времени" из
ячейки F6, что пользователь копирует туда вручную при закрытии сделки.

КАК РАБОТАЕТ: каждые SLTP_CHECK_INTERVAL_MINUTES минут (по умолчанию 2 —
заметно чаще, чем funding-алерты, т.к. тут речь о реальном закрытии
позиции, а не о ставке, которая просто накапливается) сверяем список сейчас
открытых позиций (funding_alerts.get_open_positions — та же функция, что
уже используется для остальных алертов и для команды /positions) с тем,
что было на предыдущей итерации. Если символ на бирже был открыт, а теперь
пропал — позиция закрылась, и это САМО ПО СЕБЕ уже повод для алерта на
любой из пяти бирж (сравнение списков открытых позиций работает одинаково
надёжно везде — тут не нужно лезть в историю ордеров конкретной биржи).

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

from funding_report import _get_proxies, _bybit_sign, _aster_sign, load_secrets, send_telegram
from funding_alerts import get_open_positions, EXCHANGE_LABELS
from sheets_sync import record_position_close

SLTP_CHECK_INTERVAL_MINUTES = float(os.environ.get("SLTP_CHECK_INTERVAL_MINUTES", "2"))

# Насколько глубоко ищем историю ордеров назад от текущего момента при
# обнаружении закрытия — с запасом относительно интервала проверки: позиция
# могла закрыться сразу ПОСЛЕ предыдущей проверки, а не прямо перед текущей.
SLTP_LOOKBACK_MINUTES = max(15.0, SLTP_CHECK_INTERVAL_MINUTES * 3)


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
    """
    token = secrets["telegram_token"]
    chat_id = secrets["telegram_chat_id"]

    current_open = get_open_positions(secrets)  # {exchange: [symbols]}

    # Проходим по объединению бирж из обоих состояний, а не только по
    # _CLOSE_REASON_FETCHERS — иначе MEXC/Gate/Lighter вообще выпали бы
    # из отслеживания закрытий.
    for exchange in set(prev_open) | set(current_open):
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
            try:
                send_telegram(token, chat_id, text)
                print(f"[sltp] Отправлен алерт: {exchange} {symbol} причина={reason}")
            except Exception as e:
                print(f"[sltp] Не удалось отправить алерт в Telegram: {e}")

            # Запись даты закрытия в Google Sheet — отдельным try/except:
            # если она упадёт (таблица не подключена, неоднозначное
            # совпадение строки и т.п.), это не должно повлиять на уже
            # отправленный Telegram-алерт и не должно останавливать
            # обработку остальных закрывшихся позиций в этом же проходе.
            try:
                record_position_close(exchange, symbol)
            except Exception as e:
                print(f"[sltp/{exchange}/{symbol}] Не удалось записать дату закрытия в Google Sheet: {e}")

    return {exchange: set(symbols) for exchange, symbols in current_open.items()}


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
                prev_open = {ex: set(syms) for ex, syms in get_open_positions(secrets).items()}
                first_run = False
            else:
                prev_open = check_closed_positions(secrets, prev_open)
        except Exception as e:
            print(f"[sltp] Ошибка цикла проверки: {e}", flush=True)
        time.sleep(interval_s)


if __name__ == "__main__":
    sltp_alert_loop()
