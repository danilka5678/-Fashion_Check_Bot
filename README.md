# Fashion Check Bot v3

Замена старой версии. Загрузи на GitHub (с заменой) файлы: `bot.py`, `questions.json`, `requirements.txt`, `README.md`. Railway сам перезапустит бота.

Переменные Railway: `BOT_TOKEN` (обязательно), `DB_PATH` (например `/data/fashion.db` + Volume, чтобы прогресс не терялся), `ADMIN_IDS` (твой ID из команды /myid, через запятую), `REMINDER_HOUR` (по UTC, по умолчанию 9).

Фото: админ отправляет боту фото с подписью — номером вопроса (список в `photo_tasks.csv`). `/missing` — у каких вопросов нет фото.

Старые очки не переносятся (новая система XP).
