#!/usr/bin/env python3
"""
Алерт о приближении цены к ЛИКВИДАЦИИ по открытой фьючерсной позиции —
согласовано с пользователем явно (порог 70%, формула ниже).

ИДЕЯ: раз в LIQUIDATION_CHECK_INTERVAL_MINUTES минут для каждой открытой
позиции сравнивается ТЕКУЩАЯ (mark/fair) цена с ценой ВХОДА и ценой
ЛИКВИДАЦИИ. Расстояние entry→liq — это "запас прочности" позиции; когда
цена прошла ПРОТИВ позиции (для шорта — вверх) LIQUIDATION_ALERT_THRESHOLD
(0.70 = 70%) этого расстояния, в Telegram уходит алерт.

ФОРМУЛА (у бота только ШОРТЫ, см. CLAUDE.md про стратегию — цена ликвидации
у шорта всегда ВЫШЕ цены входа):
  distance      = liq_price - entry_price
  moved_against = mark_price - entry_price
  pct_to_liq    = moved_against / distance
Алерт — при pct_to_liq >= LIQUIDATION_ALERT_THRESHOLD. Если distance <= 0
(liq_price не выше entry_price — не похоже на валидную цену ликвидации
шорта, например кросс-маржа без изолированного риска по конкретной
позиции у Binance-совместимых API) — позиция просто пропускается, без
алерта и без ошибки, это не наш баг, а особенность данных биржи в этом
режиме маржи.

EDGE-TRIGGERED, как и funding-алерты (funding_alerts.check_funding_alerts):
алерт уходит один раз при переходе pct_to_liq из "меньше порога" в "порог
и больше" по каждой (биржа, символ) — не на каждой проверке подряд, пока
позиция остаётся в опасной зоне. Как только доля опускается обратно ниже
порога — состояние сбрасывается, следующий заход в опасную зону снова
пришлёт алерт.

ВАЖНО (тот же класс бага, что уже был реальным на этом коде — см.
funding_alerts.get_open_positions/sltp_alerts.py, ложный алерт "MEXC
BTW_USDT закрыта"): временный сетевой/API-сбой при запросе позиций на
КОНКРЕТНОЙ бирже не должен читаться как "открытых позиций на ней больше
нет" — иначе единичный сбой стирал бы state для всех символов этой биржи,
и при восстановлении API уже сработавшая (но ещё не сброшенная) тревога
могла бы продублироваться. check_liquidation_alerts поэтому НЕ трогает
state для бирж, у которых запрос в этом проходе завершился ошибкой —
ровно та же защита, что и в sltp_alerts.check_closed_positions.

ИСТОЧНИКИ entry_price/liq_price/mark_price — тот же сырой ответ приватного
position-эндпоинта каждой биржи, что и в funding_report.fetch_*_open_symbols
и short_position_tracker._fetch_*_open_shorts (тот же самый запрос с теми
же параметрами делался раньше в трёх местах независимо) — здесь просто
разбираются другие поля того же ответа. HTTP-запрос выполняется один раз в
funding_report._*_positions_raw и переиспользуется всеми тремя через
funding_report._cached_raw_positions (короткий TTL-кэш, см. её докстринг) —
не копия логики, а общий источник поверх одного и того же API:
  - Bybit  — GET /v5/position/list: markPrice, liqPrice — оба поля прямо
    в ответе, доп. запрос не нужен. liqPrice официально документирован
    как пустая строка "", если вне диапазона [minPrice, maxPrice] — такие
    позиции пропускаются (нет валидной цены ликвидации, не 0/не гадаем).
  - Aster  — GET /fapi/v3/positionRisk (Binance-совместимый): markPrice,
    liquidationPrice — тоже оба поля прямо в ответе.
  - Gate   — GET /api/v4/futures/{settle}/positions: mark_price, liq_price
    — тоже оба поля прямо в ответе.
  - KuCoin — GET /api/v1/positions: markPrice, liquidationPrice — тоже оба
    поля прямо в ответе.
  - MEXC   — GET /api/v1/private/position/open_positions отдаёт
    liquidatePrice, но НЕ отдаёт текущую цену — она берётся отдельным
    ПУБЛИЧНЫМ (без ключей) запросом GET /api/v1/contract/fair_price/
    {symbol} (поле fairPrice), по одному на каждый открытый на MEXC
    символ (подтверждено по офиц. docs — mexc.com/api-docs/futures/
    market-endpoints/get-fair-price).
  - Lighter — ТЕПЕРЬ ВКЛЮЧЁН (явная просьба пользователя, "настрой Lighter
    так же, как MEXC и Gate"). Раньше считалось, что поле цены ликвидации
    нигде не документировано — это было верно на момент той проверки, но
    офиц. SDK (elliottech/lighter-python, docs/AccountPosition.md,
    дословно проверено 06.10.2026) с тех пор получил поле
    liquidation_price прямо в ответе позиции (GET /api/v1/account), рядом
    с avg_entry_price. mark_price берётся отдельным публичным запросом —
    funding_report.fetch_lighter_mark_prices() (GET /api/v1/orderBook
    Details), тот же источник, что уже использовался для /rates. ВАЖНО:
    в отличие от остальных бирж здесь это поле НЕ проверено против
    реального счёта с открытой позицией (доступа к боевому Lighter-аккаунту
    из среды разработки нет) — та же оговорка, что уже стоит у "sign"
    (определение шорта) в short_position_tracker.py. Первый реальный алерт
    по Lighter стоит явно сверить с тем, что показывает само приложение
    Lighter, прежде чем полностью доверять порогу.
"""

import os
import time

import requests

from funding_report import (
    _cached_raw_positions,
    _aster_positions_raw, _bybit_positions_raw, _gate_positions_raw,
    _kucoin_positions_raw, _mexc_positions_raw, _lighter_positions_raw,
    _get_mexc_proxies,
    fetch_lighter_markets, fetch_lighter_mark_prices,
    load_secrets, send_telegram_broadcast,
)

LIQUIDATION_CHECK_INTERVAL_MINUTES = float(os.environ.get("LIQUIDATION_CHECK_INTERVAL_MINUTES", "5"))
LIQUIDATION_ALERT_THRESHOLD = float(os.environ.get("LIQUIDATION_ALERT_THRESHOLD", "0.70"))

_LABELS = {"aster": "Aster", "bybit": "Bybit", "mexc": "MEXC", "gate": "Gate", "kucoin": "KuCoin", "lighter": "Lighter"}


# ── Позиции с ценой входа/ликвидации/текущей — по одной функции на биржу ─────

def _bybit_liquidation_positions(secrets: dict) -> list:
    items = _cached_raw_positions("bybit", lambda: _bybit_positions_raw(
        secrets["bybit_api_key"], secrets["bybit_api_secret"],
    ))
    out = []
    for p in items:
        if p.get("side") != "Sell" or float(p.get("size", 0) or 0) <= 0:
            continue
        entry, liq, mark = p.get("avgPrice"), p.get("liqPrice"), p.get("markPrice")
        if not entry or not liq or not mark:
            continue  # liqPrice — часто "" при кросс-марже, см. докстринг модуля
        out.append({"symbol": p["symbol"], "entry_price": float(entry), "liq_price": float(liq), "mark_price": float(mark)})
    return out


def _aster_liquidation_positions(secrets: dict) -> list:
    items = _cached_raw_positions("aster", lambda: _aster_positions_raw(
        secrets["user"], secrets["signer"], secrets["signer_private_key"],
    ))
    out = []
    for p in items:
        amt = float(p.get("positionAmt", 0) or 0)
        if amt >= 0:
            continue
        entry, liq, mark = p.get("entryPrice"), p.get("liquidationPrice"), p.get("markPrice")
        if not entry or not liq or not mark or float(liq) == 0:
            continue  # liquidationPrice == 0 у Binance-совместимых API — кросс-маржа, см. докстринг модуля
        out.append({"symbol": p["symbol"], "entry_price": float(entry), "liq_price": float(liq), "mark_price": float(mark)})
    return out


def _gate_liquidation_positions(secrets: dict, settle: str = "usdt") -> list:
    items = _cached_raw_positions(f"gate:{settle}", lambda: _gate_positions_raw(
        secrets["gate_api_key"], secrets["gate_api_secret"], settle,
    ))
    out = []
    for p in items:
        size = float(p.get("size", 0) or 0)
        if size >= 0:
            continue
        entry, liq, mark = p.get("entry_price"), p.get("liq_price"), p.get("mark_price")
        if not entry or not liq or not mark:
            continue
        out.append({"symbol": p["contract"], "entry_price": float(entry), "liq_price": float(liq), "mark_price": float(mark)})
    return out


def _kucoin_liquidation_positions(secrets: dict) -> list:
    items = _cached_raw_positions("kucoin", lambda: _kucoin_positions_raw(
        secrets["kucoin_api_key"], secrets["kucoin_api_secret"], secrets["kucoin_api_passphrase"],
    ))
    out = []
    for p in items:
        qty = float(p.get("currentQty", 0) or 0)
        if not p.get("isOpen") or qty >= 0:
            continue
        entry, liq, mark = p.get("avgEntryPrice"), p.get("liquidationPrice"), p.get("markPrice")
        if not entry or not liq or not mark:
            continue
        out.append({"symbol": p["symbol"], "entry_price": float(entry), "liq_price": float(liq), "mark_price": float(mark)})
    return out


def _mexc_fair_price(symbol: str) -> float:
    resp = requests.get(
        f"https://api.mexc.com/api/v1/contract/fair_price/{symbol}",
        timeout=15, proxies=_get_mexc_proxies(),
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success", False):
        raise RuntimeError(f"MEXC fair_price error {data.get('code')}: {data.get('message') or data}")
    return float(data["data"]["fairPrice"])


def _mexc_liquidation_positions(secrets: dict) -> list:
    items = _cached_raw_positions("mexc", lambda: _mexc_positions_raw(
        secrets["mexc_api_key"], secrets["mexc_api_secret"],
    ))
    out = []
    for p in items:
        if str(p.get("positionType")) == "1" or float(p.get("holdVol", 0) or 0) <= 0:
            continue  # positionType 1 == long
        entry = p.get("openAvgPrice") or p.get("holdAvgPrice")
        liq = p.get("liquidatePrice")
        if not entry or not liq:
            continue
        symbol = p["symbol"]
        try:
            mark = _mexc_fair_price(symbol)
        except Exception as e:
            print(f"[liquidation/mexc] Не удалось получить текущую цену {symbol}: {e}")
            continue
        out.append({"symbol": symbol, "entry_price": float(entry), "liq_price": float(liq), "mark_price": mark})
    return out


def _lighter_liquidation_positions(secrets: dict) -> list:
    """
    entry_price/liq_price — поля avg_entry_price/liquidation_price того же
    сырого ответа позиций, что и в short_position_tracker._fetch_lighter_
    open_shorts (через общий _cached_raw_positions); mark_price — отдельный
    публичный запрос fetch_lighter_mark_prices() (см. докстринг модуля про
    статус поддержки Lighter — поле liquidation_price не проверено против
    реального счёта).
    """
    markets = fetch_lighter_markets()
    mark_prices = fetch_lighter_mark_prices()
    items = _cached_raw_positions("lighter", lambda: _lighter_positions_raw(
        secrets["lighter_account_index"], secrets["lighter_auth_token"],
    ))
    out = []
    for pos in items:
        size = float(pos.get("position", pos.get("size", pos.get("position_size", 0))) or 0)
        if size == 0:
            continue
        sign = pos.get("sign")
        # "sign" не задокументирован официально — см. докстринг short_position_
        # tracker.py про то же предположение (отрицательное = шорт).
        is_short = (int(sign) < 0) if sign is not None else (size < 0)
        if not is_short:
            continue
        entry = None
        for key in ("avg_entry_price", "entry_price", "avgEntryPrice", "entryPrice"):
            if pos.get(key) not in (None, ""):
                entry = float(pos[key])
                break
        liq = pos.get("liquidation_price")
        if entry is None or not liq:
            continue
        market_id = pos.get("market_id", pos.get("market_index"))
        symbol = markets.get(market_id, f"MARKET_{market_id}")
        mark = mark_prices.get(symbol)
        if mark is None:
            continue
        out.append({"symbol": symbol, "entry_price": entry, "liq_price": float(liq), "mark_price": mark})
    return out


# Только биржи с подтверждённым полем цены ликвидации в ответе — см. докстринг
# модуля (Lighter добавлен 06.10.2026, с оговоркой про непроверенность поля).
_LIQUIDATION_POSITION_FETCHERS = {
    "bybit": (_bybit_liquidation_positions, "bybit_api_key"),
    "aster": (_aster_liquidation_positions, "user"),
    "gate": (_gate_liquidation_positions, "gate_api_key"),
    "kucoin": (_kucoin_liquidation_positions, "kucoin_api_key"),
    "mexc": (_mexc_liquidation_positions, "mexc_api_key"),
    "lighter": (_lighter_liquidation_positions, "lighter_account_index"),
}


def _fmt_alert(exchange: str, symbol: str, entry: float, mark: float, liq: float, pct: float) -> str:
    label = _LABELS.get(exchange, exchange)
    return (
        f"🚨 {label} {symbol}: цена прошла {pct * 100:.0f}% пути от входа до ликвидации\n"
        f"Вход: {entry:g} → сейчас: {mark:g} → ликвидация: {liq:g}"
    )


def check_liquidation_alerts(secrets: dict, state: dict) -> None:
    """
    Один проход проверки: обновляет state на месте, шлёт алерт в Telegram
    при переходе доли пройденного пути до ликвидации из "меньше порога" в
    "порог и больше" по (exchange, symbol). См. докстринг модуля про
    формулу и про защиту от ложного сброса state при сбое API одной биржи.
    """
    token = secrets["telegram_token"]
    chat_ids = secrets["telegram_chat_ids"]

    seen_keys = set()
    failed_exchanges = set()

    for exchange, (fetcher, secret_key) in _LIQUIDATION_POSITION_FETCHERS.items():
        if secret_key not in secrets:
            continue
        try:
            positions = fetcher(secrets)
        except Exception as e:
            print(f"[liquidation/{exchange}] Не удалось получить позиции: {e}")
            failed_exchanges.add(exchange)
            continue

        for p in positions:
            symbol = p["symbol"]
            key = (exchange, symbol)

            distance = p["liq_price"] - p["entry_price"]
            if distance <= 0:
                # Не похоже на валидную цену ликвидации шорта (см. докстринг
                # модуля, например кросс-маржа) — пропускаем эту позицию, НЕ
                # отмечая её как "виденную" в этом проходе: если данные о
                # цене ликвидации по этому символу временно невалидны, но
                # раньше уже была сработавшая тревога — пусть state сохранится
                # как есть, а не сбросится молча.
                continue

            seen_keys.add(key)
            pct = (p["mark_price"] - p["entry_price"]) / distance
            was_triggered = state.get(key, False)
            is_triggered = pct >= LIQUIDATION_ALERT_THRESHOLD

            if is_triggered and not was_triggered:
                text = _fmt_alert(exchange, symbol, p["entry_price"], p["mark_price"], p["liq_price"], pct)
                send_telegram_broadcast(token, chat_ids, text)
                print(f"[liquidation] Отправлен алерт: {exchange} {symbol} {pct:.1%}")

            state[key] = is_triggered

    # Символы, которых больше нет в открытых позициях — сброс state, ТОЛЬКО
    # для бирж, чей запрос в этом проходе прошёл успешно (см. докстринг
    # модуля про класс бага "сбой API == позиция закрылась").
    for key in list(state.keys()):
        if key not in seen_keys and key[0] not in failed_exchanges:
            del state[key]


def liquidation_alert_loop(secrets: dict | None = None) -> None:
    """Бесконечный цикл проверки раз в LIQUIDATION_CHECK_INTERVAL_MINUTES минут."""
    if secrets is None:
        secrets = load_secrets()
    state: dict = {}
    interval_s = max(30.0, LIQUIDATION_CHECK_INTERVAL_MINUTES * 60)
    print(f"[liquidation] Запущен цикл проверки риска ликвидации каждые "
          f"{LIQUIDATION_CHECK_INTERVAL_MINUTES:.0f} мин, порог {LIQUIDATION_ALERT_THRESHOLD:.0%}.", flush=True)

    while True:
        try:
            check_liquidation_alerts(secrets, state)
        except Exception as e:
            print(f"[liquidation] Ошибка цикла проверки: {e}", flush=True)
        time.sleep(interval_s)
