#!/usr/bin/env python3
"""
Алерт о приближении НЕРЕАЛИЗОВАННОГО УБЫТКА к порогу от поддерживаемой
маржи на фьючерсном счёте биржи — согласовано с пользователем явно (порог
70%, формула и её источник — ниже). Сначала только MEXC.

ФОРМУЛА (согласована с пользователем явно — после ДВУХ неверных попыток,
проверена по РЕАЛЬНЫМ сырым данным его счёта, см. историю правок модуля):
  margin_base = cashBalance + positionMargin
Обе величины — сырые поля того же ответа account/assets, НЕ зависящие от
текущего unrealized PnL: cashBalance ("Withdrawable balance") — свободные,
не занятые ни одной позицией деньги; positionMargin — маржа, уже
заложенная под открытую позицию. Их сумма — весь РЕАЛЬНЫЙ капитал на
фьючерсном счёте (не считая floating PnL), который не скачет вместе с
текущей ценой — только от депозита/вывода или изменения размера позиции.

Проверено на реальном счёте пользователя (диагностический лог сырых
полей, 2026-10-05): cashBalance=0.369, positionMargin=395.458,
unrealized=-169.40, equity=226.43. Пользователь подтвердил margin_base≈395
("228-(-167)=395" — 228 на экране это equity в его момент снятия
показаний, а equity - unrealized = cashBalance + positionMargin — то же
самое тождество, другими словами). Совпадает с positionMargin+cashBalance
=395.83 с точностью до момента снятия показаний.

В отличие от двух предыдущих версий, у ЭТОЙ формулы margin_base НЕ зависит
от unrealized вообще — поэтому нет самореференции знаменателя: порог
MARGIN_ALERT_THRESHOLD (0.70 = 70%) — это ровно 70% от реального,
статичного капитала счёта, без побочных математических эффектов.

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
    фьючерсного счёта MEXC — margin_base = cashBalance + positionMargin
    (см. докстринг модуля про формулу, проверенную на реальных данных).
    Валюты с нулевым margin_base (нет ни свободных денег, ни маржи под
    позицией) пропускаются — там нет смысла гонять их через state.
    """
    raw = _mexc_account_assets_raw(secrets["mexc_api_key"], secrets["mexc_api_secret"])
    out = []
    for a in raw:
        cash_balance = float(a.get("cashBalance", 0) or 0)
        position_margin = float(a.get("positionMargin", 0) or 0)
        margin_base = cash_balance + position_margin
        if margin_base <= 0:
            continue
        out.append({
            "currency": a.get("currency", "?"),
            "margin_base": margin_base,
            "unrealized": float(a.get("unrealized", 0) or 0),
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
