"""Fashion Check Bot v4: один режим — Квиз (4 варианта, без фото). Известность × сложность, XP, 12 рангов."""
import asyncio, html, json, urllib.parse, logging, os, random, re, sqlite3, time
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, BufferedInputFile, InlineQuery, InlineQueryResultArticle,
                           InputMediaPhoto, InputTextMessageContent, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder

BASE = Path(__file__).parent
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
DB_PATH = Path(os.getenv("DB_PATH", BASE / "fashion.db"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))  # по UTC; 9 = 12:00 по Москве
QUESTIONS = {x["id"]: x for x in json.loads((BASE / "questions.json").read_text(encoding="utf-8"))}

FAME = {1: "🟢 Лёгкий", 2: "🟡 Средний", 3: "🔴 Сложный", 4: "⚫ Нишевый"}  # уровни
CX = {1: "лёгкий", 2: "средний", 3: "сложный"}
BASE_XP = {1: 10, 2: 25, 3: 50}
FAME_MULT = {1: 1.0, 2: 1.3, 3: 1.7, 4: 2.0}

RANKS = [  # (порог XP, название, описание)
    (0, "Новичок гардероба", "Первые шаги: лёгкий уровень."),
    (250, "Уличный наблюдатель", "Замечаешь, что носят вокруг."),
    (700, "Охотник за кроссовками", "Открыт средний уровень."),
    (1500, "Знаток денима", "Средний уровень — твой."),
    (2600, "Модный детектив", "Ты узнаёшь бренды по деталям."),
    (4000, "Стилист-аналитик", "Открыт сложный уровень."),
    (5800, "Охотник за архивом", "Скоро нишевый уровень."),
    (8000, "Куратор винтажа", "Открыт нишевый уровень."),
    (10500, "Архивист моды", "Открыты архивные нишевые вещи."),
    (14000, "Мастер нишевых имён", "Титул мастера."),
    (19000, "Легенда подиума", "Почётный титул."),
    (26000, "Хранитель архива", "Высший ранг."),
]
UNLOCK_TEXT = {1: "🟢 лёгкий", 3: "🟡 средний", 5: "🔴 сложный", 7: "⚫ нишевый", 9: "🕰 нишевый архив"}

SETS = {  # название набора -> (тег, подпись)
    "sneakers": "👟 Кроссовки", "jpdenim": "🧵 Японский деним", "luxury": "💎 Люкс",
    "japan": "🇯🇵 Японские бренды", "outerwear": "🧥 Верхняя одежда", "bags": "👜 Сумки",
    "street": "🛹 Уличная мода",
}
SET_BONUS = 150

EVENTS = [  # (месяц, день от, день до, название) — ×1.5 XP всем
    (2, 10, 25, "Неделя моды (зима)"), (3, 1, 10, "Парижская неделя моды"),
    (9, 5, 20, "Неделя моды (осень)"), (10, 1, 7, "Неделя нишевых брендов"),
    (11, 1, 10, "Неделя Японии 🇯🇵"), (11, 24, 30, "Чёрная пятница стритвира"),
    (12, 20, 31, "Новогодний марафон"),
]

router = Router()
sess: dict[int, dict] = {}


# ---------- БД ----------
def q(sql, args=(), fetch="all"):
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        cur = con.execute(sql, args)
        res = cur.fetchall() if fetch == "all" else cur.fetchone() if fetch == "one" else None
        con.commit()
        return res
    finally:
        con.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    for s in [
        """CREATE TABLE IF NOT EXISTS u3(uid INTEGER PRIMARY KEY, name TEXT, xp INTEGER DEFAULT 0, run INTEGER DEFAULT 0,
           best_run INTEGER DEFAULT 0, day_streak INTEGER DEFAULT 0, last_day TEXT, last_seen TEXT, total INTEGER DEFAULT 0,
           correct INTEGER DEFAULT 0, boost INTEGER DEFAULT 0, remind INTEGER DEFAULT 1)""",
        "CREATE TABLE IF NOT EXISTS seen3(uid INTEGER, qid INTEGER, ok INTEGER, PRIMARY KEY(uid,qid))",
        "CREATE TABLE IF NOT EXISTS sets3(uid INTEGER, name TEXT, PRIMARY KEY(uid,name))",
        "CREATE TABLE IF NOT EXISTS ach3(uid INTEGER, key TEXT, PRIMARY KEY(uid,key))",
    ]:
        q(s, fetch=None)
    try:
        q("ALTER TABLE u3 ADD COLUMN diff INTEGER DEFAULT 0", fetch=None)
    except sqlite3.OperationalError:
        pass
    q("UPDATE u3 SET diff=0 WHERE diff>4", fetch=None)
    q("CREATE TABLE IF NOT EXISTS logos3(brand TEXT PRIMARY KEY, file_id TEXT)", fetch=None)
    q("CREATE TABLE IF NOT EXISTS xplog(uid INTEGER, day TEXT, xp INTEGER, PRIMARY KEY(uid,day))", fetch=None)
    q("CREATE TABLE IF NOT EXISTS daily3(uid INTEGER, day TEXT, qid INTEGER, ok INTEGER, gain INTEGER, PRIMARY KEY(uid,day))", fetch=None)


def log_xp(uid, n):
    if n:
        q("INSERT INTO xplog(uid,day,xp) VALUES(?,?,?) ON CONFLICT(uid,day) DO UPDATE SET xp=xp+excluded.xp",
          (uid, str(date.today()), n), fetch=None)


DIFF_NAMES = {0: "🎲 Авто (по рангу)", 1: "🟢 Лёгкий", 2: "🟡 Средний", 3: "🔴 Сложный", 4: "⚫ Нишевый"}


def get_diff(uid):
    r = q("SELECT diff FROM u3 WHERE uid=?", (uid,), "one")
    return (r["diff"] or 0) if r else 0


def ensure_user(m) -> sqlite3.Row:
    u = m.from_user
    q("INSERT OR IGNORE INTO u3(uid,name) VALUES(?,?)", (u.id, u.full_name), fetch=None)
    q("UPDATE u3 SET name=? WHERE uid=?", (u.full_name, u.id), fetch=None)
    return q("SELECT * FROM u3 WHERE uid=?", (u.id,), "one")


def rank_of(xp: int) -> int:
    return sum(1 for t, _, _ in RANKS if xp >= t)  # 1..12


def event_now():
    d = date.today()
    for mo, a, b, name in EVENTS:
        if d.month == mo and a <= d.day <= b:
            return name
    return None


def touch(uid: int) -> str:
    """Мягкий возврат: ≥7 дней без игры — бонус, серии сброшены, прогресс цел."""
    r = q("SELECT last_seen FROM u3 WHERE uid=?", (uid,), "one")
    today = str(date.today())
    note = ""
    if r and r["last_seen"]:
        gap = (date.today() - date.fromisoformat(r["last_seen"])).days
        if gap >= 7:
            q("UPDATE u3 SET xp=xp+100, boost=5, run=0, day_streak=0 WHERE uid=?", (uid,), fetch=None)
            note = "🎁 С возвращением! +100 XP и двойной XP за следующие 5 ответов. Твой прогресс сохранён."
    q("UPDATE u3 SET last_seen=? WHERE uid=?", (today, uid), fetch=None)
    return note


# ---------- доступ по рангу ----------
def allowed(rank: int, x: dict) -> bool:
    """Авто-режим: уровни открываются по рангу (явный выбор уровня рангом не ограничен)."""
    lv = x["level"]
    if lv == 1: return True
    if lv == 2: return rank >= 3
    if lv == 3: return rank >= 5
    return rank >= (9 if x["archive"] else 7)


# ---------- XP ----------
def award(uid: int, x: dict, frac: float, elapsed: float, mult: float = 1.0):
    """frac: доля правильного (1 — верно). Возвращает (xp, заметки)."""
    u = q("SELECT * FROM u3 WHERE uid=?", (uid,), "one")
    prior = q("SELECT ok FROM seen3 WHERE uid=? AND qid=?", (uid, x["id"]), "one")
    notes = []
    old_rank = rank_of(u["xp"])
    q("INSERT OR IGNORE INTO seen3(uid,qid,ok) VALUES(?,?,0)", (uid, x["id"]), fetch=None)
    today, yest = str(date.today()), str(date.today() - timedelta(days=1))
    day = u["day_streak"]
    if u["last_day"] != today:
        day = day + 1 if u["last_day"] == yest else 1
    ok = frac >= 0.999
    run = u["run"] + 1 if ok else 0
    gain = 0
    if frac > 0:
        bonus = 0.0
        if run >= 5: bonus += 0.5; notes.append("🔥 серия ×1.5")
        if elapsed <= 10 and ok: bonus += 0.2; notes.append("⚡ скорость +20%")
        if x["archive"]: bonus += 0.2; notes.append("🕰 архив +20%")
        v = BASE_XP[x["cx"]] * FAME_MULT[x["level"]] * (1 + bonus) * frac
        if mult < 1: v *= mult; notes.append(f"💡 подсказка ×{mult}")
        if prior: v *= 0.5; notes.append("♻️ повтор −50%")
        dm = 1 + min(0.25, 0.05 * (day - 1))
        if dm > 1: notes.append(f"📅 дни подряд +{round((dm - 1) * 100)}%")
        v *= dm
        if event_now(): v *= 1.5; notes.append(f"🎉 {event_now()} ×1.5")
        if u["boost"] > 0: v *= 2; notes.append("🎁 двойной XP")
        gain = max(1, round(v))
    boost = max(0, u["boost"] - 1) if u["boost"] > 0 else 0
    if ok: q("UPDATE seen3 SET ok=1 WHERE uid=? AND qid=?", (uid, x["id"]), fetch=None)
    q("""UPDATE u3 SET xp=xp+?, run=?, best_run=MAX(best_run,?), day_streak=?, last_day=?, total=total+1,
         correct=correct+?, boost=? WHERE uid=?""", (gain, run, run, day, today, 1 if ok else 0, boost, uid), fetch=None)
    extra = check_progress(uid)
    notes += extra[0]
    gain += extra[1]
    log_xp(uid, gain)
    new_rank = rank_of(q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")["xp"])
    if new_rank > old_rank:
        notes.append(f"🏅 НОВЫЙ РАНГ: {RANKS[new_rank - 1][1]}!")
    return gain, notes


def correct_rows(uid):
    return [QUESTIONS[r["qid"]] for r in q("SELECT qid FROM seen3 WHERE uid=? AND ok=1", (uid,)) if r["qid"] in QUESTIONS]


def set_total(tag):
    return sum(1 for x in QUESTIONS.values() if tag in x["cats"])


def check_progress(uid):
    rows = correct_rows(uid)
    notes, bonus = [], 0
    for tag, title in SETS.items():
        need = min(10, set_total(tag))
        if need and sum(1 for x in rows if tag in x["cats"]) >= need:
            if not q("SELECT 1 FROM sets3 WHERE uid=? AND name=?", (uid, tag), "one"):
                q("INSERT INTO sets3 VALUES(?,?)", (uid, tag), fetch=None)
                bonus += SET_BONUS
                notes.append(f"📚 Набор «{title}» пройден! +{SET_BONUS} XP")
    u = q("SELECT * FROM u3 WHERE uid=?", (uid,), "one")
    jp = {x["brand"] for x in rows if "japan" in x["cats"]}
    ach = {
        "first": ("🌟 Первый верный ответ", u["correct"] >= 1),
        "run10": ("🔥 10 верных подряд", u["best_run"] >= 10),
        "jp10": ("🇯🇵 10 японских брендов", len(jp) >= 10),
        "arch10": ("🕰 10 архивных вещей", sum(1 for x in rows if x["archive"]) >= 10),
        "brands20": ("🏛 Коллекция из 20 брендов", len({x["brand"] for x in rows if x["brand"] != "Общее"}) >= 20),
        "rank5": ("💎 Ранг «Модный детектив»", rank_of(u["xp"]) >= 5),
    }
    for k, (title, cond) in ach.items():
        if cond and not q("SELECT 1 FROM ach3 WHERE uid=? AND key=?", (uid, k), "one"):
            q("INSERT INTO ach3 VALUES(?,?)", (uid, k), fetch=None)
            bonus += 50
            notes.append(f"🏆 Достижение: {title} (+50 XP)")
    if bonus:
        q("UPDATE u3 SET xp=xp+? WHERE uid=?", (bonus, uid), fetch=None)
    return notes, bonus


ACH_TITLES = {"first": "🌟 Первый верный ответ", "run10": "🔥 10 верных подряд", "jp10": "🇯🇵 10 японских брендов",
              "arch10": "🕰 10 архивных вещей",
              "brands20": "🏛 Коллекция из 20 брендов", "rank5": "💎 Ранг «Модный детектив»"}


# ---------- клавиатуры ----------
def menu_kb(uid):
    rank = rank_of(q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")["xp"])
    kb = InlineKeyboardBuilder()
    kb.button(text="🧠 Играть в квиз", callback_data="play:g")
    kb.button(text="📅 Вопрос дня", callback_data="m:daily")
    kb.button(text="⚔️ Дуэль с другом", callback_data="m:duel")
    kb.button(text="📚 Наборы", callback_data="m:sets")
    kb.button(text="👤 Профиль", callback_data="m:prof")
    kb.button(text="🗂 Коллекции", callback_data="m:coll")
    kb.button(text="🏆 Рейтинг", callback_data="m:top")
    kb.button(text=f"⚙️ Сложность: {DIFF_NAMES[get_diff(uid)]}", callback_data="m:diff")
    kb.adjust(1, 1, 1, 2, 2, 1)
    return kb.as_markup()


def menu_text(uid, note=""):
    u = q("SELECT * FROM u3 WHERE uid=?", (uid,), "one")
    r = rank_of(u["xp"])
    ev = event_now()
    s = f"<b>Fashion Check</b>\nРанг: <b>{r}. {RANKS[r - 1][1]}</b> · {u['xp']} XP\n"
    if ev: s += f"🎉 Событие: {ev} — ×1.5 XP!\n"
    if note: s += "\n" + note + "\n"
    return s + "\nВыбери режим:"


# ---------- вопросы ----------
import base64
LOGO_FILES = {}
for _f in sorted(p.name for p in BASE.glob("logos_data*.json")):
    try:
        LOGO_FILES.update(json.loads((BASE / _f).read_text(encoding="utf-8")))
    except Exception:
        pass


def logo_brands():
    return set(LOGO_FILES)


async def send_img(msg: Message, key: str, kind: str, caption: str = "", kb=None):
    """Несколько снимков вещи: альбом, а подпись с кнопками — отдельным сообщением."""
    extra = LOGO_FILES[key].get(kind + "x")
    if not extra:
        return await send_logo(msg, key, kind, caption, kb)
    pics = [LOGO_FILES[key][kind]] + extra
    await msg.answer_media_group([InputMediaPhoto(media=BufferedInputFile(base64.b64decode(b), filename=f"{i}.jpg")) for i, b in enumerate(pics)])
    return await msg.answer(caption, reply_markup=kb)


async def send_logo(msg: Message, brand: str, kind: str, caption: str = "", kb=None):
    """kind: q — фрагмент логотипа, full — полный. file_id кэшируется."""
    key = f"v5|{brand}|{kind}"
    row = q("SELECT file_id FROM logos3 WHERE brand=?", (key,), "one")
    if row:
        return await msg.answer_photo(row["file_id"], caption=caption, reply_markup=kb)
    sent = await msg.answer_photo(BufferedInputFile(base64.b64decode(LOGO_FILES[brand][kind]), filename=f"{kind}.jpg"), caption=caption, reply_markup=kb)
    q("INSERT OR REPLACE INTO logos3 VALUES(?,?)", (key, sent.photo[-1].file_id), fetch=None)
    return sent


def pick(uid, mode="g", tag=None):
    u = q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")
    rank = rank_of(u["xp"])
    seen = {r["qid"] for r in q("SELECT qid FROM seen3 WHERE uid=?", (uid,))}
    lb = logo_brands()
    base = [x for x in QUESTIONS.values() if (x["mode"] == "g" or x.get("img", x["brand"]) in lb) and (not tag or tag in x["cats"])]
    d = get_diff(uid)
    pool = [x for x in base if x["level"] == d] if d else [x for x in base if allowed(rank, x)]
    fresh = [x for x in pool if x["id"] not in seen]
    if fresh:
        top = max(x["level"] for x in fresh)
        w = [3 if x["level"] == top else 1 for x in fresh]
        return random.choices(fresh, w)[0]
    return random.choice(pool) if pool else None


async def ask(msg: Message, uid: int, mode: str = "g", tag=None, x=None):
    x = x or pick(uid, "g", tag)
    if not x:
        await msg.answer("В этом наборе пока нет доступных вопросов. Прокачай ранг или смени сложность!")
        return
    sess[uid] = {"qid": x["id"], "t0": time.time(), "mode": "g", "tag": tag, "stage": 1}
    head = f"{FAME[x['level']]}" + ((" · 📸 фото" if x.get("img") else " · 🖼 логотип") if x["mode"] == "l" else "")
    if x["archive"]: head += " · 🕰 архив"
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(x["opts"]):
        kb.button(text=f"{'ABCD'[i]}) {o}", callback_data=f"a:{x['id']}:{i}")
    kb.adjust(1)
    text = f"<b>{head}</b>\n\n❓ {html.escape(x['q'])}"
    if x["mode"] == "l" and x.get("img", x["brand"]) in LOGO_FILES:
        if x.get("plain"):
            kb.adjust(1)
            await send_img(msg, x.get("img", x["brand"]), "full", text, kb.as_markup())
            return
        if not x.get("blur"):
            kb.button(text="💡 Подсказка (меньше XP)", callback_data=f"h:{x['id']}")
        kb.adjust(1)
        await send_img(msg, x.get("img", x["brand"]), "q1", text, kb.as_markup())
        return
    await msg.answer(text, reply_markup=kb.as_markup())


def next_kb(uid):
    s = sess.get(uid, {"mode": "g", "tag": None})
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Дальше", callback_data="next")
    kb.button(text="🏠 Меню", callback_data="m:menu")
    kb.adjust(2)
    return kb.as_markup()


def result_text(x, ok_text, gain, notes):
    s = ok_text + f"\n\n💡 {html.escape(x['ex'])}"
    if x["archive"] and x["archive_note"]: s += f"\n🕰 <b>Архив:</b> {html.escape(x['archive_note'])}"
    if gain: s += f"\n\n<b>+{gain} XP</b>"
    if notes: s += "\n" + "\n".join(notes)
    return s


# ---------- хендлеры ----------
@router.message(CommandStart())
@router.message(Command("menu"))
async def start(m: Message, bot: Bot, command: CommandObject = None):
    ensure_user(m)
    arg = (command.args or "") if command else ""
    if arg.startswith("duel_"):
        await duel_join(bot, arg[5:], m.from_user, m.chat.id)
        return
    note = touch(m.from_user.id)
    await m.answer(menu_text(m.from_user.id, note), reply_markup=menu_kb(m.from_user.id))


@router.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Твой ID: <code>{m.from_user.id}</code>")


@router.callback_query(F.data.startswith("m:"))
async def menus(c: CallbackQuery):
    ensure_user(c)
    uid, what = c.from_user.id, c.data[2:]
    await c.answer()
    if what == "menu":
        await c.message.answer(menu_text(uid, touch(uid)), reply_markup=menu_kb(uid))
    elif what == "sets":
        rows = correct_rows(uid)
        kb = InlineKeyboardBuilder()
        txt = "<b>📚 Тематические наборы</b>\nОтветь верно на 10 разных вопросов набора — бонус +150 XP.\n"
        for tag, title in SETS.items():
            need = min(10, set_total(tag))
            have = min(need, sum(1 for x in rows if tag in x["cats"]))
            done = q("SELECT 1 FROM sets3 WHERE uid=? AND name=?", (uid, tag), "one")
            kb.button(text=f"{title} {'✅' if done else f'{have}/{need}'}", callback_data=f"set:{tag}")
        kb.button(text="🕰 Архив (вперемешку)", callback_data="set:archive")
        kb.button(text="🏠 Меню", callback_data="m:menu")
        kb.adjust(1)
        await c.message.answer(txt, reply_markup=kb.as_markup())
    elif what == "prof":
        u = q("SELECT * FROM u3 WHERE uid=?", (uid,), "one")
        r = rank_of(u["xp"])
        nxt = RANKS[r][0] if r < len(RANKS) else None
        bar = ""
        if nxt:
            p = (u["xp"] - RANKS[r - 1][0]) / (nxt - RANKS[r - 1][0])
            bar = "▓" * int(p * 10) + "░" * (10 - int(p * 10)) + f" {u['xp']}/{nxt}\nСледующий: {RANKS[r][1]} — {RANKS[r][2]}"
        acc = round(100 * u["correct"] / u["total"]) if u["total"] else 0
        achs = [ACH_TITLES[a["key"]] for a in q("SELECT key FROM ach3 WHERE uid=?", (uid,)) if a["key"] in ACH_TITLES]
        opened = ", ".join(UNLOCK_TEXT[i] for i in range(1, min(r, 9) + 1) if i in UNLOCK_TEXT and i >= 1)
        s = (f"<b>👤 {html.escape(u['name'] or '')}</b>\nРанг: <b>{r}. {RANKS[r - 1][1]}</b>\n{RANKS[r - 1][2]}\n{bar}\n\n"
             f"Ответов: {u['total']} · точность {acc}%\nЛучшая серия: {u['best_run']} · дней подряд: {u['day_streak']}\n"
             f"🏆 Достижения: {', '.join(achs) if achs else 'пока нет'}")
        kb = InlineKeyboardBuilder()
        kb.button(text=f"🔔 Напоминания: {'вкл' if u['remind'] else 'выкл'}", callback_data="m:rem")
        kb.button(text="🏠 Меню", callback_data="m:menu")
        kb.adjust(1)
        await c.message.answer(s, reply_markup=kb.as_markup())
    elif what == "diff":
        kb = InlineKeyboardBuilder()
        for k, v in DIFF_NAMES.items():
            kb.button(text=("✅ " if k == get_diff(uid) else "") + v, callback_data=f"diff:{k}")
        kb.adjust(1)
        await c.message.answer("<b>⚙️ Сложность</b>\n🟢 Лёгкий: очевидные факты — знаковые детали, страны.\n"
                               "🟡 Средний: модели, истории брендов, значения названий.\n"
                               "🔴 Сложный: годы, основатели, креативные директора, сделки.\n"
                               "⚫ Нишевый: японские бренды, авангард, архив, логотипы. XP ×2.\n"
                               "🎲 Авто: уровни открываются с ростом ранга.\nВыбранный уровень доступен сразу.", reply_markup=kb.as_markup())
    elif what == "rem":
        q("UPDATE u3 SET remind=1-remind WHERE uid=?", (uid,), fetch=None)
        await c.message.answer("Настройка обновлена.", reply_markup=menu_kb(uid))
    elif what == "coll":
        rows = correct_rows(uid)
        brands = sorted({x["brand"] for x in rows if x["brand"] != "Общее"})
        arch = [x for x in rows if x["archive"]]
        s = (f"<b>🗂 Коллекции</b>\n\n🏛 Бренды: {len(brands)}/20\n"
             f"{html.escape(', '.join(brands[:40])) if brands else 'пока пусто'}\n\n"
             f"🕰 Архивные вещи: {len(arch)}/10\n" + "\n".join("• " + html.escape(x["brand"] + " — " + x["q"][:60]) for x in arch[:20]))
        kb = InlineKeyboardBuilder(); kb.button(text="🏠 Меню", callback_data="m:menu")
        await c.message.answer(s, reply_markup=kb.as_markup())
    elif what == "top":
        await show_top(c.message, uid, "all")
    elif what == "daily":
        await daily_start(c.message, uid)
    elif what == "duel":
        kb = InlineKeyboardBuilder()
        kb.button(text="⚔️ Создать дуэль", callback_data="du:new")
        kb.button(text="🏠 Меню", callback_data="m:menu")
        kb.adjust(1)
        await c.message.answer(
            "<b>⚔️ Дуэль</b>\n15 вопросов: 2 лёгких, 3 средних, 5 сложных и 5 нишевых, среди них фото и логотипы. На каждый 25 секунд. "
            "Верный ответ даёт очки, а чем быстрее — тем больше. Победитель получает +60 XP, проигравший +15 (до 5 дуэлей в день).\n\n"
            "<b>Как вызвать друга:</b>\n• Прямо в вашем чате: напиши @" + BOT_USERNAME["v"] + " и выбери «Вызвать на дуэль». Друг нажмёт «Принять» — игра идёт в личке с ботом, а итог появится в вашем чате.\n"
            "• Ссылкой: нажми «Создать дуэль» и отправь ссылку другу.\n"
            "• В группе с ботом: напиши /duel — игра идёт прямо в группе.",
            reply_markup=kb.as_markup())


TOP_TITLES = {"day": "за сегодня", "week": "за 7 дней", "all": "за всё время"}


async def show_top(msg: Message, uid: int, per: str):
    if per == "all":
        rows = q("SELECT uid,name,xp FROM u3 WHERE total>0 ORDER BY xp DESC, uid LIMIT 10")
        full = q("SELECT uid FROM u3 WHERE total>0 ORDER BY xp DESC, uid")
    else:
        since = str(date.today() if per == "day" else date.today() - timedelta(days=6))
        sql = ("SELECT u3.uid AS uid, u3.name AS name, SUM(xplog.xp) AS pxp FROM xplog JOIN u3 ON u3.uid=xplog.uid "
               "WHERE xplog.day>=? GROUP BY u3.uid HAVING pxp>0 ORDER BY pxp DESC, u3.uid")
        rows = q(sql + " LIMIT 10", (since,))
        full = q(sql, (since,))
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = []
    val = "xp" if per == "all" else "pxp"
    for i, r in enumerate(rows, 1):
        me = " ← ты" if r["uid"] == uid else ""
        lines.append(f"{medals.get(i, str(i) + '.')} {html.escape(r['name'] or '—')} — {r[val]} XP{me}")
    s = f"<b>🏆 Таблица лидеров {TOP_TITLES[per]}</b>\n" + ("\n".join(lines) if lines else "Пока никого — стань первым!")
    place = next((i for i, r in enumerate(full, 1) if r["uid"] == uid), None)
    if place and place > 10:
        mine = q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")["xp"] if per == "all" else next(r["pxp"] for r in full if r["uid"] == uid)
        s += f"\n…\n{place}. ты — {mine} XP"
    kb = InlineKeyboardBuilder()
    for k, t in (("day", "Сегодня"), ("week", "Неделя"), ("all", "Всё время")):
        kb.button(text=("• " if k == per else "") + t, callback_data=f"top:{k}")
    kb.button(text="🏠 Меню", callback_data="m:menu")
    kb.adjust(3, 1)
    await msg.answer(s, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("top:"))
async def top_cb(c: CallbackQuery):
    ensure_user(c)
    await c.answer()
    await strip_kb(c)
    await show_top(c.message, c.from_user.id, c.data[4:])


# ---------- вопрос дня ----------
DAILY_BONUS = 50
DT0: dict[int, float] = {}


def daily_question():
    pool = sorted((x for x in QUESTIONS.values() if x["mode"] == "g" and x["level"] >= 2), key=lambda x: x["id"])
    return random.Random(str(date.today())).choice(pool)


async def daily_start(msg: Message, uid: int):
    day = str(date.today())
    x = daily_question()
    done = q("SELECT * FROM daily3 WHERE uid=? AND day=?", (uid, day), "one")
    kb = InlineKeyboardBuilder()
    if done:
        right = f"{'ABCD'[x['correct']]}) {html.escape(x['opts'][x['correct']])}"
        n = q("SELECT COUNT(*) AS n, SUM(ok) AS k FROM daily3 WHERE day=?", (day,), "one")
        kb.button(text="🧠 Играть в квиз", callback_data="play:g")
        kb.button(text="🏠 Меню", callback_data="m:menu")
        kb.adjust(1)
        await msg.answer(f"📅 <b>Вопрос дня</b> уже пройден: {'✅ верно' if done['ok'] else '❌ неверно'}.\n"
                         f"Правильный ответ: <b>{right}</b>\nОтветили сегодня: {n['n']}, верно: {n['k'] or 0}.\n\nНовый вопрос будет завтра.",
                         reply_markup=kb.as_markup())
        return
    for i, o in enumerate(x["opts"]):
        kb.button(text=f"{'ABCD'[i]}) {o}", callback_data=f"d:{x['id']}:{i}")
    kb.adjust(1)
    DT0[uid] = time.time()
    await msg.answer(f"📅 <b>Вопрос дня</b> · {FAME[x['level']]} · бонус +{DAILY_BONUS} XP за верный ответ\n\n❓ {html.escape(x['q'])}",
                     reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("d:"))
async def daily_answer(c: CallbackQuery):
    uid = c.from_user.id
    ensure_user(c)
    _, qid, i = c.data.split(":")
    qid, i = int(qid), int(i)
    day = str(date.today())
    x = daily_question()
    if qid != x["id"] or q("SELECT 1 FROM daily3 WHERE uid=? AND day=?", (uid, day), "one"):
        await c.answer("Этот вопрос дня уже закрыт."); return
    ok = i == x["correct"]
    elapsed = time.time() - DT0.get(uid, time.time())
    gain, notes = award(uid, x, 1.0 if ok else 0.0, elapsed)
    if ok:
        q("UPDATE u3 SET xp=xp+? WHERE uid=?", (DAILY_BONUS, uid), fetch=None)
        log_xp(uid, DAILY_BONUS)
        gain += DAILY_BONUS
        notes.append(f"📅 бонус вопроса дня +{DAILY_BONUS}")
    q("INSERT INTO daily3(uid,day,qid,ok,gain) VALUES(?,?,?,?,?)", (uid, day, qid, 1 if ok else 0, gain), fetch=None)
    await c.answer()
    await strip_kb(c)
    right = f"{'ABCD'[x['correct']]}) {html.escape(x['opts'][x['correct']])}"
    head = "✅ <b>Верно!</b>" if ok else f"❌ <b>Неверно.</b> Правильный ответ: <b>{right}</b>"
    await c.message.answer(result_text(x, head, gain, notes), reply_markup=next_kb(uid))


@router.callback_query(F.data.startswith("diff:"))
async def set_diff(c: CallbackQuery):
    ensure_user(c)
    q("UPDATE u3 SET diff=? WHERE uid=?", (int(c.data[5:]), c.from_user.id), fetch=None)
    await c.answer("Сохранено")
    await c.message.answer(menu_text(c.from_user.id), reply_markup=menu_kb(c.from_user.id))


@router.callback_query(F.data.startswith("play:"))
async def play(c: CallbackQuery):
    ensure_user(c)
    uid, mode = c.from_user.id, c.data[5:]
    await c.answer()
    rank = rank_of(q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")["xp"])
    await ask(c.message, uid)


@router.callback_query(F.data.startswith("set:"))
async def set_play(c: CallbackQuery):
    ensure_user(c)
    uid, tag = c.from_user.id, c.data[4:]
    await c.answer()
    if tag != "archive":
        await ask(c.message, uid, "g", tag); return
    rank = rank_of(q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")["xp"])
    seen = {r["qid"] for r in q("SELECT qid FROM seen3 WHERE uid=?", (uid,))}
    pool = [x for x in QUESTIONS.values() if x["mode"] == "g" and x["archive"] and allowed(rank, x)]
    if not pool:
        await c.message.answer("Архивные вопросы откроются по мере роста ранга."); return
    fresh = [x for x in pool if x["id"] not in seen] or pool
    await ask(c.message, uid, "g", None, random.choice(fresh))


@router.callback_query(F.data == "next")
async def nxt(c: CallbackQuery):
    uid = c.from_user.id
    await c.answer()
    s = sess.get(uid, {"mode": "g", "tag": None})
    await ask(c.message, uid, "g", s.get("tag"))


async def strip_kb(c: CallbackQuery):
    try: await c.message.edit_reply_markup(reply_markup=None)
    except Exception: pass


@router.callback_query(F.data.startswith("h:"))
async def hint_logo(c: CallbackQuery):
    uid = c.from_user.id
    qid = int(c.data.split(":")[1])
    s = sess.get(uid)
    if not s or s["qid"] != qid:
        await c.answer("Этот вопрос уже закрыт."); return
    x = QUESTIONS[qid]
    st = s.get("stage", 1)
    if st >= 3 or x.get("img", x["brand"]) not in LOGO_FILES:
        await c.answer("Подсказок больше нет."); return
    st += 1
    sess[uid] = {**s, "stage": st}
    await c.answer("Открыл больше логотипа")
    await strip_kb(c)
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(x["opts"]):
        kb.button(text=f"{'ABCD'[i]}) {o}", callback_data=f"a:{x['id']}:{i}")
    if st < 3:
        kb.button(text="💡 Ещё подсказка", callback_data=f"h:{x['id']}")
    kb.adjust(1)
    pen = {2: "×0.7", 3: "×0.4"}[st]
    head = f"{FAME[x['level']]} · {'📸 фото' if x.get('img') else '🖼 логотип'} · 💡 подсказка {st - 1}/2 (XP {pen})"
    await send_logo(c.message, x.get("img", x["brand"]), f"q{st}", f"<b>{head}</b>\n\n❓ {html.escape(x['q'])}", kb.as_markup())


@router.callback_query(F.data.startswith("a:"))
async def answer_g(c: CallbackQuery):
    uid = c.from_user.id
    _, qid, i = c.data.split(":")
    qid, i = int(qid), int(i)
    s = sess.get(uid)
    if not s or s["qid"] != qid:
        await c.answer("Этот вопрос уже закрыт.", show_alert=False); return
    x = QUESTIONS[qid]
    elapsed = time.time() - s["t0"]
    sess[uid] = {**s, "qid": None}
    ok = i == x["correct"]
    mult = {1: 1.0, 2: 0.7, 3: 0.4}[s.get("stage", 1)] if x["mode"] == "l" else 1.0
    gain, notes = award(uid, x, 1.0 if ok else 0.0, elapsed, mult)
    await c.answer()
    await strip_kb(c)
    right = f"{'ABCD'[x['correct']]}) {html.escape(x['opts'][x['correct']])}"
    head = "✅ <b>Верно!</b>" if ok else f"❌ <b>Неверно.</b> Правильный ответ: <b>{right}</b>"
    if x["mode"] == "l" and x.get("img", x["brand"]) in LOGO_FILES and not x.get("plain"):
        await send_img(c.message, x.get("img", x["brand"]), "full", result_text(x, head, gain, notes), next_kb(uid))
    else:
        await c.message.answer(result_text(x, head, gain, notes), reply_markup=next_kb(uid))


# ---------- ДУЭЛИ ----------
DUELS: dict[str, dict] = {}
BOT_USERNAME = {"v": ""}
DUEL_ROUNDS, DUEL_TIME = 15, 25
DUEL_WIN_XP, DUEL_LOSE_XP, DUEL_DRAW_XP, DUEL_DAILY_CAP = 60, 15, 35, 5
INLINE_USED: set = set()
DUEL_DAYS: dict = {}


def duel_questions():
    """15 вопросов по нарастающей: 2 лёгких, 3 средних, 5 сложных, 5 нишевых; среди них фото и логотипы."""
    lb = logo_brands()
    pool = [x for x in QUESTIONS.values() if x["mode"] == "g" or x.get("img", x["brand"]) in lb]

    def kind(x):
        return "g" if x["mode"] == "g" else ("p" if x.get("img") else "l")
    plan = [(1, "g"), (1, "g"), (2, "g"), (2, "g"), (2, "l"), (3, "g"), (3, "g"), (3, "g"), (3, "p"), (3, "p"),
            (4, "g"), (4, "g"), (4, "g"), (4, "p"), (4, "p")]
    out = []
    for lv, k in plan:
        c = [x for x in pool if x["level"] == lv and kind(x) == k and x["id"] not in out]
        c = c or [x for x in pool if x["level"] == lv and x["id"] not in out]
        out.append(random.choice(c)["id"])
    return out


def duel_busy(uid):
    return any(uid in d["players"] and d["state"] in ("wait", "play") for d in DUELS.values())


def dn(d, uid):
    return html.escape(d["names"].get(uid, "Игрок"))


async def duel_send_pic(bot: Bot, ch, key, kind, caption, kb=None):
    extra = LOGO_FILES[key].get(kind + "x")
    if extra:
        pics = [LOGO_FILES[key][kind]] + extra
        await bot.send_media_group(ch, [InputMediaPhoto(media=BufferedInputFile(base64.b64decode(p), filename=f"{i}.jpg")) for i, p in enumerate(pics)])
        return await bot.send_message(ch, caption, reply_markup=kb)
    ck = f"v5|{key}|{kind}"
    row = q("SELECT file_id FROM logos3 WHERE brand=?", (ck,), "one")
    if row:
        return await bot.send_photo(ch, row["file_id"], caption=caption, reply_markup=kb)
    sent = await bot.send_photo(ch, BufferedInputFile(base64.b64decode(LOGO_FILES[key][kind]), filename=f"{kind}.jpg"), caption=caption, reply_markup=kb)
    q("INSERT OR REPLACE INTO logos3 VALUES(?,?)", (ck, sent.photo[-1].file_id), fetch=None)
    return sent


async def duel_say(bot: Bot, d, text, kb=None, pic=None):
    out = []
    for ch in dict.fromkeys(d["chats"].values()):
        try:
            if pic:
                out.append(await duel_send_pic(bot, ch, pic[0], pic[1], text, kb))
            else:
                out.append(await bot.send_message(ch, text, reply_markup=kb))
        except Exception:
            logging.exception("duel_say")
    return out


async def inline_set(bot: Bot, d, text):
    if d.get("inline"):
        try:
            await bot.edit_message_text(text=text, inline_message_id=d["inline"], reply_markup=None)
        except Exception:
            pass


async def duel_create(bot: Bot, user, chat_id, private):
    did = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=6))
    d = {"id": did, "private": private, "players": [user.id], "names": {user.id: user.full_name},
         "chats": {user.id: chat_id}, "state": "wait", "score": {user.id: 0}, "ans": {}, "round": 0, "qs": [], "ev": None, "t0": 0}
    DUELS[did] = d
    kb = InlineKeyboardBuilder()
    if private:
        link = f"https://t.me/{BOT_USERNAME['v']}?start=duel_{did}"
        share = "https://t.me/share/url?url=" + urllib.parse.quote(link, safe="") + "&text=" + urllib.parse.quote("⚔️ Вызываю тебя на дуэль по моде! 15 вопросов, есть фото. Принимай:")
        kb.button(text="📤 Отправить другу", url=share)
        kb.button(text="❌ Отмена", callback_data=f"du:x:{did}")
        kb.adjust(1)
        await bot.send_message(chat_id, f"⚔️ <b>Дуэль создана!</b>\nНажми «Отправить другу», выбери чат — или перешли ему эту ссылку:\n\n{link}\n\nКак только друг откроет ссылку и нажмёт Start, игра начнётся у вас обоих. Ссылка живёт 30 минут.", reply_markup=kb.as_markup())
    else:
        kb.button(text="⚔️ Принять вызов", callback_data=f"du:j:{did}")
        kb.button(text="❌ Отмена", callback_data=f"du:x:{did}")
        kb.adjust(1)
        await bot.send_message(chat_id, f"⚔️ <b>{dn(d, user.id)}</b> вызывает на дуэль по моде!\n15 вопросов, 5 из них нишевые, есть фото. Кто примет вызов?", reply_markup=kb.as_markup())
    asyncio.create_task(duel_expire(bot, did))


def d_private(did):
    d = DUELS.get(did)
    return bool(d and d['private'])


async def duel_expire(bot: Bot, did):
    await asyncio.sleep(1800 if d_private(did) else 300)
    d = DUELS.get(did)
    if d and d["state"] == "wait":
        d["state"] = "dead"
        await duel_say(bot, d, "⌛ Никто не принял вызов, дуэль отменена.")


async def duel_join(bot: Bot, did, user, chat_id):
    d = DUELS.get(did)
    if not d or d["state"] == "dead":
        await bot.send_message(chat_id, "Эта дуэль уже недоступна. Создай новую: /duel")
        return "gone"
    if d["state"] != "wait":
        await bot.send_message(chat_id, "В этой дуэли уже играют.")
        return "busy"
    if user.id in d["players"]:
        await bot.send_message(chat_id, "Это твоя собственная ссылка — отправь её другу, а не открывай сам.")
        return "self"
    if duel_busy(user.id):
        await bot.send_message(chat_id, "Ты уже участвуешь в другой дуэли — доиграй её.")
        return "busy"
    d["players"].append(user.id)
    d["names"][user.id] = user.full_name
    d["chats"][user.id] = chat_id
    d["score"][user.id] = 0
    d["state"] = "play"
    asyncio.create_task(run_duel(bot, d))
    return "ok"


def duel_points(d, uid):
    a = d["ans"].get(uid)
    x = QUESTIONS[d["qs"][d["cur"] - 1]]
    if not a or a[0] != x["correct"]:
        return 0
    base = 100 + int(50 * max(0.0, 1 - (a[1] - d["t0"]) / DUEL_TIME))
    return int(base * {1: 1.0, 2: 0.7, 3: 0.4}[a[2]])


def duel_kb(d, x, r, stage):
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(x["opts"]):
        kb.button(text=f"{'ABCD'[i]}) {o}", callback_data=f"du:a:{d['id']}:{r}:{i}")
    if d.get("can_hint") and stage < 3:
        kb.button(text=f"💡 Подсказка (очки ×{0.7 if stage == 1 else 0.4})", callback_data=f"du:h:{d['id']}:{r}")
    kb.adjust(1)
    return kb.as_markup()


async def run_duel(bot: Bot, d):
    try:
        a, b = d["players"]
        d["qs"] = duel_questions()
        await duel_say(bot, d, f"⚔️ <b>Дуэль!</b> {dn(d, a)} 🆚 {dn(d, b)}\n{DUEL_ROUNDS} вопросов (5 из них нишевые, есть фото и логотипы), на каждый {DUEL_TIME} сек. Верный ответ — 100 очков + бонус за скорость.\nГотовьтесь!")
        await asyncio.sleep(4)
        await inline_set(bot, d, f"⚔️ <b>Дуэль идёт!</b> {dn(d, a)} 🆚 {dn(d, b)}\nИграют в личке с ботом, результат появится здесь.")
        for r in range(1, DUEL_ROUNDS + 1):
            x = QUESTIONS[d["qs"][r - 1]]
            d["cur"], d["ans"], d["ev"], d["t0"], d["round"] = r, {}, asyncio.Event(), time.time(), r
            pk = x.get("img", x["brand"]) if x["mode"] == "l" and x.get("img", x["brand"]) in LOGO_FILES else None
            pic = (pk, "full" if x.get("plain") else "q1") if pk else None
            d["pk"], d["stage"] = pk, 1
            d["can_hint"] = bool(pk and not x.get("plain") and not x.get("blur") and "q2" in LOGO_FILES[pk] and "q3" in LOGO_FILES[pk])
            kb = duel_kb(d, x, r, 1)
            tag = (" · 📸 фото" if x.get("img") else " · 🖼 логотип") if x["mode"] == "l" else ""
            msgs = await duel_say(bot, d, f"<b>Раунд {r}/{DUEL_ROUNDS}</b> · {FAME[x['level']]}{tag} · ⏱ {DUEL_TIME} сек\n\n❓ {html.escape(x['q'])}", kb, pic)
            d["msgs"] = msgs
            try:
                await asyncio.wait_for(d["ev"].wait(), DUEL_TIME)
            except asyncio.TimeoutError:
                pass
            d["round"] = 0
            for m in d.get("msgs", []):
                try:
                    await bot.edit_message_reply_markup(chat_id=m.chat.id, message_id=m.message_id, reply_markup=None)
                except Exception:
                    pass
            lines = []
            for u in (a, b):
                pts = duel_points(d, u)
                d["score"][u] += pts
                ans = d["ans"].get(u)
                if not ans:
                    st = "⏳ не успел(а)"
                elif ans[0] == x["correct"]:
                    st = f"✅ +{pts} ({ans[1] - d['t0']:.1f} с)"
                else:
                    st = "❌ неверно"
                lines.append(f"{dn(d, u)}: {st}")
            right = f"{'ABCD'[x['correct']]}) {html.escape(x['opts'][x['correct']])}"
            if pic and not x.get("plain"):
                await duel_say(bot, d, "📸 А вот оригинал", None, (pk, "full"))
            await duel_say(bot, d, f"Правильный ответ: <b>{right}</b>\n\n" + "\n".join(lines) +
                           f"\n\n💡 {html.escape(x['ex'])}\n\n<b>Счёт:</b> {dn(d, a)} {d['score'][a]} : {d['score'][b]} {dn(d, b)}")
            await asyncio.sleep(4)
        sa, sb = d["score"][a], d["score"][b]
        today = str(date.today())
        res = []
        for u, mine, other in ((a, sa, sb), (b, sb, sa)):
            xp = DUEL_WIN_XP if mine > other else DUEL_DRAW_XP if mine == other else DUEL_LOSE_XP
            n = DUEL_DAYS.get((u, today), 0)
            if n < DUEL_DAILY_CAP:
                DUEL_DAYS[(u, today)] = n + 1
                q("UPDATE u3 SET xp=xp+? WHERE uid=?", (xp, u), fetch=None)
                log_xp(u, xp)
                res.append(f"{dn(d, u)}: +{xp} XP")
            else:
                res.append(f"{dn(d, u)}: лимит XP за дуэли на сегодня исчерпан")
        title = "🤝 <b>Ничья!</b>" if sa == sb else f"🏆 <b>Победил(а) {dn(d, a if sa > sb else b)}!</b>"
        d["state"] = "done"
        kb = InlineKeyboardBuilder()
        kb.button(text="🏠 Меню", callback_data="m:menu")
        await inline_set(bot, d, f"{title}\n\n{dn(d, a)} — {sa} очков\n{dn(d, b)} — {sb} очков")
        await duel_say(bot, d, f"{title}\n\n{dn(d, a)} — {sa} очков\n{dn(d, b)} — {sb} очков\n\n" + "\n".join(res) + "\n\nЕщё раз: /duel", kb.as_markup())
    except Exception:
        logging.exception("duel failed")
        d["state"] = "dead"
        await inline_set(bot, d, "Дуэль прервалась из-за ошибки.")
        await duel_say(bot, d, "Дуэль прервалась из-за ошибки. Попробуйте ещё раз: /duel")
    finally:
        DUELS.pop(d["id"], None)


@router.message(Command("duel"))
async def duel_cmd(m: Message, bot: Bot):
    ensure_user(m)
    if duel_busy(m.from_user.id):
        await m.answer("Ты уже участвуешь в дуэли — доиграй её.")
        return
    await duel_create(bot, m.from_user, m.chat.id, m.chat.type == "private")


@router.callback_query(F.data.startswith("du:"))
async def duel_cb(c: CallbackQuery, bot: Bot):
    ensure_user(c)
    p = c.data.split(":")
    uid = c.from_user.id
    if p[1] == "new":
        await c.answer()
        if duel_busy(uid):
            await c.message.answer("Ты уже участвуешь в дуэли — доиграй её."); return
        await duel_create(bot, c.from_user, c.message.chat.id, True)
    elif p[1] == "j":
        d = DUELS.get(p[2])
        if d and uid in d["players"]:
            await c.answer("Нужен другой игрок — ты сам создал дуэль.", show_alert=True); return
        r = await duel_join(bot, p[2], c.from_user, c.message.chat.id)
        await c.answer("Дуэль начинается!" if r == "ok" else "")
        if r == "ok":
            await strip_kb(c)
    elif p[1] == "x":
        d = DUELS.get(p[2])
        if not d or d["state"] != "wait" or d["players"][0] != uid:
            await c.answer("Отменить может только создатель до начала.", show_alert=True); return
        d["state"] = "dead"
        DUELS.pop(p[2], None)
        await c.answer("Отменено")
        await strip_kb(c)
        await c.message.answer("Дуэль отменена.")
    elif p[1] == "h":
        d = DUELS.get(p[2])
        r = int(p[3])
        if not d or uid not in d["players"]:
            await c.answer("Ты не участвуешь в этой дуэли.", show_alert=True); return
        if d["round"] != r or not d.get("can_hint"):
            await c.answer("Подсказка недоступна."); return
        if d["stage"] >= 3:
            await c.answer("Подсказок больше нет."); return
        d["stage"] += 1
        st = d["stage"]
        x = QUESTIONS[d["qs"][r - 1]]
        await c.answer("Открыл больше логотипа")
        for m in d.get("msgs", []):
            try:
                await bot.edit_message_reply_markup(chat_id=m.chat.id, message_id=m.message_id, reply_markup=None)
            except Exception:
                pass
        pen = {2: "×0.7", 3: "×0.4"}[st]
        d["msgs"] = await duel_say(bot, d, f"<b>💡 Подсказка {st - 1}/2 · очки {pen}</b> (для верных ответов, данных после подсказки)\n\n❓ {html.escape(x['q'])}", duel_kb(d, x, r, st), (d["pk"], f"q{st}"))
    elif p[1] == "a":
        d = DUELS.get(p[2])
        r, i = int(p[3]), int(p[4])
        if not d or uid not in d["players"]:
            await c.answer("Ты не участвуешь в этой дуэли.", show_alert=True); return
        if d["round"] != r:
            await c.answer("Раунд уже закончился."); return
        if uid in d["ans"]:
            await c.answer("Ты уже ответил(а).", show_alert=False); return
        d["ans"][uid] = (i, time.time(), d.get("stage", 1))
        await c.answer("Ответ принят ✅")
        if len(d["ans"]) == 2:
            d["ev"].set()


@router.inline_query()
async def duel_inline(iq: InlineQuery):
    uid = iq.from_user.id
    known = q("SELECT 1 FROM u3 WHERE uid=?", (uid,), "one")
    kb = InlineKeyboardBuilder()
    if known:
        kb.button(text="⚔️ Принять вызов", callback_data=f"di:{uid}")
        res = InlineQueryResultArticle(
            id=f"duel{uid}", title="⚔️ Вызвать на дуэль", description="15 вопросов о моде, 5 нишевых, с фото",
            input_message_content=InputTextMessageContent(
                message_text=f"⚔️ <b>{html.escape(iq.from_user.full_name)}</b> вызывает тебя на дуэль по моде!\n15 вопросов, 5 из них нишевые, есть фото. Принимаешь?",
                parse_mode="HTML"),
            reply_markup=kb.as_markup())
    else:
        res = InlineQueryResultArticle(
            id="start", title="Сначала запусти бота", description="Нажми Start в @" + BOT_USERNAME["v"] + ", потом вызывай на дуэль",
            input_message_content=InputTextMessageContent(message_text=f"Чтобы играть в дуэли, открой https://t.me/{BOT_USERNAME['v']} и нажми Start."))
    await iq.answer([res], cache_time=1, is_personal=True)


@router.callback_query(F.data.startswith("di:"))
async def duel_inline_accept(c: CallbackQuery, bot: Bot):
    ensure_user(c)
    iid = c.inline_message_id
    creator_id = int(c.data[3:])
    joiner = c.from_user
    if not iid:
        await c.answer(); return
    if joiner.id == creator_id:
        await c.answer("Нужен другой игрок — вызов отправил ты сам.", show_alert=True); return
    if iid in INLINE_USED:
        await c.answer("Этот вызов уже принят."); return
    if duel_busy(joiner.id) or duel_busy(creator_id):
        await c.answer("Один из игроков уже в дуэли — подождите.", show_alert=True); return
    cr = q("SELECT name FROM u3 WHERE uid=?", (creator_id,), "one")
    if not cr:
        await c.answer("Автор вызова не запускал бота.", show_alert=True); return
    try:
        await bot.send_message(joiner.id, "⚔️ Принимаю вызов… сейчас начнём!")
    except Exception:
        await c.answer(url=f"https://t.me/{BOT_USERNAME['v']}?start=duel_hi")
        return
    try:
        await bot.send_message(creator_id, f"⚔️ {html.escape(joiner.full_name)} принял(а) твой вызов!")
    except Exception:
        await c.answer("Автор вызова не открывал личку с ботом.", show_alert=True); return
    INLINE_USED.add(iid)
    did = "".join(random.choices("abcdefghjkmnpqrstuvwxyz23456789", k=6))
    d = {"id": did, "private": True, "players": [creator_id, joiner.id], "names": {creator_id: cr["name"] or "Игрок", joiner.id: joiner.full_name},
         "chats": {creator_id: creator_id, joiner.id: joiner.id}, "state": "play", "score": {creator_id: 0, joiner.id: 0},
         "ans": {}, "round": 0, "qs": [], "ev": None, "t0": 0, "inline": iid}
    DUELS[did] = d
    await c.answer("Дуэль начинается — смотри личку с ботом!")
    asyncio.create_task(run_duel(bot, d))


# ---------- напоминание ----------
async def daily_reminder(bot: Bot):
    last = None
    while True:
        now = datetime.utcnow()
        if now.hour == REMINDER_HOUR and last != now.date():
            last = now.date()
            rows = q("SELECT uid FROM u3 WHERE remind=1 AND (last_day IS NULL OR last_day != ?)", (str(date.today()),))
            kb = InlineKeyboardBuilder(); kb.button(text="👗 Сыграть", callback_data="m:menu")
            for r in rows:
                try: await bot.send_message(r["uid"], "👗 Сегодня ещё не играл(а)! Продли серию дней.", reply_markup=kb.as_markup())
                except Exception: pass
                await asyncio.sleep(0.05)
        await asyncio.sleep(60)


async def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("Задай переменную BOT_TOKEN.")
    init_db()
    bot = Bot(token, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)
    BOT_USERNAME["v"] = (await bot.get_me()).username
    await bot.set_my_commands([BotCommand(command="start", description="Меню"), BotCommand(command="duel", description="Дуэль с другом"), BotCommand(command="myid", description="Мой ID")])
    asyncio.create_task(daily_reminder(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
