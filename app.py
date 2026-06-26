
import random, string, json, os, sqlite3, time
from flask import Flask, request, jsonify, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)
rooms = {}

SHIPS = [
    {"id":1,"name":"Radarträger","len":5,"emoji":"📡","bonus":"radar"},
    {"id":2,"name":"Artillerieschiff","len":4,"emoji":"🎯","bonus":"artillery"},
    {"id":3,"name":"Minenkreuzer","len":3,"emoji":"🧨","bonus":"mine"},
    {"id":4,"name":"Sonar-U-Boot","len":3,"emoji":"🔎","bonus":"sonar"},
    {"id":5,"name":"Rache-Zerstörer","len":2,"emoji":"💣","bonus":"revenge"},
]

def rc(): return "".join(random.choice(string.ascii_uppercase+string.digits) for _ in range(5))
def clean(n,f): n=(n or "").strip(); return n[:18] if n else f
def eb(): return [[0 for _ in range(10)] for _ in range(10)]
def ep(): return {"bomb":0,"radar":0,"artillery":0,"sonar":0,"mine":0,"revenge":0}
def mk(x,y): return f"{x},{y}"
def np(name): return {"name":name,"board":eb(),"ready":False,"sunk":set(),"dead":False,"powers":ep(),"streak":0,"mines":set(),"intel":{},"power_used_this_turn":0}

def _plain(obj):
    if isinstance(obj, set):
        return list(obj)
    if isinstance(obj, dict):
        return {k:_plain(v) for k,v in obj.items()}
    if isinstance(obj, list):
        return [_plain(v) for v in obj]
    return obj

DB_PATH = os.environ.get("ROOM_DB_PATH", "schiffe_rooms.sqlite3")
JSON_FALLBACK_PATH = os.environ.get("ROOM_STORE_PATH", "rooms_state.json")

def _rehydrate_room(data):
    for _, room in data.items():
        for _, p in room.get("players", {}).items():
            p["sunk"] = set(p.get("sunk", []))
            p["mines"] = set(p.get("mines", []))
            p["intel"] = {k:set(v) for k,v in p.get("intel", {}).items()}
            p.setdefault("power_used_this_turn", 0)
            p.setdefault("powers", ep())
            p.setdefault("streak", 0)
            p.setdefault("mines", set())
            p.setdefault("intel", {})
    return data

def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rooms (
            code TEXT PRIMARY KEY,
            payload TEXT NOT NULL,
            updated_at REAL NOT NULL
        )
    """)
    conn.commit()
    return conn

def save_room(code):
    try:
        if code not in rooms:
            return
        payload = json.dumps(_plain(rooms[code]), ensure_ascii=False)
        with _db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO rooms(code,payload,updated_at) VALUES(?,?,?)",
                (code, payload, time.time())
            )
            conn.commit()
        # Extra fallback for environments where SQLite file is temporarily locked.
        try:
            with open(JSON_FALLBACK_PATH, "w", encoding="utf-8") as f:
                json.dump(_plain(rooms), f, ensure_ascii=False)
        except Exception:
            pass
    except Exception:
        pass

def save_rooms():
    try:
        with _db() as conn:
            for code, room in rooms.items():
                conn.execute(
                    "INSERT OR REPLACE INTO rooms(code,payload,updated_at) VALUES(?,?,?)",
                    (code, json.dumps(_plain(room), ensure_ascii=False), time.time())
                )
            conn.commit()
        try:
            with open(JSON_FALLBACK_PATH, "w", encoding="utf-8") as f:
                json.dump(_plain(rooms), f, ensure_ascii=False)
        except Exception:
            pass
    except Exception:
        pass

def load_rooms():
    global rooms
    loaded = {}
    try:
        with _db() as conn:
            for code, payload, _updated_at in conn.execute("SELECT code,payload,updated_at FROM rooms"):
                loaded[code] = json.loads(payload)
        if loaded:
            rooms = _rehydrate_room(loaded)
            return
    except Exception:
        pass

    try:
        if os.path.exists(JSON_FALLBACK_PATH):
            with open(JSON_FALLBACK_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            rooms = _rehydrate_room(data)
    except Exception:
        rooms = {}

load_rooms()

def can_place(b,x,y,l,h):
    cells=[]
    for i in range(l):
        xx=x+i if h else x
        yy=y if h else y+i
        if xx<0 or yy<0 or xx>=10 or yy>=10 or b[yy][xx]!=0: return None
        cells.append((xx,yy))
    return cells

def sunk(b,sid): return not any(c==sid for r in b for c in r)
def dead(p): return not any(isinstance(c,int) and c>0 for r in p["board"] for c in r)
def alive(r): return [p for p in r["order"] if p in r["players"] and r["players"][p]["ready"] and not r["players"][p]["dead"]]
def cur(r):
    a=alive(r)
    if not a: return None
    r["turn_index"]%=len(a)
    return a[r["turn_index"]]
def setturn(r,p):
    a=alive(r)
    if p in a: r["turn_index"]=a.index(p)
def adv(r):
    a=alive(r)
    if not a: return
    c=cur(r)
    r["turn_index"]=(a.index(c)+1)%len(a) if c in a else 0
    new=cur(r)
    if new in r["players"]: r["players"][new]["power_used_this_turn"]=0
def grant(r,p,pow): r["players"][p]["powers"][pow]+=1
def intel(r,v,t,x,y): r["players"][v]["intel"].setdefault(t,set()).add(mk(x,y))

def view(r,v,t):
    p=r["players"][t]; vo=r["players"][v]; out=[]
    reveal=r["phase"]=="done" or v==t
    intelset=vo.get("intel",{}).get(t,set())
    for y,row in enumerate(p["board"]):
        rr=[]
        for x,c in enumerate(row):
            k=mk(x,y)
            if c in ["X","M"]: rr.append(c)
            elif v==t and k in p["mines"]: rr.append("N")
            elif isinstance(c,int) and c>0 and (reveal or c in p["sunk"]): rr.append(c)
            elif isinstance(c,int) and c>0 and k in intelset: rr.append("I")
            else: rr.append(0)
        out.append(rr)
    return out

def winner(r):
    a=alive(r)
    if len(a)==1:
        r["phase"]="done"; r["winner"]=a[0]; r["last"]={"type":"win","by":a[0]}
        return True
    return False

def selfhit(r,pid):
    p=r["players"][pid]; fs=[]
    for y in range(10):
        for x in range(10):
            c=p["board"][y][x]
            if isinstance(c,int) and c>0: fs.append((x,y,c))
    if not fs: return None
    x,y,sid=random.choice(fs)
    p["board"][y][x]="X"
    ship=SHIPS[sid-1]
    if sunk(p["board"],sid) and sid not in p["sunk"]:
        p["sunk"].add(sid); grant(r,pid,ship["bonus"])
    if dead(p): p["dead"]=True
    return {"x":x,"y":y,"ship":ship["name"],"emoji":ship["emoji"]}

def attack(r,sh,tg,x,y,streak=False):
    if x<0 or y<0 or x>=10 or y>=10: return {"kind":"out"}
    t=r["players"][tg]; b=t["board"]
    if b[y][x] in ["X","M"]: return {"kind":"old"}
    if mk(x,y) in t["mines"]:
        t["mines"].remove(mk(x,y)); b[y][x]="M"; r["players"][sh]["streak"]=0
        mh=selfhit(r,sh)
        cells=[{"target":tg,"x":x,"y":y}]
        if mh:
            cells.append({"target":sh,"x":mh["x"],"y":mh["y"]})
        r["last"]={"type":"mine_trigger","by":sh,"target":tg,"cells":cells,"mineHit":mh}
        did_win=winner(r)
        if did_win:
            r["last"]["cells"]=cells
            r["last"]["mineHit"]=mh
            r["last"]["type"]="mine_win"
            r["last"]["by"]=r["winner"]
            r["last"]["triggeredBy"]=sh
            r["last"]["target"]=tg
        return {"kind":"mine"}
    c=b[y][x]
    if isinstance(c,int) and c>0:
        sid=c; b[y][x]="X"
        if streak:
            r["players"][sh]["streak"]+=1
            if r["players"][sh]["streak"]>=3:
                grant(r,sh,"bomb"); r["players"][sh]["streak"]=0
                r["last"]={"type":"bomb_grant","by":sh,"cells":[{"target":tg,"x":x,"y":y}]}
        if sunk(b,sid) and sid not in t["sunk"]:
            t["sunk"].add(sid); ship=SHIPS[sid-1]; grant(r,tg,ship["bonus"])
            r["last"]={"type":"sunk","by":sh,"target":tg,"ship":ship["name"],"emoji":ship["emoji"],"bonus":ship["bonus"],"cells":[{"target":tg,"x":x,"y":y}]}
            if dead(t): t["dead"]=True; setturn(r,sh)
            winner(r); return {"kind":"sunk"}
        if not r["last"] or r["last"].get("type")!="bomb_grant":
            r["last"]={"type":"hit","by":sh,"target":tg,"cells":[{"target":tg,"x":x,"y":y}]}
        return {"kind":"hit"}
    b[y][x]="M"; r["players"][sh]["streak"]=0; r["last"]={"type":"miss","by":sh,"target":tg,"cells":[{"target":tg,"x":x,"y":y}]}
    return {"kind":"miss"}

def state(c,v):
    r=rooms[c]; players=[]; boards={}
    for pid in r["order"]:
        if pid in r["players"]:
            p=r["players"][pid]
            players.append({"id":pid,"name":p["name"],"ready":p["ready"],"dead":p["dead"]})
            boards[pid]=view(r,v,pid)
    return {"room":c,"you":v,"phase":r["phase"],"players":players,"boards":boards,
            "turn":cur(r) if r["phase"]=="battle" else None,"winner":r["winner"],"last":r["last"],
            "chat":r["chat"][-80:],"powers":r["players"][v]["powers"],
            "powerUsed":r["players"][v].get("power_used_this_turn",0),"powerLimit":2}


@app.after_request
def persist_after_request(response):
    if request.method == "POST":
        try:
            data = request.get_json(silent=True) or {}
            c = str(data.get("room","")).upper().strip()
            if c:
                save_room(c)
            else:
                save_rooms()
        except Exception:
            save_rooms()
    return response

@app.route("/")
def home(): return Response(HTML,mimetype="text/html")

@app.route("/create",methods=["POST"])
def create():
    c=rc(); name=clean(request.json.get("name"),"Player 1")
    rooms[c]={"players":{"p1":np(name)},"order":["p1"],"phase":"lobby","turn_index":0,
              "winner":None,"last":None,"chat":[{"from":"System","text":f"{name} hat den Raum erstellt."}]}
    return jsonify({"room":c,"player":"p1"})

@app.route("/join",methods=["POST"])
def join():
    c=request.json.get("room","").upper().strip()
    if c not in rooms: return jsonify({"error":"Raum nicht gefunden."}),404
    r=rooms[c]
    if r["phase"]!="lobby": return jsonify({"error":"Spiel läuft schon."}),400
    if len(r["players"])>=6: return jsonify({"error":"Raum ist voll."}),400
    for i in range(1,7):
        pid=f"p{i}"
        if pid not in r["players"]:
            name=clean(request.json.get("name"),f"Player {i}")
            r["players"][pid]=np(name); r["order"].append(pid)
            r["chat"].append({"from":"System","text":f"{name} ist beigetreten."})
            return jsonify({"room":c,"player":pid})
    return jsonify({"error":"Kein Slot frei."}),400

@app.route("/state")
def getstate():
    c=request.args.get("room","").upper().strip(); p=request.args.get("player","")
    if c not in rooms or p not in rooms[c]["players"]: return jsonify({"error":"Raum nicht gefunden. Wahrscheinlich wurde der Server neu gestartet oder ein Deploy lief. Bitte neue Runde erstellen."}),404
    return jsonify(state(c,p))

@app.route("/ready",methods=["POST"])
def ready():
    d=request.json; c=d.get("room","").upper().strip(); p=d.get("player",""); pls=d.get("placements",[])
    if c not in rooms or p not in rooms[c]["players"]: return jsonify({"error":"Raum/Spieler nicht gefunden."}),404
    r=rooms[c]
    if r["phase"]!="lobby": return jsonify({"error":"Platzierung ist vorbei."}),400
    if len(pls)!=5: return jsonify({"error":f"Nur {len(pls)}/5 Schiffe platziert."}),400
    b=eb(); used=set()
    for pl in pls:
        sid=int(pl["id"]); x=int(pl["x"]); y=int(pl["y"]); h=bool(pl["horizontal"])
        if sid in used or sid<1 or sid>5: return jsonify({"error":"Schiffs-Setup ungültig."}),400
        used.add(sid); ship=SHIPS[sid-1]; cells=can_place(b,x,y,ship["len"],h)
        if not cells: return jsonify({"error":f"{ship['name']} liegt ungültig."}),400
        for xx,yy in cells: b[yy][xx]=sid
    r["players"][p]["board"]=b; r["players"][p]["ready"]=True
    r["chat"].append({"from":"System","text":f"{r['players'][p]['name']} ist bereit."})
    return jsonify(state(c,p))

@app.route("/start",methods=["POST"])
def start():
    d=request.json; c=d.get("room","").upper().strip(); p=d.get("player","")
    if c not in rooms: return jsonify({"error":"Raum nicht gefunden."}),404
    r=rooms[c]
    if p!="p1": return jsonify({"error":"Nur Host kann starten."}),403
    if len(r["players"])<2: return jsonify({"error":"Mindestens 2 Spieler nötig."}),400
    nr=[r["players"][pid]["name"] for pid in r["order"] if not r["players"][pid]["ready"]]
    if nr: return jsonify({"error":"Noch nicht bereit: "+", ".join(nr)}),400
    r["phase"]="battle"; r["turn_index"]=0; r["last"]=None; r["chat"]=[]
    cp=cur(r)
    if cp: r["players"][cp]["power_used_this_turn"]=0
    return jsonify(state(c,p))

@app.route("/restart",methods=["POST"])
def restart():
    d=request.json; c=d.get("room","").upper().strip(); p=d.get("player","")
    if c not in rooms: return jsonify({"error":"Raum nicht gefunden."}),404
    if p!="p1": return jsonify({"error":"Nur Host kann neu starten."}),403
    r=rooms[c]
    for pid in r["players"]:
        r["players"][pid]=np(r["players"][pid]["name"])
    r["phase"]="lobby"; r["turn_index"]=0; r["winner"]=None; r["last"]=None
    r["chat"]=[{"from":"System","text":"Neue Runde gestartet."}]
    return jsonify(state(c,p))

@app.route("/shoot",methods=["POST"])
def shoot():
    d=request.json; c=d.get("room","").upper().strip(); sh=d.get("player",""); tg=d.get("target","")
    x=int(d.get("x")); y=int(d.get("y"))
    if c not in rooms: return jsonify({"error":"Raum nicht gefunden."}),404
    r=rooms[c]
    if r["phase"]!="battle": return jsonify({"error":"Spiel läuft noch nicht."}),400
    if sh!=cur(r): return jsonify({"error":"Nicht dein Zug."}),400
    if tg==sh or tg not in r["players"] or r["players"][tg]["dead"]: return jsonify({"error":"Ziel ungültig."}),400
    res=attack(r,sh,tg,x,y,True)
    if res["kind"]=="old": return jsonify({"error":"Da wurde schon geschossen."}),400
    if r["phase"]!="done":
        if res["kind"] in ["miss","mine"]: adv(r)
        else: setturn(r,sh)
    return jsonify(state(c,sh))

@app.route("/power",methods=["POST"])
def power():
    d=request.json; c=d.get("room","").upper().strip(); p=d.get("player",""); kind=d.get("kind",""); target=d.get("target","")
    x=int(d.get("x",0)); y=int(d.get("y",0)); h=bool(d.get("horizontal",True))
    if c not in rooms or p not in rooms[c]["players"]: return jsonify({"error":"Raum/Spieler nicht gefunden."}),404
    r=rooms[c]
    if r["phase"]!="battle": return jsonify({"error":"Power-ups erst im Spiel."}),400
    me=r["players"][p]
    if me.get("power_used_this_turn",0)>=2: return jsonify({"error":"Maximal 2 Power-ups bis zu deinem nächsten Zug."}),400
    if me["powers"].get(kind,0)<=0: return jsonify({"error":"Dieses Power-up hast du nicht."}),400
    msg=""
    if kind in ["bomb","artillery","radar","sonar"]:
        if target==p or target not in r["players"] or r["players"][target]["dead"]: return jsonify({"error":"Ziel ungültig."}),400
    if kind=="bomb":
        me["powers"][kind]-=1; me["power_used_this_turn"]+=1; hits=0
        power_cells=[(x,y),(x+1,y),(x,y+1),(x+1,y+1)]
        for xx,yy in power_cells:
            res=attack(r,p,target,xx,yy,False)
            if res["kind"] in ["hit","sunk"]: hits+=1
        r["last"]={"type":"power_shot","kind":"bomb","by":p,"target":target,"cells":[{"target":target,"x":xx,"y":yy} for xx,yy in power_cells if 0<=xx<10 and 0<=yy<10],"hits":hits}
        msg=f"Bombe: {hits} Treffer. Dein normaler Schuss bleibt erhalten."
    elif kind=="artillery":
        me["powers"][kind]-=1; me["power_used_this_turn"]+=1
        cells=[(x-1,y),(x,y),(x+1,y)] if h else [(x,y-1),(x,y),(x,y+1)]
        hits=0
        for xx,yy in cells:
            res=attack(r,p,target,xx,yy,False)
            if res["kind"] in ["hit","sunk"]: hits+=1
        r["last"]={"type":"power_shot","kind":"artillery","by":p,"target":target,"cells":[{"target":target,"x":xx,"y":yy} for xx,yy in cells if 0<=xx<10 and 0<=yy<10],"hits":hits}
        msg=f"Artillerie: {hits} Treffer. Dein normaler Schuss bleibt erhalten."
    elif kind=="radar":
        me["powers"][kind]-=1; me["power_used_this_turn"]+=1
        b=r["players"][target]["board"]; count=0; found_cells=[]
        for yy in range(max(0,y-1),min(10,y+2)):
            for xx in range(max(0,x-1),min(10,x+2)):
                if isinstance(b[yy][xx],int) and b[yy][xx]>0:
                    count+=1; intel(r,p,target,xx,yy); found_cells.append({"target":target,"x":xx,"y":yy})
        r["last"]={"type":"intel","by":p,"target":target,"cells":found_cells}
        msg=f"Radar: {count} Schiffsfelder markiert."
    elif kind=="sonar":
        me["powers"][kind]-=1; me["power_used_this_turn"]+=1
        b=r["players"][target]["board"]; count=0; found_cells=[]
        if h:
            for xx in range(10):
                if isinstance(b[y][xx],int) and b[y][xx]>0:
                    count+=1; intel(r,p,target,xx,y); found_cells.append({"target":target,"x":xx,"y":y})
            r["last"]={"type":"intel","by":p,"target":target,"cells":found_cells}
            msg=f"Sonar Reihe {y+1}: {count} Schiffsfelder markiert."
        else:
            for yy in range(10):
                if isinstance(b[yy][x],int) and b[yy][x]>0:
                    count+=1; intel(r,p,target,x,yy); found_cells.append({"target":target,"x":x,"y":yy})
            r["last"]={"type":"intel","by":p,"target":target,"cells":found_cells}
            msg=f"Sonar Spalte {x+1}: {count} Schiffsfelder markiert."
    elif kind=="mine":
        if x<0 or y<0 or x>=10 or y>=10: return jsonify({"error":"Feld ungültig."}),400
        if me["board"][y][x]!=0: return jsonify({"error":"Mine nur auf leeres Wasser."}),400
        if mk(x,y) in me["mines"]: return jsonify({"error":"Da liegt schon eine Mine."}),400
        me["powers"][kind]-=1; me["power_used_this_turn"]+=1; me["mines"].add(mk(x,y)); msg="Mine gelegt."
    elif kind=="revenge":
        if target==p or target not in r["players"] or r["players"][target]["dead"]: return jsonify({"error":"Ziel ungültig."}),400
        me["powers"][kind]-=1; me["power_used_this_turn"]+=1
        attack(r,p,target,x,y,False); r["last"]={"type":"power_shot","kind":"revenge","by":p,"target":target,"cells":[{"target":target,"x":x,"y":y}],"hits":0}; msg="Rache-Schuss abgefeuert."
    else:
        return jsonify({"error":"Unbekanntes Power-up."}),400
    s=state(c,p); s["privateMessage"]=msg; return jsonify(s)


@app.route("/backup")
def backup():
    c=request.args.get("room","").upper().strip()
    p=request.args.get("player","")
    if c not in rooms or p not in rooms[c]["players"]:
        return jsonify({"error":"Raum/Spieler nicht gefunden."}),404
    return jsonify({"room":c,"player":p,"data":_plain(rooms[c])})

@app.route("/restore",methods=["POST"])
def restore():
    d=request.json or {}
    c=str(d.get("room","")).upper().strip()
    p=str(d.get("player",""))
    data=d.get("data")
    if not c or not p or not isinstance(data,dict):
        return jsonify({"error":"Backup ungültig."}),400
    try:
        for _, pl in data.get("players",{}).items():
            pl["sunk"]=set(pl.get("sunk",[]))
            pl["mines"]=set(pl.get("mines",[]))
            pl["intel"]={k:set(v) for k,v in pl.get("intel",{}).items()}
            pl.setdefault("power_used_this_turn",0)
            pl.setdefault("powers",ep())
            pl.setdefault("streak",0)
        rooms[c]=data
        save_rooms()
        if p not in rooms[c]["players"]:
            return jsonify({"error":"Backup geladen, aber dein Spieler fehlt."}),400
        return jsonify(state(c,p))
    except Exception:
        return jsonify({"error":"Backup konnte nicht geladen werden."}),400

@app.route("/chat",methods=["POST"])
def chat():
    d=request.json; c=d.get("room","").upper().strip(); p=d.get("player",""); text=d.get("text","").strip()
    if c not in rooms or p not in rooms[c]["players"]: return jsonify({"error":"Raum/Spieler nicht gefunden."}),404
    if text: rooms[c]["chat"].append({"from":rooms[c]["players"][p]["name"],"text":text[:180]})
    return jsonify(state(c,p))

HTML = r"""
<!doctype html><html><head><meta charset="utf-8"><title>Schiffe Versenken Multiplayer</title>
<style>
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at top left,rgba(37,99,235,.24),transparent 32%),radial-gradient(circle at top right,rgba(16,185,129,.16),transparent 30%),#07111f;color:#eef4ff;font-family:Arial,sans-serif}h1{text-align:center;margin:16px 0 8px;font-size:32px}button,input{border:0;border-radius:12px;padding:9px 12px;margin:4px;font-weight:800}button{cursor:pointer;background:#f3f6fb;color:#07111f}button.active{background:#22c55e}button:disabled{opacity:.35}input{background:#eaf0f8}.hidden{display:none!important}.shell{display:grid;grid-template-columns:250px 1fr 260px;gap:14px;width:min(1620px,97vw);margin:0 auto 26px}.panel{padding:12px;background:rgba(16,29,49,.92);border:1px solid #2c4466;border-radius:18px;box-shadow:0 18px 60px rgba(0,0,0,.22)}.left,.side{position:sticky;top:10px;max-height:calc(100vh - 20px);overflow:auto}#lobby{width:min(680px,94vw);margin:20px auto;text-align:center}.status,.dock,.controls,.powerGrid{display:flex;justify-content:center;gap:7px;flex-wrap:wrap}.badge{padding:8px 11px;border-radius:999px;background:#17263d;border:1px solid #334b6c;font-weight:900;font-size:14px}.turnMe{background:#15803d}.turnOther{background:#7f1d1d}.ready{background:#1d4ed8}.dead{background:#374151;text-decoration:line-through;opacity:.65}#notice{min-height:32px;display:flex;justify-content:center;align-items:center;flex-wrap:wrap;gap:6px;margin-top:8px}.help p{font-size:12px;line-height:1.25;margin:7px 0;color:#dbeafe}.powerBtn{font-size:12px;padding:8px 9px}.powerNote{margin-top:9px;font-size:12px;color:#fde68a;line-height:1.3}.boardGrid{display:flex;justify-content:center;gap:16px;flex-wrap:wrap;margin:14px auto 28px}.card{background:rgba(16,29,49,.94);border:1px solid #2c4466;border-radius:18px;padding:11px;box-shadow:0 12px 35px rgba(0,0,0,.20)}.card h2{margin:4px 0 9px;font-size:19px;text-align:center}.card.activeTarget{outline:4px solid #22c55e;box-shadow:0 0 28px rgba(34,197,94,.45)}.card.activeTarget h2{color:#22c55e;text-shadow:0 0 12px rgba(34,197,94,.8)}.card.dangerOwn{outline:4px solid #facc15;box-shadow:0 0 28px rgba(250,204,21,.45)}.card.dangerOwn h2{color:#fde68a;text-shadow:0 0 12px rgba(250,204,21,.75)}.card.currentShooter h2{color:#f87171;text-shadow:0 0 12px rgba(239,68,68,.75)}.turnName{font-weight:1000}.shotFlash{animation:shotFlash .75s ease-out 1}@keyframes shotFlash{0%{transform:scale(1);box-shadow:0 0 0 0 rgba(250,204,21,.95);outline:4px solid #facc15}45%{transform:scale(1.22);box-shadow:0 0 22px 8px rgba(250,204,21,.65);outline:4px solid #fde047}100%{transform:scale(1);box-shadow:none}}.board{display:grid;grid-template-columns:repeat(10,31px);gap:4px;background:#0d1728;border:1px solid #263d5c;padding:10px;border-radius:15px;min-width:382px;min-height:382px}.cell{width:31px;height:31px;border-radius:7px;background:#15507f;display:flex;align-items:center;justify-content:center;user-select:none;cursor:pointer;font-weight:900}.cell:hover{outline:2px solid white}.ship{background:#89939f}.hit{background:#b91c1c!important;color:#fff}.miss{background:#b8d9ef!important;color:#06243c}.reveal{background:#334155;color:#dbeafe}.mine{background:#7c2d12;color:white}.intel{background:#facc15!important;color:#111827}.preview{outline:3px solid #fbbf24!important;filter:brightness(1.35)}.dragShip{display:inline-flex;align-items:center;gap:4px;padding:8px;border-radius:14px;background:#17263d;border:1px solid #314767;cursor:pointer}.dragShip.selected{outline:3px solid #22c55e}.dragShip.placed{opacity:.35;text-decoration:line-through}.dragPart{width:22px;height:22px;background:#d0d7e2;border-radius:5px}#chatLog{height:300px;overflow:auto;text-align:left;background:#0b1526;border:1px solid #263d5c;border-radius:14px;padding:9px;font-size:13px}.chatLine{margin-bottom:6px;line-height:1.25}.chatForm{display:flex;gap:5px;margin-top:8px}#chatInput{flex:1;min-width:0;text-transform:none;margin:0}#overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:50;align-items:center;justify-content:center;pointer-events:none}#overlayBox{background:#f8fafc;color:#07111f;padding:24px 36px;border-radius:24px;font-size:26px;font-weight:1000;max-width:86vw;text-align:center}@media(max-width:1200px){.shell{grid-template-columns:1fr}.left,.side{position:relative;max-height:none}#chatLog{height:190px}.board{grid-template-columns:repeat(10,28px);min-width:352px;min-height:352px}.cell{width:28px;height:28px}}
</style></head><body>
<div id="overlay"><div id="overlayBox"></div></div><h1>🚢 Schiffe Versenken Multiplayer</h1>
<div id="lobby" class="panel"><input id="nameInput" placeholder="Dein Name" style="text-transform:none"><br><button onclick="createRoom()">Raum erstellen</button><input id="roomInput" placeholder="ROOM CODE"><button onclick="joinRoom()">Joinen</button></div>
<div id="game" class="hidden shell">
<div class="left panel"><h3>Power-ups</h3><div class="powerGrid" id="powerBox"></div><div class="powerNote" id="powerCounter">Power-ups genutzt: 0/2</div><hr style="border-color:#2c4466"><div class="help"><p><b>🔥 3 Treffer</b> geben dir eine Bombe.</p><p><b>💥 Bombe:</b> 2x2 auf Gegner.</p><p><b>📡 Radar:</b> 3x3 Scan.</p><p><b>🎯 Artillerie:</b> 3 Felder Linie, R dreht.</p><p><b>🔎 Sonar:</b> Reihe/Spalte, R dreht.</p><p><b>🧨 Mine:</b> aktiviere Mine, dann eigenes Wasserfeld klicken.</p><p><b>💣 Rache:</b> Extraschuss jederzeit.</p><hr style="border-color:#2c4466"><p><b>Neue Regel:</b> Power-ups kannst du jederzeit benutzen, auch wenn jemand anderes dran ist.</p><p><b>Limit:</b> Maximal 2 Power-ups bis zu deinem nächsten normalen Zug.</p><p><b>Wichtig:</b> Bombe/Artillerie verbrauchen deinen normalen Schuss nicht.</p></div></div>
<div class="main"><div class="panel"><div class="status"><div class="badge">Raum: <b id="roomShow"></b></div><div class="badge">Du: <b id="playerShow"></b></div><div class="badge" id="phaseBadge">Lobby</div><div class="badge" id="turnBadge">-</div></div><div id="notice"></div><div id="placement"><p style="text-align:center">Schiff anklicken, dann aufs eigene Brett klicken. <b>R</b> dreht.</p><p style="text-align:center">Ausrichtung: <b id="dir">horizontal ➡️</b></p><div class="dock" id="shipDock"></div><div class="controls"><button onclick="randomPlacement()">Zufällig platzieren</button><button onclick="resetPlacement()">Reset</button><button onclick="readyUp()">Bereit</button><button id="startBtn" onclick="startGame()" class="hidden">Spiel starten</button><button id="restartBtn" onclick="restartGame()" class="hidden">Neue Runde</button></div></div></div><div id="boards" class="boardGrid"></div></div>
<div class="side"><div class="panel"><h3 style="margin:0 0 8px;text-align:center">Chat</h3><div id="chatLog"></div><div class="chatForm"><input id="chatInput" placeholder="Nachricht"><button onclick="sendChat()">OK</button></div></div></div>
</div>
<script>
let room=null,player=null,state=null,horizontal=true,selectedShip=null,placements=[],localBoard=Array.from({length:10},()=>Array(10).fill(0)),locked=false,lastEvent="",lastRender="",activePower=null;
const ships=[{id:1,name:"Radarträger",len:5,emoji:"📡"},{id:2,name:"Artillerieschiff",len:4,emoji:"🎯"},{id:3,name:"Minenkreuzer",len:3,emoji:"🧨"},{id:4,name:"Sonar-U-Boot",len:3,emoji:"🔎"},{id:5,name:"Rache-Zerstörer",len:2,emoji:"💣"}];
const powerLabels={bomb:"💥 Bombe",radar:"📡 Radar",artillery:"🎯 Arty",sonar:"🔎 Sonar",mine:"🧨 Mine",revenge:"💣 Rache"};
function myName(){return document.getElementById("nameInput").value.trim()||"No Name Admiral"}function playerName(pid){return state?.players?.find(p=>p.id===pid)?.name||pid}
async function post(url,data){const r=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(data)});let j=null;try{j=await r.json()}catch(e){showOverlay("Server antwortet nicht sauber. Bitte kurz neu laden.");return null}if(!r.ok){showOverlay(j.error||"Fehler");return null}if(j.privateMessage)showOverlay(j.privateMessage);return j}
async function createRoom(){const j=await post("/create",{name:myName()});if(!j)return;room=j.room;player=j.player;startUi()}
async function joinRoom(){const c=document.getElementById("roomInput").value.trim().toUpperCase();if(!c)return showOverlay("Code eingeben.");const j=await post("/join",{room:c,name:myName()});if(!j)return;room=j.room;player=j.player;startUi()}
function startUi(){document.getElementById("lobby").classList.add("hidden");document.getElementById("game").classList.remove("hidden");document.getElementById("roomShow").textContent=room;document.getElementById("playerShow").textContent=player;renderDock();renderPowers();renderAll();loadState();setInterval(loadState,1200)}
async function loadState(){if(!room||!player)return;const r=await fetch(`/state?room=${room}&player=${player}`);let j=null;try{j=await r.json()}catch(e){return}if(!r.ok){const key="schiffe_backup_"+room+"_"+player;const old=localStorage.getItem(key);if(old&&confirm("Verbindung/Raum verloren. Lokales Backup laden?")){try{const b=JSON.parse(old);const res=await post("/restore",b);if(res){state=res;lastRender="";renderAll();updateHeader();updateChat();renderPowers();showOverlay("Backup geladen.")}}catch(e){}}else{showOverlay(j.error||"Raum/Spieler nicht gefunden.")}return}state=j;locked=false;storeLocalBackup();document.getElementById("playerShow").textContent=playerName(player);updateHeader();updateChat();renderPowers();const ev=JSON.stringify(state.last);if(state.last&&ev!==lastEvent){lastEvent=ev;if(state.last.type==="sunk")showOverlay(`💥 Schiff gesprengt:<br>${state.last.emoji} ${state.last.ship}<br>Bonus: ${powerLabels[state.last.bonus]||state.last.bonus}`);if(state.last.type==="mine_trigger")showOverlay(`🧨 Mine!<br>${playerName(state.last.by)} trifft sich selbst.`);if(state.last.type==="mine_win")showOverlay(`🧨 Mine!<br>${playerName(state.last.triggeredBy)} trifft sich selbst.<br>🏆 ${playerName(state.last.by)} gewinnt.`);if(state.last.type==="win")showOverlay(state.last.by===player?"🏆 Du hast gewonnen.":`🏆 ${playerName(state.last.by)} gewinnt.`);if(state.last.type==="intel"&&state.last.by===player)showOverlay("Markierung gesetzt.");if(state.last.type==="bomb_grant")showOverlay(`💥 ${playerName(state.last.by)} bekommt eine Bombe.`)}const key=JSON.stringify({phase:state.phase,turn:state.turn,boards:state.boards,players:state.players,winner:state.winner,powers:state.powers,powerUsed:state.powerUsed});if(key!==lastRender){lastRender=key;renderAll();setTimeout(()=>animateLastShot(),30)}else{setTimeout(()=>animateLastShot(),30)}}
function updateHeader(){document.getElementById("phaseBadge").textContent=state.phase==="lobby"?"Lobby":state.phase==="battle"?"Battle":"Ende";const t=document.getElementById("turnBadge");if(state.phase==="battle"){t.textContent=state.turn===player?"DU BIST DRAN":`${playerName(state.turn)} ist dran`;t.className="badge "+(state.turn===player?"turnMe":"turnOther")}else{t.textContent="-";t.className="badge"}document.getElementById("notice").innerHTML=state.players.map(p=>`<span class="badge ${p.dead?"dead":p.ready?"ready":""}">${escapeHtml(p.name)}: ${p.ready?"bereit":"nicht bereit"}${p.dead?" / raus":""}</span>`).join("");const allReady=state.players.length>=2&&state.players.every(p=>p.ready);document.getElementById("startBtn").classList.toggle("hidden",!(player==="p1"&&state.phase==="lobby"&&allReady));document.getElementById("restartBtn").classList.toggle("hidden",!(player==="p1"&&state.phase==="done"));document.getElementById("placement").classList.toggle("hidden",state.phase!=="lobby")}
function renderPowers(){const box=document.getElementById("powerBox");if(!box)return;box.innerHTML="";const powers=state?.powers||{bomb:0,radar:0,artillery:0,sonar:0,mine:0,revenge:0};const used=state?.powerUsed||0,limit=state?.powerLimit||2;const counter=document.getElementById("powerCounter");if(counter)counter.textContent=`Power-ups genutzt: ${used}/${limit}`;Object.keys(powerLabels).forEach(k=>{const b=document.createElement("button");b.className="powerBtn"+(activePower===k?" active":"");b.textContent=`${powerLabels[k]} (${powers[k]||0})`;b.disabled=(powers[k]||0)<=0||state?.phase!=="battle"||used>=limit;b.onclick=()=>{activePower=activePower===k?null:k;clearPreview();renderPowers()};box.appendChild(b)})}
function updateChat(){const log=document.getElementById("chatLog");const html=state.chat.map(m=>`<div class="chatLine"><b>${escapeHtml(m.from)}:</b> ${escapeHtml(m.text)}</div>`).join("");if(log.innerHTML!==html){log.innerHTML=html;log.scrollTop=log.scrollHeight}}
function escapeHtml(s){return String(s).replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#039;"}[m]))}
function storeLocalBackup(){if(!room||!player||!state)return;try{fetch(`/backup?room=${room}&player=${player}`).then(r=>r.json()).then(j=>{if(j&&j.data)localStorage.setItem("schiffe_backup_"+room+"_"+player,JSON.stringify(j))}).catch(()=>{})}catch(e){}}
async function backupRoom(){if(!room||!player)return showOverlay("Kein Raum aktiv.");const r=await fetch(`/backup?room=${room}&player=${player}`);const j=await r.json();if(!r.ok)return showOverlay(j.error||"Backup Fehler");const txt=JSON.stringify(j);try{await navigator.clipboard.writeText(txt);showOverlay("Backup kopiert.")}catch(e){prompt("Backup kopieren:",txt)}}
async function restoreRoom(){const txt=prompt("Backup hier einfügen:");if(!txt)return;try{const b=JSON.parse(txt);const res=await post("/restore",b);if(res){room=b.room;player=b.player;state=res;document.getElementById("lobby").classList.add("hidden");document.getElementById("game").classList.remove("hidden");document.getElementById("roomShow").textContent=room;lastRender="";renderAll();updateHeader();updateChat();renderPowers();showOverlay("Backup geladen.")}}catch(e){showOverlay("Backup ungültig.")}}
async function sendChat(){const inp=document.getElementById("chatInput"),text=inp.value.trim();if(!text)return;inp.value="";const res=await post("/chat",{room,player,text});if(res){state=res;updateChat()}}
document.getElementById("chatInput").addEventListener("keydown",e=>{if(e.key==="Enter")sendChat()});function showOverlay(text){const o=document.getElementById("overlay"),b=document.getElementById("overlayBox");b.innerHTML=text;o.style.display="flex";setTimeout(()=>o.style.display="none",1900)}document.addEventListener("keydown",e=>{if(e.key.toLowerCase()==="r")rotate()});function rotate(){horizontal=!horizontal;document.getElementById("dir").textContent=horizontal?"horizontal ➡️":"vertikal ⬇️";clearPreview()}
function renderDock(){const dock=document.getElementById("shipDock");dock.innerHTML="";ships.forEach(s=>{const placed=placements.some(p=>p.id===s.id);const box=document.createElement("div");box.className="dragShip"+(placed?" placed":"")+(selectedShip===s.id?" selected":"");box.onclick=()=>{if(!placed){selectedShip=s.id;renderDock()}};box.innerHTML=`<b>${s.emoji} ${s.name} (${s.len})</b>`;for(let i=0;i<s.len;i++){const part=document.createElement("div");part.className="dragPart";box.appendChild(part)}dock.appendChild(box)})}
function canPlaceLocal(x,y,len,dir=horizontal){const cells=[];for(let i=0;i<len;i++){const xx=dir?x+i:x,yy=dir?y:y+i;if(xx<0||yy<0||xx>=10||yy>=10||localBoard[yy][xx]!==0)return null;cells.push([xx,yy])}return cells}
function placeSelected(x,y){if(!selectedShip)return showOverlay("Erst ein Schiff auswählen.");const ship=ships.find(s=>s.id===selectedShip),cells=canPlaceLocal(x,y,ship.len);if(!cells)return showOverlay("Passt da nicht hin.");cells.forEach(([xx,yy])=>localBoard[yy][xx]=ship.id);placements.push({id:ship.id,x,y,horizontal});selectedShip=null;renderDock();renderAll()}
function resetPlacement(){placements=[];selectedShip=null;localBoard=Array.from({length:10},()=>Array(10).fill(0));renderDock();renderAll()}
function randomPlacement(){resetPlacement();ships.forEach(ship=>{let done=false;for(let tries=0;tries<1000&&!done;tries++){const dir=Math.random()>.5,x=Math.floor(Math.random()*10),y=Math.floor(Math.random()*10),cells=canPlaceLocal(x,y,ship.len,dir);if(cells){cells.forEach(([xx,yy])=>localBoard[yy][xx]=ship.id);placements.push({id:ship.id,x,y,horizontal:dir});done=true}}});selectedShip=null;renderDock();renderAll()}
async function readyUp(){if(placements.length<5)return showOverlay(`Erst alle Schiffe platzieren. ${placements.length}/5`);const res=await post("/ready",{room,player,placements});if(res){state=res;renderAll();updateHeader();updateChat();renderPowers()}}
async function startGame(){const res=await post("/start",{room,player});if(res){state=res;renderAll();updateHeader();updateChat();renderPowers()}}
async function restartGame(){const res=await post("/restart",{room,player});if(res){state=res;placements=[];selectedShip=null;activePower=null;localBoard=Array.from({length:10},()=>Array(10).fill(0));lastEvent="";lastRender="";renderDock();renderAll();updateHeader();updateChat();renderPowers()}}
function clearPreview(){document.querySelectorAll(".cell.preview").forEach(el=>el.classList.remove("preview"))}
function applyPreview(target,x,y,enemy){clearPreview();if(!activePower)return;let cells=[];if(activePower==="bomb")cells=[[x,y],[x+1,y],[x,y+1],[x+1,y+1]];else if(activePower==="artillery")cells=horizontal?[[x-1,y],[x,y],[x+1,y]]:[[x,y-1],[x,y],[x,y+1]];else if(activePower==="radar"){for(let yy=y-1;yy<=y+1;yy++)for(let xx=x-1;xx<=x+1;xx++)cells.push([xx,yy])}else if(activePower==="sonar"){if(horizontal){for(let xx=0;xx<10;xx++)cells.push([xx,y])}else{for(let yy=0;yy<10;yy++)cells.push([x,yy])}}else if(activePower==="mine"&&!enemy)cells=[[x,y]];else if(activePower==="revenge")cells=[[x,y]];cells.forEach(([xx,yy])=>{if(xx<0||yy<0||xx>=10||yy>=10)return;const el=document.querySelector(`[data-target="${target}"][data-x="${xx}"][data-y="${yy}"]`);if(el)el.classList.add("preview")})}
function animateLastShot(){
 if(!state||!state.last||!state.last.cells)return;
 state.last.cells.forEach(c=>{
  const el=document.querySelector(`[data-target="${c.target}"][data-x="${c.x}"][data-y="${c.y}"]`);
  if(el){el.classList.remove("shotFlash");void el.offsetWidth;el.classList.add("shotFlash");setTimeout(()=>el.classList.remove("shotFlash"),800)}
 });
}
function renderAll(){const root=document.getElementById("boards");root.innerHTML="";if(!state||state.phase==="lobby"){addBoard(root,"Dein Brett",localBoard,false,player);return}state.players.forEach(p=>{const own=p.id===player;addBoard(root,own?"Dein Brett":`${p.name}${p.dead?" - raus":""}`,state.boards[p.id],!own,p.id)})}
function addBoard(root,title,board,enemy,target){const card=document.createElement("div");card.className="card";const battle=state?.phase==="battle";const yourTurn=battle&&state?.turn===player;if(enemy&&yourTurn)card.classList.add("activeTarget");if(!enemy&&battle&&!yourTurn)card.classList.add("dangerOwn");if(enemy&&battle&&state?.turn===target&&!yourTurn)card.classList.add("currentShooter");const h=document.createElement("h2");if(enemy&&yourTurn){h.innerHTML=`🟢 <span class="turnName">${escapeHtml(title)}</span> angreifen`;}else if(!enemy&&battle&&!yourTurn){h.innerHTML=`🟡 <span class="turnName">${escapeHtml(title)}</span> - Gegnerzug`;}else if(enemy&&battle&&state?.turn===target){h.innerHTML=`🔴 <span class="turnName">${escapeHtml(title)}</span> ist dran`;}else{h.innerHTML=escapeHtml(title);}card.appendChild(h);const b=document.createElement("div");b.className="board";for(let y=0;y<10;y++)for(let x=0;x<10;x++){const d=document.createElement("div");d.className="cell";d.dataset.target=target;d.dataset.x=x;d.dataset.y=y;const c=board[y][x];if(c>0){d.classList.add(enemy?"reveal":"ship");d.textContent=ships.find(s=>s.id===c)?.emoji||"■"}if(c==="I"){d.classList.add("intel");d.textContent="?"}if(c==="X"){d.classList.add("hit");d.textContent="×"}if(c==="M"){d.classList.add("miss");d.textContent="·"}if(c==="N"){d.classList.add("mine");d.textContent="🧨"}d.onmouseenter=()=>applyPreview(target,x,y,enemy);d.onmouseleave=()=>clearPreview();d.onclick=()=>{if(activePower==="mine"){if(target!==player)return showOverlay("Mine nur aufs eigene Brett.");return usePower("mine",player,x,y)}if(!enemy&&(!state||state.phase==="lobby"))return placeSelected(x,y);if(enemy){if(activePower)return usePower(activePower,target,x,y);return shoot(target,x,y)}};b.appendChild(d)}card.appendChild(b);root.appendChild(card)}
async function usePower(kind,target,x,y){if(!state||state.phase!=="battle")return showOverlay("Spiel läuft noch nicht.");const res=await post("/power",{room,player,kind,target,x,y,horizontal});if(res){state=res;activePower=null;clearPreview();lastRender="";renderPowers();renderAll();updateHeader();updateChat()}}
async function shoot(target,x,y){if(locked)return;if(!state||state.phase!=="battle")return showOverlay("Spiel läuft noch nicht.");if(state.turn!==player)return showOverlay("Nicht dein Zug.");locked=true;const res=await post("/shoot",{room,player,target,x,y});if(res){state=res;clearPreview();lastRender="";renderAll();updateHeader();renderPowers()}locked=false}
</script></body></html>
"""

# v6 frame fix: own board red during enemy turn, only target boards green during own turn

# v7: room persistence + clearer lost-room message

# v8 color fix: current opponent red, own board yellow during enemy turn

# v9: radar/sonar intel fix + backup export/import + local restore

# v10: SQLite autosave per action + JSON fallback + manual/browser backup

# v12 emergency stable single-file deploy
