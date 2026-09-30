import asyncio
import json
import sqlite3
import random
import time
import os
import hashlib
import websockets

DB = "game.db"
clients = {}

def hash_pass(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS players (uid INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, gender TEXT, money INTEGER DEFAULT 5000, is_admin INTEGER DEFAULT 0, banned INTEGER DEFAULT 0, password TEXT)")
    con.commit()
    con.close()

def get_by_uid(uid):
    con = sqlite3.connect(DB)
    r = con.execute("SELECT * FROM players WHERE uid=?", (uid,)).fetchone()
    con.close()
    return r

def get_by_name(fn, ln):
    con = sqlite3.connect(DB)
    r = con.execute("SELECT * FROM players WHERE first_name=? AND last_name=?", (fn, ln)).fetchone()
    con.close()
    return r

def register(fn, ln, gender, password):
    con = sqlite3.connect(DB)
    while True:
        uid = random.randint(100000, 999999)
        exists = con.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone()
        if not exists:
            break
    con.execute("INSERT INTO players (uid,first_name,last_name,gender,password) VALUES (?,?,?,?,?)", (uid, fn, ln, gender, hash_pass(password)))
    con.commit()
    con.close()
    return get_by_uid(uid)

def upd_money(uid, delta):
    con = sqlite3.connect(DB)
    con.execute("UPDATE players SET money = money + ? WHERE uid=?", (delta, uid))
    con.commit()
    r = con.execute("SELECT money FROM players WHERE uid=?", (uid,)).fetchone()
    con.close()
    return r[0]

def set_field(uid, field, val):
    con = sqlite3.connect(DB)
    con.execute("UPDATE players SET " + field + " = ? WHERE uid=?", (val, uid))
    con.commit()
    con.close()

async def send(ws, obj):
    try:
        await ws.send(json.dumps(obj))
    except:
        pass

async def broadcast(obj, exclude=None):
    for n in list(clients.keys()):
        if n == exclude:
            continue
        try:
            await clients[n]["ws"].send(json.dumps(obj))
        except:
            pass

async def sys_msg(text, color):
    await broadcast({"type": "chat", "text": text, "color": color, "system": True})

JOBS = {
    "курьер": (800, 1500, 30),
    "таксист": (1500, 3000, 45),
    "грузчик": (1000, 2200, 40),
    "программист": (3000, 6000, 60),
    "дальнобойщик": (2500, 5000, 50)
}

work_cd = {}

def do_work(uid, job):
    if job not in JOBS:
        return None, "Нет такой работы"
    now = time.time()
    last = work_cd.get(uid, 0)
    cd = JOBS[job][2]
    if now - last < cd:
        return None, "Отдохни " + str(int(cd - (now - last))) + " сек"
    pay = random.randint(JOBS[job][0], JOBS[job][1])
    upd_money(uid, pay)
    work_cd[uid] = now
    return pay, None

def hand_value(hand):
    total = 0
    aces = 0
    for c in hand:
        rank = c[:-1]
        if rank in ("J", "Q", "K"):
            total += 10
        elif rank == "A":
            total += 11
            aces += 1
        else:
            total += int(rank)
    while total > 21 and aces > 0:
        total -= 10
        aces -= 1
    return total

def new_card():
    ranks = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]
    suits = ["S", "H", "D", "C"]
    return random.choice(ranks) + random.choice(suits)

async def handle_dice(ws, uid, bet):
    bet = int(bet)
    if bet < 10000:
        await send(ws, {"type": "error", "msg": "Минимальная ставка 10000"})
        return
    p = get_by_uid(uid)
    if p[4] < bet:
        await send(ws, {"type": "error", "msg": "Мало денег"})
        return
    my = random.randint(1, 11)
    dl = random.randint(1, 11)
    if my > dl:
        upd_money(uid, bet)
        result = "Ты: " + str(my) + " | Дилер: " + str(dl) + " - ПОБЕДА +" + str(bet)
    elif my == dl:
        result = "Ты: " + str(my) + " | Дилер: " + str(dl) + " - Ничья"
    else:
        upd_money(uid, -bet)
        result = "Ты: " + str(my) + " | Дилер: " + str(dl) + " - Проигрыш -" + str(bet)
    bal = get_by_uid(uid)[4]
    await send(ws, {"type": "dice_result", "player": my, "dealer": dl, "result": result, "balance": bal})

async def handle_bj_start(ws, uid, bet):
    bet = int(bet)
    if bet < 1000:
        await send(ws, {"type": "error", "msg": "Минимальная ставка 1000"})
        return
    p = get_by_uid(uid)
    if p[4] < bet:
        await send(ws, {"type": "error", "msg": "Мало денег"})
        return
    upd_money(uid, -bet)
    ph = [new_card(), new_card()]
    dh = [new_card(), new_card()]
    pv = hand_value(ph)
    if pv == 21:
        win = int(bet * 2.5)
        upd_money(uid, win)
        await send(ws, {"type": "bj_result", "hands": {"player": ph, "dealer": dh}, "player_value": pv, "dealer_value": hand_value(dh), "result": "БЛЭКДЖЕК +" + str(win), "balance": get_by_uid(uid)[4]})
        return
    name = None
    for n in clients:
        if clients[n]["uid"] == uid:
            name = n
            break
    if name:
        clients[name]["bj"] = {"bet": bet, "player": ph, "dealer": dh}
    await send(ws, {"type": "bj_state", "hands": {"player": ph, "dealer": [dh[0], "??"]}, "player_value": pv, "msg": "Взять карту или хватит?"})

async def handle_bj_hit(ws, uid):
    name = None
    for n in clients:
        if clients[n]["uid"] == uid:
            name = n
            break
    if not name or "bj" not in clients[name]:
        await send(ws, {"type": "error", "msg": "Нет игры"})
        return
    st = clients[name]["bj"]
    st["player"].append(new_card())
    pv = hand_value(st["player"])
    if pv > 21:
        await send(ws, {"type": "bj_result", "hands": {"player": st["player"], "dealer": st["dealer"]}, "player_value": pv, "dealer_value": hand_value(st["dealer"]), "result": "Перебор " + str(pv), "balance": get_by_uid(uid)[4]})
        del clients[name]["bj"]
        return
    await send(ws, {"type": "bj_state", "hands": {"player": st["player"], "dealer": [st["dealer"][0], "??"]}, "player_value": pv, "msg": "Ещё?"})

async def handle_bj_stand(ws, uid):
    name = None
    for n in clients:
        if clients[n]["uid"] == uid:
            name = n
            break
    if not name or "bj" not in clients[name]:
        await send(ws, {"type": "error", "msg": "Нет игры"})
        return
    st = clients[name]["bj"]
    pv = hand_value(st["player"])
    while hand_value(st["dealer"]) < 17:
        st["dealer"].append(new_card())
    dv = hand_value(st["dealer"])
    if dv > 21:
        upd_money(uid, st["bet"] * 2)
        result = "Дилер перебрал! +" + str(st["bet"])
    elif pv > dv:
        upd_money(uid, st["bet"] * 2)
        result = "Победа " + str(pv) + " vs " + str(dv) + " +" + str(st["bet"])
    elif pv == dv:
        upd_money(uid, st["bet"])
        result = "Ничья " + str(pv) + " vs " + str(dv)
    else:
        result = "Проигрыш " + str(pv) + " vs " + str(dv)
    await send(ws, {"type": "bj_result", "hands": {"player": st["player"], "dealer": st["dealer"]}, "player_value": pv, "dealer_value": dv, "result": result, "balance": get_by_uid(uid)[4]})
    del clients[name]["bj"]

async def handle_command(ws, uid, line):
    p = get_by_uid(uid)
    is_admin = bool(p[6])
    parts = line.split()
    cmd = parts[0].lower()
    args = parts[1:]

    if cmd == "/help":
        text = "/help /stats /work /pay /me"
        if is_admin:
            text += " /givemoney /take /set /selfgive /ban /unban /online /broadcast"
        await send(ws, {"type": "chat", "text": text, "color": "cyan", "system": True})

    elif cmd == "/stats":
        await send(ws, {"type": "chat", "text": p[1] + " " + p[2] + " | ID: " + str(p[0]) + " | " + str(p[4]), "color": "gold", "system": True})

    elif cmd == "/work":
        if not args:
            await send(ws, {"type": "chat", "text": "Работы: курьер, таксист, грузчик, программист, дальнобойщик", "color": "cyan", "system": True})
            return
        pay, err = do_work(uid, args[0].lower())
        if err:
            await send(ws, {"type": "chat", "text": err, "color": "red", "system": True})
            return
        bal = get_by_uid(uid)[4]
        await send(ws, {"type": "chat", "text": "Заработал " + str(pay) + ". Баланс: " + str(bal), "color": "lime", "system": True})
        await send(ws, {"type": "money_update", "money": bal})

    elif cmd == "/pay":
        if len(args) < 2:
            await send(ws, {"type": "chat", "text": "Формат: /pay ID сумма", "color": "red", "system": True})
            return
        target = int(args[0])
        amount = int(args[1])
        tp = get_by_uid(target)
        if not tp:
            await send(ws, {"type": "chat", "text": "Игрок не найден", "color": "red", "system": True})
            return
        if p[4] < amount:
            await send(ws, {"type": "chat", "text": "Мало денег", "color": "red", "system": True})
            return
        upd_money(uid, -amount)
        upd_money(target, amount)
        await send(ws, {"type": "chat", "text": "Перевёл " + str(amount), "color": "lime", "system": True})
        await send(ws, {"type": "money_update", "money": get_by_uid(uid)[4]})
        for n in clients:
            if clients[n]["uid"] == target:
                await send(clients[n]["ws"], {"type": "chat", "text": "Получил " + str(amount) + " от " + p[1], "color": "lime", "system": True})
                await send(clients[n]["ws"], {"type": "money_update", "money": get_by_uid(target)[4]})

    elif is_admin and cmd == "/givemoney":
        target = int(args[0])
        amount = int(args[1])
        upd_money(target, amount)
        await sys_msg("Выдано " + str(amount) + " игроку " + str(target), "lime")

    elif is_admin and cmd == "/take":
        target = int(args[0])
        amount = int(args[1])
        upd_money(target, -amount)
        await sys_msg("Забрано " + str(amount) + " у " + str(target), "orange")

    elif is_admin and cmd == "/set":
        target = int(args[0])
        amount = int(args[1])
        con = sqlite3.connect(DB)
        con.execute("UPDATE players SET money=? WHERE uid=?", (amount, target))
        con.commit()
        con.close()
        await sys_msg("Баланс " + str(target) + " = " + str(amount), "cyan")

    elif is_admin and cmd == "/selfgive":
        amount = int(args[0])
        upd_money(uid, amount)
        await send(ws, {"type": "money_update", "money": get_by_uid(uid)[4]})
        await send(ws, {"type": "chat", "text": "+" + str(amount), "color": "gold", "system": True})

    elif is_admin and cmd == "/ban":
        set_field(int(args[0]), "banned", 1)
        await sys_msg("Забанен " + args[0], "red")

    elif is_admin and cmd == "/unban":
        set_field(int(args[0]), "banned", 0)
        await sys_msg("Разбанен " + args[0], "lime")

    elif is_admin and cmd == "/online":
        names = []
        for n in clients:
            names.append(n + " ID:" + str(clients[n]["uid"]))
        await send(ws, {"type": "chat", "text": "Онлайн: " + ", ".join(names), "color": "cyan", "system": True})

    elif is_admin and cmd == "/broadcast":
        await sys_msg(" ".join(args), "orange")

    else:
        await send(ws, {"type": "chat", "text": "Команда не найдена", "color": "red", "system": True})

async def handler(ws):
    uid = None
    name = None
    try:
        async for msg in ws:
            d = json.loads(msg)
            cmd = d.get("cmd")

            if cmd == "login":
                fn = d["first"]
                ln = d["last"]
                gender = d["gender"]
                password = d["password"]
                if len(fn) < 2 or len(ln) < 2:
                    await send(ws, {"type": "error", "msg": "Имя и фамилия от 2 символов"})
                    return
                if len(password) < 3:
                    await send(ws, {"type": "error", "msg": "Пароль от 3 символов"})
                    return
                p = get_by_name(fn, ln)
                if p:
                    if p[8] != hash_pass(password):
                        await send(ws, {"type": "error", "msg": "Неверный пароль"})
                        return
                else:
                    p = register(fn, ln, gender, password)
                admin_name = os.environ.get("ADMIN_NAME", "")
                if admin_name and (fn + " " + ln) == admin_name:
                    set_field(p[0], "is_admin", 1)
                    p = get_by_uid(p[0])
                if p[7]:
                    await send(ws, {"type": "kicked", "reason": "Забанен"})
                    return
                uid = p[0]
                name = fn + " " + ln
                clients[name] = {"ws": ws, "uid": uid}
                await send(ws, {"type": "welcome", "uid": uid, "first": fn, "last": ln, "gender": gender, "money": p[4], "is_admin": bool(p[6])})
                await sys_msg(name + " зашёл [ID:" + str(uid) + "]", "lime")

            elif cmd == "chat":
                if uid is None:
                    continue
                text = d["text"].strip()
                if not text:
                    continue
                if text.startswith("/"):
                    await handle_command(ws, uid, text)
                else:
                    p = get_by_uid(uid)
                    await broadcast({"type": "chat", "text": p[1] + " " + p[2] + ": " + text, "color": "white"})

            elif cmd == "dice":
                if uid is None:
                    continue
                await handle_dice(ws, uid, d["bet"])

            elif cmd == "bj_start":
                if uid is None:
                    continue
                await handle_bj_start(ws, uid, d["bet"])

            elif cmd == "bj_hit":
                if uid is None:
                    continue
                await handle_bj_hit(ws, uid)

            elif cmd == "bj_stand":
                if uid is None:
                    continue
                await handle_bj_stand(ws, uid)

    finally:
        if name and name in clients:
            del clients[name]
        if uid:
            await sys_msg("Игрок вышел", "gray")

async def main():
    init_db()
    port = int(os.environ.get("PORT", 8765))
    async with websockets.serve(handler, "0.0.0.0", port):
        print("Server started on port " + str(port))
        await asyncio.Future()

asyncio.run(main())
