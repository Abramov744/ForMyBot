#!/usr/bin/env python3
"""
Реестр СИМВОЛОВ, когда-либо виденных с открытой позицией на KuCoin —
отдельная маленькая вкладка в том же Google Sheet, копится сама по себе
побочным эффектом уже существующего часового цикла синхронизации
(sheets_sync.build_open_symbol_index) — без единого лишнего запроса к
бирже (fetch_kucoin_open_symbols там и так уже вызывается каждый час для
столбца P).

ЗАЧЕМ ЭТО НУЖНО: в отличие от Aster/Bybit/Lighter/MEXC/Gate,
funding_report.fetch_kucoin() (GET /api/v1/funding-history) принимает
symbol как ОБЯЗАТЕЛЬНЫЙ параметр запроса — нет способа спросить у KuCoin
"что вообще начислялось на аккаунте за этот период", только "что
начислялось по ВОТ ЭТОМУ конкретному символу" (см. докстринг fetch_kucoin
в funding_report.py). Для /positions и калькулятора это не проблема —
символ там и так уже известен (текущая открытая позиция). Но для /report
и /calendar (сколько всего начислилось за произвольный день/период) нужен
список символов ЗАРАНЕЕ, включая уже закрытые позиции — который KuCoin
сам не отдаёт.

Решение: копим список символов САМИ, по мере того как видим их с открытой
позицией (тем же способом, что и /positions уже делает через
fetch_kucoin_open_symbols). /report и /calendar затем опрашивают funding-
историю по КАЖДОМУ когда-либо виденному символу за запрошенный период —
для большинства символов это вернёт пусто (уже закрыты, активности в этом
периоде не было), но для тех, что были открыты — вернёт то же самое, что
и прямой запрос по всему аккаунту у остальных бирж. Функция один раз
навсегда — запись только РАСТЁТ (символ, который перестал быть открытым,
из реестра не убирается: он мог быть активен в ЛЮБОМ прошлом периоде,
который когда-нибудь попросят в /report или /calendar).

ХРАНИЛИЩЕ — тот же принцип, что и у aster_interval_history.py (см. её
докстринг): отдельная вкладка ("kucoin_symbols") в ТОЙ ЖЕ Google Таблице,
переживает передеплой Railway (там нет постоянного диска). Формат — два
столбца: first_seen_ms | symbol. Строка добавляется, ТОЛЬКО когда символ
ещё не встречался (реестр — множество, не лог наблюдений).

ВАЖНО: funding_report.py сам НЕ импортирует этот модуль (и вообще ничего
не знает про Google Sheets) — список известных символов передаётся в
fetch_all()/fetch_all_windowed() параметром, который собирает вызывающий
код (bot_poll.py). Иначе получился бы цикл импортов: funding_report.py →
kucoin_symbol_history.py → sheets_sync.py → funding_report.py.
"""

import json
import os
import time

import gspread

from sheets_sync import _with_sheets_retry

_SYMBOL_SHEET_TAB = "kucoin_symbols"
_HEADER = ["first_seen_ms", "symbol"]

# Кэш известных символов В ЭТОМ ПРОЦЕССЕ — и sheet_sync_loop (пишет), и
# обработчики команд /report, /calendar (читают) живут в одном процессе
# (см. app.py — всё в одних и тех же фоновых потоках), поэтому запись сразу
# видна чтению без похода в Google Sheets повторно. Лениво заполняется из
# уже сохранённых данных при первом обращении — после передеплоя (новый
# процесс) первое обращение просто перечитает вкладку.
_known_symbols_cache: set | None = None


def _open_symbol_worksheet():
    sheet_id = os.environ["GOOGLE_SHEET_ID"]
    creds_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"])
    gc = gspread.service_account_from_dict(creds_info)
    sh = gc.open_by_key(sheet_id)
    try:
        return sh.worksheet(_SYMBOL_SHEET_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=_SYMBOL_SHEET_TAB, rows=1000, cols=len(_HEADER))
        ws.append_row(_HEADER)
        return ws


def _ensure_cache_loaded() -> set:
    global _known_symbols_cache
    if _known_symbols_cache is not None:
        return _known_symbols_cache
    cache: set = set()
    try:
        ws = _with_sheets_retry(_open_symbol_worksheet)
        rows = _with_sheets_retry(ws.get_all_values)
        for row in rows[1:]:  # первая строка — заголовок
            if len(row) >= 2 and row[1]:
                cache.add(row[1])
    except Exception as e:
        print(f"[kucoin_symbol_history] Не удалось прочитать реестр символов, начинаю с пустого: {e}")
    _known_symbols_cache = cache
    return cache


def record_observation(symbol: str) -> None:
    """Дописывает строку (first_seen_ms, symbol), только если symbol ещё
    не встречался ни разу — реестр только растёт, повторные наблюдения уже
    известного символа ничего не пишут (не захламляют вкладку на каждый
    часовой цикл)."""
    cache = _ensure_cache_loaded()
    if symbol in cache:
        return
    now_ms = int(time.time() * 1000)
    try:
        ws = _with_sheets_retry(_open_symbol_worksheet)
        _with_sheets_retry(ws.append_row, [now_ms, symbol])
        cache.add(symbol)
        print(f"[kucoin_symbol_history] Новый символ: {symbol}, записано в реестр.")
    except Exception as e:
        # Не роняем вызывающий цикл (sheets_sync) из-за этого — реестр
        # символов второстепенен по сравнению со столбцом P.
        print(f"[kucoin_symbol_history] Не удалось записать наблюдение {symbol}: {e}")


def load_known_symbols() -> set:
    """Все когда-либо виденные символы KuCoin — для funding_report.fetch_all()/
    fetch_all_windowed() (см. докстринг модуля). Пустое множество, если
    реестр ещё пуст (совсем свежий деплой без единого наблюдения) или
    Google Sheets не настроены."""
    if not (os.environ.get("GOOGLE_SHEET_ID") and os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")):
        return set()
    return set(_ensure_cache_loaded())
