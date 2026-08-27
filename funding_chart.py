#!/usr/bin/env python3
"""
График годовой ставки funding (APR) по всем открытым сейчас позициям —
дополнение к текстовому отчёту по кнопке "📈 Открытые позиции"
(см. bot_poll.py: send_open_positions_report()).

ЧТО НА ГРАФИКЕ:
  - Одна линия на каждую открытую сейчас позицию (пара биржа+символ);
    цвет линии — по бирже (EXCHANGE_COLORS), так что позиции на одной
    бирже совпадают цветом, а различить их можно по подписи в легенде
    "Биржа: СИМВОЛ".
  - По оси X — время (UTC), от даты открытия САМОЙ РАННЕЙ из всех сейчас
    открытых позиций (на любой бирже) до текущего момента. Линия каждой
    отдельной позиции начинается со своего собственного момента открытия
    (если он позже глобального начала графика) — до этого момента для
    неё просто нет данных, разрыв в линии, а не нулевая/придуманная ставка.
  - По оси Y — ГОДОВАЯ ставка funding (APR, %), а не "ставка за одну
    выплату" — периодичность выплат разная по биржам и даже по символам
    (1ч/4ч/8ч), поэтому сырые ставки за выплату между собой не сравнить
    напрямую. Приведение к годовым — funding_alerts.annualize(), та же
    функция, что и в отчёте по кнопке "🔮 Ставка финансирования".
  - Сетка по оси X — шаг GRID_STEP_HOURS часов (по умолчанию 4): реальная
    история ставок funding "проецируется" на эту сетку методом step-hold
    (в каждой точке сетки берётся последняя РЕАЛЬНО применённая на этот
    момент ставка) — так позиции с разной периодичностью выплат оказываются
    на одной сравнимой шкале, а не дёргаются каждая в свои моменты времени.

ИСТОЧНИК ДАННЫХ — фактически ПРИМЕНЁННЫЕ (не прогнозные) ставки funding:
  - Bybit, Lighter — берутся из уже полученных для текстовой части отчёта
    записей fetch_all_time_open_positions() (funding_report.py), без
    дополнительных запросов к бирже: у Bybit в записи транзакции уже есть
    поле feeRate (реальная применённая ставка по конкретному начислению —
    но со знаком списания по счёту, а не рыночной ставки, знак
    переворачивается в _bybit_series_from_records, см. её докстринг),
    у Lighter — поле rate (см. WebSocket-схему PositionFunding в докстринге
    funding_report.fetch_lighter — тот же REST-эндпоинт отдаёт эти же поля).
  - Aster, MEXC, Gate — их записи о начислениях (income / funding_records /
    account_book) содержат только СУММУ в валюте, не сам процент ставки,
    поэтому для них отдельно (но так же без каких-либо ключей — это
    публичные рыночные данные) запрашивается история ставок funding по
    бирже в целом.

Построение графика — best-effort: если для какой-то отдельной позиции не
удалось получить историю ставок, эта позиция просто не попадает на график
(остальные строятся как обычно), а если не получилось построить график
целиком (нет вообще ни одной серии) — build_positions_apr_chart() вернёт
None, и вызывающий код (bot_poll.py) не станет прикладывать график к отчёту,
но текстовая часть отчёта к этому моменту уже отправлена.
"""

import time
from collections import defaultdict
from datetime import datetime, timezone

import requests

import matplotlib
matplotlib.use("Agg")  # без дисплея — рендерим сразу в файл
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patheffects as pe

from funding_report import _get_mexc_proxies, _get_gate_proxies, CONTINUOUS_FUNDING_GAP_HOURS
from funding_alerts import (
    get_predicted_rate,
    annualize,
    _fetch_aster_funding_intervals,
    ASTER_DEFAULT_FUNDING_INTERVAL_HOURS,
    BYBIT_DEFAULT_FUNDING_INTERVAL_HOURS,
    MEXC_DEFAULT_FUNDING_INTERVAL_HOURS,
    GATE_DEFAULT_FUNDING_INTERVAL_HOURS,
    LIGHTER_FUNDING_INTERVAL_HOURS,
)

EXCHANGE_LABELS = {
    "aster": "Aster", "bybit": "Bybit", "lighter": "Lighter",
    "mexc": "MEXC", "gate": "Gate",
}
# Кислотная неоновая палитра — тёмный фон + свечение линий, см. build_positions_apr_chart.
EXCHANGE_COLORS = {
    "aster":   "#00f0ff",  # кислотный циан
    "bybit":   "#faff00",  # кислотный жёлтый
    "lighter": "#ff00e6",  # кислотный маджента
    "mexc":    "#39ff14",  # кислотный зелёный
    "gate":    "#ff3d3d",  # кислотный красный
}

# Оформление графика: тёмный фон + светящиеся линии.
CHART_BG_COLOR = "#0a0a0a"
CHART_FG_COLOR = "#dcdcdc"
CHART_GRID_COLOR = "#2a2a2a"

# Шаг сетки по оси X — см. докстринг модуля, раздел "ЧТО НА ГРАФИКЕ".
GRID_STEP_HOURS = 4

# Выше этого числа точек сетки подписи по оси X переключаются на
# автоматическое прореживание (matplotlib AutoDateLocator) — иначе на
# длинных позициях (недели-месяцы) подписи каждые 4 часа превращаются в
# нечитаемое месиво. Сами ДАННЫЕ при этом всё равно считаются на полной
# 4-часовой сетке (см. _build_grid) — прореживаются только подписи осей,
# не сама точность графика.
MAX_TICKS_AT_FULL_RESOLUTION = 60

# График в любом случае не заглядывает дальше этого числа дней назад, даже
# если самая ранняя из открытых позиций старше — на практике связки редко
# висят открытыми дольше месяца, а чем длиннее график, тем менее читаема
# 4-часовая детализация на нём (см. MAX_TICKS_AT_FULL_RESOLUTION). Позиции
# старше этого порога просто не показывают свою историю целиком — график
# начинается с даты открытия САМОЙ РАННЕЙ позиции, но не раньше, чем
# MAX_CHART_LOOKBACK_DAYS дней назад.
MAX_CHART_LOOKBACK_DAYS = 30


# ── Публичная история ставок funding — Aster, MEXC, Gate ─────────────────────
# (без ключей: это рыночные данные биржи, не привязанные к аккаунту)

def _fetch_aster_rate_history(symbol: str, start_ms: int, end_ms: int) -> list:
    """
    [(fundingTime_ms, rate), ...] по возрастанию времени — публичный
    GET /fapi/v1/fundingRate, пагинация как в funding_report.fetch_aster()
    (лимит 1000 записей за раз, при усечении сдвигаем startTime на время
    последней полученной записи + 1мс).
    """
    all_points: list = []
    limit = 1000
    cur_start = start_ms
    while cur_start < end_ms:
        resp = requests.get(
            "https://fapi.asterdex.com/fapi/v1/fundingRate",
            params={"symbol": symbol, "startTime": cur_start, "endTime": end_ms, "limit": limit},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            break
        for item in data:
            all_points.append((int(item["fundingTime"]), float(item["fundingRate"])))
        if len(data) < limit:
            break
        cur_start = int(data[-1]["fundingTime"]) + 1
    return all_points


def _fetch_mexc_rate_history(symbol: str, start_ms: int, end_ms: int) -> list:
    """
    [(settleTime_ms, rate, interval_hours), ...] — публичный
    GET /api/v1/contract/funding_rate/history. Интервал (collectCycle,
    часы) приходит в каждой записи — в отличие от Aster/Gate, отдельно
    запрашивать не нужно. Постранично, новые записи на первой странице —
    останавливаемся, как только встретили запись старше start_ms.
    """
    all_points: list = []
    page_num = 1
    page_size = 100
    proxies = _get_mexc_proxies()
    while True:
        resp = requests.get(
            "https://api.mexc.com/api/v1/contract/funding_rate/history",
            params={"symbol": symbol, "page_num": page_num, "page_size": page_size},
            timeout=15, proxies=proxies,
        )
        resp.raise_for_status()
        data = resp.json()
        if not data.get("success", False):
            raise RuntimeError(f"MEXC funding_rate/history error {data.get('code')}: {data.get('message') or data}")
        payload = data.get("data") or {}
        items = payload.get("resultList", [])
        if not items:
            break

        reached_start = False
        for item in items:
            t = int(item["settleTime"])
            if t < start_ms:
                reached_start = True
                continue
            if t > end_ms:
                continue
            interval_hours = float(item.get("collectCycle") or MEXC_DEFAULT_FUNDING_INTERVAL_HOURS)
            all_points.append((t, float(item["fundingRate"]), interval_hours))

        total_page = payload.get("totalPage", 1)
        if reached_start or page_num >= total_page:
            break
        page_num += 1
    return all_points


def _fetch_gate_rate_history(symbol: str, start_ms: int, end_ms: int, settle: str = "usdt") -> list:
    """
    [(time_ms, rate), ...] — публичный GET /futures/{settle}/funding_rate.
    Поля ответа по документации Gate API v4 (модель FundingRateRecord) —
    "r" (ставка) и "t" (unix-время в секундах). Не проверено вживую (у
    сессии Claude нет доступа к api.gateio.ws) — если формат вдруг другой,
    записи просто пропускаются (не роняют остальной график), как уже было
    с MEXC contractSize — см. funding-bot-handoff.md.
    """
    proxies = _get_gate_proxies()
    resp = requests.get(
        f"https://api.gateio.ws/api/v4/futures/{settle}/funding_rate",
        params={"contract": symbol, "limit": 1000, "from": start_ms // 1000, "to": end_ms // 1000},
        timeout=15, proxies=proxies,
    )
    resp.raise_for_status()
    data = resp.json()
    if isinstance(data, dict) and data.get("label"):
        raise RuntimeError(f"Gate funding_rate error {data.get('label')}: {data.get('message')}")

    all_points: list = []
    skipped = 0
    for item in data if isinstance(data, list) else []:
        t_raw, r_raw = item.get("t"), item.get("r")
        if t_raw is None or r_raw is None:
            skipped += 1
            continue
        all_points.append((int(float(t_raw)) * 1000, float(r_raw)))

    if skipped and not all_points:
        print(
            f"[chart/gate] Ни одной записи не распозналось в ответе funding_rate "
            f"для {symbol} — формат полей отличается от ожидаемого 'r'/'t'."
        )
    return all_points


# ── Реальные ставки из уже полученных записей — Bybit, Lighter ───────────────

def _bybit_series_from_records(records: list) -> dict:
    """symbol -> [(transactionTime_ms, rate), ...] из поля feeRate записей
    транзакций (уже получены для текстовой части отчёта, см. докстринг модуля).

    ЗНАК: в истории транзакций (/v5/account/transaction-log, type=SETTLEMENT)
    Bybit указывает feeRate со знаком списания по счёту (минус — когда по
    этому начислению аккаунт заплатил), а не со знаком рыночной ставки
    funding, в отличие от остальных бирж в этом модуле (Aster/MEXC/Gate/
    Lighter — там сохраняется знак самой рыночной ставки: рынок в плюсе —
    лонги платят шортам, независимо от того, на какой вы стороне). Из-за
    этого при построении графика знак Bybit оказывался зеркальным
    относительно остальных бирж — переворачиваем здесь же, один раз, чтобы
    все серии на графике были в одной системе координат."""
    by_symbol: dict = defaultdict(list)
    for r in records:
        sym, t, rate = r.get("symbol"), r.get("transactionTime"), r.get("feeRate")
        if sym is None or t is None or rate in (None, ""):
            continue
        by_symbol[sym].append((int(t), -float(rate)))
    for points in by_symbol.values():
        points.sort()
    return by_symbol


def _lighter_series_from_records(records: list) -> dict:
    """symbol -> [(timestamp_ms, rate), ...] из поля rate записей
    positionFunding (уже получены для текстовой части отчёта)."""
    by_symbol: dict = defaultdict(list)
    for r in records:
        sym, t, rate = r.get("symbol"), r.get("timestamp"), r.get("rate")
        if sym is None or t is None or rate in (None, ""):
            continue
        by_symbol[sym].append((int(float(t)) * 1000, float(rate)))
    for points in by_symbol.values():
        points.sort()
    return by_symbol


# ── Сбор всех серий (биржа, символ) -> [(time_ms, rate, interval_hours)] ─────

def _collect_series(open_results: dict) -> dict:
    """
    open_results — результат funding_report.fetch_all_time_open_positions(),
    переданный вызывающим кодом (bot_poll.py), а не запрошенный заново —
    он уже понадобился для текстовой части отчёта, повторный запрос ко всем
    биржам удвоил бы время построения отчёта без всякой пользы.

    Возвращает {(exchange, symbol): [(time_ms, rate, interval_hours), ...]}
    отсортированные по времени. Биржи/символы, для которых не удалось
    получить ни одной точки, просто отсутствуют в результате.

    История дальше MAX_CHART_LOOKBACK_DAYS дней назад не собирается вовсе
    (не только не показывается, а даже не запрашивается лишним запросом к
    бирже для Aster/MEXC/Gate) — см. докстринг константы.
    """
    series: dict = {}
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - MAX_CHART_LOOKBACK_DAYS * 24 * 60 * 60 * 1000

    for exchange, (records, error) in open_results.items():
        if error or not records:
            continue

        if exchange == "bybit":
            for symbol, points in _bybit_series_from_records(records).items():
                points = [(t, r) for t, r in points if t >= cutoff_ms]
                if not points:
                    continue
                try:
                    _, _, interval_hours = get_predicted_rate("bybit", symbol)
                except Exception as e:
                    print(f"[chart/bybit] интервал для {symbol}: беру дефолт, {e}")
                    interval_hours = BYBIT_DEFAULT_FUNDING_INTERVAL_HOURS
                series[("bybit", symbol)] = [(t, r, interval_hours) for t, r in points]

        elif exchange == "lighter":
            for symbol, points in _lighter_series_from_records(records).items():
                points = [(t, r) for t, r in points if t >= cutoff_ms]
                if points:
                    series[("lighter", symbol)] = [(t, r, LIGHTER_FUNDING_INTERVAL_HOURS) for t, r in points]

        elif exchange in ("aster", "mexc", "gate"):
            time_field = {"aster": "time", "mexc": "settleTime", "gate": "time"}[exchange]
            time_in_seconds = exchange == "gate"

            by_symbol_times: dict = defaultdict(list)
            for r in records:
                sym, raw_t = r.get("symbol"), r.get(time_field)
                if not sym or raw_t is None:
                    continue
                t_ms = int(float(raw_t)) * (1000 if time_in_seconds else 1)
                by_symbol_times[sym].append(t_ms)

            for symbol, times in by_symbol_times.items():
                # max(), не min() — не запрашиваем историю раньше cutoff_ms,
                # даже если позиция открыта раньше (см. MAX_CHART_LOOKBACK_DAYS)
                start_ms = max(min(times), cutoff_ms)
                try:
                    if exchange == "aster":
                        raw_points = _fetch_aster_rate_history(symbol, start_ms, now_ms)
                        interval_hours = _fetch_aster_funding_intervals().get(
                            symbol, ASTER_DEFAULT_FUNDING_INTERVAL_HOURS,
                        )
                        points = [(t, r, interval_hours) for t, r in raw_points]
                    elif exchange == "mexc":
                        points = _fetch_mexc_rate_history(symbol, start_ms, now_ms)
                    else:  # gate
                        raw_points = _fetch_gate_rate_history(symbol, start_ms, now_ms)
                        try:
                            _, _, interval_hours = get_predicted_rate("gate", symbol)
                        except Exception as e:
                            print(f"[chart/gate] интервал для {symbol}: беру дефолт, {e}")
                            interval_hours = GATE_DEFAULT_FUNDING_INTERVAL_HOURS
                        points = [(t, r, interval_hours) for t, r in raw_points]
                except Exception as e:
                    print(f"[chart/{exchange}] Не удалось получить историю ставок {symbol}: {e}")
                    continue

                if points:
                    series[(exchange, symbol)] = sorted(points)

    return series


# ── 4-часовая сетка и проекция реальных точек на неё (step-hold) ─────────────

def _build_grid(start_ms: int, end_ms: int, step_hours: int = GRID_STEP_HOURS) -> list:
    step_ms = step_hours * 60 * 60 * 1000
    aligned_start = (start_ms // step_ms) * step_ms  # выравниваем по границе шага в UTC
    grid = list(range(aligned_start, end_ms, step_ms))
    if not grid or grid[-1] < end_ms:
        grid.append(end_ms)
    return grid


def _step_hold_on_grid(points: list, grid: list) -> list:
    """
    Для каждой точки сетки — последняя РЕАЛЬНО применённая точка (time,
    rate, interval_hours) на этот момент или раньше, либо None, если такой
    точки ещё не было (позиция на этот момент ещё не существовала).
    points должны быть отсортированы по времени по возрастанию.

    Значение "держится" вперёд только до CONTINUOUS_FUNDING_GAP_HOURS часов
    от своего момента — тот же порог и то же обоснование, что и в
    funding_report._trim_to_continuous_run (funding у перпетуалов идёт не
    реже раза в 8ч, поэтому более длинный разрыв — надёжный признак, что
    позиции в этот промежуток уже не было). Без этой границы одна закравшаяся
    в данные старая/ошибочная запись (см. handoff про Bybit createdTime,
    который не всегда сбрасывается при переоткрытии позиции) "протянула" бы
    линию плоской вперёд вплоть до самого графика — эта граница не даёт
    визуально исказить график, даже если выше по цепочке (funding_report.py)
    в данные всё же просочится что-то лишнее.
    """
    max_hold_ms = CONTINUOUS_FUNDING_GAP_HOURS * 60 * 60 * 1000
    result: list = []
    j, n = 0, len(points)
    last = None
    for gt in grid:
        while j < n and points[j][0] <= gt:
            last = points[j]
            j += 1
        if last is not None and gt - last[0] > max_hold_ms:
            result.append(None)
        else:
            result.append(last)
    return result


# ── Построение и сохранение PNG ───────────────────────────────────────────────

def build_positions_apr_chart(open_results: dict) -> str | None:
    """
    Строит график годовой ставки (APR) по всем открытым сейчас позициям и
    сохраняет его в /tmp. Возвращает путь к PNG-файлу, либо None, если не
    набралось ни одной серии данных (тогда вызывающий код просто не
    прикладывает график к отчёту — текстовая часть при этом не страдает).
    """
    series = _collect_series(open_results)
    if not series:
        return None

    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - MAX_CHART_LOOKBACK_DAYS * 24 * 60 * 60 * 1000
    # _collect_series уже не собирает точки старше cutoff_ms, так что это
    # избыточная подстраховка (на случай если сюда когда-нибудь передадут
    # серии в обход _collect_series) — дешёвая и ничего не меняет в обычном случае.
    earliest_ms = max(min(points[0][0] for points in series.values()), cutoff_ms)
    grid = _build_grid(earliest_ms, now_ms)

    # Тёмная неоновая тема: тёмный фон, кислотные линии со свечением
    # (несколько всё более тонких и ярких полупрозрачных слоёв поверх
    # базовой линии + лёгкий контурный path_effect) — чисто визуальное
    # оформление, логика графика (сетка/step-hold/аннуализация) не меняется.
    fig, ax = plt.subplots(figsize=(11, 6), dpi=150)
    fig.patch.set_facecolor(CHART_BG_COLOR)
    ax.set_facecolor(CHART_BG_COLOR)

    for (exchange, symbol), points in sorted(series.items()):
        held = _step_hold_on_grid(points, grid)
        xs, ys = [], []
        for gt, held_point in zip(grid, held):
            if held_point is None:
                continue  # позиции ещё не было на этот момент — разрыв линии, не ноль
            _, rate, interval_hours = held_point
            xs.append(datetime.fromtimestamp(gt / 1000, tz=timezone.utc))
            ys.append(annualize(rate, interval_hours))
        if not xs:
            continue

        color = EXCHANGE_COLORS.get(exchange, "#999999")
        label = f"{EXCHANGE_LABELS.get(exchange, exchange)}: {symbol}"
        # Слои свечения (широкие, полупрозрачные, снизу) + основная яркая линия сверху.
        for lw, alpha in [(6, 0.10), (4, 0.18), (2.4, 0.35)]:
            ax.plot(xs, ys, color=color, linewidth=lw, alpha=alpha, solid_capstyle="round")
        ax.plot(
            xs, ys, color=color, linewidth=1.4, marker="o", markersize=2.2, label=label,
            path_effects=[pe.Stroke(linewidth=2.2, foreground=color, alpha=0.5), pe.Normal()],
        )

    ax.set_title("Годовая ставка funding (APR) по открытым позициям", color=CHART_FG_COLOR)
    ax.set_xlabel("Время (UTC)", color=CHART_FG_COLOR)
    ax.set_ylabel("APR, %", color=CHART_FG_COLOR)
    ax.axhline(0, color="#666666", linewidth=0.8, linestyle="--")
    ax.grid(True, color=CHART_GRID_COLOR, alpha=0.5)
    ax.tick_params(colors=CHART_FG_COLOR)
    for spine in ax.spines.values():
        spine.set_color(CHART_GRID_COLOR)

    if len(grid) <= MAX_TICKS_AT_FULL_RESOLUTION:
        ax.xaxis.set_major_locator(mdates.HourLocator(interval=GRID_STEP_HOURS))
    else:
        # Длинная позиция — подписи каждые 4ч были бы нечитаемы, прореживаем
        # автоматически. Сами данные всё равно на полной 4-часовой сетке.
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=12))
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    fig.autofmt_xdate(rotation=45)

    legend = ax.legend(loc="upper left", fontsize=8, ncol=2, facecolor=CHART_BG_COLOR, edgecolor=CHART_GRID_COLOR)
    for text in legend.get_texts():
        text.set_color(CHART_FG_COLOR)
    fig.tight_layout()

    path = f"/tmp/positions_apr_chart_{int(time.time())}.png"
    fig.savefig(path, facecolor=CHART_BG_COLOR)
    plt.close(fig)
    return path
