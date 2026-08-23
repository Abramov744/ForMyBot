#!/usr/bin/env python3
"""
Постоянно работающий Telegram-бот (long polling).

В отличие от bot_poll.py (одноразовый запуск по расписанию через GitHub
Actions, с задержкой ответа до интервала между запусками cron), этот
скрипт сам крутится в бесконечном цикле и отвечает мгновенно, как только
приходит сообщение или нажатие на кнопку.

Предназначен для хостинга на отдельном сервисе, который держит процесс
запущенным 24/7 (Railway, Fly.io, VPS и т.п.) — НЕ для GitHub Actions.

ВАЖНО: используйте либо этот скрипт, либо cron-workflow "Telegram Bot Poll"
из bot_poll.py, но не оба одновременно — оба консьюмят один и тот же
Telegram getUpdates offset, и при параллельной работе будут друг другу
мешать (пропущенные/задвоенные апдейты). Если запускаете этот скрипт —
отключите cron-workflow в GitHub Actions.

Запуск: python bot_worker.py
"""

import time

from bot_poll import get_updates, handle_message, handle_callback_query
from funding_report import load_secrets

LONG_POLL_TIMEOUT = 30  # секунд — Telegram будет держать соединение открытым


def poll_forever() -> None:
    secrets = load_secrets()
    token = secrets["telegram_token"]
    allowed_chat_id = str(secrets["telegram_chat_id"])

    offset = None
    print("Бот запущен, жду сообщений...", flush=True)

    while True:
        try:
            updates = get_updates(token, offset=offset, timeout=LONG_POLL_TIMEOUT)
        except Exception as e:
            print(f"Ошибка при опросе Telegram: {e}. Пауза 5 сек и повтор.", flush=True)
            time.sleep(5)
            continue

        for upd in updates:
            # Сдвигаем offset сразу, до обработки — чтобы сломавшийся апдейт
            # не обрабатывался повторно на следующей итерации до бесконечности
            offset = upd["update_id"] + 1
            try:
                if "callback_query" in upd:
                    handle_callback_query(secrets, upd["callback_query"], allowed_chat_id)
                    continue
                message = upd.get("message") or upd.get("edited_message")
                if message:
                    handle_message(secrets, message, allowed_chat_id)
            except Exception as e:
                print(f"Ошибка при обработке апдейта {upd.get('update_id')}: {e}", flush=True)


if __name__ == "__main__":
    poll_forever()
