#!/usr/bin/env python3
"""
Алерт о приближении нереализованного убытка к порогу от поддерживаемой
маржи на фьючерсном счёте. Поддерживаются Gate, Lighter, dYdX и HTX.

Общая логика находится в этом модуле, а различия форматов аккаунта и
источников PnL изолированы в отдельных fetcher-функциях. Порог задаётся
через MARGIN_ALERT_THRESHOLD (по умолчанию 0.70 = 70%).

GATE — та же идея, аналогичные поля того же ответа GET /api/v4/futures/
{settle}/accounts (ПОДТВЕРЖДЕНО по офиц. докам Gate API v4): total =
position_margin + order_margin + available — то есть total уже САМ есть
margin_base (сумма всех трёх компонентов капитала счёта, без unrealized
PnL, который отдаётся отдельным полем unrealised_pnl) — никакого
дополнительного сложения полей не требуется:
  margin_base = total
  unrealized  = unrealised_pnl

LIGHTER — для маржи используется агрегированное поле collateral из
официального WebSocket-канала user_stats/{account_index}. В отличие от
REST account.collateral, этот account-stats канал предназначен именно для
показателей collateral, leverage и margin usage, включая isolated margin.
  margin_base = user_stats.stats.collateral
  unrealized  = sum(position.unrealized_pnl)
Так не приходится вручную складывать account collateral и allocated_margin
и рисковать задвоить isolated collateral.

DYDX (v4) — margin_base = equity (поле саб-аккаунта, см. _dydx_margin_
snapshot ниже), unrealized = сумма unrealizedPnl по открытым позициям.
В ОТЛИЧИЕ от Lighter, equity у dYdX УЖЕ включает floating PnL
(Indexer не отдаёт отдельного поля "капитал без PnL") — порог 70%
здесь означает "убыток съел 70% текущего equity", а не "70% от статичного
капитала без PnL", как у остальных трёх бирж. Цены ликвидации для dYdX
пока нет ни в этом алерте, ни в /rates — Indexer не отдаёт её напрямую
(см. README).

HTX — GET /v5/account/balance (новый API, см. докстринг funding_report.
_htx_cross_account_raw про миграцию со старого /linear-swap-api/v1/
swap_cross_account_info — тот отключён, реальный случай 19.10.2026).
margin_base = equity - profit_unreal (на каждую валюту из "details") —
в новом API нет готового поля "статический капитал без PnL" (margin_static
было в старом), поэтому вычитается вручную, в отличие от предыдущей
версии. equity/profit_unreal у HTX включают cross и isolated вперемешку
— для этого бота не проблема (используется только cross). НЕ ПРОВЕРЕНО
на реальном счёте — первый реальный алерт по HTX стоит сверить с суммами
на экране приложения HTX.

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

from balances import _gate_futures_account_raw
from funding_report import (
    load_secrets, send_telegram_broadcast, _cached_raw_positions,
    _lighter_user_stats_raw, _lighter_positions_raw,
    _dydx_subaccount_raw, _htx_cross_account_raw,
)

MARGIN_CHECK_INTERVAL_MINUTES = float(os.environ.get("MARGIN_CHECK_INTERVAL_MINUTES", "5"))
MARGIN_ALERT_THRESHOLD = float(os.environ.get("MARGIN_ALERT_THRESHOLD", "0.70"))

_LABELS = {"gate": "Gate", "lighter": "Lighter", "dydx": "dYdX", "htx": "HTX"}


def _gate_margin_snapshot(secrets: dict, settle: str = "usdt") -> list:
    """
    Список {"currency", "margin_base", "unrealized"} для фьючерсного счёта
    Gate (settle-валюта, на практике почти всегда только usdt, см. README)
    — margin_base = total (уже сам сумма position_margin+order_margin+
    available, см. докстринг модуля), unrealized = unrealised_pnl. В
    отличие от Gate — единственный аккаунт на settle-валюту, а не список
    по всем валютам, поэтому здесь список из 0 или 1 элемента.
    """
    data = _gate_futures_account_raw(secrets["gate_api_key"], secrets["gate_api_secret"], settle)
    margin_base = float(data.get("total", 0) or 0)
    if margin_base <= 0:
        return []
    return [{
        "currency": (data.get("currency") or settle).upper(),
        "margin_base": margin_base,
        "unrealized": float(data.get("unrealised_pnl", 0) or 0),
    }]


def _lighter_margin_snapshot(secrets: dict) -> list:
    """
    Список {"currency", "margin_base", "unrealized"} для счёта Lighter —
    margin_base = collateral аккаунта + allocated_margin всех isolated-
    позиций (margin_mode == 1), unrealized = сумма unrealized_pnl по всем
    открытым позициям. Cross collateral и isolated allocated margin —
    отдельные компоненты обеспечения; их нельзя заменять друг другом.
    Валюта всегда USDC —
    Lighter торгуется с расчётами в USDC (тот же факт, что уже используется
    в funding_report.fetch_lighter).
    """
    stats = _cached_raw_positions("lighter:user_stats", lambda: _lighter_user_stats_raw(
        secrets["lighter_account_index"], secrets["lighter_auth_token"],
    ))
    positions = _cached_raw_positions("lighter", lambda: _lighter_positions_raw(
        secrets["lighter_account_index"], secrets["lighter_auth_token"],
    ))
    margin_base = float(stats.get("collateral", 0) or 0)
    unrealized = sum(float(pos.get("unrealized_pnl", 0) or 0) for pos in positions)
    if margin_base <= 0:
        return []
    return [{"currency": "USDC", "margin_base": margin_base, "unrealized": unrealized}]


def _dydx_margin_snapshot(secrets: dict) -> list:
    """
    margin_base = equity (поле саб-аккаунта dYdX — весь капитал счёта,
    включая floating PnL, см. комментарий над funding_report.
    _dydx_subaccount_raw; ЭТО ОТЛИЧАЕТСЯ от Gate/Lighter, где
    margin_base сознательно НЕ включает unrealized PnL — у dYdX
    отдельного "капитал без floating PnL" поля в ответе Indexer нет,
    equity — единственная прямая метрика "сколько реально на счету", и
    именно она используется биржей для расчёта риска ликвидации, поэтому
    используем её как есть, а не пытаемся вычесть PnL вручную и
    рискнуть ошибиться в формуле). unrealized — сумма unrealizedPnl по
    всем открытым позициям саб-аккаунта. Валюта — USDC (подтверждено
    живым ответом Indexer: assetPositions.USDC).
    """
    subaccount = _cached_raw_positions("dydx", lambda: _dydx_subaccount_raw(
        secrets["dydx_address"], secrets["dydx_subaccount_number"],
    ))
    margin_base = float(subaccount.get("equity", 0) or 0)
    if margin_base <= 0:
        return []
    unrealized = sum(
        float(pos.get("unrealizedPnl", 0) or 0)
        for pos in subaccount.get("openPerpetualPositions", {}).values()
    )
    return [{"currency": "USDC", "margin_base": margin_base, "unrealized": unrealized}]


def _htx_margin_snapshot(secrets: dict) -> list:
    """
    HTX перешла на новый /v5/account/balance (см. докстринг
    funding_report._htx_cross_account_raw про полную историю миграции) —
    структура ответа изменилась: один объект на аккаунт, разбивка по
    валютам в "details", без отдельного поля "статический капитал без
    PnL" (margin_static), которое было в старом API. margin_base
    вычисляется как equity - profit_unreal на каждую валюту (та же идея,
    что и раньше — "эквити минус то, что в нём floating", просто без
    готового поля под неё). per-currency equity/profit_unreal у HTX
    включают ОБЕ маржи (cross и isolated) вперемешку — отдельного
    cross-only среза в ответе нет; для этого бота это не проблема на
    практике (поддерживается только cross margin, isolated не
    используется — см. докстринг _htx_cross_account_raw), но если
    когда-нибудь появится isolated-позиция, margin_base/unrealized будут
    завышены на её часть. НЕ ПРОВЕРЕНО на реальном счёте (нет доступа к
    аккаунту из этой среды разработки) — первый реальный алерт по HTX
    стоит явно сверить с суммами на экране приложения HTX.
    """
    account = _cached_raw_positions("htx:account", lambda: _htx_cross_account_raw(
        secrets["htx_api_key"], secrets["htx_api_secret"],
    ))
    out = []
    for d in account.get("details", []):
        equity = float(d.get("equity", 0) or 0)
        unrealized = float(d.get("profit_unreal", 0) or 0)
        margin_base = equity - unrealized
        if margin_base <= 0:
            continue
        out.append({
            "currency": d.get("currency", "USDT"),
            "margin_base": margin_base,
            "unrealized": unrealized,
        })
    return out


_MARGIN_FETCHERS = {
    "gate": (_gate_margin_snapshot, "gate_api_key"),
    "lighter": (_lighter_margin_snapshot, "lighter_account_index"),
    "dydx": (_dydx_margin_snapshot, "dydx_address"),
    "htx": (_htx_margin_snapshot, "htx_api_key"),
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
