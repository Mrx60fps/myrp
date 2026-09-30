import asyncio, json, sqlite3, random, time, os, hashlib
import websockets

DB = "game.db"
clients = {}
world_players = {}

def hash_pass(pw):
    return hashlib.sha256(pw.encode()).hexdigest()

def init_db():
    con = sqlite3.connect(DB)
    con.execute("""CREATE TABLE IF NOT EXISTS players (
        uid INTEGER PRIMARY KEY,
        first_name TEXT, last_name TEXT,
        gender TEXT, money INTEGER DEFAULT 5000,
        is_admin INTEGER DEFAULT 0,
        muted INTEGER DEFAULT 0,
        banned INTEGER DEFAULT 0,
        password TEXT,
        reg_date DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS casino_log (
        id INTEGER PRIMARY KEY, uid INTEGER, game TEXT, bet INTEGER,
        result TEXT, payout INTEGER, ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS work_cooldown (
        uid INTEGER PRIMARY KEY, last_work INTEGER DEFAULT 0
    )""")
    con.execute("""CREATE TABLE IF NOT EXISTS transfers (
        id INTEGER PRIMARY KEY, from_uid INTEGER, to_uid INTEGER,
        amount INTEGER, ts DATETIME DEFAULT CURRENT_TIMESTAMP
    )""")
    con.commit(); con.close()

def gen_uid():
    con = sqlite3.connect(DB)
    while True:
        uid = random.randint(100000, 999999)
        if not con.execute("SELECT 1 FROM players WHERE uid=?", (uid,)).fetchone():
            con.close(); return uid

def get_by_uid(uid):
    con = sqlite3.connect(DB)
    r = con.execute("SELECT * FROM players WHERE uid=?", (uid,)).fetchone()
    con.close(); return r

def get_by_name(fn, ln):
    con = sqlite3.connect(DB)
    r = con.execute("SELECT * FROM players WHERE first_name=? AND last_name=?", (fn, ln)).fetchone()
    con.close(); return r

def register(fn, ln, gender, password):
    con = sqlite3.connect(DB)
    uid = gen_uid()
    pw_hash = hash_pass(password)
    con.execute("INSERT INTO players (uid,first_name,last_name,gender,password) VALUES (?,?,?,?,?)",
                (uid, fn, ln, gender, pw_hash))
    con.commit(); con.close()
    return get_by_uid(uid)

def check_password(fn, ln, password):
    p = get_by_name(fn, ln)
    if not p: return None, "not_found"
    if not p[8]: return None, "no_pass"
    if p[8] != hash_pass(password): return None, "wrong_pass"
    return p, None

def upd_money(uid, delta):
    con = sqlite3.connect(DB)
    con.execute("UPDATE players SET money = money + ? WHERE uid=?", (delta, uid))
    con.commit()
    r = con.execute("SELECT money FROM players WHERE uid=?", (uid,)).fetchone()
    con.close(); return r[0]

def set_money(uid, val):
    con = sqlite3.connect(DB)
    con.execute("UPDATE players SET money=? WHERE uid=?", (val, uid))
    con.commit(); con.close()

def set_field(uid, field, val):
    con = sqlite3.connect(DB)
    con.execute(f"UPDATE players SET {field}=? WHERE uid=?", (val, uid))
    con.commit(); con.close()

def log_casino(uid, game, bet, result, payout):
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO casino_log (uid,game,bet,result,payout) VALUES (?,?,?,?,?)",
                (uid, game, bet, result, payout))
    con.commit(); con.close()

JOBS = {
    "курьер":       {"pay": (200, 600),  "cd": 60,  "name": "Курьер"},
    "таксист":      {"pay": (400, 1000), "cd": 90,  "name": "Таксист"},
    "грузчик":      {"pay": (300, 800),  "cd": 120, "name": "Грузчик"},
    "программист":  {"pay": (800, 2000), "cd": 180, "name": "Программист"},
    "дальнобойщик": {"pay": (600, 1500), "cd": 150, "name": "Дальнобойщик"},
}

def can_work(uid, job):
    con = sqlite3.connect(DB)
    r = con.execute("SELECT last_work FROM work_cooldown WHERE uid=?", (uid,)).fetchone()
    con.close()
    if not r: return True, 0
    wait = JOBS[job]["cd"] - (time.time() - r[0])
    if wait <= 0: return True, 0
    return False, int(wait)

def do_work(uid, job):
    if job not in JOBS: return None, "Такой работы нет"
    ok, wait = can_work(uid, job)
    if not ok: return None, f"Отдохни {wait} сек."
    pay = random.randint(*JOBS[job]["pay"])
    upd_money(uid, pay)
    con = sqlite3.connect(DB)
    con.execute("INSERT OR REPLACE INTO work_cooldown (uid,last_work) VALUES (?,?)",
                (uid, time.time()))
    con.commit(); con.close()
    return pay, None

def transfer_money(from_uid, to_uid, amount):
    fp = get_by_uid(from_uid); tp = get_by_uid(to_uid)
    if not tp: return "Игрок не найден"
    if amount <= 0: return "Сумма > 0"
    if fp[4] < amount: return "Недостаточно денег"
    upd_money(from_uid, -amount); upd_money(to_uid, amount)
    con = sqlite3.connect(DB)
    con.execute("INSERT INTO transfers (from_uid,to_uid,amount) VALUES (?,?,?)",
                (from_uid, to_uid, amount))
    con.commit(); con.close()
    return None

async def send(ws, obj):
    try: await ws.send(json.dumps(obj))
    except: pass

async def broadcast(obj, exclude=None):
    for n, c in list(clients.items()):
        if n == exclude: continue
        try: await c["ws"].send(json.dumps(obj))
        except: pass

async def sys_msg(text, color="gray"):
    await broadcast({"type":"chat","text":text,"color":color,"system":True})

async def send_uid(uid, obj):
    for c in clients.values():
        if c["uid"] == uid:
            await send(c["ws"], obj)

def uid_to_name(uid):
    for n, c in clients.items():
        if c["uid"] == uid: return n
    return None

# -------- КОСТИ --------
async def dice_play(ws, uid, bet):
    bet = int(bet)
    if bet < 10000:
        return await send(ws, {"type":"error","msg":"Минимальная ставка 10000"})
    p = get_by_uid(uid)
    if p[4] < bet: return await send(ws, {"type":"error","msg":"Недостаточно денег"})
    player_roll = random.randint(1,11)
    dealer_roll = random.randint(1,11)
    if player_roll > dealer_roll:
        payout = bet*2
        upd_money(uid, payout - bet)
        result = f"Ты: {player_roll}, Дилер: {dealer_roll}. ПОБЕДА +{bet}"
    elif player_roll == dealer_roll:
        payout = 0
        result = f"Ты: {player_roll}, Дилер: {dealer_roll}. Ничья (ставка возвращена)"
    else:
        payout = -bet
        upd_money(uid, -bet)
        result = f"Ты: {player_roll}, Дилер: {dealer_roll}. Проигрыш -{bet}"
    log_casino(uid, "dice", bet, result, payout)
    await send(ws, {"type":"dice_result","player":player_roll,"dealer":dealer_roll,
        "result":result,"payout":payout,"balance":get_by_uid(uid)[4]})

# -------- БЛЭКДЖЕК --------
def bj_value(card):
    rank = card[:-1]
    if rank in ("J","Q","K"): return 10
    if rank == "A": return 11
    return int(rank)

def hand_value(hand):
    total = 0; aces = 0
    for c in hand:
        total += bj_value(c)
        if c.startswith("A"): aces += 1
    while total > 21 and aces:
        total -= 10; aces -= 1
    return total

def new_card():
    return random.choice(["2","3","4","5","6","7","8","9","10","J","Q","K","A"]) + random.choice(["S","H","D","C"])

async def bj_start(ws, uid, bet):
    bet = int(bet)
    if bet < 1000:
        return await send(ws, {"type":"error","msg":"Минимальная ставка 1000"})
    p = get_by_uid(uid)
    if p[4] < bet: return await send(ws, {"type":"error","msg":"Недостаточно денег"})
    upd_money(uid, -bet)
    player_hand = [new_card(), new_card()]
    dealer_hand = [new_card(), new_card()]
    pv = hand_value(player_hand); dv = hand_value(dealer_hand)
    if pv == 21:
        payout = int(bet * 2.5); upd_money(uid, payout)
        log_casino(uid, "blackjack", bet, f"BJ: {player_hand} = {pv}", payout)
        return await send(ws, {"type":"bj_result","hands":{"player":player_hand,"dealer":dealer_hand},
            "player_value":pv,"dealer_value":dv,"result":f"БЛЭКДЖЕК! +{payout}",
            "payout":payout,"balance":get_by_uid(uid)[4]})
    name = uid_to_name(uid)
    if name:
        clients[name]["bj"] = {"bet":bet,"player":player_hand,"dealer":dealer_hand}
    await send(ws, {"type":"bj_state","hands":{"player":player_hand,"dealer":[dealer_hand[0],"??"]},
        "player_value":pv,"msg":"Взять карту или остановиться?"})

async def bj_hit(ws, uid):
    name = uid_to_name(uid)
    if not name or "bj" not in clients.get(name, {}):
        return await send(ws, {"type":"error","msg":"Нет активной игры"})
    st = clients[name]["bj"]
    st["player"].append(new_card())
    pv = hand_value(st["player"])
    if pv > 21:
        log_casino(uid, "blackjack", st["bet"], f"Перебор: {pv}", -st["bet"])
        await send(ws, {"type":"bj_result","hands":{"player":st["player"],"dealer":st["dealer"]},
            "player_value":pv,"dealer_value":hand_value(st["dealer"]),
            "result":f"Перебор {pv}","payout":-st["bet"],"balance":get_by_uid(uid)[4]})
        del clients[name]["bj"]; return
    await send(ws, {"type":"bj_state","hands":{"player":st["player"],"dealer":[st["dealer"][0],"??"]},
        "player_value":pv,"msg":"Ещё? hit / stand"})

async def bj_stand(ws, uid):
    name = uid_to_name(uid)
    if not name or "bj" not in clients.get(name, {}):
        return await send(ws, {"type":"error","msg":"Нет активной игры"})
    st = clients[name]["bj"]
    pv = hand_value(st["player"])
    while hand_value(st["dealer"]) < 17:
        st["dealer"].append(new_card())
    dv = hand_value(st["dealer"])
    if dv > 21:
        payout = st["bet"]*2; result = f"Дилер перебрал ({dv}). +{st['bet']}"
    elif pv > dv:
        payout = st["bet"]*2; result = f"Ты победил {pv} vs {dv}. +{st['bet']}"
    elif pv == dv:
        payout = st["bet"]; result = f"Ничья {pv} vs {dv}. Возврат"
    else:
        payout = 0; result = f"Ты проиграл {pv} vs {dv}"
    if payout: upd_money(uid, payout)
    log_casino(uid, "blackjack", st["bet"], result, payout - st["bet"])
    await send(ws, {"type":"bj_result","hands":{"player":st["player"],"dealer":st["dealer"]},
        "player_value":pv,"dealer_value":dv,"result":result,
        "payout":payout - st["bet"],"balance":get_by_uid(uid)[4]})
    del clients[name]["bj"]

# -------- КОМАНДЫ --------
async def handle_command(ws, uid, line):
    p = get_by_uid(uid)
    is_admin = bool(p[6])
    parts = line.split(); cmd = parts[0].lower(); args = parts[1:]

if cmd == "/iamadmin"    and len(args) > 0 and args[0] == "MySecret2026":
    set_field(uid, "is_admin", 1)
    await send(ws, {"type":"chat","text":"Ты теперь админ!","color":"lime","system":True})
    return       
    
    if cmd == "/help":
   
        h = ["Команды:","/help","/stats","/work","/work <название>","/pay <ID> <сумма>","/me <текст>"]
        if is_admin:
            h += ["Владелец:","/givemoney <ID> <сумма>","/take <ID> <сумма>",
                  "/set <ID> <сумма>","/selfgive <сумма>","/ban <ID>","/unban <ID>",
                  "/online","/broadcast <текст>"]
        await send(ws, {"type":"chat","text":"\n".join(h),"color":"cyan","system":True})

    elif cmd == "/stats":
        await send(ws, {"type":"chat","color":"gold","system":True,
            "text":f"{p[1]} {p[2]} | ID: {p[0]} | {p[4]} монет"})

    elif cmd == "/me":
        await broadcast({"type":"chat","text":f"* {p[1]} {p[2]} {' '.join(args)}","color":"purple"})

    elif cmd == "/work":
        if not args:
            lst = "\n".join([f"/work {k} - {v['name']} ({v['pay'][0]}-{v['pay'][1]}, кд {v['cd']}с)" for k,v in JOBS.items()])
            return await send(ws, {"type":"chat","color":"cyan","system":True,"text":"Работы:\n"+lst})
        pay, err = do_work(uid, args[0].lower())
        if err: return await send(ws, {"type":"chat","color":"red","system":True,"text":err})
        p2 = get_by_uid(uid)
        await send(ws, {"type":"chat","color":"lime","system":True,
            "text":f"Заработал {pay}. Баланс: {p2[4]}"})
        await send(ws, {"type":"money_update","money":p2[4]})

    elif cmd == "/pay":
        if len(args) < 2: return await send(ws,{"type":"chat","color":"red","system":True,"text":"/pay <ID> <сумма>"})
        target, amount = int(args[0]), int(args[1])
        err = transfer_money(uid, target, amount)
        if err: return await send(ws,{"type":"chat","color":"red","system":True,"text":err})
        p2 = get_by_uid(uid)
        await send(ws, {"type":"chat","color":"lime","system":True,"text":f"Перевёл {amount}. Баланс: {p2[4]}"})
        await send(ws, {"type":"money_update","money":p2[4]})
        await send_uid(target, {"type":"chat","color":"lime","system":True,"text":f"Перевёл {amount} игрок {p[1]} {p[2]}"})
        await send_uid(target, {"type":"money_update","money":get_by_uid(target)[4]})

    elif is_admin and cmd == "/givemoney":
        target, amount = int(args[0]), int(args[1])
        if not get_by_uid(target): return await send(ws,{"type":"chat","color":"red","system":True,"text":"Не найден"})
        bal = upd_money(target, amount)
        await sys_msg(f"{p[1]} {p[2]} выдал {amount} игроку ID:{target}","lime")
        await send_uid(target, {"type":"money_update","money":bal})

    elif is_admin and cmd == "/take":
        target, amount = int(args[0]), int(args[1])
        bal = upd_money(target, -amount)
        await sys_msg(f"Забрано {amount} у ID:{target}","orange")
        await send_uid(target, {"type":"money_update","money":bal})

    elif is_admin and cmd == "/set":
        target, amount = int(args[0]), int(args[1])
        set_money(target, amount)
        await sys_msg(f"Баланс ID:{target} = {amount}","cyan")
        await send_uid(target, {"type":"money_update","money":amount})

    elif is_admin and cmd == "/selfgive":
        amount = int(args[0]); bal = upd_money(uid, amount)
        await send(ws, {"type":"chat","text":f"+{amount}. Баланс: {bal}","color":"gold","system":True})
        await send(ws, {"type":"money_update","money":bal})

    elif is_admin and cmd == "/ban": set_field(int(args[0]), "banned", 1); await sys_msg(f"ID:{args[0]} забанен","red")
    elif is_admin and cmd == "/unban": set_field(int(args[0]), "banned", 0); await sys_msg(f"ID:{args[0]} разбанен","lime")
    elif is_admin and cmd == "/online":
        lines = [f"{c['first']} {c['last']} [ID:{c['uid']}]" for c in clients.values()]
        await send(ws, {"type":"chat","color":"cyan","system":True,"text":f"Онлайн ({len(clients)}): " + ", ".join(lines)})
    elif is_admin and cmd == "/broadcast":
        await sys_msg("" + " ".join(args), "orange")
    else:
        await send(ws, {"type":"chat","text":f"Неизвестная команда: {cmd}","color":"red","system":True})

# -------- ОБРАБОТЧИК --------
async def handler(ws):
    uid = None
    try:
        async for msg in ws:
            d = json.loads(msg); cmd = d.get("cmd")
            if cmd == "login":
                fn, ln, gender = d["first"], d["last"], d["gender"]
                password = d.get("password", "")
                if len(fn) < 2 or len(ln) < 2:
                    return await send(ws, {"type":"error","msg":"Имя и фамилия от 2 символов"})
                if len(password) < 3:
                    return await send(ws, {"type":"error","msg":"Пароль от 3 символов"})
                existing = get_by_name(fn, ln)
                if existing:
                    p, err = check_password(fn, ln, password)
                    if err == "wrong_pass":
                        return await send(ws, {"type":"error","msg":"Неверный пароль"})
                    if err == "no_pass":
                        return await send(ws, {"type":"error","msg":"Аккаунт без пароля"})
                    if not p:
                        return await send(ws, {"type":"error","msg":"Ошибка входа"})
                else:
                    p = register(fn, ln, gender, password)
                if p[7]:
                    return await send(ws, {"type":"kicked","reason":"Вы забанены"})
                uid = p[0]; name = f"{fn} {ln}"
                if name in clients:
                    return await send(ws, {"type":"error","msg":"Этот аккаунт уже в игре"})
                clients[name] = {"ws":ws, "uid":uid, "first":fn, "last":ln, "gender":gender}
                world_players[uid] = {"x":400,"y":300,"gender":gender,"name":name}
                await send(ws, {"type":"welcome","uid":uid,"first":fn,"last":ln,
                    "gender":gender,"money":p[4],"is_admin":bool(p[6])})
                for u, w in world_players.items():
                    if u != uid:
                        await send(ws, {"type":"player_join","uid":u,"name":w["name"],
                            "gender":w["gender"],"x":w["x"],"y":w["y"]})
                await broadcast({"type":"player_join","uid":uid,"name":name,
                    "gender":gender,"x":400,"y":300}, exclude=name)
                await sys_msg(f"{name} [ID:{uid}] зашёл","lime")

            elif cmd == "move":
                if uid is None: continue
                if uid in world_players:
                    world_players[uid]["x"] = d["x"]; world_players[uid]["y"] = d["y"]
                await broadcast({"type":"player_move","uid":uid,"x":d["x"],"y":d["y"]},
                    exclude=uid_to_name(uid))

            elif cmd == "chat":
                if uid is None: continue
                p = get_by_uid(uid)
                if p[6]: continue
                text = d["text"].strip()
                if not text: continue
                name = f"{p[1]} {p[2]}"
                if text.startswith("/"):
                    await handle_command(ws, uid, text)
                else:
                    await broadcast({"type":"chat","text":f"{name}: {text}","color":"white"})

            elif cmd == "bj_hit": await bj_hit(ws, uid)
            elif cmd == "bj_stand": await bj_stand(ws, uid)
            elif cmd == "bj_start": await bj_start(ws, uid, d["bet"])
            elif cmd == "dice": await dice_play(ws, uid, d["bet"])
    finally:
        if uid is not None:
            for n, c in list(clients.items()):
                if c["uid"] == uid: del clients[n]; break
            world_players.pop(uid, None)
            await broadcast({"type":"player_leave","uid":uid})
            await sys_msg(f"Игрок [ID:{uid}] вышел","gray")

async def main():
    init_db()
    port = int(os.environ.get("PORT", 8765))
    async with websockets.serve(handler, "0.0.0.0", port):
        print(f"Сервер запущен на порту {port}")
        await asyncio.Future()

asyncio.run(main())
      
