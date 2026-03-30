import os
import json
import time
import random
import threading
import math
from datetime import datetime
from flask import Flask, jsonify, request
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

DATA_FILE = "agent_state.json"

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

def default_state():
    scores = {}
    for sym in BASE_PRICES:
        scores[sym] = {"score": 50, "trades": 0, "wins": 0, "last": "hold"}
    prices = {}
    for sym, p in BASE_PRICES.items():
        prices[sym] = {"price": p, "move": 0, "trend": 0}
    return {
        "cash": 1000.0,
        "start_cap": 1000.0,
        "positions": {},
        "history": [],
        "decisions": [],
        "log": [],
        "scores": scores,
        "prices": prices,
        "patterns": [],
        "memory": [],
        "wins": 0,
        "losses": 0,
        "cycle": 0,
        "running": False,
        "config": {"freq": 60, "sl": 4, "tp": 6, "sz": 20, "risk": "balanced"}
    }

def load_state():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return default_state()

def save_state(state):
    with open(DATA_FILE, "w") as f:
        json.dump(state, f)

state = load_state()
state["running"] = False
agent_thread = None
stop_event = threading.Event()

def ts():
    return datetime.now().strftime("%H:%M:%S")

def add_log(state, msg, type_="think"):
    entry = {"t": ts(), "msg": msg, "type": type_}
    state["log"].insert(0, entry)
    if len(state["log"]) > 300:
        state["log"] = state["log"][:300]

def simulate_prices(state):
    for sym in BASE_PRICES:
        vol = VOLATILITY[sym]
        shock = (random.random() - 0.5) * 2
        trend = state["prices"][sym]["trend"] * 0.85 + shock * 0.15
        move = (trend + (random.random() - 0.5) * 2) * vol
        prev = state["prices"][sym]["price"]
        new_price = prev * (1 + move)
        if sym == "BTC":
            new_price = round(new_price)
        elif sym == "ETH":
            new_price = round(new_price, 1)
        else:
            new_price = round(new_price, 2)
        state["prices"][sym] = {"price": new_price, "move": round(move, 5), "trend": round(trend, 5)}

def calc_signal(state, sym):
    move = state["prices"][sym]["move"]
    score = state["scores"][sym]["score"]
    vol = VOLATILITY[sym]
    risk = state["config"]["risk"]
    mult = 2.5 if risk == "aggressive" else 0.7 if risk == "conservative" else 1.4
    sig = (move / vol) * mult + ((score - 50) / 50) * 0.4 + (random.random() - 0.5) * 0.2
    return round(sig, 3)

THRESHOLDS = {
    "conservative": {"buy": 0.8, "sell": -0.6},
    "balanced": {"buy": 0.4, "sell": -0.3},
    "aggressive": {"buy": 0.15, "sell": -0.1}
}

def get_price(state, sym):
    return state["prices"][sym]["price"]

def update_brain(state, sym, won, ret):
    sc = state["scores"][sym]
    sc["trades"] += 1
    if won:
        sc["wins"] += 1
        state["wins"] += 1
    else:
        state["losses"] += 1
    delta = (7 + random.random() * 3) if won else -(9 + random.random() * 4)
    sc["score"] = min(97, max(3, sc["score"] + delta))

    mem_entry = {"sym": sym, "won": won, "ret": round(ret * 100, 2), "sector": SECTORS[sym], "t": ts()}
    state["memory"].insert(0, mem_entry)
    if len(state["memory"]) > 80:
        state["memory"] = state["memory"][:80]

    sector_mem = [m for m in state["memory"] if m["sector"] == SECTORS[sym]]
    if len(sector_mem) >= 3:
        wr = round(len([m for m in sector_mem if m["won"]]) / len(sector_mem) * 100)
        existing = next((i for i, p in enumerate(state["patterns"]) if p["sector"] == SECTORS[sym]), -1)
        obj = {"sector": SECTORS[sym], "wr": wr, "ops": len(sector_mem)}
        if existing >= 0:
            state["patterns"][existing] = obj
        else:
            state["patterns"].append(obj)

def check_sl_tp(state):
    sl = state["config"]["sl"] / 100
    tp = state["config"]["tp"] / 100
    for sym in list(state["positions"].keys()):
        pos = state["positions"].get(sym)
        if not pos or pos.get("qty", 0) <= 0:
            continue
        cur = get_price(state, sym)
        ret = (cur - pos["avg_cost"]) / pos["avg_cost"]
        reason = None
        if ret <= -sl:
            reason = "stop-loss"
        elif ret >= tp:
            reason = "take-profit"
        if not reason:
            continue
        proceeds = round(pos["qty"] * cur, 2)
        pnl = round(proceeds - pos["qty"] * pos["avg_cost"], 2)
        state["cash"] = round(state["cash"] + proceeds, 2)
        won = pnl > 0
        update_brain(state, sym, won, ret)
        state["history"].insert(0, {"t": ts(), "sym": sym, "type": reason, "qty": pos["qty"], "price": cur, "pnl": pnl})
        state["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "SL" if reason == "stop-loss" else "TP",
                                       "price": cur, "detail": f"ret {round(ret*100,2)}%", "won": won})
        add_log(state, f"{reason.upper()} {sym} · ret {round(ret*100,2)}% · P&L {pnl}", "sell" if not won else "buy")
        state["positions"][sym]["qty"] = 0

def agent_cycle(state):
    state["cycle"] += 1
    simulate_prices(state)
    add_log(state, f"━━ Ciclo #{state['cycle']} ━━", "think")
    check_sl_tp(state)

    risk = state["config"]["risk"]
    T = THRESHOLDS[risk]
    total = state["cash"] + sum(
        pos["qty"] * get_price(state, sym)
        for sym, pos in state["positions"].items() if pos.get("qty", 0) > 0
    )
    budget = round(total * state["config"]["sz"] / 100, 2)
    bought = sold = held = 0

    for sym in BASE_PRICES:
        sig = calc_signal(state, sym)
        pos = state["positions"].get(sym)
        has_pos = pos and pos.get("qty", 0) > 0
        price = get_price(state, sym)

        if sig >= T["buy"] and not has_pos and state["cash"] > price * 0.001:
            spend = min(budget, state["cash"] * 0.9)
            raw_qty = spend / price
            if price > 10000:
                qty = round(raw_qty, 6)
            elif price > 1000:
                qty = round(raw_qty, 5)
            elif price > 100:
                qty = round(raw_qty, 3)
            else:
                qty = round(raw_qty, 2)
            cost = round(qty * price, 2)
            if qty > 0 and cost <= state["cash"] + 0.01:
                state["cash"] = round(state["cash"] - cost, 2)
                state["positions"][sym] = {"qty": qty, "avg_cost": price}
                state["history"].insert(0, {"t": ts(), "sym": sym, "type": "Compra", "qty": qty, "price": price, "pnl": None})
                state["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "COMPRA", "price": price,
                                               "detail": f"señal {sig} · move {round(get_price(state,sym),3)}%"})
                state["scores"][sym]["last"] = "compra"
                add_log(state, f"COMPRA {qty} {sym} a ${price} · señal {sig} · score {round(state['scores'][sym]['score'])}", "buy")
                bought += 1

        elif sig <= T["sell"] and has_pos:
            proceeds = round(pos["qty"] * price, 2)
            pnl = round(proceeds - pos["qty"] * pos["avg_cost"], 2)
            state["cash"] = round(state["cash"] + proceeds, 2)
            ret = (price - pos["avg_cost"]) / pos["avg_cost"]
            update_brain(state, sym, pnl > 0, ret)
            state["history"].insert(0, {"t": ts(), "sym": sym, "type": "Venta", "qty": pos["qty"], "price": price, "pnl": pnl})
            state["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "VENTA", "price": price,
                                           "detail": f"señal {sig} · P&L ${pnl}", "won": pnl > 0})
            state["scores"][sym]["last"] = "venta"
            add_log(state, f"VENTA {pos['qty']} {sym} a ${price} · P&L ${pnl} · señal {sig}", "buy" if pnl > 0 else "sell")
            state["positions"][sym]["qty"] = 0
            sold += 1
        else:
            state["scores"][sym]["last"] = "hold"
            held += 1

    if len(state["history"]) > 500:
        state["history"] = state["history"][:500]
    if len(state["decisions"]) > 200:
        state["decisions"] = state["decisions"][:200]

    add_log(state, f"Compras: {bought} · Ventas: {sold} · Hold: {held}", "think")
    save_state(state)

def agent_loop():
    global state
    while not stop_event.is_set():
        if state["running"]:
            agent_cycle(state)
        freq = state["config"].get("freq", 60)
        stop_event.wait(timeout=freq)

@app.route("/")
def index():
    return jsonify({"status": "TradingAgent API running", "cycle": state["cycle"]})

@app.route("/state")
def get_state():
    total = state["cash"] + sum(
        pos["qty"] * get_price(state, sym)
        for sym, pos in state["positions"].items() if pos.get("qty", 0) > 0
    )
    pnl = round(total - state["start_cap"], 2)
    wr = round(state["wins"] / (state["wins"] + state["losses"]) * 100) if (state["wins"] + state["losses"]) > 0 else 0
    return jsonify({
        "running": state["running"],
        "cycle": state["cycle"],
        "cash": round(state["cash"], 2),
        "total": round(total, 2),
        "pnl": pnl,
        "pnl_pct": round(pnl / state["start_cap"] * 100, 2),
        "wins": state["wins"],
        "losses": state["losses"],
        "win_rate": wr,
        "positions": {k: v for k, v in state["positions"].items() if v.get("qty", 0) > 0},
        "decisions": state["decisions"][:20],
        "log": state["log"][:100],
        "scores": state["scores"],
        "prices": state["prices"],
        "patterns": state["patterns"],
        "config": state["config"],
        "history_count": len(state["history"])
    })

@app.route("/start", methods=["POST"])
def start_agent():
    state["running"] = True
    save_state(state)
    add_log(state, "Agente iniciado via API", "think")
    return jsonify({"ok": True, "running": True})

@app.route("/stop", methods=["POST"])
def stop_agent():
    state["running"] = False
    save_state(state)
    add_log(state, "Agente detenido via API", "warn")
    return jsonify({"ok": True, "running": False})

@app.route("/config", methods=["POST"])
def update_config():
    data = request.json
    for key in ["freq", "sl", "tp", "sz"]:
        if key in data:
            state["config"][key] = float(data[key])
    if "risk" in data:
        state["config"]["risk"] = data["risk"]
    save_state(state)
    add_log(state, f"Config actualizada: {state['config']}", "warn")
    return jsonify({"ok": True, "config": state["config"]})

@app.route("/reset", methods=["POST"])
def reset_agent():
    global state
    state = default_state()
    save_state(state)
    return jsonify({"ok": True, "message": "Estado reiniciado"})

@app.route("/history")
def get_history():
    return jsonify({"history": state["history"][:100]})

if __name__ == "__main__":
    stop_event.clear()
    agent_thread = threading.Thread(target=agent_loop, daemon=True)
    agent_thread.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
