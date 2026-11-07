"""Fashion Check Bot v4: один режим — Квиз (4 варианта, без фото). Известность × сложность, XP, 12 рангов."""
import asyncio, html, json, logging, os, random, re, sqlite3, time
from datetime import date, datetime, timedelta
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, BufferedInputFile, Message
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
def award(uid: int, x: dict, frac: float, elapsed: float):
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
    kb.button(text="📚 Наборы", callback_data="m:sets")
    kb.button(text="👤 Профиль", callback_data="m:prof")
    kb.button(text="🗂 Коллекции", callback_data="m:coll")
    kb.button(text="🏆 Рейтинг", callback_data="m:top")
    kb.button(text=f"⚙️ Сложность: {DIFF_NAMES[get_diff(uid)]}", callback_data="m:diff")
    kb.adjust(1, 2, 2, 1)
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
try:
    LOGO_FILES = json.loads((BASE / "logos_data.json").read_text(encoding="utf-8"))
except Exception:
    LOGO_FILES = {}


def logo_brands():
    return set(LOGO_FILES)


async def send_logo(msg: Message, brand: str, kind: str, caption: str = "", kb=None):
    """kind: q — фрагмент логотипа, full — полный. file_id кэшируется."""
    key = f"{brand}|{kind}"
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
    base = [x for x in QUESTIONS.values() if (x["mode"] == "g" or x["brand"] in lb) and (not tag or tag in x["cats"])]
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
    sess[uid] = {"qid": x["id"], "t0": time.time(), "mode": "g", "tag": tag}
    head = f"{FAME[x['level']]}" + (" · 🖼 логотип" if x["mode"] == "l" else "")
    if x["archive"]: head += " · 🕰 архив"
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(x["opts"]):
        kb.button(text=f"{'ABCD'[i]}) {o}", callback_data=f"a:{x['id']}:{i}")
    kb.adjust(1)
    text = f"<b>{head}</b>\n\n❓ {html.escape(x['q'])}"
    if x["mode"] == "l" and x["brand"] in LOGO_FILES:
        await send_logo(msg, x["brand"], "q", text, kb.as_markup())
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
async def start(m: Message):
    ensure_user(m)
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
        rows = q("SELECT name,xp FROM u3 ORDER BY xp DESC LIMIT 10")
        s = "<b>🏆 Рейтинг по XP</b>\n" + "\n".join(
            f"{i}. {html.escape(r['name'] or '—')} — {r['xp']} XP · {RANKS[rank_of(r['xp']) - 1][1]}" for i, r in enumerate(rows, 1))
        kb = InlineKeyboardBuilder(); kb.button(text="🏠 Меню", callback_data="m:menu")
        await c.message.answer(s, reply_markup=kb.as_markup())


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
    gain, notes = award(uid, x, 1.0 if ok else 0.0, elapsed)
    await c.answer()
    await strip_kb(c)
    right = f"{'ABCD'[x['correct']]}) {html.escape(x['opts'][x['correct']])}"
    head = "✅ <b>Верно!</b>" if ok else f"❌ <b>Неверно.</b> Правильный ответ: <b>{right}</b>"
    if x["mode"] == "l" and x["brand"] in LOGO_FILES:
        await send_logo(c.message, x["brand"], "full", result_text(x, head, gain, notes), next_kb(uid))
    else:
        await c.message.answer(result_text(x, head, gain, notes), reply_markup=next_kb(uid))


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
    await bot.set_my_commands([BotCommand(command="start", description="Меню"), BotCommand(command="myid", description="Мой ID")])
    asyncio.create_task(daily_reminder(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
