#!/usr/bin/env python3
"""
Алерт о приближении НЕРЕАЛИЗОВАННОГО УБЫТКА к порогу от поддерживаемой
маржи на фьючерсном счёте биржи — согласовано с пользователем явно (порог
70%, формула и её источник — ниже). Сначала только MEXC.

ФОРМУЛА (согласована с пользователем явно, двумя сообщениями):
  margin_base = сумма на фьючерсном счёте + нереализованный PNL
Для MEXC "сумма на фьючерсном счёте" (без PnL) — это поле cashBalance
("Withdrawable balance" по офиц. docs), а "нереализованный PNL" — поле
unrealized. То есть:
  margin_base = cashBalance + unrealized
Это АЛГЕБРАИЧЕСКИ РАВНО полю equity ("Total equity" по офиц. docs) —
третьему полю того же ответа account/assets, которое и так уже показывает
команда /balance (см. balances.fetch_mexc_futures_balance). Никакого
двойного учёта PnL здесь нет: equity уже включает unrealized, складывать
unrealized ещё раз сверху НЕ нужно — это подтверждено явно пользователем
(уточняющий вопрос был задан именно из-за этого риска).

Алерт срабатывает, когда unrealized ОТРИЦАТЕЛЕН и его модуль достиг
MARGIN_ALERT_THRESHOLD (0.70 = 70%) от margin_base:
  unrealized <= -MARGIN_ALERT_THRESHOLD * margin_base

ВАЖНО (самореференция знаменателя): margin_base сам УМЕНЬШАЕТСЯ по мере
роста убытка (margin_base = cashBalance + unrealized, а unrealized < 0) —
поэтому порог "70% от margin_base" — это НЕ то же самое, что "70% от
фиксированного cashBalance". Алгебраически:
  -unrealized >= 0.70 * (cashBalance + unrealized)
  -unrealized - 0.70*unrealized >= 0.70 * cashBalance   (unrealized < 0)
  -1.70 * unrealized >= 0.70 * cashBalance
  -unrealized >= (0.70/1.70) * cashBalance ≈ 0.4118 * cashBalance
То есть условие срабатывает уже при убытке примерно в 41.2% от исходного
cashBalance, а не в 70% — если нужен порог именно от фиксированной суммы
на счёте (без самоуменьшения знаменателя убытком), скажите — формула легко
меняется на unrealized <= -threshold * cashBalance.

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
    Список {"currency", "equity", "unrealized"} по каждой валюте фьючерсного
    счёта MEXC (см. докстринг модуля — equity тут и есть margin_base).
    Валюты с нулевым equity (нет позиции и ничего не заведено) пропускаются —
    формально 0 всегда даст pct=0, но нет смысла гонять их через state.
    """
    raw = _mexc_account_assets_raw(secrets["mexc_api_key"], secrets["mexc_api_secret"])
    out = []
    for a in raw:
        equity = float(a.get("equity", 0) or 0)
        if equity <= 0:
            continue
        out.append({
            "currency": a.get("currency", "?"),
            "equity": equity,
            "unrealized": float(a.get("unrealized", 0) or 0),
        })
    return out


_MARGIN_FETCHERS = {
    "mexc": (_mexc_margin_snapshot, "mexc_api_key"),
}


def _fmt_alert(exchange: str, currency: str, equity: float, unrealized: float, pct: float) -> str:
    label = _LABELS.get(exchange, exchange)
    return (
        f"🚨 {label} ({currency}): нереализованный убыток достиг {pct * 100:.0f}% от маржи на счёте\n"
        f"Маржа (equity): {equity:g} {currency} → нереализованный PNL: {unrealized:g} {currency}"
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

            equity, unrealized = item["equity"], item["unrealized"]
            pct = (-unrealized / equity) if unrealized < 0 else 0.0
            was_triggered = state.get(key, False)
            is_triggered = unrealized < 0 and pct >= MARGIN_ALERT_THRESHOLD

            if is_triggered and not was_triggered:
                text = _fmt_alert(exchange, currency, equity, unrealized, pct)
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
