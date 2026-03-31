import os
import json
import random
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DATA_FILE = "agent_state.json"
REPORTS_FILE = "reports_history.json"

BASE_PRICES = {
    "NVDA": 875, "MSFT": 380, "META": 515, "GOOGL": 168,
    "AMZN": 192, "PLTR": 24, "ARKK": 48, "QQQ": 447,
    "BTC": 85000, "ETH": 1900
}
VOLATILITY = {
    "NVDA": 0.022, "MSFT": 0.014, "META": 0.020, "GOOGL": 0.015,
    "AMZN": 0.017, "PLTR": 0.035, "ARKK": 0.025, "QQQ": 0.013,
    "BTC": 0.045, "ETH": 0.050
}
SECTORS = {
    "NVDA": "Chips IA", "MSFT": "Cloud/IA", "META": "IA Consumo",
    "GOOGL": "IA/Search", "AMZN": "Cloud", "PLTR": "Data IA",
    "ARKK": "ETF Tech", "QQQ": "ETF NASDAQ", "BTC": "Crypto", "ETH": "Crypto"
}
THRESHOLDS = {
    "conservative": {"buy": 0.8, "sell": -0.6},
    "balanced": {"buy": 0.4, "sell": -0.3},
    "aggressive": {"buy": 0.15, "sell": -0.1}
}

def default_state():
    return {
        "cash": 1000.0, "start_cap": 1000.0,
        "positions": {}, "history": [], "decisions": [], "log": [],
        "scores": {s: {"score": 50, "trades": 0, "wins": 0, "last": "hold"} for s in BASE_PRICES},
        "prices": {s: {"price": p, "move": 0, "trend": 0} for s, p in BASE_PRICES.items()},
        "patterns": [], "memory": [], "wins": 0, "losses": 0, "cycle": 0,
        "running": False, "last_cycle_time": 0,
        "config": {"freq": 60, "sl": 4, "tp": 6, "sz": 20, "risk": "balanced"}
    }

def load_state():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE) as f:
                return json.load(f)
        except:
            pass
    return default_state()

def save_state(s):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(s, f)
    except:
        pass

def load_reports():
    if os.path.exists(REPORTS_FILE):
        try:
            with open(REPORTS_FILE) as f:
                return json.load(f)
        except:
            pass
    return []

def save_reports(reports):
    try:
        with open(REPORTS_FILE, "w") as f:
            json.dump(reports, f)
    except:
        pass

def ts():
    return datetime.now().strftime("%H:%M:%S")

def ts_full():
    return datetime.now().strftime("%d/%m/%Y %H:%M:%S")

def log(s, msg, t="think"):
    s["log"].insert(0, {"t": ts(), "msg": msg, "type": t})
    if len(s["log"]) > 300:
        s["log"] = s["log"][:300]

def simulate_prices(s):
    for sym in BASE_PRICES:
        vol = VOLATILITY[sym]
        shock = (random.random() - 0.5) * 2
        trend = s["prices"][sym]["trend"] * 0.85 + shock * 0.15
        move = (trend + (random.random() - 0.5) * 2) * vol
        prev = s["prices"][sym]["price"]
        np_ = prev * (1 + move)
        if sym == "BTC": np_ = round(np_)
        elif sym == "ETH": np_ = round(np_, 1)
        else: np_ = round(np_, 2)
        s["prices"][sym] = {"price": np_, "move": round(move, 5), "trend": round(trend, 5)}

def gp(s, sym):
    return s["prices"][sym]["price"]

def update_brain(s, sym, won, ret):
    sc = s["scores"][sym]
    sc["trades"] += 1
    if won: sc["wins"] += 1; s["wins"] += 1
    else: s["losses"] += 1
    sc["score"] = min(97, max(3, sc["score"] + ((7 + random.random()*3) if won else -(9 + random.random()*4))))
    s["memory"].insert(0, {"sym": sym, "won": won, "ret": round(ret*100,2), "sector": SECTORS[sym], "t": ts()})
    if len(s["memory"]) > 80: s["memory"] = s["memory"][:80]
    sm = [m for m in s["memory"] if m["sector"] == SECTORS[sym]]
    if len(sm) >= 3:
        wr = round(len([m for m in sm if m["won"]]) / len(sm) * 100)
        idx = next((i for i,p in enumerate(s["patterns"]) if p["sector"]==SECTORS[sym]), -1)
        obj = {"sector": SECTORS[sym], "wr": wr, "ops": len(sm)}
        if idx >= 0: s["patterns"][idx] = obj
        else: s["patterns"].append(obj)

def check_sl_tp(s):
    sl = s["config"]["sl"] / 100
    tp = s["config"]["tp"] / 100
    for sym in list(s["positions"].keys()):
        pos = s["positions"].get(sym)
        if not pos or pos.get("qty", 0) <= 0: continue
        cur = gp(s, sym)
        ret = (cur - pos["avg_cost"]) / pos["avg_cost"]
        reason = "stop-loss" if ret <= -sl else "take-profit" if ret >= tp else None
        if not reason: continue
        proceeds = round(pos["qty"] * cur, 2)
        pnl = round(proceeds - pos["qty"] * pos["avg_cost"], 2)
        s["cash"] = round(s["cash"] + proceeds, 2)
        update_brain(s, sym, pnl > 0, ret)
        s["history"].insert(0, {"t": ts(), "sym": sym, "type": reason, "qty": pos["qty"], "price": cur, "pnl": pnl})
        s["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "SL" if reason=="stop-loss" else "TP",
                                   "price": cur, "detail": f"ret {round(ret*100,2)}%", "won": pnl > 0})
        log(s, f"{reason.upper()} {sym} · ret {round(ret*100,2)}% · P&L ${pnl}", "buy" if pnl>0 else "sell")
        pos["qty"] = 0

def run_cycle(s):
    import time
    now = time.time()
    freq = s["config"].get("freq", 60)
    last = s.get("last_cycle_time", 0)
    if now - last < freq:
        return
    s["last_cycle_time"] = now
    s["cycle"] += 1
    simulate_prices(s)
    log(s, f"Ciclo #{s['cycle']}", "think")
    check_sl_tp(s)
    T = THRESHOLDS[s["config"]["risk"]]
    total = s["cash"] + sum(pos["qty"]*gp(s,sym) for sym,pos in s["positions"].items() if pos.get("qty",0)>0)
    budget = round(total * s["config"]["sz"] / 100, 2)
    bought = sold = held = 0
    for sym in BASE_PRICES:
        move = s["prices"][sym]["move"]
        vol = VOLATILITY[sym]
        sc = s["scores"][sym]["score"]
        risk = s["config"]["risk"]
        mult = 2.5 if risk=="aggressive" else 0.7 if risk=="conservative" else 1.4
        sig = round((move/vol)*mult + ((sc-50)/50)*0.4 + (random.random()-0.5)*0.2, 3)
        pos = s["positions"].get(sym)
        has_pos = pos and pos.get("qty", 0) > 0
        price = gp(s, sym)
        s["scores"][sym]["last"] = "compra" if sig>=T["buy"] else "venta" if sig<=T["sell"] else "hold"
        if sig >= T["buy"] and not has_pos and s["cash"] > price * 0.001:
            spend = min(budget, s["cash"] * 0.9)
            qty = round(spend/price, 6 if price>10000 else 5 if price>1000 else 3 if price>100 else 2)
            cost = round(qty * price, 2)
            if qty > 0 and cost <= s["cash"] + 0.01:
                s["cash"] = round(s["cash"] - cost, 2)
                s["positions"][sym] = {"qty": qty, "avg_cost": price}
                s["history"].insert(0, {"t": ts(), "sym": sym, "type": "Compra", "qty": qty, "price": price, "pnl": None})
                s["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "COMPRA", "price": price, "detail": f"señal {sig}"})
                log(s, f"COMPRA {qty} {sym} a ${price}", "buy")
                bought += 1
        elif sig <= T["sell"] and has_pos:
            proceeds = round(pos["qty"]*price, 2)
            pnl = round(proceeds - pos["qty"]*pos["avg_cost"], 2)
            s["cash"] = round(s["cash"] + proceeds, 2)
            update_brain(s, sym, pnl>0, (price-pos["avg_cost"])/pos["avg_cost"])
            s["history"].insert(0, {"t": ts(), "sym": sym, "type": "Venta", "qty": pos["qty"], "price": price, "pnl": pnl})
            s["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "VENTA", "price": price,
                                       "detail": f"señal {sig} · P&L ${pnl}", "won": pnl>0})
            log(s, f"VENTA {pos['qty']} {sym} a ${price} · P&L ${pnl}", "buy" if pnl>0 else "sell")
            pos["qty"] = 0
            sold += 1
        else:
            held += 1
    if len(s["history"]) > 500: s["history"] = s["history"][:500]
    if len(s["decisions"]) > 200: s["decisions"] = s["decisions"][:200]
    save_state(s)

state = load_state()
state["running"] = False

@app.route("/")
def index():
    return jsonify({"status": "TradingAgent API running", "cycle": state["cycle"]})

@app.route("/dashboard")
def dashboard():
    return send_from_directory(".", "dashboard.html")

@app.route("/state")
def get_state():
    if state["running"]:
        run_cycle(state)
    total = state["cash"] + sum(pos["qty"]*gp(state,sym) for sym,pos in state["positions"].items() if pos.get("qty",0)>0)
    pnl = round(total - state["start_cap"], 2)
    wr = round(state["wins"]/(state["wins"]+state["losses"])*100) if (state["wins"]+state["losses"])>0 else 0
    return jsonify({
        "running": state["running"], "cycle": state["cycle"],
        "cash": round(state["cash"],2), "total": round(total,2),
        "pnl": pnl, "pnl_pct": round(pnl/state["start_cap"]*100,2),
        "wins": state["wins"], "losses": state["losses"], "win_rate": wr,
        "positions": {k:v for k,v in state["positions"].items() if v.get("qty",0)>0},
        "decisions": state["decisions"][:20], "log": state["log"][:100],
        "scores": state["scores"], "prices": state["prices"],
        "patterns": state["patterns"], "config": state["config"],
        "history_count": len(state["history"])
    })

@app.route("/start", methods=["POST"])
def start():
    state["running"] = True
    save_state(state)
    log(state, "Agente iniciado", "think")
    return jsonify({"ok": True, "running": True})

@app.route("/stop", methods=["POST"])
def stop():
    state["running"] = False
    save_state(state)
    log(state, "Agente detenido", "warn")
    return jsonify({"ok": True, "running": False})

@app.route("/config", methods=["POST"])
def config():
    data = request.json
    for k in ["freq","sl","tp","sz"]:
        if k in data: state["config"][k] = float(data[k])
    if "risk" in data: state["config"]["risk"] = data["risk"]
    save_state(state)
    return jsonify({"ok": True, "config": state["config"]})

@app.route("/reset", methods=["POST"])
def reset():
    global state
    state = default_state()
    save_state(state)
    return jsonify({"ok": True})

@app.route("/history")
def history():
    return jsonify({"history": state["history"][:100]})

@app.route("/save_report", methods=["POST"])
def save_report():
    data = request.json
    reports = load_reports()
    report_entry = {
        "id": len(reports) + 1,
        "fecha": ts_full(),
        "timestamp": datetime.now().isoformat(),
        "texto": data.get("texto", ""),
        "resumen": {
            "capital": data.get("capital", 0),
            "pnl_pct": data.get("pnl_pct", 0),
            "ops": data.get("ops", 0),
            "win_rate": data.get("win_rate", 0),
            "ciclos": data.get("ciclos", 0)
        }
    }
    reports.insert(0, report_entry)
    if len(reports) > 100:
        reports = reports[:100]
    save_reports(reports)
    return jsonify({"ok": True, "id": report_entry["id"], "total": len(reports)})

@app.route("/reports")
def get_reports():
    reports = load_reports()
    return jsonify({"reports": reports, "total": len(reports)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
