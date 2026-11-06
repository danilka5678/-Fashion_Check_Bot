"""Fashion Check Bot v3: «Угадай вещь» + «Модный детектив», известность × сложность, XP и 12 рангов."""
import asyncio, html, json, logging, os, random, re, sqlite3, time
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("DB_PATH", BASE / "fashion.db"))
REMINDER_HOUR = int(os.getenv("REMINDER_HOUR", "9"))  # по UTC; 9 = 12:00 по Москве
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
QUESTIONS = {x["id"]: x for x in json.loads((BASE / "questions.json").read_text(encoding="utf-8"))}

FAME = {1: "🟢 Популярный", 2: "🟡 Средний", 3: "🔴 Сложный", 4: "⚫ Нишевый"}
FAME_ICON = {1: "🟢", 2: "🟡", 3: "🔴", 4: "⚫"}
CX = {1: "лёгкий", 2: "средний", 3: "сложный"}
BASE_XP = {1: 10, 2: 25, 3: 50}
FAME_MULT = {1: 1.0, 2: 1.2, 3: 1.5, 4: 2.0}

RANKS = [  # (порог XP, название, описание)
    (0, "Новичок гардероба", "Первые шаги: популярные бренды."),
    (250, "Уличный наблюдатель", "Замечаешь, что носят вокруг."),
    (700, "Охотник за кроссовками", "Открыты сложные популярные вопросы и «Детектив»."),
    (1500, "Знаток денима", "Открыт средний уровень известности целиком."),
    (2600, "Модный детектив", "Открыты сложные бренды."),
    (4000, "Стилист-аналитик", "Открыты самые трудные вопросы про сложные бренды."),
    (5800, "Охотник за архивом", "Открыт нишевый уровень (лёгкие и средние)."),
    (8000, "Куратор винтажа", "Нишевые вопросы любой сложности."),
    (10500, "Архивист моды", "Открыты архивные нишевые вещи."),
    (14000, "Мастер нишевых имён", "Титул мастера: вся база открыта."),
    (19000, "Легенда подиума", "Почётный титул."),
    (26000, "Хранитель архива", "Высший ранг."),
]
UNLOCK_TEXT = {1: "🟢 лёгкие", 2: "🟢 средние", 3: "🟢 сложные, 🕵️ Детектив, 🟡 лёгкие/средние",
               4: "🟡 все", 5: "🔴 лёгкие/средние", 6: "🔴 все", 7: "⚫ лёгкие/средние",
               8: "⚫ все (без архива)", 9: "⚫ архив"}

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
        "CREATE TABLE IF NOT EXISTS photos3(qid INTEGER PRIMARY KEY, file_id TEXT)",
    ]:
        q(s, fetch=None)
    try:
        q("ALTER TABLE u3 ADD COLUMN diff INTEGER DEFAULT 0", fetch=None)
    except sqlite3.OperationalError:
        pass


DIFF_NAMES = {0: "🎲 Авто (по рангу)", 1: "🟢 Лёгкая", 2: "🟡 Средняя", 3: "🔴 Сложная"}


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
    f, c = x["fame"], x["cx"]
    if x["mode"] == "d" and rank < 3:
        return False
    if f == 1: return rank >= (1 if c == 1 else 2 if c == 2 else 3)
    if f == 2: return rank >= (3 if c <= 2 else 4) if rank >= 3 else False
    if f == 3: return rank >= (5 if c <= 2 else 6)
    if f == 4:
        if x["archive"]: return rank >= 9
        return rank >= (7 if c <= 2 else 8)
    return False


# ---------- сопоставление ответов детектива ----------
def norm(s: str) -> str:
    s = s.lower().replace("ё", "е")
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-zа-я ]+", " ", s)).strip()


def alias_hit(alias: str, text: str) -> bool:
    a = norm(alias)
    if not a: return False
    if re.search(r"(?<![0-9a-zа-я])" + re.escape(a) + r"(?![0-9a-zа-я])", text):
        return True
    if len(a) < 5: return False
    toks, n = text.split(), len(a.split())
    for i in range(len(toks) - n + 1):
        if SequenceMatcher(None, a, " ".join(toks[i:i + n])).ratio() >= 0.82:
            return True
    return False


def match_frac(x: dict, text: str) -> float:
    t = norm(text)
    groups = x["groups"]
    return sum(any(alias_hit(a, t) for a in g) for g in groups) / len(groups)


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
        if elapsed <= (10 if x["mode"] == "g" else 25) and ok: bonus += 0.2; notes.append("⚡ скорость +20%")
        if x["archive"]: bonus += 0.2; notes.append("🕰 архив +20%")
        v = BASE_XP[x["cx"]] * FAME_MULT[x["fame"]] * (1.5 if x["mode"] == "d" else 1.0) * (1 + bonus) * frac
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
        "det10": ("🕵️ 10 побед в Детективе", sum(1 for x in rows if x["mode"] == "d") >= 10),
        "arch10": ("🕰 10 архивных вещей", sum(1 for x in rows if x["archive"]) >= 10),
        "brands20": ("🏛 Коллекция из 20 брендов", len({x["brand"] for x in rows}) >= 20),
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
              "det10": "🕵️ 10 побед в Детективе", "arch10": "🕰 10 архивных вещей",
              "brands20": "🏛 Коллекция из 20 брендов", "rank5": "💎 Ранг «Модный детектив»"}


# ---------- клавиатуры ----------
def menu_kb(uid):
    rank = rank_of(q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")["xp"])
    kb = InlineKeyboardBuilder()
    kb.button(text="📸 Угадай одежду (фото)", callback_data="play:p")
    kb.button(text="🧠 Квиз", callback_data="play:g")
    kb.button(text="🕵️ Модный детектив" + ("" if rank >= 3 else " 🔒"), callback_data="play:d")
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
def pick(uid, mode, tag=None):
    u = q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")
    rank = rank_of(u["xp"])
    seen = {r["qid"] for r in q("SELECT qid FROM seen3 WHERE uid=?", (uid,))}
    pool = [x for x in QUESTIONS.values() if x["mode"] == mode and allowed(rank, x) and (not tag or tag in x["cats"])]
    d = get_diff(uid)
    if d:
        byd = [x for x in pool if x["cx"] == d]
        pool = byd or pool
    fresh = [x for x in pool if x["id"] not in seen]
    if fresh:
        # чаще берём вопросы с самых высоких открытых уровней
        top = max(x["fame"] for x in fresh)
        w = [3 if x["fame"] == top else 1 for x in fresh]
        return random.choices(fresh, w)[0]
    return random.choice(pool) if pool else None


async def ask(msg: Message, uid: int, mode: str, tag=None, x=None):
    x = x or pick(uid, mode, tag)
    if not x:
        await msg.answer("В этом наборе пока нет доступных вопросов. Прокачай ранг!")
        return
    sess[uid] = {"qid": x["id"], "t0": time.time(), "mode": mode, "tag": tag}
    head = f"{FAME[x['fame']]} · вопрос {CX[x['cx']]} · {'Квиз' if mode == 'g' else 'Детектив'}"
    if x["archive"]: head += " · 🕰 архив"
    fid = q("SELECT file_id FROM photos3 WHERE qid=?", (x["id"],), "one")
    body = f"<b>{head}</b>\n"
    if not fid and mode == "d": body += f"📸 <i>Фото-улика (описание): {html.escape(x['photo_desc'])}</i>\n"
    body += f"\n❓ {html.escape(x['q'])}"
    if x["archive"] and x["archive_note"]: body += "\n\n<i>Подсказка: вещь из архива.</i>"
    kb = InlineKeyboardBuilder()
    if mode == "g":
        for i, o in enumerate(x["opts"]):
            kb.button(text=f"{'ABCD'[i]}) {o}", callback_data=f"a:{x['id']}:{i}")
        kb.adjust(1)
    else:
        body += "\n\n✍️ Напиши ответ сообщением."
        kb.button(text="🤷 Не знаю", callback_data=f"skip:{x['id']}")
    if fid:
        await msg.answer_photo(fid["file_id"], caption=body[:1024], reply_markup=kb.as_markup())
    else:
        await msg.answer(body, reply_markup=kb.as_markup())


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


@router.message(Command("missing"))
async def missing(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    have = {r["qid"] for r in q("SELECT qid FROM photos3")}
    miss = [i for i in QUESTIONS if i not in have]
    await m.answer(f"Фото есть у {len(have)} из {len(QUESTIONS)}.\nБез фото (id): {', '.join(map(str, miss[:80]))}"
                   "\nОтправь фото с подписью-номером вопроса, чтобы прикрепить. Список задач: файл photo_tasks.csv.")


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
        await c.message.answer("<b>⚙️ Сложность</b>\n🟢 Лёгкая: в фото-игре угадываешь бренд, в квизе лёгкие вопросы.\n"
                               "🟡 Средняя: в фото-игре — название модели.\n🔴 Сложная: модель среди очень похожих вариантов.\n"
                               "🎲 Авто: растёт вместе с рангом.\nДоступность бренда всё равно зависит от ранга.", reply_markup=kb.as_markup())
    elif what == "rem":
        q("UPDATE u3 SET remind=1-remind WHERE uid=?", (uid,), fetch=None)
        await c.message.answer("Настройка обновлена.", reply_markup=menu_kb(uid))
    elif what == "coll":
        rows = correct_rows(uid)
        brands = sorted({x["brand"] for x in rows})
        arch = [x for x in rows if x["archive"]]
        s = (f"<b>🗂 Коллекции</b>\n\n🏛 Бренды: {len(brands)}/20\n"
             f"{html.escape(', '.join(brands[:40])) if brands else 'пока пусто'}\n\n"
             f"🕰 Архивные вещи: {len(arch)}/10\n" + "\n".join("• " + html.escape(x["photo_query"]) for x in arch[:20]))
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
    if mode == "d" and rank < 3:
        await c.message.answer("🔒 «Модный детектив» откроется на ранге 3 — «Охотник за кроссовками».")
        return
    if mode == "p":
        await ask_photo(c.message, uid)
    else:
        await ask(c.message, uid, mode)


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
    if s["mode"] == "p":
        await ask_photo(c.message, uid)
    else:
        await ask(c.message, uid, s["mode"], s.get("tag"))


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
    await c.message.answer(result_text(x, head, gain, notes), reply_markup=next_kb(uid))


@router.callback_query(F.data.startswith("skip:"))
async def skip(c: CallbackQuery):
    uid, qid = c.from_user.id, int(c.data[5:])
    s = sess.get(uid)
    if not s or s["qid"] != qid:
        await c.answer("Этот вопрос уже закрыт."); return
    x = QUESTIONS[qid]
    sess[uid] = {**s, "qid": None}
    gain, notes = award(uid, x, 0.0, 999)
    await c.answer(); await strip_kb(c)
    await c.message.answer(result_text(x, f"🤷 Ответ: <b>{html.escape(x['ans'])}</b>", gain, notes), reply_markup=next_kb(uid))


@router.message(F.photo)
async def admin_photo(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    cap = (m.caption or "").strip()
    if not cap.isdigit() or int(cap) not in QUESTIONS:
        await m.answer("Подпись к фото — номер вопроса (см. photo_tasks.csv)."); return
    q("INSERT OR REPLACE INTO photos3 VALUES(?,?)", (int(cap), m.photo[-1].file_id), fetch=None)
    await m.answer(f"Фото привязано к вопросу {cap}.")


@router.message(F.text & ~F.text.startswith("/"))
async def answer_d(m: Message):
    uid = m.from_user.id
    s = sess.get(uid)
    if not s or not s.get("qid") or s["mode"] != "d":
        return
    x = QUESTIONS[s["qid"]]
    elapsed = time.time() - s["t0"]
    frac = match_frac(x, m.text)
    sess[uid] = {**s, "qid": None}
    if frac >= 0.999: head = "✅ <b>Верно!</b>"
    elif frac >= 0.5: head = f"🟨 <b>Почти.</b> Полный ответ: <b>{html.escape(x['ans'])}</b>"
    else: head = f"❌ <b>Неверно.</b> Ответ: <b>{html.escape(x['ans'])}</b>"
    frac_xp = frac if frac >= 0.5 else 0.0
    gain, notes = award(uid, x, frac_xp, elapsed)
    await m.answer(result_text(x, head, gain, notes), reply_markup=next_kb(uid))


# ---------- «Угадай одежду»: фото + бренд / модель ----------
GENERIC = {"jacket", "suit", "polo", "sneaker", "sneakers", "shoes", "shoe", "jeans", "denim", "bag", "show", "archive",
           "hoodie", "tee", "coat", "boots", "designers", "mini", "leather", "label", "patch", "shirt", "stripe", "logo",
           "store", "window", "vintage", "stripes", "perfume", "bottle", "canvas", "pocket", "tab", "camo", "heart", "embroidery",
           "puffer", "mountain", "skate", "cardigan", "loafer", "mocassin", "moccasin", "fleece", "wool", "stitching", "raw", "drapery", "drape"}
MODEL_OK = {"Adidas Stan Smith", "Puma Suede", "Louis Vuitton Speedy", "Nike Cortez", "Adidas Superstar", "Chanel 2.55",
    "Nike Waffle Trainer", "Nike Air Max 1", "Louis Vuitton Multicolore", "Adidas Samba", "Nike Air Force 1",
    "Converse Chuck Taylor", "New Balance 574", "Vans Old Skool", "The North Face Nuptse", "Dr. Martens 1460",
    "Jacquemus Le Chiquito", "Issey Miyake Pleats Please", "Issey Miyake A-POC", "Raf Simons Ozweego",
    "Comme des Garçons Lumps and Bumps", "Comme des Garçons Body Meets Dress", "Uniqlo ultra light down",
    "Vans Slip-On checkerboard", "Acne Studios face"}
FOTO = []
for _x in QUESTIONS.values():
    _b = _x["brand"]
    _pq = _x["photo_query"]
    if _pq.lower().startswith(_b.lower()) and _x["mode"] in ("g", "d"):
        _model = _pq[len(_b):].strip()
        _ok = _pq in MODEL_OK
        FOTO.append({"id": _x["id"], "brand": _b, "fame": _x["fame"], "cats": _x["cats"], "archive": _x["archive"],
                     "query": _pq, "model": _model if _ok else "", "desc": _x["photo_desc"], "ex": _x["ex"]})
_seen_pq = set()
FOTO = [f for f in FOTO if not (f["query"] in _seen_pq or _seen_pq.add(f["query"]))]


def photo_level(uid, rank):
    d = get_diff(uid)
    if d == 0:
        d = 1 if rank < 4 else 2 if rank < 7 else 3
    return d


def make_photo_task(uid):
    rank = rank_of(q("SELECT xp FROM u3 WHERE uid=?", (uid,), "one")["xp"])
    lvl = photo_level(uid, rank)
    ok_f = lambda f: allowed(rank, {"mode": "g", "fame": f["fame"], "cx": 1, "archive": f["archive"]})
    pool = [f for f in FOTO if ok_f(f) and (lvl == 1 or f["model"])] or [f for f in FOTO if f["fame"] == 1]
    seen = {r["qid"] for r in q("SELECT qid FROM seen3 WHERE uid=?", (uid,))}
    fresh = [f for f in pool if 10000 + f["id"] not in seen] or pool
    f = random.choice(fresh)
    if lvl == 1:
        right = f["brand"]
        others = list({g["brand"] for g in FOTO if g["brand"] != right and g["fame"] == f["fame"]})
        if len(others) < 3: others = list({g["brand"] for g in FOTO if g["brand"] != right})
        text = "Какой это бренд?"
    else:
        right = f["query"]
        same = [g["query"] for g in FOTO if g["brand"] == f["brand"] and g["query"] != right and g["model"]]
        cat = [g["query"] for g in FOTO if g["query"] != right and g["model"] and set(g["cats"]) & set(f["cats"])]
        rest = [g["query"] for g in FOTO if g["query"] != right and g["model"]]
        if lvl == 3: cand = list(dict.fromkeys(same + cat + rest))
        else: cand = list(dict.fromkeys([g for g in cat if g not in same] + rest))
        others = cand[:6] if lvl == 3 else cand
        text = "Какая это модель?" if lvl == 2 else "Какая это модель? Варианты очень похожи."
    if lvl == 3:
        ds = others[:3]
    else:
        ds = random.sample(others, 3)
    opts = ds + [right]
    random.shuffle(opts)
    return f, lvl, text, opts, opts.index(right)


async def fetch_commons(query):
    import aiohttp
    params = {"action": "query", "format": "json", "generator": "search", "gsrnamespace": "6", "gsrsearch": query,
              "gsrlimit": "8", "prop": "imageinfo", "iiprop": "url|mime", "iiurlwidth": "800"}
    hdr = {"User-Agent": "FashionCheckBot/3.0 (telegram quiz bot)"}
    try:
        async with aiohttp.ClientSession(headers=hdr, timeout=aiohttp.ClientTimeout(total=10)) as s:
            async with s.get("https://commons.wikimedia.org/w/api.php", params=params) as r:
                data = await r.json()
        pages = sorted(data.get("query", {}).get("pages", {}).values(), key=lambda p: p.get("index", 99))
        for p in pages:
            ii = (p.get("imageinfo") or [{}])[0]
            if ii.get("mime") in ("image/jpeg", "image/png") and ii.get("thumburl"):
                return ii["thumburl"]
    except Exception:
        logging.exception("commons fetch failed")
    return None


async def ask_photo(msg: Message, uid: int):
    f, lvl, text, opts, ci = make_photo_task(uid)
    sess[uid] = {"qid": None, "pid": f["id"], "t0": time.time(), "mode": "p", "tag": None,
                 "opts": opts, "ci": ci, "lvl": lvl, "f": f}
    kb = InlineKeyboardBuilder()
    for i, o in enumerate(opts):
        kb.button(text=f"{'ABCD'[i]}) {o}", callback_data=f"pa:{f['id']}:{i}")
    kb.adjust(1)
    head = f"<b>{FAME[f['fame']]} · {'бренд' if lvl == 1 else 'модель'} · 📸 Угадай одежду</b>"
    if f["archive"]: head += " · 🕰 архив"
    cap = f"{head}\n\n❓ {text}"
    cached = q("SELECT file_id FROM photos3 WHERE qid=?", (f["id"],), "one")
    try:
        if cached:
            await msg.answer_photo(cached["file_id"], caption=cap, reply_markup=kb.as_markup())
            return
        url = await fetch_commons(f["query"])
        if url:
            sent = await msg.answer_photo(url, caption=cap, reply_markup=kb.as_markup())
            q("INSERT OR IGNORE INTO photos3 VALUES(?,?)", (f["id"], sent.photo[-1].file_id), fetch=None)
            return
    except Exception:
        logging.exception("photo send failed")
    await msg.answer(cap + f"\n\n📸 <i>Фото не загрузилось. Описание: {html.escape(f['desc'])}</i>", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("pa:"))
async def answer_p(c: CallbackQuery):
    uid = c.from_user.id
    _, pid, i = c.data.split(":")
    s = sess.get(uid)
    if not s or s.get("mode") != "p" or s.get("pid") != int(pid):
        await c.answer("Этот вопрос уже закрыт."); return
    f, lvl = s["f"], s["lvl"]
    sess[uid] = {**s, "pid": None}
    ok = int(i) == s["ci"]
    synth = {"id": 10000 + f["id"], "mode": "g", "cx": 1 if lvl == 1 else 2 if lvl == 2 else 3, "fame": f["fame"],
             "archive": f["archive"]}
    gain, notes = award(uid, synth, 1.0 if ok else 0.0, time.time() - s["t0"])
    await c.answer(); await strip_kb(c)
    right = f"{'ABCD'[s['ci']]}) {html.escape(s['opts'][s['ci']])}"
    head = "✅ <b>Верно!</b>" if ok else f"❌ <b>Неверно.</b> Правильный ответ: <b>{right}</b>"
    body = head + f"\n\n💡 {html.escape(f['ex'])}"
    if gain: body += f"\n\n<b>+{gain} XP</b>"
    if notes: body += "\n" + "\n".join(notes)
    await c.message.answer(body, reply_markup=next_kb(uid))


@router.message(Command("resetphoto"))
async def resetphoto(m: Message):
    if m.from_user.id not in ADMIN_IDS: return
    arg = (m.text or "").split()[1:]
    if arg and arg[0].isdigit():
        q("DELETE FROM photos3 WHERE qid=?", (int(arg[0]),), fetch=None)
        await m.answer("Фото сброшено, при следующем показе бот найдёт другое. Или пришли своё фото с подписью-номером.")


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
