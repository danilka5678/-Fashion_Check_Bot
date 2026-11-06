# Fashion Check Bot v4 — один режим: Квиз

Загрузи на GitHub с заменой: `bot.py`, `questions.json`, `requirements.txt`.
Переменные Railway: `BOT_TOKEN`, `DB_PATH` (например `/data/fashion.db` + Volume), `REMINDER_HOUR` (UTC, по умолчанию 9).

- 202 текстовых вопроса, 4 варианта, без фото.
- Сложность: Авто / Лёгкая / Средняя / Сложная / Нишевый.
- XP, 12 рангов, серии, наборы, коллекции, достижения, рейтинг.
- Контент редактируется в папке `content/` и собирается скриптом `build_content.py`.
