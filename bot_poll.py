#!/usr/bin/env python3
"""
Обработчик команд /report и /calendar в Telegram.

Не является постоянно работающим ботом — запускается по расписанию (cron,
например раз в 3-5 минут) отдельным GitHub Actions workflow, проверяет,
не пришла ли новая команда или нажатие на кнопку, и если пришло — отвечает
отчётом. Задержка ответа равна интервалу между запусками workflow.

Состояние (какие сообщения уже обработаны) хранится на стороне Telegram:
после обработки скрипт подтверждает офсет через повторный вызов getUpdates,
и Telegram больше не отдаёт эти апдейты — никакого файла/базы для этого
заводить не нужно.

Команды в Telegram:
  /report                — отчёт за вчера (по МСК), как в ежедневной рассылке
  /report 2026-08-15     — отчёт за конкретный день (по МСК)
  /report сегодня        — отчёт за сегодня (за уже прошедшую часть суток)
  /calendar               — прислать интерактивный календарь с кнопками:
                             стрелки « / » листают месяцы, нажатие на число
                             сразу присылает отчёт за этот день; отдельными
                             кнопками — отчёт за 7/30/365 последних дней или
                             за произвольный диапазон (выбор начальной и
                             конечной даты по тому же календарю) — к отчёту
                             за период (в отличие от отчёта за один день)
                             прикладываются два графика прибыли по дням
                             (см. send_period_report/funding_chart.py)
  /positions               — суммарный НАЧИСЛЕННЫЙ funding по открытым сейчас
                             позициям (с момента открытия каждой)
  /rates                    — ПРОГНОЗНАЯ ставка funding по открытым сейчас
                             позициям на следующую выплату (см. funding_alerts.py
                             — та же логика, что и в фоновых алертах)
  /balance                  — сводный баланс по всем биржам + Aave (см.
                             balances.py); подробная разбивка — на веб-странице /balances
"""

import calendar as calendar_mod
import json
import os
import re
from datetime import datetime, timedelta

import requests

from funding_report import (
    MSK,
    load_secrets,
    fetch_all,
    fetch_all_windowed,
    fetch_all_time_open_positions,
    build_report,
    build_open_positions_report,
    send_telegram,
)
from funding_alerts import build_predicted_rates_report
from funding_chart import (
    build_positions_apr_chart,
    daily_totals_msk,
    build_period_profit_bar_chart,
    build_period_profit_cumulative_chart,
)
from balances import fetch_all_balances, build_balances_report

# /report, /report@ИмяБота, с необязательным аргументом-датой после пробела
COMMAND_RE = re.compile(r"^/report(?:@\w+)?\s*(.*)$", re.IGNORECASE)
CALENDAR_RE = re.compile(r"^/calendar(?:@\w+)?\s*$", re.IGNORECASE)
POSITIONS_RE = re.compile(r"^/positions(?:@\w+)?\s*$", re.IGNORECASE)
RATES_RE = re.compile(r"^/rates(?:@\w+)?\s*$", re.IGNORECASE)
BALANCE_RE = re.compile(r"^/balance(?:@\w+)?\s*$", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
START_RE = re.compile(r"^/(start|menu)(?:@\w+)?\s*$", re.IGNORECASE)

# Подписи постоянных кнопок внизу чата (Reply Keyboard) — при нажатии
# Telegram отправляет боту этот же текст, как будто пользователь напечатал его сам
BUTTON_REPORT = "📊 Отчёт за вчера"
BUTTON_CALENDAR = "📅 Календарь"
BUTTON_POSITIONS = "📈 Открытые позиции"
BUTTON_RATES = "🔮 Ставка финансирования"
BUTTON_BALANCE = "💰 Баланс"

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
WEEKDAY_HEADERS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


def build_main_menu_keyboard() -> dict:
    """
    Постоянная клавиатура с кнопками внизу чата (не путать с inline-кнопками
    календаря — это два независимых механизма Telegram, могут работать
    одновременно). resize_keyboard делает кнопки компактными, is_persistent
    держит клавиатуру видимой, пока её явно не уберут.
    """
    return {
        "keyboard": [
            [{"text": BUTTON_REPORT}],
            [{"text": BUTTON_CALENDAR}],
            [{"text": BUTTON_POSITIONS}],
            [{"text": BUTTON_RATES}],
            [{"text": BUTTON_BALANCE}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }



# ── Разбор даты/периода ───────────────────────────────────────────────────────

def day_bounds_msk(date_arg: str) -> tuple[int, int]:
    """
    Возвращает (start_ms, end_ms) суток по МСК для аргумента команды.
    ''/'вчера'/'yesterday'      -> предыдущие календарные сутки
    'сегодня'/'today'           -> сегодня, от 00:00 МСК до текущего момента
    'YYYY-MM-DD'                -> конкретные календарные сутки
    Некорректный формат/дата -> ValueError с понятным текстом.
    """
    now_msk = datetime.now(MSK)
    today_msk = now_msk.replace(hour=0, minute=0, second=0, microsecond=0)
    arg = date_arg.strip().lower()

    if arg in ("", "вчера", "yesterday"):
        start, end = today_msk - timedelta(days=1), today_msk
    elif arg in ("сегодня", "today"):
        start, end = today_msk, now_msk
    elif DATE_RE.match(arg):
        try:
            start = datetime.strptime(arg, "%Y-%m-%d").replace(tzinfo=MSK)
        except ValueError:
            raise ValueError(f"Не распознал дату {date_arg!r} — проверьте, что она существует")
        end = start + timedelta(days=1)
    else:
        raise ValueError(
            f"Не понимаю аргумент {date_arg!r}.\n"
            f"Форматы: /report, /report вчера, /report сегодня, /report ГГГГ-ММ-ДД"
        )

    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def rolling_period_bounds_msk(days: int) -> tuple[int, int]:
    """Последние `days` дней по МСК, заканчивая ТЕКУЩИМ моментом (не
    полночью) — "сколько заработано за последние N дней", а не "за N
    прошедших ЦЕЛЫХ календарных суток". Используется кнопками 7/30/365
    дней в календаре (см. build_calendar_markup)."""
    now_msk = datetime.now(MSK)
    start = now_msk - timedelta(days=days)
    return int(start.timestamp() * 1000), int(now_msk.timestamp() * 1000)


def range_bounds_msk(start_date: str, end_date: str) -> tuple[int, int]:
    """[start_date 00:00, end_date+1 день 00:00) по МСК — обе даты
    включительно, обе в формате ГГГГ-ММ-ДД. Если пользователь в календаре
    выбрал конечную дату раньше начальной — переставляются местами молча
    (сравнение строк ISO-дат ГГГГ-ММ-ДД лексикографически совпадает с
    хронологическим порядком, отдельный datetime.strptime для сравнения
    не нужен)."""
    if end_date < start_date:
        start_date, end_date = end_date, start_date
    start = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=MSK)
    end = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=MSK) + timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


# ── Построение inline-календаря ───────────────────────────────────────────────

# Ярлыки кнопок-периодов под сеткой дней (только в обычном режиме, mode="pick"
# — см. build_calendar_markup) — дни, а не "неделя/месяц/год" буквально: и
# "месяц" (30 дней), и "год" (365 дней) здесь — СКОЛЬЗЯЩЕЕ окно назад от
# текущего момента, а не календарный месяц/год, явное число дней в подписи
# не даёт пользователю ошибочно ожидать календарную семантику.
_PERIOD_ROLLING_DAYS = {"week": 7, "month": 30, "year": 365}
_PERIOD_LABELS_RU = {"week": "последние 7 дней", "month": "последние 30 дней", "year": "последние 365 дней"}
_PERIOD_BUTTON_ROW = [
    {"text": "🗓 7 дней", "callback_data": "cal:period:week"},
    {"text": "🗓 30 дней", "callback_data": "cal:period:month"},
    {"text": "🗓 365 дней", "callback_data": "cal:period:year"},
]


def _nav_callback_data(year: int, month: int, delta_months: int, mode: str = "pick", range_start: str | None = None) -> str:
    m, y = month + delta_months, year
    if m < 1:
        m, y = 12, y - 1
    elif m > 12:
        m, y = 1, y + 1
    ym = f"{y:04d}-{m:02d}"
    if mode == "rend":
        return f"cal:nav:rend:{range_start}:{ym}"
    return f"cal:nav:{mode}:{ym}"


def build_calendar_markup(year: int, month: int, mode: str = "pick", range_start: str | None = None) -> dict:
    """
    Строит inline-клавиатуру Telegram с календарём на указанный месяц (по МСК).

    mode:
      "pick"   — обычный режим (по умолчанию): клик по дню сразу шлёт отчёт
                 за этот день (cal:pick:ДАТА); под сеткой — кнопки
                 быстрых периодов (7/30/365 дней) и кнопка "Диапазон".
      "rstart" — выбор НАЧАЛЬНОЙ даты произвольного диапазона (клик по дню
                 -> cal:rstart:ДАТА, не шлёт отчёт, а переключает календарь
                 в режим rend); вместо кнопок периода — кнопка "Отмена".
      "rend"   — выбор КОНЕЧНОЙ даты диапазона; range_start (уже выбранная
                 начальная дата) обязателен и "путешествует" через
                 callback_data кнопок навигации/дня, чтобы не хранить
                 состояние на стороне бота (тот же принцип, что и везде в
                 проекте — Telegram сам держит состояние диалога).
    """
    today = datetime.now(MSK).date()
    weeks = calendar_mod.Calendar(firstweekday=0).monthdayscalendar(year, month)

    rows = [
        [
            {"text": "«", "callback_data": _nav_callback_data(year, month, -1, mode, range_start)},
            {"text": f"{MONTH_NAMES_RU[month]} {year}", "callback_data": "cal:noop"},
            {"text": "»", "callback_data": _nav_callback_data(year, month, +1, mode, range_start)},
        ],
        [{"text": d, "callback_data": "cal:noop"} for d in WEEKDAY_HEADERS_RU],
    ]

    for week in weeks:
        row = []
        for day in week:
            if day == 0:
                row.append({"text": " ", "callback_data": "cal:noop"})
            else:
                date_str = f"{year:04d}-{month:02d}-{day:02d}"
                is_today = datetime(year, month, day).date() == today
                label = f"[{day}]" if is_today else str(day)
                if mode == "rstart":
                    callback_data = f"cal:rstart:{date_str}"
                elif mode == "rend":
                    callback_data = f"cal:rend:{range_start}:{date_str}"
                else:
                    callback_data = f"cal:pick:{date_str}"
                row.append({"text": label, "callback_data": callback_data})
        rows.append(row)

    if mode == "pick":
        rows.append(list(_PERIOD_BUTTON_ROW))
        rows.append([{"text": "📆 Диапазон (с даты по дату)", "callback_data": "cal:rangestart"}])
    else:
        rows.append([{"text": "❌ Отмена", "callback_data": "cal:cancelrange"}])

    return {"inline_keyboard": rows}


# ── Низкоуровневые вызовы Telegram Bot API ────────────────────────────────────

def get_updates(token: str, offset: int | None = None, timeout: int = 0) -> list:
    """
    Запрос к Telegram getUpdates.
    timeout=0   — короткий запрос, как используется в этом файле (cron-версия
                  bot_poll.py, одноразовый запуск по расписанию).
    timeout>0   — long polling: Telegram держит соединение открытым до
                  timeout секунд и отвечает сразу же, как только придёт
                  новое сообщение. Используется в bot_worker.py — постоянно
                  работающем боте на отдельном хостинге.
    """
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": timeout}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=timeout + 15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates error: {data}")
    return data["result"]


def send_message(token: str, chat_id: str, text: str, reply_markup: dict | None = None) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()


def send_photo(token: str, chat_id: str, photo_path: str, caption: str | None = None) -> None:
    """Отправляет локальный файл как фото (multipart/form-data), в отличие
    от send_message — тут нельзя просто передать JSON с URL, т.к. график
    рендерится на лету и существует только на диске самого процесса."""
    url = f"https://api.telegram.org/bot{token}/sendPhoto"
    data = {"chat_id": chat_id}
    if caption:
        data["caption"] = caption
    with open(photo_path, "rb") as f:
        resp = requests.post(url, data=data, files={"photo": f}, timeout=60)
    resp.raise_for_status()


def edit_message_reply_markup(token: str, chat_id: str, message_id: int, reply_markup: dict) -> None:
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": json.dumps(reply_markup)}
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()


def edit_message_text(token: str, chat_id: str, message_id: int, text: str, reply_markup: dict | None = None) -> None:
    """Как edit_message_reply_markup, но меняет ещё и текст сообщения —
    нужно при переключении режима календаря (обычный день / начало
    диапазона / конец диапазона), где меняется сама подсказка пользователю,
    не только кнопки под ней."""
    url = f"https://api.telegram.org/bot{token}/editMessageText"
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = json.dumps(reply_markup)
    resp = requests.post(url, json=payload, timeout=30)
    resp.raise_for_status()


def answer_callback_query(token: str, callback_query_id: str, text: str | None = None) -> None:
    """
    Снимает «часики» с нажатой кнопки в Telegram. Из-за задержки опроса
    (cron раз в несколько минут) к моменту вызова callback_query иногда
    уже "протухает" — Telegram отвечает 400 Bad Request. Это не критично
    (чисто косметический эффект — кнопка чуть дольше выглядит "нажатой"),
    поэтому ошибку здесь глотаем и не даём ей прервать формирование отчёта.
    """
    url = f"https://api.telegram.org/bot{token}/answerCallbackQuery"
    payload = {"callback_query_id": callback_query_id}
    if text:
        payload["text"] = text
    try:
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[answerCallbackQuery] не критично, пропускаю: {e}")


# ── Формирование и отправка отчёта ────────────────────────────────────────────

def send_report_for_period(secrets: dict, chat_id: str, start_ms: int, end_ms: int) -> None:
    token = secrets["telegram_token"]
    try:
        results = fetch_all(secrets, start_ms, end_ms)
        report = build_report(
            start_ms, end_ms,
            *results.get("aster",   (None, None)),
            *results.get("bybit",   (None, None)),
            *results.get("lighter", (None, None)),
            *results.get("mexc",    (None, None)),
            *results.get("gate",    (None, None)),
        )
        send_telegram(token, chat_id, report)
        print("Отправлен отчёт.")
    except Exception as e:
        # Не даём одной сломавшейся команде уронить обработку остальных апдейтов
        print(f"Ошибка при формировании/отправке отчёта: {e}")
        try:
            send_telegram(token, chat_id, f"❌ Не получилось сформировать отчёт: {e}")
        except Exception as e2:
            print(f"Не удалось даже отправить сообщение об ошибке: {e2}")


# Периоды длиннее этого предупреждают пользователя, что сбор данных может
# занять время — у части бирж жёсткий лимит на диапазон ОДНОГО запроса
# истории (7-90 дней, см. funding_report._EXCHANGE_WINDOW_DAYS), поэтому
# fetch_all_windowed() внутри send_period_report дробит период на десятки
# последовательных запросов, и это не мгновенно (в отличие от отчёта за
# один день, который всегда укладывается в лимит одним запросом).
PERIOD_WARNING_THRESHOLD_DAYS = 3


def send_period_report(secrets: dict, chat_id: str, start_ms: int, end_ms: int, label: str) -> None:
    """
    Отчёт за произвольный ПЕРИОД (неделя/месяц/год/диапазон — см.
    build_calendar_markup), в отличие от send_report_for_period (один
    день, /report):
      - использует fetch_all_windowed(), а не fetch_all() — период может
        быть длиннее лимита одного запроса истории у биржи (см. докстринг
        fetch_all_windowed в funding_report.py);
      - к тексту отчёта дописываются предупреждения fetch_all_windowed
        (сейчас единственное — про обрезку глубины истории у Gate), если
        они есть;
      - после текста дополнительно шлются два графика прибыли по дням
        (funding_chart.build_period_profit_bar_chart/_cumulative_chart) —
        БЕЗ разбивки по биржам, только общий итог (согласовано явно).
    """
    token = secrets["telegram_token"]
    period_days = max(1, round((end_ms - start_ms) / (24 * 60 * 60 * 1000)))
    try:
        if period_days > PERIOD_WARNING_THRESHOLD_DAYS:
            send_telegram(
                token, chat_id,
                f"⏳ Собираю отчёт за {label} — период длинный, у части бирж жёсткий "
                f"лимит на диапазон одного запроса истории, это может занять несколько минут…",
            )
        results, warnings = fetch_all_windowed(secrets, start_ms, end_ms)
        report = build_report(
            start_ms, end_ms,
            *results.get("aster",   (None, None)),
            *results.get("bybit",   (None, None)),
            *results.get("lighter", (None, None)),
            *results.get("mexc",    (None, None)),
            *results.get("gate",    (None, None)),
        )
        if warnings:
            report += "\n\n" + "\n".join(warnings)
        send_telegram(token, chat_id, report)
        print(f"Отправлен отчёт за период ({label}).")
    except Exception as e:
        print(f"Ошибка при формировании/отправке отчёта за период: {e}")
        try:
            send_telegram(token, chat_id, f"❌ Не получилось сформировать отчёт: {e}")
        except Exception as e2:
            print(f"Не удалось даже отправить сообщение об ошибке: {e2}")
        return

    # Графики — отдельным шагом со своим try/except каждый: текстовый отчёт
    # уже успешно ушёл, ошибка построения ОДНОГО из графиков не должна
    # мешать ни второму, ни уже отправленному тексту (тот же принцип, что
    # и в send_open_positions_report для графика APR).
    daily = daily_totals_msk(results, start_ms, end_ms)
    for build_chart, caption in (
        (build_period_profit_bar_chart, f"Прибыль по дням — {label}"),
        (build_period_profit_cumulative_chart, f"Накопительная прибыль — {label}"),
    ):
        chart_path = None
        try:
            chart_path = build_chart(daily)
            if chart_path:
                send_photo(token, chat_id, chart_path, caption=caption)
                print(f"Отправлен график: {caption}.")
        except Exception as e:
            print(f"Ошибка при построении/отправке графика ({caption}): {e}")
        finally:
            if chart_path and os.path.exists(chart_path):
                os.remove(chart_path)


def send_open_positions_report(secrets: dict, chat_id: str) -> None:
    """
    Отчёт по всем текущим открытым позициям — требует запроса списка
    открытых позиций на каждой бирже плюс всей доступной истории funding,
    поэтому может занять заметно больше времени, чем /report за один день.
    """
    token = secrets["telegram_token"]
    try:
        send_telegram(token, chat_id, "⏳ Собираю данные по открытым позициям, это может занять до минуты…")
        results = fetch_all_time_open_positions(secrets)
        report = build_open_positions_report(results)
        send_telegram(token, chat_id, report)
        print("Отправлен отчёт по открытым позициям.")
    except Exception as e:
        print(f"Ошибка при формировании отчёта по открытым позициям: {e}")
        try:
            send_telegram(token, chat_id, f"❌ Не получилось сформировать отчёт: {e}")
        except Exception as e2:
            print(f"Не удалось даже отправить сообщение об ошибке: {e2}")
        return

    # График APR — отдельным шагом со своим try/except: к этому моменту
    # текстовый отчёт уже успешно ушёл, поэтому проблема именно с графиком
    # не должна "откатывать" уже отправленный текст или ронять весь метод.
    # results уже содержит всё нужное (те же открытые позиции, что и в
    # тексте выше) — заново запрашивать биржи не нужно.
    chart_path = None
    try:
        chart_path = build_positions_apr_chart(results)
        if chart_path:
            send_photo(
                token, chat_id, chart_path,
                caption="Годовая ставка funding (APR) по открытым позициям — раз в 4ч, по времени открытия самой ранней позиции",
            )
            print("Отправлен график APR по открытым позициям.")
    except Exception as e:
        print(f"Ошибка при построении/отправке графика APR: {e}")
    finally:
        if chart_path and os.path.exists(chart_path):
            os.remove(chart_path)


def send_predicted_rates_report(secrets: dict, chat_id: str) -> None:
    """
    Прогнозная ставка funding по открытым сейчас позициям (build_predicted_rates_report
    в funding_alerts.py — та же логика, что и в фоновых алертах на отрицательный
    фандинг, просто по запросу и сразу по всем позициям, а не только при
    переходе в минус).
    """
    token = secrets["telegram_token"]
    try:
        send_telegram(token, chat_id, "⏳ Запрашиваю прогнозные ставки по открытым позициям…")
        report = build_predicted_rates_report(secrets)
        send_telegram(token, chat_id, report)
        print("Отправлен отчёт по прогнозным ставкам.")
    except Exception as e:
        print(f"Ошибка при формировании отчёта по прогнозным ставкам: {e}")
        try:
            send_telegram(token, chat_id, f"❌ Не получилось сформировать отчёт: {e}")
        except Exception as e2:
            print(f"Не удалось даже отправить сообщение об ошибке: {e2}")


def send_balances_report(secrets: dict, chat_id: str) -> None:
    """Сводный баланс по всем биржам + Aave (balances.py)."""
    token = secrets["telegram_token"]
    try:
        send_telegram(token, chat_id, "⏳ Собираю балансы (биржи + скан сетей кошелька — может занять до ~30 секунд)…")
        result = fetch_all_balances(secrets)
        report = build_balances_report(result)
        send_telegram(token, chat_id, report)
        print("Отправлен отчёт по балансам.")
    except Exception as e:
        print(f"Ошибка при формировании отчёта по балансам: {e}")
        try:
            send_telegram(token, chat_id, f"❌ Не получилось собрать баланс: {e}")
        except Exception as e2:
            print(f"Не удалось даже отправить сообщение об ошибке: {e2}")


# ── Обработка входящих сообщений и нажатий на кнопки ──────────────────────────

def handle_message(secrets: dict, message: dict, allowed_chat_id: str) -> None:
    token = secrets["telegram_token"]
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if chat_id != allowed_chat_id:
        print(f"Игнорирую сообщение из чужого чата {chat_id}")
        return

    if START_RE.match(text):
        send_message(
            token, chat_id,
            "Готов присылать отчёты по funding fee.\n"
            "Кнопки внизу — под рукой, либо команды /report, /calendar, /positions, /rates, /balance текстом.",
            reply_markup=build_main_menu_keyboard(),
        )
        return

    # Нажатие на постоянную кнопку — по сути то же самое, что и команда,
    # просто текст сообщения не начинается с "/"
    if text == BUTTON_POSITIONS:
        text = "/positions"
    elif text == BUTTON_CALENDAR:
        text = "/calendar"
    elif text == BUTTON_REPORT:
        text = "/report"
    elif text == BUTTON_RATES:
        text = "/rates"
    elif text == BUTTON_BALANCE:
        text = "/balance"

    if CALENDAR_RE.match(text):
        now_msk = datetime.now(MSK)
        markup = build_calendar_markup(now_msk.year, now_msk.month)
        send_message(token, chat_id, "Выберите дату отчёта:", reply_markup=markup)
        return

    if POSITIONS_RE.match(text):
        print("Обрабатываю команду: '/positions'")
        send_open_positions_report(secrets, chat_id)
        return

    if RATES_RE.match(text):
        print("Обрабатываю команду: '/rates'")
        send_predicted_rates_report(secrets, chat_id)
        return

    if BALANCE_RE.match(text):
        print("Обрабатываю команду: '/balance'")
        send_balances_report(secrets, chat_id)
        return

    m = COMMAND_RE.match(text)
    if not m:
        return

    date_arg = m.group(1)
    print(f"Обрабатываю команду: {text!r}")

    try:
        start_ms, end_ms = day_bounds_msk(date_arg)
    except ValueError as e:
        send_telegram(token, chat_id, f"⚠️ {e}")
        return

    send_report_for_period(secrets, chat_id, start_ms, end_ms)


def handle_callback_query(secrets: dict, cq: dict, allowed_chat_id: str) -> None:
    token = secrets["telegram_token"]
    data = cq.get("data", "")
    message = cq.get("message", {})
    chat_id = str(message.get("chat", {}).get("id", ""))
    message_id = message.get("message_id")
    callback_id = cq["id"]

    if chat_id != allowed_chat_id:
        answer_callback_query(token, callback_id)
        print(f"Игнорирую callback из чужого чата {chat_id}")
        return

    if data == "cal:noop":
        answer_callback_query(token, callback_id)
        return

    if data.startswith("cal:nav:"):
        # Формат: "cal:nav:<mode>:<YYYY-MM>" (mode pick/rstart) или
        # "cal:nav:rend:<range_start>:<YYYY-MM>" (mode rend, три части
        # после "cal:nav:") — см. build_calendar_markup/_nav_callback_data.
        payload = data[len("cal:nav:"):]
        parts = payload.split(":")
        answer_callback_query(token, callback_id)
        try:
            if parts[0] == "rend" and len(parts) == 3:
                mode, range_start, ym = parts
            elif parts[0] in ("pick", "rstart") and len(parts) == 2:
                mode, ym = parts
                range_start = None
            else:
                raise ValueError(f"неожиданный формат {data!r}")
            year, month = (int(x) for x in ym.split("-"))
            edit_message_reply_markup(
                token, chat_id, message_id,
                build_calendar_markup(year, month, mode, range_start),
            )
        except Exception as e:
            print(f"Не удалось обновить календарь: {e}")
        return

    if data.startswith("cal:pick:"):
        date_str = data[len("cal:pick:"):]
        answer_callback_query(token, callback_id, text=f"Формирую отчёт за {date_str}…")
        print(f"Выбрана дата в календаре: {date_str}")
        try:
            start_ms, end_ms = day_bounds_msk(date_str)
        except ValueError as e:
            send_telegram(token, chat_id, f"⚠️ {e}")
            return
        send_report_for_period(secrets, chat_id, start_ms, end_ms)
        return

    if data.startswith("cal:period:"):
        key = data[len("cal:period:"):]
        days = _PERIOD_ROLLING_DAYS.get(key)
        label = _PERIOD_LABELS_RU.get(key, key)
        answer_callback_query(token, callback_id, text=f"Формирую отчёт за {label}…")
        if days is None:
            print(f"Неизвестный период в cal:period: {key!r}")
            return
        print(f"Выбран период в календаре: {label}")
        start_ms, end_ms = rolling_period_bounds_msk(days)
        send_period_report(secrets, chat_id, start_ms, end_ms, label)
        return

    if data == "cal:rangestart":
        answer_callback_query(token, callback_id)
        now_msk = datetime.now(MSK)
        try:
            edit_message_text(
                token, chat_id, message_id,
                "Выберите НАЧАЛЬНУЮ дату диапазона:",
                build_calendar_markup(now_msk.year, now_msk.month, mode="rstart"),
            )
        except Exception as e:
            print(f"Не удалось начать выбор диапазона: {e}")
        return

    if data.startswith("cal:rstart:"):
        start_date = data[len("cal:rstart:"):]
        answer_callback_query(token, callback_id)
        try:
            year, month, _ = (int(x) for x in start_date.split("-"))
            edit_message_text(
                token, chat_id, message_id,
                f"Начало диапазона: {start_date}.\nВыберите КОНЕЧНУЮ дату диапазона:",
                build_calendar_markup(year, month, mode="rend", range_start=start_date),
            )
        except Exception as e:
            print(f"Не удалось продолжить выбор диапазона: {e}")
        return

    if data.startswith("cal:rend:"):
        # "cal:rend:<start_date>:<end_date>" — дата в формате ГГГГ-ММ-ДД
        # не содержит ":", поэтому rsplit по последнему ":" однозначно
        # отделяет конечную дату от начальной.
        payload = data[len("cal:rend:"):]
        start_date, end_date = payload.rsplit(":", 1)
        answer_callback_query(token, callback_id, text=f"Формирую отчёт за {start_date} — {end_date}…")
        print(f"Выбран диапазон в календаре: {start_date} — {end_date}")
        try:
            start_ms, end_ms = range_bounds_msk(start_date, end_date)
        except ValueError as e:
            send_telegram(token, chat_id, f"⚠️ {e}")
            return
        send_period_report(secrets, chat_id, start_ms, end_ms, f"{start_date} — {end_date}")
        return

    if data == "cal:cancelrange":
        answer_callback_query(token, callback_id)
        now_msk = datetime.now(MSK)
        try:
            edit_message_text(
                token, chat_id, message_id,
                "Выберите дату отчёта:",
                build_calendar_markup(now_msk.year, now_msk.month, mode="pick"),
            )
        except Exception as e:
            print(f"Не удалось отменить выбор диапазона: {e}")
        return

    # Неизвестный callback_data — на всякий случай снимаем «часики» с кнопки
    answer_callback_query(token, callback_id)


# ── Точка входа ───────────────────────────────────────────────────────────────

def main():
    secrets = load_secrets()
    token = secrets["telegram_token"]
    allowed_chat_id = str(secrets["telegram_chat_id"])

    updates = get_updates(token)
    if not updates:
        print("Новых сообщений нет.")
        return

    max_update_id = None
    for upd in updates:
        max_update_id = upd["update_id"]

        if "callback_query" in upd:
            handle_callback_query(secrets, upd["callback_query"], allowed_chat_id)
            continue

        message = upd.get("message") or upd.get("edited_message")
        if message:
            handle_message(secrets, message, allowed_chat_id)

    # Подтверждаем офсет — иначе те же апдейты придут и на следующем запуске
    if max_update_id is not None:
        get_updates(token, offset=max_update_id + 1)
        print(f"Офсет подтверждён (update_id >= {max_update_id + 1}).")


if __name__ == "__main__":
    main()
