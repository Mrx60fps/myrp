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
tables = {}

def hash_pass(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS players (uid INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT, gender TEXT, money INTEGER DEFAULT 5000, is_admin INTEGER DEFAULT 0, banned INTEGER DEFAULT 0, password TEXT)")
    con.commit()
    con.close()

def get_player(uid):
    con = sqlite3.connect(DB)
    r = con.execute("SELECT uid, first_name, last_name, gender, money, is_admin, banned, password FROM players WHERE uid=?", (uid,)).fetchone()
    con.close()
    return r

def get_by_name(fn, ln):
    con = sqlite3.connect(DB)
    r = con.execute("SELECT uid, first_name, last_name, gender, money, is_admin, banned, password FROM players WHERE first_name=? AND last_name=?", (fn, ln)).fetchone()
    con.close()
    return r

def register(fn, ln, gender, password):
    con = sqlite3.connect(DB)
    while True:
        uid = random.randint(100000, 999999)
        exists = con.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone()
        if not exists:
            break
    con.execute("INSERT INTO players (uid, first_name, last_name, gender, password, money, is_admin, banned) VALUES (?,?,?,?,?,5000,0,0)", (uid, fn, ln, gender, hash_pass(password)))
    con.commit()
    con.close()
    return get_by_name(fn, ln)

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
    await broadcast({"type":"chat","text":text,"color":color,"system":True})

JOBS = {
    "курьер": (800, 1500, 30),
    "таксист": (1500, 3000, 45),
    "грузчик": (1000, 2200, 40),
    "программист": (3000, 6000, 60),
    "дальнобойщик": (2500, 5000, 50)
}

work_time = {}

def do_work(uid, job):
    if job not in JOBS:
        return None, "Нет такой работы"
    now = time.time()
    last = work_time.get(uid, 0)
    cd = JOBS[job][2]
    if now - last < cd:
        return None, "Отдохни " + str(int(cd - (now - last))) + " сек"
    pay = random.randint(JOBS[job][0], JOBS[job][1])
    upd_money(uid, pay)
    work_time[uid] = now
    return pay, None

def hand_val(hand):
    total = 0
    aces = 0
    for c in hand:
        rank = c[:-1]
        if rank in ("J","Q","K"):
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
    ranks = ["2","3","4","5","6","7","8","9","10","J","Q","K","A"]
    suits = ["S","H","D","C"]
    return random.choice(ranks) + random.choice(suits)

def get_name_by_uid(uid):
    for n in clients:
        if clients[n]["uid"] == uid:
            return n
    return None

# ========== БЛЭКДЖЕК ==========

async def bj_send_state():
    t = tables.get("bj")
    if not t:
        return
    for uid in list(t["players"].keys()):
        name = get_name_by_uid(uid)
        if not name:
            continue
        pdata = t["players"][uid]
        dealer_show = t["dealer"] if pdata["done"] else [t["dealer"][0], "??"]
        players_info = []
        for puid, p2 in t["players"].items():
            pn = get_player(puid)
            players_info.append({
                "name": (pn[1] + " " + pn[2]) if pn else "?",
                "cards": p2["hand"],
                "value": hand_val(p2["hand"]),
                "done": p2["done"],
                "bet": p2["bet"]
            })
        await send(clients[name]["ws"], {
            "type":"bj_table",
            "dealer": dealer_show,
            "dealer_value": hand_val(t["dealer"]) if pdata["done"] else "??",
            "your_hand": pdata["hand"],
            "your_value": hand_val(pdata["hand"]),
            "your_done": pdata["done"],
            "players": players_info,
            "players_count": len(t["players"])
        })

async def bj_join(ws, uid, bet):
    bet = int(bet)
    if bet < 1000:
        await send(ws, {"type":"error","msg":"Минимальная ставка 1000"})
        return
    p = get_player(uid)
    if p[4] < bet:
        await send(ws, {"type":"error","msg":"Мало денег"})
        return
    if "bj" not in tables:
        tables["bj"] = {"dealer": [], "players": {}, "started": False, "host": uid}
    t = tables["bj"]
    if uid in t["players"]:
        await send(ws, {"type":"error","msg":"Ты уже за столом"})
        return
    upd_money(uid, -bet)
    t["players"][uid] = {"bet": bet, "hand": [], "done": False}
    if not t["started"]:
        t["started"] = True
        for puid in t["players"]:
            t["players"][puid]["hand"] = [new_card(), new_card()]
        t["dealer"] = [new_card(), new_card()]
        await sys_msg("Дилер начал игру в блэкджек! Игроков: " + str(len(t["players"])), "gold")
        for puid in list(t["players"].keys()):
            if hand_val(t["players"][puid]["hand"]) == 21:
                t["players"][puid]["done"] = True
    await bj_send_state()

async def bj_hit(ws, uid):
    t = tables.get("bj")
    if not t or uid not in t["players"]:
        await send(ws, {"type":"error","msg":"Ты не за столом"})
        return
    if t["players"][uid]["done"]:
        await send(ws, {"type":"error","msg":"Ты уже остановился"})
        return
    t["players"][uid]["hand"].append(new_card())
    pv = hand_val(t["players"][uid]["hand"])
    if pv >= 21:
        t["players"][uid]["done"] = True
    await bj_send_state()
    if all(pl["done"] for pl in t["players"].values()):
        await bj_finish()

async def bj_stand(ws, uid):
    t = tables.get("bj")
    if not t or uid not in t["players"]:
        await send(ws, {"type":"error","msg":"Ты не за столом"})
        return
    t["players"][uid]["done"] = True
    await bj_send_state()
    if all(pl["done"] for pl in t["players"].values()):
        await bj_finish()

async def bj_finish():
    t = tables.get("bj")
    if not t:
        return
    while hand_val(t["dealer"]) < 17:
        t["dealer"].append(new_card())
    dv = hand_val(t["dealer"])
    for uid, pdata in t["players"].items():
        pv = hand_val(pdata["hand"])
        name = get_name_by_uid(uid)
        if pv > 21:
            res = "Перебор " + str(pv) + " - проигрыш"
            win = 0
        elif dv > 21:
            res = "Дилер перебрал! +" + str(pdata["bet"] * 2)
            win = pdata["bet"] * 2
        elif pv > dv:
            res = "Победа " + str(pv) + " vs " + str(dv) + " +" + str(pdata["bet"] * 2)
            win = pdata["bet"] * 2
        elif pv == dv:
            res = "Ничья " + str(pv) + " vs " + str(dv) + " (возврат)"
            win = pdata["bet"]
        else:
            res = "Проигрыш " + str(pv) + " vs " + str(dv)
            win = 0
        if win:
            upd_money(uid, win)
        bal = get_player(uid)[4]
        if name:
            await send(clients[name]["ws"], {
                "type":"bj_result",
                "dealer": t["dealer"],
                "dealer_value": dv,
                "your_hand": pdata["hand"],
                "your_value": pv,
                "result": res,
                "balance": bal
            })
    tables.pop("bj", None)

# ========== КОСТИ ==========

async def dice_join(ws, uid, bet):
    bet = int(bet)
    if bet < 10000:
        await send(ws, {"type":"error","msg":"Минимальная ставка 10000"})
        return
    p = get_player(uid)
    if p[4] < bet:
        await send(ws, {"type":"error","msg":"Мало денег"})
        return
    if "dice" not in tables:
        tables["dice"] = {"bets": {}, "started": False}
    t = tables["dice"]
    if uid in t["bets"]:
        await send(ws, {"type":"error","msg":"Ты уже сделал ставку"})
        return
    upd_money(uid, -bet)
    t["bets"][uid] = bet
    count = len(t["bets"])
    await sys_msg("Ставка в кости. Игроков: " + str(count) + "/2", "gold")
    await send(ws, {"type":"dice_wait","count": count, "need": 2})
    if count >= 2 and not t["started"]:
        t["started"] = True
        await sys_msg("Дилер запускает кости! Игроков: " + str(count), "gold")
        await asyncio.sleep(1)
        await dice_play()

async def dice_play():
    t = tables.get("dice")
    if not t:
        return
    results = []
    for uid, bet in t["bets"].items():
        roll = random.randint(1, 11)
        results.append({"uid": uid, "roll": roll, "bet": bet})
    results.sort(key=lambda x: x["roll"], reverse=True)
    top_roll = results[0]["roll"]
    winners = [r for r in results if r["roll"] == top_roll]
    all_rolls = []
    for x in results:
        pn = get_player(x["uid"])
        all_rolls.append({"name": (pn[1] + " " + pn[2]) if pn else "?", "roll": x["roll"]})
    for r in results:
        name = get_name_by_uid(r["uid"])
        if r["roll"] == top_roll:
            if len(winners) == 1:
                win = r["bet"] * 2
                upd_money(r["uid"], win)
                res = "Бросок " + str(r["roll"]) + " - ПОБЕДА! +" + str(r["bet"])
            else:
                win = r["bet"]
                upd_money(r["uid"], win)
                res = "Бросок " + str(r["roll"]) + " - Ничья, возврат"
        else:
            res = "Бросок " + str(r["roll"]) + " - Проигрыш -" + str(r["bet"])
        bal = get_player(r["uid"])[4]
        if name:
            await send(clients[name]["ws"], {
                "type":"dice_result",
                "your_roll": r["roll"],
                "all_rolls": all_rolls,
                "result": res,
                "balance": bal
            })
    tables.pop("dice", None)

# ========== КОМАНДЫ ==========

async def do_command(ws, uid, line):
    p = get_player(uid)
    is_admin = bool(p[5])
    parts = line.split()
    cmd = parts[0].lower()
    args = parts[1:]
    if cmd == "/help":
        text = "/help /stats /work /pay /me"
        if is_admin:
            text += " /givemoney /selfgive /ban /unban /online /broadcast"
        await send(ws, {"type":"chat","text":text,"color":"cyan","system":True})
    elif cmd == "/stats":
        await send(ws, {"type":"chat","text":p[1] + " " + p[2] + " | ID:" + str(p[0]) + " | " + str(p[4]) + " монет","color":"gold","system":True})
    elif cmd == "/work":
        if not args:
            await send(ws, {"type":"chat","text":"Работы: курьер, таксист, грузчик, программист, дальнобойщик","color":"cyan","system":True})
            return
        pay, err = do_work(uid, args[0].lower())
        if err:
            await send(ws, {"type":"chat","text":err,"color":"red","system":True})
            return
        bal = get_player(uid)[4]
        await send(ws, {"type":"chat","text":"Заработал " + str(pay) + ". Баланс: " + str(bal),"color":"lime","system":True})
        await send(ws, {"type":"money_update","money":bal})
    elif cmd == "/pay":
        if len(args) < 2:
            await send(ws, {"type":"chat","text":"/pay ID сумма","color":"red","system":True})
            return
        target = int(args[0])
        amount = int(args[1])
        tp = get_player(target)
        if not tp:
            await send(ws, {"type":"chat","text":"Не найден","color":"red","system":True})
            return
        if p[4] < amount:
            await send(ws, {"type":"chat","text":"Мало денег","color":"red","system":True})
            return
        upd_money(uid, -amount)
        upd_money(target, amount)
        await send(ws, {"type":"chat","text":"Перевёл " + str(amount),"color":"lime","system":True})
        await send(ws, {"type":"money_update","money":get_player(uid)[4]})
    elif is_admin and cmd == "/selfgive":
        amount = int(args[0])
        upd_money(uid, amount)
        await send(ws, {"type":"money_update","money":get_player(uid)[4]})
    elif is_admin and cmd == "/givemoney":
        target = int(args[0])
        amount = int(args[1])
        upd_money(target, amount)
        await sys_msg("Выдано " + str(amount) + " игроку " + str(target), "lime")
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
        await send(ws, {"type":"chat","text":"Онлайн: " + ", ".join(names),"color":"cyan","system":True})
    elif is_admin and cmd == "/broadcast":
        await sys_msg(" ".join(args), "orange")
    else:
        await send(ws, {"type":"chat","text":"Команда не найдена","color":"red","system":True})

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
                    await send(ws, {"type":"error","msg":"Имя/фамилия от 2 символов"})
                    continue
                if len(password) < 3:
                    await send(ws, {"type":"error","msg":"Пароль от 3 символов"})
                    continue
                p = get_by_name(fn, ln)
                if p:
                    if p[7] != hash_pass(password):
                        await send(ws, {"type":"error","msg":"Неверный пароль"})
                        continue
                else:
                    p = register(fn, ln, gender, password)
                admin_name = os.environ.get("ADMIN_NAME", "")
                if admin_name and (fn + " " + ln) == admin_name:
                    set_field(p[0], "is_admin", 1)
                    p = get_player(p[0])
                if p[6]:
                    await send(ws, {"type":"kicked","reason":"Забанен"})
                    continue
                uid = p[0]
                name = fn + " " + ln
                clients[name] = {"ws":ws,"uid":uid}
                await send(ws, {"type":"welcome","uid":uid,"first":fn,"last":ln,"gender":gender,"money":p[4],"is_admin":bool(p[5])})
                await sys_msg(name + " зашёл [" + str(uid) + "]", "lime")
            elif cmd == "chat":
                if uid is None:
                    continue
                text = d["text"].strip()
                if not text:
                    continue
                if text.startswith("/"):
                    await do_command(ws, uid, text)
                else:
                    p = get_player(uid)
                    await broadcast({"type":"chat","text":p[1] + " " + p[2] + ": " + text,"color":"white"})
            elif cmd == "move":
                if uid is None:
                    continue
                await broadcast({"type":"player_move","uid":uid,"x":d["x"],"y":d["y"]}, exclude=name)
            elif cmd == "bj_join":
                if uid is None:
                    continue
                await bj_join(ws, uid, d["bet"])
            elif cmd == "bj_hit":
                if uid is None:
                    continue
                await bj_hit(ws, uid)
            elif cmd == "bj_stand":
                if uid is None:
                    continue
                await bj_stand(ws, uid)
            elif cmd == "bj_leave":
                t = tables.get("bj")
                if t and uid in t["players"]:
                    pdata = t["players"][uid]
                    upd_money(uid, pdata["bet"])
                    del t["players"][uid]
                    await send(ws, {"type":"bj_result","dealer":[],"dealer_value":0,"your_hand":pdata["hand"],"your_value":hand_val(pdata["hand"]),"result":"Вышел. Ставка возвращена","balance":get_player(uid)[4]})
                    if not t["players"]:
                        tables.pop("bj", None)
                    else:
                        await bj_send_state()
            elif cmd == "dice_join":
                if uid is None:
                    continue
                await dice_join(ws, uid, d["bet"])
            elif cmd == "dice_leave":
                t = tables.get("dice")
                if t and uid in t["bets"]:
                    upd_money(uid, t["bets"][uid])
                    del t["bets"][uid]
                    await send(ws, {"type":"chat","text":"Ставка отменена","color":"orange","system":True})
    finally:
        for tbl in list(tables.keys()):
            t = tables[tbl]
            if tbl == "bj" and uid in t.get("players", {}):
                upd_money(uid, t["players"][uid]["bet"])
                del t["players"][uid]
                if not t["players"]:
                    tables.pop("bj", None)
            if tbl == "dice" and uid in t.get("bets", {}):
                upd_money(uid, t["bets"][uid])
                del t["bets"][uid]
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
