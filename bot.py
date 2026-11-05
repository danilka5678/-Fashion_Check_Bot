"""Телеграм-бот: проверь свои знания в мире моды.

Запуск:
    pip install -r requirements.txt
    set BOT_TOKEN=токен_от_BotFather        (Windows)
    export BOT_TOKEN=токен_от_BotFather     (macOS / Linux)
    python bot.py
"""

import asyncio
import json
import logging
import os
import random
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

# ---------- настройки ----------
BASE = Path(__file__).parent
DB_PATH = BASE / "fashion.db"
ROUND_SIZE = 10          # вопросов в одном раунде
POINTS_PER_ANSWER = 10   # очков за верный ответ
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "12"))  # во сколько напоминать про вопрос дня (время сервера)

CATEGORIES = {
    "brands": "🏛 Бренды и основатели",
    "designers": "✂️ Дизайнеры",
    "history": "📜 История и события",
    "items": "👜 Знаковые вещи",
}

LEVELS = [
    (0, "Новичок 🌱"),
    (100, "Любитель 👗"),
    (300, "Стилист ✂️"),
    (600, "Модный эксперт 💎"),
    (1000, "Главред Vogue 👑"),
]

QUESTIONS = json.loads((BASE / "questions.json").read_text(encoding="utf-8"))

router = Router()
sessions: dict[int, dict] = {}  # текущие игры в памяти: user_id -> состояние


# ---------- база данных ----------
def q(sql, params=(), one=False, write=False):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, params)
        if write:
            con.commit()
        rows = cur.fetchall()
        if one:
            return rows[0] if rows else None
        return rows
    finally:
        con.close()


def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript(
        """
        CREATE TABLE IF NOT EXISTS users(
            user_id INTEGER PRIMARY KEY,
            name TEXT,
            points INTEGER DEFAULT 0,
            answered INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            games INTEGER DEFAULT 0,
            best INTEGER DEFAULT 0,
            last_daily TEXT,
            streak INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS cat_stats(
            user_id INTEGER,
            category TEXT,
            answered INTEGER DEFAULT 0,
            correct INTEGER DEFAULT 0,
            PRIMARY KEY(user_id, category)
        );
        """
    )
    con.commit()
    con.close()


def ensure_user(user):
    q(
        "INSERT INTO users(user_id, name) VALUES(?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET name=excluded.name",
        (user.id, user.full_name),
        write=True,
    )


def record_answer(uid, cat, ok):
    q(
        "UPDATE users SET answered=answered+1, correct=correct+?, points=points+? WHERE user_id=?",
        (1 if ok else 0, POINTS_PER_ANSWER if ok else 0, uid),
        write=True,
    )
    q(
        "INSERT INTO cat_stats(user_id, category, answered, correct) VALUES(?, ?, 1, ?) "
        "ON CONFLICT(user_id, category) DO UPDATE SET "
        "answered=answered+1, correct=correct+excluded.correct",
        (uid, cat, 1 if ok else 0),
        write=True,
    )


def mark_daily(uid) -> int:
    u = q("SELECT streak, last_daily FROM users WHERE user_id=?", (uid,), one=True)
    yesterday = str(date.today() - timedelta(days=1))
    streak = u["streak"] + 1 if u and u["last_daily"] == yesterday else 1
    q(
        "UPDATE users SET last_daily=?, streak=? WHERE user_id=?",
        (str(date.today()), streak, uid),
        write=True,
    )
    return streak


# ---------- уровни ----------
def level_name(points):
    name = LEVELS[0][1]
    for threshold, title in LEVELS:
        if points >= threshold:
            name = title
    return name


def next_level_hint(points):
    for threshold, title in LEVELS:
        if points < threshold:
            return f"до уровня «{title}» осталось {threshold - points} очк."
    return "максимальный уровень достигнут!"


# ---------- клавиатуры ----------
def menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🎮 Играть (10 вопросов)", callback_data="menu:play")
    kb.button(text="📚 По категориям", callback_data="menu:cats")
    kb.button(text="☀️ Вопрос дня", callback_data="menu:daily")
    kb.button(text="📊 Мой профиль", callback_data="menu:profile")
    kb.button(text="🏆 Рейтинг", callback_data="menu:top")
    kb.adjust(1, 1, 1, 2)
    return kb.as_markup()


def cats_kb():
    kb = InlineKeyboardBuilder()
    for key, title in CATEGORIES.items():
        kb.button(text=title, callback_data=f"cat:{key}")
    kb.button(text="⬅️ Назад", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()


def after_game_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="🔁 Ещё раунд", callback_data="menu:play")
    kb.button(text="🏠 В меню", callback_data="menu:home")
    kb.adjust(1)
    return kb.as_markup()


# ---------- игровая логика ----------
def daily_question_id():
    return random.Random(date.today().isoformat()).randrange(len(QUESTIONS))


async def ask(target: Message, uid: int):
    s = sessions[uid]
    qq = QUESTIONS[s["ids"][s["pos"]]]
    order = list(range(len(qq["options"])))
    random.shuffle(order)
    s["order"] = order
    s["answered"] = False

    kb = InlineKeyboardBuilder()
    for i, opt_index in enumerate(order):
        kb.button(text=qq["options"][opt_index], callback_data=f"a:{i}")
    kb.adjust(1)

    if s["mode"] == "daily":
        head = "☀️ Вопрос дня"
    else:
        head = f"Вопрос {s['pos'] + 1} из {len(s['ids'])}"
    await target.answer(f"{head}\n\n{qq['q']}", reply_markup=kb.as_markup())


async def start_game(target: Message, user, category=None):
    ensure_user(user)
    pool = [i for i, x in enumerate(QUESTIONS) if category in (None, x["cat"])]
    ids = random.sample(pool, min(ROUND_SIZE, len(pool)))
    sessions[user.id] = {"ids": ids, "pos": 0, "score": 0, "mode": "game", "order": [], "answered": False}
    await ask(target, user.id)


async def start_daily(target: Message, user):
    ensure_user(user)
    u = q("SELECT streak, last_daily FROM users WHERE user_id=?", (user.id,), one=True)
    if u and u["last_daily"] == str(date.today()):
        await target.answer(
            f"Сегодня ты уже ответил на вопрос дня. Серия: {u['streak']} 🔥\nНовый вопрос будет завтра.",
            reply_markup=menu_kb(),
        )
        return
    sessions[user.id] = {
        "ids": [daily_question_id()], "pos": 0, "score": 0,
        "mode": "daily", "order": [], "answered": False,
    }
    await ask(target, user.id)


async def finish_game(target: Message, user):
    s = sessions.pop(user.id)
    total, score = len(s["ids"]), s["score"]
    q("UPDATE users SET games=games+1, best=MAX(best, ?) WHERE user_id=?", (score, user.id), write=True)

    ratio = score / total
    if ratio == 1:
        verdict = "Безупречно! Тебе пора вести собственную колонку в Vogue 👑"
    elif ratio >= 0.7:
        verdict = "Отличный результат, ты явно в теме 💎"
    elif ratio >= 0.4:
        verdict = "Неплохо! Есть куда расти ✂️"
    else:
        verdict = "Пока это только начало, но каждый эксперт с чего-то начинал 🌱"

    u = q("SELECT points FROM users WHERE user_id=?", (user.id,), one=True)
    await target.answer(
        f"🏁 Раунд завершён!\n\nРезультат: {score} из {total}\n{verdict}\n\n"
        f"Всего очков: {u['points']} · {level_name(u['points'])}\n"
        f"{next_level_hint(u['points']).capitalize()}",
        reply_markup=after_game_kb(),
    )


def profile_text(user) -> str:
    u = q("SELECT * FROM users WHERE user_id=?", (user.id,), one=True)
    if not u or not u["answered"]:
        return "Ты ещё не отвечал на вопросы. Сыграй первый раунд, и здесь появится твоя статистика!"

    acc = round(100 * u["correct"] / u["answered"])
    lines = [
        f"📊 {u['name']}",
        f"Уровень: {level_name(u['points'])}",
        f"Очки: {u['points']} ({next_level_hint(u['points'])})",
        f"Точность: {acc}% ({u['correct']} из {u['answered']})",
        f"Раундов сыграно: {u['games']} · лучший результат: {u['best']}",
        f"Серия вопроса дня: {u['streak']} 🔥",
        "",
        "По категориям:",
    ]
    stats = {r["category"]: r for r in q("SELECT * FROM cat_stats WHERE user_id=?", (user.id,))}
    best, worst = None, None
    for key, title in CATEGORIES.items():
        r = stats.get(key)
        if not r:
            lines.append(f"{title}: пока нет ответов")
            continue
        pct = round(100 * r["correct"] / r["answered"])
        lines.append(f"{title}: {pct}% ({r['correct']}/{r['answered']})")
        if r["answered"] >= 3:
            if best is None or pct > best[1]:
                best = (title, pct)
            if worst is None or pct < worst[1]:
                worst = (title, pct)
    if best and worst and best[0] != worst[0]:
        lines += ["", f"💪 Сильная сторона: {best[0]}", f"📖 Стоит подтянуть: {worst[0]}"]
    return "\n".join(lines)


def top_text() -> str:
    rows = q("SELECT name, points FROM users WHERE points > 0 ORDER BY points DESC LIMIT 10")
    if not rows:
        return "Рейтинг пока пуст. Сыграй раунд и стань первым!"
    medals = ["🥇", "🥈", "🥉"]
    lines = ["🏆 Топ-10 знатоков моды:", ""]
    for i, r in enumerate(rows):
        mark = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{mark} {r['name']}: {r['points']} очк.")
    return "\n".join(lines)


# ---------- хендлеры ----------
WELCOME = (
    "Привет! Это квиз по миру моды 👗\n\n"
    "Проверь, как хорошо ты знаешь бренды, дизайнеров, показы и знаковые вещи. "
    "За верные ответы получай очки и повышай уровень: от Новичка до Главреда Vogue.\n\n"
    "Выбери, с чего начать:"
)


@router.message(CommandStart())
async def cmd_start(m: Message):
    ensure_user(m.from_user)
    await m.answer(WELCOME, reply_markup=menu_kb())


@router.message(Command("help"))
async def cmd_help(m: Message):
    await m.answer(
        "/play — раунд из 10 вопросов\n/daily — вопрос дня\n/profile — моя статистика\n"
        "/top — рейтинг\n/start — главное меню",
        reply_markup=menu_kb(),
    )


@router.message(Command("play"))
async def cmd_play(m: Message):
    await start_game(m, m.from_user)


@router.message(Command("daily"))
async def cmd_daily(m: Message):
    await start_daily(m, m.from_user)


@router.message(Command("profile"))
async def cmd_profile(m: Message):
    ensure_user(m.from_user)
    await m.answer(profile_text(m.from_user), reply_markup=menu_kb())


@router.message(Command("top"))
async def cmd_top(m: Message):
    await m.answer(top_text(), reply_markup=menu_kb())


@router.callback_query(F.data.startswith("menu:"))
async def on_menu(cb: CallbackQuery):
    await cb.answer()
    action = cb.data.split(":", 1)[1]
    msg, user = cb.message, cb.from_user
    if action == "home":
        await msg.answer("Главное меню:", reply_markup=menu_kb())
    elif action == "play":
        await start_game(msg, user)
    elif action == "cats":
        await msg.answer("Выбери категорию:", reply_markup=cats_kb())
    elif action == "daily":
        await start_daily(msg, user)
    elif action == "profile":
        ensure_user(user)
        await msg.answer(profile_text(user), reply_markup=menu_kb())
    elif action == "top":
        await msg.answer(top_text(), reply_markup=menu_kb())


@router.callback_query(F.data.startswith("cat:"))
async def on_category(cb: CallbackQuery):
    await cb.answer()
    await start_game(cb.message, cb.from_user, category=cb.data.split(":", 1)[1])


@router.callback_query(F.data.startswith("a:"))
async def on_answer(cb: CallbackQuery):
    uid = cb.from_user.id
    s = sessions.get(uid)
    if not s or s["answered"]:
        await cb.answer("Эта игра уже закончилась. Нажми /play, чтобы начать новую.", show_alert=True)
        return
    s["answered"] = True

    qq = QUESTIONS[s["ids"][s["pos"]]]
    chosen = s["order"][int(cb.data.split(":", 1)[1])]
    ok = chosen == qq["answer"]
    record_answer(uid, qq["cat"], ok)
    if ok:
        s["score"] += 1

    result = "✅ Верно!" if ok else f"❌ Неверно. Правильный ответ: {qq['options'][qq['answer']]}"
    text = f"{cb.message.text}\n\n{result}\n\n💡 {qq['fact']}"

    kb = InlineKeyboardBuilder()
    if s["mode"] == "daily":
        streak = mark_daily(uid)
        text += f"\n\nСерия вопроса дня: {streak} 🔥"
        kb.button(text="🏠 В меню", callback_data="menu:home")
        sessions.pop(uid, None)
    else:
        last = s["pos"] + 1 >= len(s["ids"])
        kb.button(text="🏁 Итоги" if last else "Дальше ➡️", callback_data="next")
    await cb.message.edit_text(text, reply_markup=kb.as_markup())
    await cb.answer("Верно!" if ok else "Мимо")


@router.callback_query(F.data == "next")
async def on_next(cb: CallbackQuery):
    s = sessions.get(cb.from_user.id)
    if not s:
        await cb.answer("Эта игра уже закончилась. Нажми /play.", show_alert=True)
        return
    await cb.answer()
    await cb.message.edit_reply_markup(reply_markup=None)
    s["pos"] += 1
    if s["pos"] >= len(s["ids"]):
        await finish_game(cb.message, cb.from_user)
    else:
        await ask(cb.message, cb.from_user.id)


# ---------- ежедневное напоминание ----------
async def daily_reminder(bot: Bot):
    last_sent = None
    while True:
        now = datetime.now()
        if now.hour == REMINDER_HOUR and last_sent != now.date():
            last_sent = now.date()
            rows = q(
                "SELECT user_id FROM users WHERE last_daily IS NULL OR last_daily != ?",
                (str(now.date()),),
            )
            kb = InlineKeyboardBuilder()
            kb.button(text="☀️ Ответить", callback_data="menu:daily")
            for r in rows:
                try:
                    await bot.send_message(r["user_id"], "☀️ Новый вопрос дня уже ждёт тебя!", reply_markup=kb.as_markup())
                except Exception:
                    pass  # пользователь мог заблокировать бота
                await asyncio.sleep(0.05)
        await asyncio.sleep(60)


# ---------- запуск ----------
async def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise SystemExit("Задай токен: переменная окружения BOT_TOKEN (получить у @BotFather).")

    init_db()
    bot = Bot(token)
    dp = Dispatcher()
    dp.include_router(router)

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="play", description="Раунд из 10 вопросов"),
        BotCommand(command="daily", description="Вопрос дня"),
        BotCommand(command="profile", description="Моя статистика"),
        BotCommand(command="top", description="Рейтинг"),
    ])
    asyncio.create_task(daily_reminder(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
