#!/usr/bin/env python3
"""
История ФАКТИЧЕСКИ действовавшего интервала начисления funding на Aster по
символам — отдельная маленькая вкладка в том же Google Sheet, копится сама
по себе фоновым циклом алертов (funding_alerts.check_funding_alerts).

ЗАЧЕМ ЭТО НУЖНО (обсуждение с пользователем: график APR по Aster, построенный
в разное время, показывал для одних и тех же ПРОШЛЫХ дат совершенно разный
годовой процент):
  Aster по умолчанию начисляет funding раз в 8ч (см.
  ASTER_DEFAULT_FUNDING_INTERVAL_HOURS в funding_alerts.py), но имеет
  задокументированное право временно СОКРАЩАТЬ этот интервал (до 1-4ч) при
  экстремальной волатильности по символу —
  https://docs.asterdex.com/trading/perpetuals/fees-and-specs/funding-rate.

  Публичная история ставок (GET /fapi/v1/fundingRate, см.
  funding_chart._fetch_aster_rate_history) отдаёт по каждой записи только
  fundingTime+fundingRate — БЕЗ интервала, который действовал именно в тот
  момент. Единственный источник интервала — снимок ТЕКУЩЕГО состояния (GET
  /fapi/v1/fundingInfo, funding_alerts._fetch_aster_funding_intervals),
  причём он появляется в этом снимке, только пока нестандартный интервал
  ещё активен. Раньше funding_chart._collect_series брал этот текущий
  снимок и применял его КО ВСЕЙ истории символа разом — из-за этого график,
  построенный ПОКА короткий интервал ещё действовал, и график, построенный
  ПОСЛЕ того как интервал вернулся к дефолту, пересчитывали одни и те же
  старые высоковолатильные выплаты по разным интервалам и получали разный
  APR для одних и тех же дат.

  Полностью это не чинит — историю ДО появления этой вкладки восстановить
  нечем (Aster её не отдаёт), это фундаментальное ограничение публичного
  API, не наш недочёт. Но НАЧИНАЯ с момента внедрения — funding_chart
  подставляет для каждой точки графика ПОСЛЕДНЕЕ известное на тот момент
  наблюдение интервала, а не всегда текущее.

  У Gate теоретически та же проблема (funding_chart тоже берёт "текущий"
  интервал через get_predicted_rate и применяет его ко всей истории), но
  пользователь просил именно про Aster — Gate здесь не трогаем.

ХРАНИЛИЩЕ — отдельная вкладка ("aster_funding_intervals") в ТОЙ ЖЕ Google
Таблице, что и основной учёт (GOOGLE_SHEET_ID/GOOGLE_SERVICE_ACCOUNT_JSON):
отдельной инфраструктуры не заводим, всё равно нужна persistence, которая
переживает передеплой Railway (там нет постоянного диска, см. CLAUDE.md).
Формат — три столбца: timestamp_ms | symbol | interval_hours. Строка
добавляется, ТОЛЬКО когда значение по символу реально изменилось с прошлого
наблюдения — компактная история ПЕРЕХОДОВ, а не сырой лог каждого опроса
(иначе вкладка росла бы на пустом месте каждый цикл алертов).

ТОЧНОСТЬ: момент наблюдения — это момент очередного прохода
funding_alerts.check_funding_alerts (раз в ALERT_CHECK_INTERVAL_MINUTES),
а не момент фактической смены интервала на бирже — с точностью до этого
интервала между двумя наблюдениями (обычно единицы минут).
"""

import json
import os
import time
from bisect import bisect_right

import gspread

from sheets_sync import _with_sheets_retry

_INTERVAL_SHEET_TAB = "aster_funding_intervals"
_HEADER = ["timestamp_ms", "symbol", "interval_hours"]

# Кэш последнего наблюдённого значения по символу В ЭТОМ ПРОЦЕССЕ — чтобы не
# перечитывать всю вкладку на каждый цикл алертов. Лениво заполняется из уже
# сохранённых данных при первом обращении (см. _ensure_cache_loaded) — после
# передеплоя (новый процесс) первое наблюдение просто перечитает вкладку.
_last_known_cache: dict | None = None


def _open_interval_worksheet():
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    gc = gspread.service_account_from_dict(creds_info)
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(_INTERVAL_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=_INTERVAL_SHEET_TAB, rows=1000, cols=len(_HEADER))
        ws.append_row(_HEADER)
        return ws


def _ensure_cache_loaded() -> dict:
    global _last_known_cache
    if _last_known_cache is not None:
        return _last_known_cache
    cache: dict = {}
    try:
        ws = _with_sheets_retry(_open_interval_worksheet)
        rows = _with_sheets_retry(ws.get_all_values)
        for row in rows[1:]:  # первая строка — заголовок
            if len(row) < 3:
                continue
            try:
                cache[row[1]] = float(row[2])  # строки только дописываются -> последняя по порядку побеждает
            except ValueError:
                continue
    except Exception as e:
        print(f"[aster_interval_history] Не удалось прочитать историю интервалов, начинаю с пустого кэша: {e}")
    _last_known_cache = cache
    return cache


def record_observation(symbol: str, interval_hours: float) -> None:
    """Дописывает строку (timestamp_ms, symbol, interval_hours), только если
    значение для symbol отличается от последнего известного наблюдения
    (или наблюдений по нему ещё не было)."""
    cache = _ensure_cache_loaded()
    if cache.get(symbol) == interval_hours:
        return
    now_ms = int(time.time() * 1000)
    try:
        ws = _with_sheets_retry(_open_interval_worksheet)
        _with_sheets_retry(ws.append_row, [now_ms, symbol, interval_hours])
        cache[symbol] = interval_hours
        print(f"[aster_interval_history] {symbol}: интервал funding изменился на {interval_hours:g}ч, записано.")
    except Exception as e:
        # Не роняем цикл алертов из-за этого — запись истории интервалов
        # второстепенна по сравнению с самими алертами.
        print(f"[aster_interval_history] Не удалось записать наблюдение {symbol}={interval_hours:g}ч: {e}")


def load_history() -> dict:
    """{symbol: [(timestamp_ms, interval_hours), ...]} по возрастанию
    времени — для funding_chart._collect_series, чтобы подставлять для
    каждой исторической точки графика интервал, действовавший (по нашим
    наблюдениям) на тот момент, а не всегда текущий."""
    by_symbol: dict = {}
    try:
        ws = _with_sheets_retry(_open_interval_worksheet)
        rows = _with_sheets_retry(ws.get_all_values)
    except Exception as e:
        print(f"[aster_interval_history] Не удалось прочитать историю интервалов: {e}")
        return by_symbol
    for row in rows[1:]:
        if len(row) < 3:
            continue
        try:
            ts_ms, symbol, interval_hours = int(row[0]), row[1], float(row[2])
        except ValueError:
            continue
        by_symbol.setdefault(symbol, []).append((ts_ms, interval_hours))
    for points in by_symbol.values():
        points.sort()
    return by_symbol


def interval_at(points: list, t_ms: int, default_interval_hours: float) -> float:
    """Интервал, действовавший (по накопленным наблюдениям) в момент t_ms:
    последнее наблюдение НЕ ПОЗЖЕ t_ms. Если такого нет (t_ms раньше самого
    первого наблюдения — в т.ч. если наблюдений ещё вообще нет) —
    default_interval_hours (обычно ASTER_DEFAULT_FUNDING_INTERVAL_HOURS)."""
    if not points:
        return default_interval_hours
    times = [p[0] for p in points]
    idx = bisect_right(times, t_ms) - 1
    if idx < 0:
        return default_interval_hours
    return points[idx][1]
