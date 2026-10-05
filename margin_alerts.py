#!/usr/bin/env python3
"""
Алерт о приближении НЕРЕАЛИЗОВАННОГО УБЫТКА к порогу от поддерживаемой
маржи на фьючерсном счёте биржи — согласовано с пользователем явно (порог
70%, формула и её источник — ниже). Сначала только MEXC.

ФОРМУЛА (согласована с пользователем явно — ДВА РАЗА, на конкретных
числах, после того как первая версия формулы оказалась неверной):
  margin_base = сумма на фьючерсном счёте - нереализованный PNL (со знаком)
Пример от пользователя: на счёте 200 USDT, нереализованный PNL -100 USDT
(убыток) -> поддерживаемая маржа = 200 - (-100) = 300 USDT. То есть margin
РАСТЁТ по мере роста убытка — это НЕ стандартная механика "equity" (где
margin, наоборот, падает с убытком: equity = cashBalance + unrealized);
пользователь явно подтвердил именно такое, растущее с убытком, поведение
через прямой выбор между двумя вариантами (см. историю правок модуля).

"Сумма на фьючерсном счёте" — это поле cashBalance ("Withdrawable
balance" по офиц. docs MEXC) — оно НЕ включает floating PnL открытых
позиций, поэтому берётся из ответа account/assets напрямую, без вычислений
через equity:
  margin_base = cashBalance - unrealized

ВАЖНЫЙ МАТЕМАТИЧЕСКИЙ НЮАНС (идентифицирован и сообщён пользователю,
проверьте, не нужно ли скорректировать порог): поскольку знаменатель сам
РАСТЁТ вместе с убытком, порог "70% от margin_base" требует значительно
БОЛЬШЕГО убытка, чем 70% от исходного cashBalance. Алгебраически, при
L = |unrealized| (убыток), условие алерта
  L >= THRESHOLD * (cashBalance + L)
сводится к
  L >= [THRESHOLD / (1 - THRESHOLD)] * cashBalance
При THRESHOLD = 0.70 это L >= 2.33 * cashBalance — то есть алерт сработает
только когда убыток уже БОЛЕЕ ЧЕМ В ДВА РАЗА превышает весь депонированный
капитал (эквивалентно equity = cashBalance + unrealized уже значительно
ОТРИЦАТЕЛЬНОЙ). На практике биржа обычно ликвидирует позицию значительно
раньше этой точки (примерно когда equity приближается к нулю, т.е.
L ≈ cashBalance, что по ЭТОЙ формуле соответствует всего ~50% от
margin_base). Это означает, что при пороге 0.70 алерт скорее всего НЕ
успеет сработать до реальной ликвидации — сообщено пользователю явно,
порог можно изменить (переменная окружения MARGIN_ALERT_THRESHOLD) или
формулу пересмотреть, если это не то поведение, которое нужно.

Алерт срабатывает, когда unrealized ОТРИЦАТЕЛЕН и его модуль достиг
MARGIN_ALERT_THRESHOLD (0.70 = 70%) от margin_base:
  unrealized <= -MARGIN_ALERT_THRESHOLD * margin_base

EDGE-TRIGGERED, как и funding/ликвидационные алерты: отправляется один раз
при переходе доли убытка из "меньше порога" в "порог и больше" по каждой
(exchange, currency) — состояние сбрасывается, когда доля опускается
обратно ниже порога (включая случай unrealized >= 0 — позиция в плюсе
или закрыта).

ЗАЩИТА от того же класса бага, что и в liquidation_alerts.py/sltp_alerts.py:
временный сбой API биржи не должен стирать state (который иначе читался
бы как "margin больше не в опасной зоне").

Структура сознательно рассчитана на расширение на другие биржи —
_MARGIN_FETCHERS, как и _LIQUIDATION_POSITION_FETCHERS в
liquidation_alerts.py, одна запись на биржу.
"""

import os
import time

from balances import _mexc_account_assets_raw
from funding_report import load_secrets, send_telegram_broadcast

MARGIN_CHECK_INTERVAL_MINUTES = float(os.environ.get("MARGIN_CHECK_INTERVAL_MINUTES", "5"))
MARGIN_ALERT_THRESHOLD = float(os.environ.get("MARGIN_ALERT_THRESHOLD", "0.70"))

_LABELS = {"mexc": "MEXC"}


def _mexc_margin_snapshot(secrets: dict) -> list:
    """
    Список {"currency", "margin_base", "unrealized"} по каждой валюте
    фьючерсного счёта MEXC — margin_base = cashBalance - unrealized (см.
    докстринг модуля про формулу и про важный нюанс с растущим
    знаменателем). Валюты с нулевым/отрицательным cashBalance (нет
    задепонированного капитала) пропускаются — там нет смысла гонять их
    через state.
    """
    raw = _mexc_account_assets_raw(secrets["mexc_api_key"], secrets["mexc_api_secret"])
    out = []
    for a in raw:
        cash_balance = float(a.get("cashBalance", 0) or 0)
        if cash_balance <= 0:
            continue
        unrealized = float(a.get("unrealized", 0) or 0)
        out.append({
            "currency": a.get("currency", "?"),
            "margin_base": cash_balance - unrealized,
            "unrealized": unrealized,
        })
    return out


_MARGIN_FETCHERS = {
    "mexc": (_mexc_margin_snapshot, "mexc_api_key"),
}


def _fmt_alert(exchange: str, currency: str, margin_base: float, unrealized: float, pct: float) -> str:
    label = _LABELS.get(exchange, exchange)
    return (
        f"🚨 {label} ({currency}): нереализованный убыток достиг {pct * 100:.0f}% от маржи на счёте\n"
        f"Маржа: {margin_base:g} {currency} → нереализованный PNL: {unrealized:g} {currency}"
    )


def check_margin_alerts(secrets: dict, state: dict) -> None:
    """
    Один проход проверки: обновляет state на месте, шлёт алерт в Telegram
    при переходе доли убытка из "меньше порога" в "порог и больше" по
    (exchange, currency). См. докстринг модуля про формулу и защиту от
    ложного сброса state при сбое API.
    """
    token = secrets["telegram_token"]
    chat_ids = secrets["telegram_chat_ids"]

    seen_keys = set()
    failed_exchanges = set()

    for exchange, (fetcher, secret_key) in _MARGIN_FETCHERS.items():
        if secret_key not in secrets:
            continue
        try:
            snapshot = fetcher(secrets)
        except Exception as e:
            print(f"[margin/{exchange}] Не удалось получить баланс счёта: {e}")
            failed_exchanges.add(exchange)
            continue

        for item in snapshot:
            currency = item["currency"]
            key = (exchange, currency)
            seen_keys.add(key)

            margin_base, unrealized = item["margin_base"], item["unrealized"]
            pct = (-unrealized / margin_base) if unrealized < 0 and margin_base > 0 else 0.0
            was_triggered = state.get(key, False)
            is_triggered = unrealized < 0 and pct >= MARGIN_ALERT_THRESHOLD

            if is_triggered and not was_triggered:
                text = _fmt_alert(exchange, currency, margin_base, unrealized, pct)
                send_telegram_broadcast(token, chat_ids, text)
                print(f"[margin] Отправлен алерт: {exchange} {currency} {pct:.1%}")

            state[key] = is_triggered

    # См. докстринг модуля/liquidation_alerts.py про класс бага "сбой API ==
    # опасность исчезла" — state не трогаем для бирж, запрос к которым в
    # этом проходе завершился ошибкой.
    for key in list(state.keys()):
        if key not in seen_keys and key[0] not in failed_exchanges:
            del state[key]


def margin_alert_loop(secrets: dict | None = None) -> None:
    """Бесконечный цикл проверки раз в MARGIN_CHECK_INTERVAL_MINUTES минут."""
    if secrets is None:
        secrets = load_secrets()
    state: dict = {}
    interval_s = max(30.0, MARGIN_CHECK_INTERVAL_MINUTES * 60)
    print(f"[margin] Запущен цикл проверки маржи каждые "
          f"{MARGIN_CHECK_INTERVAL_MINUTES:.0f} мин, порог {MARGIN_ALERT_THRESHOLD:.0%}.", flush=True)

    while True:
        try:
            check_margin_alerts(secrets, state)
        except Exception as e:
            print(f"[margin] Ошибка цикла проверки: {e}", flush=True)
        time.sleep(interval_s)
