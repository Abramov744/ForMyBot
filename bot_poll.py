#!/usr/bin/env python3
"""
Обработчик команды /report в Telegram.

Не является постоянно работающим ботом — запускается по расписанию (cron,
например раз в 3-5 минут) отдельным GitHub Actions workflow, проверяет,
не пришла ли новая команда, и если пришла — отвечает отчётом. Задержка
ответа равна интервалу между запусками workflow.

Состояние (какие сообщения уже обработаны) хранится на стороне Telegram:
после обработки скрипт подтверждает офсет через повторный вызов getUpdates,
и Telegram больше не отдаёт эти апдейты — никакого файла/базы для этого
заводить не нужно.

Команды в Telegram:
  /report                — отчёт за вчера (по МСК), как в ежедневной рассылке
  /report 2026-08-15     — отчёт за конкретный день (по МСК)
  /report сегодня        — отчёт за сегодня (за уже прошедшую часть суток)
"""

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
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def get_updates(token: str, offset: int | None = None) -> list:
    """Короткий (не long-poll) запрос getUpdates — сама функция вызывается по cron."""
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": 0}
    if offset is not None:
        params["offset"] = offset
    resp = requests.get(url, params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram getUpdates error: {data}")
    return data["result"]


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
        message = upd.get("message") or upd.get("edited_message")
        if not message:
            continue

        chat_id = str(message.get("chat", {}).get("id", ""))
        text = (message.get("text") or "").strip()

        if chat_id != allowed_chat_id:
            print(f"Игнорирую сообщение из чужого чата {chat_id}")
            continue

        m = COMMAND_RE.match(text)
        if not m:
            continue

        date_arg = m.group(1)
        print(f"Обрабатываю команду: {text!r}")

        try:
            start_ms, end_ms = day_bounds_msk(date_arg)
        except ValueError as e:
            send_telegram(token, chat_id, f"⚠️ {e}")
            continue

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
            print("Отправлен отчёт по запросу.")
        except Exception as e:
            # Не даём одной сломавшейся команде уронить обработку остальных апдейтов
            print(f"Ошибка при формировании/отправке отчёта: {e}")
            try:
                send_telegram(token, chat_id, f"❌ Не получилось сформировать отчёт: {e}")
            except Exception as e2:
                print(f"Не удалось даже отправить сообщение об ошибке: {e2}")

    # Подтверждаем офсет — иначе те же апдейты придут и на следующем запуске
    if max_update_id is not None:
        get_updates(token, offset=max_update_id + 1)
        print(f"Офсет подтверждён (update_id >= {max_update_id + 1}).")


if __name__ == "__main__":
    main()
