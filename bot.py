"""
Bully Home — сервер и бот для игры вдвоём.

Один процесс делает всё:
  • отдаёт index.html (сама игра) по адресу  https://ВАШ-ДОМЕН/
  • API общего дома                       https://ВАШ-ДОМЕН/api/...
  • Telegram-бот: /start с кнопкой игры, приглашения по коду, уведомления партнёру

Переменные окружения:
  BOT_TOKEN     — токен от @BotFather (обязательно)
  WEBAPP_URL    — публичный https-адрес, где крутится этот сервер, например https://bully.onrender.com/
  APP_SHORT     — (необязательно) короткое имя Mini App из /newapp, тогда приглашения будут вида t.me/бот/APP_SHORT?startapp=КОД
  PORT          — порт (по умолчанию 8080)
  DB_PATH       — путь к базе (по умолчанию bully.db)
  DEV=1         — режим теста без Telegram (заголовок X-Dev-User: "id:Имя"). На проде не включать!
"""
import asyncio, hashlib, hmac, json, os, random, sqlite3, string, time, logging
from pathlib import Path
from urllib.parse import parse_qsl, unquote

from aiohttp import web

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bully")

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
WEBAPP_URL = os.getenv("WEBAPP_URL", "")
APP_SHORT = os.getenv("APP_SHORT", "")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("DB_PATH", "bully.db")
DEV = os.getenv("DEV") == "1"
HERE = Path(__file__).parent

# ---------------- база ----------------
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(uid TEXT PRIMARY KEY, name TEXT, home_id INTEGER, chat_ok INTEGER DEFAULT 1);
CREATE TABLE IF NOT EXISTS homes(id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT UNIQUE, version INTEGER DEFAULT 1, state TEXT, updated REAL);
CREATE TABLE IF NOT EXISTS sent(key TEXT PRIMARY KEY, t REAL);
CREATE TABLE IF NOT EXISTS friends(a TEXT, b TEXT, t REAL, PRIMARY KEY(a,b));
CREATE TABLE IF NOT EXISTS msgs(id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b TEXT, text TEXT, t REAL, seen INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS gifts(id INTEGER PRIMARY KEY AUTOINCREMENT, a TEXT, b TEXT, kind TEXT, item TEXT, qty INTEGER, t REAL, claimed INTEGER DEFAULT 0);
""")
for col in ("fcode TEXT", "photo TEXT"):
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {col}")
    except sqlite3.OperationalError:
        pass
db.commit()


def new_code():
    while True:
        c = "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
        if not db.execute("SELECT 1 FROM homes WHERE code=?", (c,)).fetchone():
            return c


def home_json(hid):
    h = db.execute("SELECT * FROM homes WHERE id=?", (hid,)).fetchone()
    if not h:
        return None
    members = [{"id": r["uid"], "name": r["name"]} for r in db.execute("SELECT uid,name FROM users WHERE home_id=?", (hid,))]
    return {"id": h["id"], "code": h["code"], "version": h["version"], "state": json.loads(h["state"] or "{}"), "members": members}


def create_home(uid, state):
    cur = db.execute("INSERT INTO homes(code,version,state,updated) VALUES(?,?,?,?)", (new_code(), 1, json.dumps(state, ensure_ascii=False), time.time()))
    db.execute("UPDATE users SET home_id=? WHERE uid=?", (cur.lastrowid, uid))
    db.commit()
    return cur.lastrowid


def drop_if_empty(hid):
    if hid and not db.execute("SELECT 1 FROM users WHERE home_id=?", (hid,)).fetchone():
        db.execute("DELETE FROM homes WHERE id=?", (hid,))
        db.commit()


# ---------------- проверка Telegram initData ----------------
def check_init_data(init_data: str):
    if not init_data or not BOT_TOKEN:
        return None
    data = dict(parse_qsl(init_data, keep_blank_values=True))
    got = data.pop("hash", "")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), got):
        return None
    if time.time() - int(data.get("auth_date", 0)) > 7 * 86400:
        return None
    u = json.loads(data.get("user", "{}"))
    return {"id": str(u["id"]), "name": u.get("first_name") or u.get("username") or "Игрок", "photo": u.get("photo_url", "")}


def auth(request):
    u = check_init_data(request.headers.get("X-Init-Data", ""))
    if not u and DEV and request.headers.get("X-Dev-User"):
        i, _, n = unquote(request.headers["X-Dev-User"]).partition(":")
        u = {"id": i, "name": n or "Игрок"}
    if not u:
        raise web.HTTPUnauthorized(text=json.dumps({"error": "auth"}), content_type="application/json")
    row = db.execute("SELECT * FROM users WHERE uid=?", (u["id"],)).fetchone()
    if row:
        if row["name"] != u["name"]:
            db.execute("UPDATE users SET name=? WHERE uid=?", (u["name"], u["id"]))
            db.commit()
    else:
        db.execute("INSERT INTO users(uid,name,home_id) VALUES(?,?,NULL)", (u["id"], u["name"]))
        db.commit()
    row = db.execute("SELECT home_id, fcode FROM users WHERE uid=?", (u["id"],)).fetchone()
    u["home_id"] = row["home_id"]
    if not row["fcode"]:
        while True:
            c = "F" + "".join(random.choices("ABCDEFGHJKLMNPQRSTUVWXYZ23456789", k=6))
            if not db.execute("SELECT 1 FROM users WHERE fcode=?", (c,)).fetchone():
                break
        db.execute("UPDATE users SET fcode=? WHERE uid=?", (c, u["id"]))
        db.commit()
    if u.get("photo"):
        db.execute("UPDATE users SET photo=? WHERE uid=?", (u["photo"], u["id"]))
        db.commit()
    u["fcode"] = db.execute("SELECT fcode FROM users WHERE uid=?", (u["id"],)).fetchone()["fcode"]
    return u


# ---------------- уведомления ----------------
bot = None
BOT_USERNAME = ""


async def notify(uids, text):
    if not bot:
        log.info("notify %s: %s", uids, text)
        return
    for uid in uids:
        try:
            await bot.send_message(int(uid), text, reply_markup=play_kb())
        except Exception as e:  # пользователь не писал боту / заблокировал
            log.info("notify fail %s: %s", uid, e)


def play_kb(url=None):
    from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🏠 Открыть дом", web_app=WebAppInfo(url=url or WEBAPP_URL))]])


def others(hid, uid):
    return [r["uid"] for r in db.execute("SELECT uid FROM users WHERE home_id=? AND uid<>?", (hid, uid))]


# ---------------- API ----------------
routes = web.RouteTableDef()


@routes.get("/")
async def index(request):
    return web.FileResponse(HERE / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/api/me")
async def me(request):
    u = auth(request)
    return web.json_response({"ok": True, "user": {"id": u["id"], "name": u["name"], "fcode": u["fcode"], "photo": u.get("photo") or ""},
                              "home": home_json(u["home_id"]) if u["home_id"] else None,
                              "bot": {"username": BOT_USERNAME, "app": APP_SHORT}})


@routes.post("/api/home/create")
async def create(request):
    u = auth(request)
    body = await request.json()
    if u["home_id"] and home_json(u["home_id"]):
        return web.json_response({"ok": True, "home": home_json(u["home_id"])})
    hid = create_home(u["id"], body.get("state") or {})
    return web.json_response({"ok": True, "home": home_json(hid)})


@routes.get("/api/home")
async def get_home(request):
    u = auth(request)
    if not u["home_id"]:
        return web.json_response({"ok": True, "home": None})
    v = int(request.query.get("v", 0))
    h = db.execute("SELECT version FROM homes WHERE id=?", (u["home_id"],)).fetchone()
    members = db.execute("SELECT COUNT(*) c FROM users WHERE home_id=?", (u["home_id"],)).fetchone()["c"]
    known_members = int(request.query.get("m", 0))
    if h and h["version"] == v and (not known_members or known_members == members):
        return web.json_response({"ok": True, "changed": False})
    return web.json_response({"ok": True, "home": home_json(u["home_id"])})


@routes.post("/api/home/save")
async def save(request):
    u = auth(request)
    body = await request.json()
    hid = u["home_id"]
    if not hid:
        return web.json_response({"error": "no home"}, status=400)
    state = body.get("state")
    raw = json.dumps(state, ensure_ascii=False)
    if len(raw) > 400_000:
        return web.json_response({"error": "too big"}, status=413)
    cur = db.execute("UPDATE homes SET state=?, version=version+1, updated=? WHERE id=? AND version=?",
                     (raw, time.time(), hid, int(body.get("version", 0))))
    db.commit()
    if cur.rowcount == 0:
        return web.json_response({"ok": False, "home": home_json(hid)}, status=409)
    n = body.get("notify")
    if n and isinstance(n, dict) and n.get("text"):
        asyncio.create_task(notify(others(hid, u["id"]), str(n["text"])[:300]))
    ver = db.execute("SELECT version FROM homes WHERE id=?", (hid,)).fetchone()["version"]
    return web.json_response({"ok": True, "version": ver})


@routes.post("/api/join")
async def join(request):
    u = auth(request)
    code = str((await request.json()).get("code", "")).strip().upper()
    h = db.execute("SELECT id FROM homes WHERE code=?", (code,)).fetchone()
    if not h:
        return web.json_response({"error": "Дом с таким кодом не найден"}, status=404)
    if h["id"] == u["home_id"]:
        return web.json_response({"ok": True, "home": home_json(h["id"])})
    cnt = db.execute("SELECT COUNT(*) c FROM users WHERE home_id=?", (h["id"],)).fetchone()["c"]
    if cnt >= 4:
        return web.json_response({"error": "В доме уже 4 человека"}, status=400)
    old = u["home_id"]
    db.execute("UPDATE users SET home_id=? WHERE uid=?", (h["id"], u["id"]))
    db.commit()
    drop_if_empty(old)
    return web.json_response({"ok": True, "home": home_json(h["id"])})


@routes.post("/api/leave")
async def leave(request):
    u = auth(request)
    hid = u["home_id"]
    if not hid:
        return web.json_response({"error": "no home"}, status=400)
    if not others(hid, u["id"]):
        return web.json_response({"ok": True, "home": home_json(hid)})
    state = home_json(hid)["state"]
    state["log"] = []
    state["notes"] = []
    new = create_home(u["id"], state)
    asyncio.create_task(notify(others(hid, u["id"]), f"🏠 {u['name']} съехал(а) из общего дома."))
    return web.json_response({"ok": True, "home": home_json(new)})


# ---------------- друзья, чат, подарки ----------------
def are_friends(a, b):
    return bool(db.execute("SELECT 1 FROM friends WHERE a=? AND b=?", (a, b)).fetchone())


def dogs_of(uid):
    row = db.execute("SELECT home_id FROM users WHERE uid=?", (uid,)).fetchone()
    if not row or not row["home_id"]:
        return []
    h = db.execute("SELECT state FROM homes WHERE id=?", (row["home_id"],)).fetchone()
    st = json.loads(h["state"] or "{}") if h else {}
    out = []
    for d in st.get("dogs", [])[:4]:
        out.append({k: d.get(k) for k in ("name", "coat", "collar", "xp", "born", "sleep")} | {"walk": bool(d.get("walk")), "st": d.get("st"), "t": d.get("t")})
    return out


@routes.get("/api/social")
async def social(request):
    u = auth(request)
    fr = []
    for r in db.execute("SELECT f.b uid, u.name, u.photo FROM friends f JOIN users u ON u.uid=f.b WHERE f.a=? ORDER BY f.t DESC", (u["id"],)):
        unread = db.execute("SELECT COUNT(*) c FROM msgs WHERE a=? AND b=? AND seen=0", (r["uid"], u["id"])).fetchone()["c"]
        last = db.execute("SELECT text, a, t FROM msgs WHERE (a=? AND b=?) OR (a=? AND b=?) ORDER BY id DESC LIMIT 1", (u["id"], r["uid"], r["uid"], u["id"])).fetchone()
        fr.append({"id": r["uid"], "name": r["name"], "photo": r["photo"] or "", "dogs": dogs_of(r["uid"]), "unread": unread,
                   "last": ({"text": last["text"], "mine": last["a"] == u["id"], "t": last["t"]} if last else None)})
    gifts = [{"id": g["id"], "from": g["name"], "kind": g["kind"], "item": g["item"], "qty": g["qty"]} for g in
             db.execute("SELECT g.*, u.name FROM gifts g JOIN users u ON u.uid=g.a WHERE g.b=? AND g.claimed=0 ORDER BY g.id", (u["id"],))]
    return web.json_response({"ok": True, "me": {"id": u["id"], "name": u["name"], "photo": u.get("photo") or "", "fcode": u["fcode"]}, "friends": fr, "gifts": gifts})


@routes.post("/api/friends/add")
async def friend_add(request):
    u = auth(request)
    code = str((await request.json()).get("code", "")).strip().upper()
    if code.startswith("F_"):
        code = code[2:]
    row = db.execute("SELECT uid, name FROM users WHERE fcode=?", (code,)).fetchone()
    if not row:
        return web.json_response({"error": "Игрок с таким кодом не найден"}, status=404)
    if row["uid"] == u["id"]:
        return web.json_response({"error": "Это твой собственный код"}, status=400)
    new = not are_friends(u["id"], row["uid"])
    t = time.time()
    db.execute("INSERT OR IGNORE INTO friends(a,b,t) VALUES(?,?,?)", (u["id"], row["uid"], t))
    db.execute("INSERT OR IGNORE INTO friends(a,b,t) VALUES(?,?,?)", (row["uid"], u["id"], t))
    db.commit()
    if new:
        asyncio.create_task(notify([row["uid"]], f"🐶 {u['name']} добавил(а) тебя в друзья в Bully Home!"))
    return web.json_response({"ok": True, "friend": {"id": row["uid"], "name": row["name"]}, "new": new})


@routes.post("/api/friends/remove")
async def friend_remove(request):
    u = auth(request)
    fid = str((await request.json()).get("id", ""))
    db.execute("DELETE FROM friends WHERE (a=? AND b=?) OR (a=? AND b=?)", (u["id"], fid, fid, u["id"]))
    db.commit()
    return web.json_response({"ok": True})


@routes.get("/api/chat")
async def chat_get(request):
    u = auth(request)
    fid = request.query.get("with", "")
    after = int(request.query.get("after", 0))
    if not are_friends(u["id"], fid):
        return web.json_response({"error": "not friends"}, status=403)
    rows = db.execute("SELECT id, a, text, t FROM msgs WHERE id>? AND ((a=? AND b=?) OR (a=? AND b=?)) ORDER BY id DESC LIMIT 80",
                      (after, u["id"], fid, fid, u["id"])).fetchall()
    db.execute("UPDATE msgs SET seen=1 WHERE a=? AND b=? AND seen=0", (fid, u["id"]))
    db.commit()
    return web.json_response({"ok": True, "msgs": [{"id": r["id"], "mine": r["a"] == u["id"], "text": r["text"], "t": r["t"]} for r in reversed(rows)]})


@routes.post("/api/chat")
async def chat_post(request):
    u = auth(request)
    body = await request.json()
    fid, text = str(body.get("to", "")), str(body.get("text", "")).strip()[:500]
    if not text or not are_friends(u["id"], fid):
        return web.json_response({"error": "bad"}, status=400)
    cur = db.execute("INSERT INTO msgs(a,b,text,t) VALUES(?,?,?,?)", (u["id"], fid, text, time.time()))
    db.commit()
    asyncio.create_task(notify([fid], f"💬 {u['name']}: {text[:200]}"))
    return web.json_response({"ok": True, "id": cur.lastrowid})


@routes.post("/api/gift")
async def gift_send(request):
    u = auth(request)
    b = await request.json()
    fid, kind, item, qty = str(b.get("to", "")), str(b.get("kind", "")), str(b.get("item", ""))[:20], max(1, min(20, int(b.get("qty", 1))))
    label = str(b.get("name") or item)[:40]
    if kind not in ("food", "toy") or not are_friends(u["id"], fid):
        return web.json_response({"error": "bad"}, status=400)
    db.execute("INSERT INTO gifts(a,b,kind,item,qty,t) VALUES(?,?,?,?,?,?)", (u["id"], fid, kind, item, qty, time.time()))
    db.execute("INSERT INTO msgs(a,b,text,t) VALUES(?,?,?,?)", (u["id"], fid, f"🎁 отправил(а) подарок: {label} ×{qty}", time.time()))
    db.commit()
    asyncio.create_task(notify([fid], f"🎁 {u['name']} прислал(а) тебе подарок для собаки! Загляни в «Вдвоём»."))
    return web.json_response({"ok": True})


@routes.post("/api/gift/claim")
async def gift_claim(request):
    u = auth(request)
    gid = int((await request.json()).get("id", 0))
    g = db.execute("SELECT * FROM gifts WHERE id=? AND b=? AND claimed=0", (gid, u["id"])).fetchone()
    if not g:
        return web.json_response({"error": "уже забран"}, status=404)
    db.execute("UPDATE gifts SET claimed=1 WHERE id=?", (gid,))
    db.commit()
    return web.json_response({"ok": True, "gift": {"kind": g["kind"], "item": g["item"], "qty": g["qty"]}})


# ---------------- фоновые напоминания ----------------
RATE = {"food": 7, "fun": 6, "clean": 3.5, "energy": 5}


def cur_stat(d, k):
    h = (time.time() * 1000 - d.get("t", 0)) / 3600000
    r = RATE[k]
    if d.get("walk"):
        r = 2 if k == "energy" else r * 0.4
    elif d.get("sleep"):
        r = -22 if k == "energy" else r * 0.45
    return max(0, min(100, d.get("st", {}).get(k, 50) - r * h))


def once(key, ttl):
    row = db.execute("SELECT t FROM sent WHERE key=?", (key,)).fetchone()
    if row and time.time() - row["t"] < ttl:
        return False
    db.execute("INSERT OR REPLACE INTO sent(key,t) VALUES(?,?)", (key, time.time()))
    db.commit()
    return True


async def reminders():
    while True:
        await asyncio.sleep(300)
        try:
            for h in db.execute("SELECT id,state FROM homes WHERE updated > ?", (time.time() - 14 * 86400,)).fetchall():
                uids = [r["uid"] for r in db.execute("SELECT uid FROM users WHERE home_id=?", (h["id"],))]
                if not uids:
                    continue
                st = json.loads(h["state"] or "{}")
                for d in st.get("dogs", []):
                    w = d.get("walk")
                    if w and w.get("until", 0) / 1000 < time.time() and once(f"walk:{d['id']}:{w['until']}", 10 ** 9):
                        await notify(uids, f"🦮 {d['name']} вернулся с прогулки и что-то принёс! Загляни домой.")
                    elif not w and cur_stat(d, "food") < 20 and once(f"hungry:{d['id']}", 6 * 3600):
                        await notify(uids, f"🍖 {d['name']} проголодался и смотрит на миску…")
                    elif not w and cur_stat(d, "fun") < 15 and once(f"bored:{d['id']}", 8 * 3600):
                        await notify(uids, f"🎾 {d['name']} скучает. Поиграете?")
        except Exception as e:
            log.exception("reminders: %s", e)


# ---------------- бот ----------------
async def run_bot():
    global bot, BOT_USERNAME
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import CommandStart, CommandObject
    from aiogram.types import Message, MenuButtonWebApp, WebAppInfo

    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()
    meinfo = await bot.get_me()
    BOT_USERNAME = meinfo.username
    try:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="🏠 Дом", web_app=WebAppInfo(url=WEBAPP_URL)))
    except Exception as e:
        log.warning("menu button: %s", e)

    @dp.message(CommandStart())
    async def start(m: Message, command: CommandObject):
        code = (command.args or "").strip().upper()
        if code.startswith("F_"):
            url = WEBAPP_URL + ("&" if "?" in WEBAPP_URL else "?") + "friend=" + code[2:]
            await m.answer("🐶 Тебя зовут дружить в Bully Home! Жми кнопку — и вы станете друзьями: сможете переписываться, смотреть собак друг друга и дарить вкусняшки.",
                           reply_markup=play_kb(url))
        elif code:
            url = WEBAPP_URL + ("&" if "?" in WEBAPP_URL else "?") + "join=" + code
            await m.answer(f"💌 Тебя зовут жить в общий дом с бультерьерами!\nКод дома: <b>{code}</b>\n\nЖми кнопку — и переезжай 🐶",
                           parse_mode="HTML", reply_markup=play_kb(url))
        else:
            await m.answer("🐶 <b>Bully Home</b> — уютная игра про бультерьеров на двоих.\n\n"
                           "Заведи пса, корми, играй, отправляй на прогулки за находками, обставляй дом "
                           "и позови свою половинку — будете растить собак вместе 💞",
                           parse_mode="HTML", reply_markup=play_kb())

    @dp.message(F.text)
    async def any_text(m: Message):
        await m.answer("Жми кнопку, чтобы открыть дом 🏠", reply_markup=play_kb())

    await dp.start_polling(bot)


async def main():
    app = web.Application(client_max_size=1024 * 1024)
    app.add_routes(routes)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("HTTP на порту %s (DEV=%s)", PORT, DEV)
    tasks = [asyncio.create_task(reminders())]
    if BOT_TOKEN:
        tasks.append(asyncio.create_task(run_bot()))
    else:
        log.warning("BOT_TOKEN не задан — бот не запущен, работает только сайт/API")
    await asyncio.gather(*tasks)


if __name__ == "__main__":
    asyncio.run(main())
