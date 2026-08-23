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
                             сразу присылает отчёт за этот день
"""

import calendar as calendar_mod
import json
import re
from datetime import datetime, timedelta

import requests

from funding_report import (
    MSK,
    load_secrets,
    fetch_all,
    build_report,
    send_telegram,
)

# /report, /report@ИмяБота, с необязательным аргументом-датой после пробела
COMMAND_RE = re.compile(r"^/report(?:@\w+)?\s*(.*)$", re.IGNORECASE)
CALENDAR_RE = re.compile(r"^/calendar(?:@\w+)?\s*$", re.IGNORECASE)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

MONTH_NAMES_RU = {
    1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
    5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
    9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь",
}
WEEKDAY_HEADERS_RU = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


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


# ── Построение inline-календаря ───────────────────────────────────────────────

def _nav_callback_data(year: int, month: int, delta_months: int) -> str:
    m, y = month + delta_months, year
    if m < 1:
        m, y = 12, y - 1
    elif m > 12:
        m, y = 1, y + 1
    return f"cal:nav:{y:04d}-{m:02d}"


def build_calendar_markup(year: int, month: int) -> dict:
    """Строит inline-клавиатуру Telegram с календарём на указанный месяц (по МСК)."""
    today = datetime.now(MSK).date()
    weeks = calendar_mod.Calendar(firstweekday=0).monthdayscalendar(year, month)

    rows = [
        [
            {"text": "«", "callback_data": _nav_callback_data(year, month, -1)},
            {"text": f"{MONTH_NAMES_RU[month]} {year}", "callback_data": "cal:noop"},
            {"text": "»", "callback_data": _nav_callback_data(year, month, +1)},
        ],
        [{"text": d, "callback_data": "cal:noop"} for d in WEEKDAY_HEADERS_RU],
    ]

    for week in weeks:
        row = []
        for day in week:
            if day == 0:
                row.append({"text": " ", "callback_data": "cal:noop"})
            else:
                is_today = datetime(year, month, day).date() == today
                label = f"[{day}]" if is_today else str(day)
                row.append({"text": label, "callback_data": f"cal:pick:{year:04d}-{month:02d}-{day:02d}"})
        rows.append(row)

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


def edit_message_reply_markup(token: str, chat_id: str, message_id: int, reply_markup: dict) -> None:
    url = f"https://api.telegram.org/bot{token}/editMessageReplyMarkup"
    payload = {"chat_id": chat_id, "message_id": message_id, "reply_markup": json.dumps(reply_markup)}
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


# ── Обработка входящих сообщений и нажатий на кнопки ──────────────────────────

def handle_message(secrets: dict, message: dict, allowed_chat_id: str) -> None:
    token = secrets["telegram_token"]
    chat_id = str(message.get("chat", {}).get("id", ""))
    text = (message.get("text") or "").strip()

    if chat_id != allowed_chat_id:
        print(f"Игнорирую сообщение из чужого чата {chat_id}")
        return

    if CALENDAR_RE.match(text):
        now_msk = datetime.now(MSK)
        markup = build_calendar_markup(now_msk.year, now_msk.month)
        send_message(token, chat_id, "Выберите дату отчёта:", reply_markup=markup)
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
        year, month = (int(x) for x in data[len("cal:nav:"):].split("-"))
        answer_callback_query(token, callback_id)
        try:
            edit_message_reply_markup(token, chat_id, message_id, build_calendar_markup(year, month))
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
