import os
import json
import random
import requests
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory
from flask_cors import CORS
import psycopg2
from psycopg2.extras import RealDictCursor

# --- ALPACA ---
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.data.historical import StockHistoricalDataClient, CryptoHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest, CryptoLatestQuoteRequest, StockBarsRequest, CryptoBarsRequest
from alpaca.data.timeframe import TimeFrame
from datetime import timedelta
import threading
import time

app = Flask(__name__)
CORS(app)

DATABASE_URL      = os.environ.get("DATABASE_URL")
ALPACA_API_KEY    = os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.environ.get("ALPACA_SECRET_KEY")
ALPACA_BASE_URL   = os.environ.get("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
FINNHUB_API_KEY   = os.environ.get("FINNHUB_API_KEY",  "d7arc29r01qtpbh9igcgd7arc29r01qtpbh9igd0")
NEWS_API_KEY      = os.environ.get("NEWS_API_KEY",      "0a7437acf2664cf488d2287b22e6721d")

# Clientes Alpaca
trading_client     = None
stock_data_client  = None
crypto_data_client = None

def init_alpaca():
    global trading_client, stock_data_client, crypto_data_client
    try:
        if ALPACA_API_KEY and ALPACA_SECRET_KEY:
            paper = "paper" in ALPACA_BASE_URL
            trading_client     = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=paper)
            stock_data_client  = StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
            crypto_data_client = CryptoHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY)
            print("✅ Alpaca conectado correctamente")
        else:
            print("⚠️  Variables Alpaca no encontradas — usando simulación de precios")
    except Exception as e:
        print(f"❌ Error conectando Alpaca: {e}")

CRYPTO_SYMBOLS = {}
STOCK_SYMBOLS  = ["NVDA", "MSFT", "META", "GOOGL", "AMZN", "PLTR", "ARKK", "QQQ", "MU", "TSM", "CEG", "GEV"]

# -------------------------------------------------------
# SENTIMENT — estado global cacheado
# -------------------------------------------------------
sentiment_cache = {}
sentiment_lock  = threading.Lock()
SENTIMENT_UPDATE_INTERVAL = 900  # 15 minutos

def _label(score):
    if score >= 0.15:  return "bullish"
    if score <= -0.15: return "bearish"
    return "neutral"

def _finnhub_sentiment(sym):
    try:
        url_sent = f"https://finnhub.io/api/v1/news-sentiment?symbol={sym}&token={FINNHUB_API_KEY}"
        r = requests.get(url_sent, timeout=8)
        data = r.json()
        raw = data.get("companyNewsScore", None)
        score = round((raw - 0.5) * 2, 3) if raw is not None else None

        today = datetime.utcnow().strftime("%Y-%m-%d")
        yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
        url_news = (f"https://finnhub.io/api/v1/company-news"
                    f"?symbol={sym}&from={yesterday}&to={today}&token={FINNHUB_API_KEY}")
        rn = requests.get(url_news, timeout=8)
        news_raw = rn.json() if rn.status_code == 200 else []
        news = []
        for n in news_raw[:5]:
            news.append({
                "title":  n.get("headline", ""),
                "source": "Finnhub",
                "url":    n.get("url", ""),
                "ts":     datetime.utcfromtimestamp(n.get("datetime", 0)).strftime("%d/%m %H:%M") if n.get("datetime") else ""
            })
        return score, news
    except Exception as e:
        print(f"Finnhub sentiment error {sym}: {e}")
        return None, []

def _newsapi_sentiment(sym):
    POSITIVE_WORDS = ["surge", "soar", "beat", "bullish", "gains", "record", "strong",
                      "growth", "upgrade", "buy", "profit", "rally", "outperform",
                      "rises", "higher", "positive", "boom", "jumps"]
    NEGATIVE_WORDS = ["drop", "fall", "miss", "bearish", "losses", "decline", "weak",
                      "downgrade", "sell", "loss", "crash", "underperform",
                      "lower", "negative", "warn", "cut", "risk", "plunge"]
    try:
        COMPANY_NAMES = {
            "NVDA": "NVIDIA", "MSFT": "Microsoft", "META": "Meta",
            "GOOGL": "Google", "AMZN": "Amazon", "PLTR": "Palantir",
            "ARKK": "ARK Innovation", "QQQ": "NASDAQ ETF",
            "MU": "Micron", "TSM": "TSMC", "CEG": "Constellation Energy",
            "GEV": "GE Vernova"
        }
        query = COMPANY_NAMES.get(sym, sym)
        url = (f"https://newsapi.org/v2/everything"
               f"?q={query}&language=en&sortBy=publishedAt"
               f"&pageSize=10&apiKey={NEWS_API_KEY}")
        r = requests.get(url, timeout=8)
        data = r.json()
        articles = data.get("articles", [])

        pos = neg = 0
        news = []
        for a in articles[:10]:
            title = (a.get("title") or "").lower()
            desc  = (a.get("description") or "").lower()
            text  = title + " " + desc
            p = sum(1 for w in POSITIVE_WORDS if w in text)
            n = sum(1 for w in NEGATIVE_WORDS if w in text)
            pos += p
            neg += n
            if len(news) < 5:
                news.append({
                    "title":  a.get("title", ""),
                    "source": a.get("source", {}).get("name", "NewsAPI"),
                    "url":    a.get("url", ""),
                    "ts":     a.get("publishedAt", "")[:16].replace("T", " ")
                })

        total = pos + neg
        score = round((pos - neg) / total, 3) if total > 0 else None
        return score, news
    except Exception as e:
        print(f"NewsAPI sentiment error {sym}: {e}")
        return None, []

def _alpaca_news_sentiment(sym):
    POSITIVE_WORDS = ["surge", "soar", "beat", "bullish", "gains", "record", "strong",
                      "growth", "upgrade", "buy", "profit", "rally", "outperform",
                      "rises", "higher", "positive", "boom", "jumps"]
    NEGATIVE_WORDS = ["drop", "fall", "miss", "bearish", "losses", "decline", "weak",
                      "downgrade", "sell", "loss", "crash", "underperform",
                      "lower", "negative", "warn", "cut", "risk", "plunge"]
    try:
        end   = datetime.utcnow()
        start = end - timedelta(days=1)
        url = (f"https://data.alpaca.markets/v1beta1/news"
               f"?symbols={sym}"
               f"&start={start.strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&end={end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
               f"&limit=10")
        headers = {
            "APCA-API-KEY-ID":     ALPACA_API_KEY or "",
            "APCA-API-SECRET-KEY": ALPACA_SECRET_KEY or ""
        }
        r = requests.get(url, headers=headers, timeout=8)
        data = r.json()
        articles = data.get("news", [])

        pos = neg = 0
        news = []
        for a in articles[:10]:
            headline = (a.get("headline") or "").lower()
            summary  = (a.get("summary") or "").lower()
            text = headline + " " + summary
            p = sum(1 for w in POSITIVE_WORDS if w in text)
            n = sum(1 for w in NEGATIVE_WORDS if w in text)
            pos += p
            neg += n
            if len(news) < 5:
                news.append({
                    "title":  a.get("headline", ""),
                    "source": a.get("source", "Alpaca News"),
                    "url":    a.get("url", ""),
                    "ts":     (a.get("created_at") or "")[:16].replace("T", " ")
                })

        total = pos + neg
        score = round((pos - neg) / total, 3) if total > 0 else None
        return score, news
    except Exception as e:
        print(f"Alpaca news sentiment error {sym}: {e}")
        return None, []

def _combined_sentiment(sym):
    fh_score, fh_news = _finnhub_sentiment(sym)
    na_score, na_news = _newsapi_sentiment(sym)
    al_score, al_news = _alpaca_news_sentiment(sym)

    WEIGHTS = {"finnhub": 0.5, "alpaca": 0.3, "newsapi": 0.2}

    weighted_sum = 0.0
    weight_total = 0.0
    if fh_score is not None:
        weighted_sum += fh_score * WEIGHTS["finnhub"]
        weight_total += WEIGHTS["finnhub"]
    if al_score is not None:
        weighted_sum += al_score * WEIGHTS["alpaca"]
        weight_total += WEIGHTS["alpaca"]
    if na_score is not None:
        weighted_sum += na_score * WEIGHTS["newsapi"]
        weight_total += WEIGHTS["newsapi"]

    combined = round(weighted_sum / weight_total, 3) if weight_total > 0 else 0.0

    seen = set()
    all_news = []
    for n in (fh_news + al_news + na_news):
        key = n["title"][:60]
        if key and key not in seen:
            seen.add(key)
            all_news.append(n)
        if len(all_news) >= 8:
            break

    return {
        "score":   combined,
        "label":   _label(combined),
        "sources": {
            "finnhub": round(fh_score, 3) if fh_score is not None else None,
            "alpaca":  round(al_score, 3) if al_score is not None else None,
            "newsapi": round(na_score, 3) if na_score is not None else None,
        },
        "news":    all_news,
        "updated": datetime.utcnow().strftime("%H:%M UTC")
    }

def update_sentiment_cache():
    print("📰 Actualizando sentiment cache...")
    for sym in STOCK_SYMBOLS:
        try:
            result = _combined_sentiment(sym)
            with sentiment_lock:
                sentiment_cache[sym] = result
            print(f"  {sym}: {result['label']} ({result['score']:+.2f})")
            time.sleep(1.2)
        except Exception as e:
            print(f"  Error sentiment {sym}: {e}")
    print("✅ Sentiment cache actualizado")

def get_sentiment_bonus(sym):
    with sentiment_lock:
        s = sentiment_cache.get(sym)
    if not s:
        return 0.0
    return round(s["score"] * 0.3, 4)

def sentiment_loop():
    print("📰 Sentiment loop iniciado")
    time.sleep(10)
    while True:
        try:
            update_sentiment_cache()
        except Exception as e:
            print(f"❌ Error en sentiment loop: {e}")
        time.sleep(SENTIMENT_UPDATE_INTERVAL)

# -------------------------------------------------------

def is_market_open():
    import pytz
    et = pytz.timezone("America/New_York")
    now_et = datetime.now(et)
    if now_et.weekday() >= 5:
        return False
    market_open  = now_et.replace(hour=9,  minute=30, second=0, microsecond=0)
    market_close = now_et.replace(hour=16, minute=0,  second=0, microsecond=0)
    return market_open <= now_et < market_close

def get_stock_bar_prices():
    """
    Obtiene el cierre y volumen de la última vela de 5min para cada acción.
    Devuelve dict: { sym: {"price": float, "volume": int} }
    """
    result = {}
    try:
        end   = datetime.utcnow()
        start = end - timedelta(minutes=15)
        req = StockBarsRequest(
            symbol_or_symbols=STOCK_SYMBOLS,
            timeframe=TimeFrame.Minute5,
            start=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            end=end.strftime("%Y-%m-%dT%H:%M:%SZ"),
            feed="iex"
        )
        bars = stock_data_client.get_stock_bars(req)
        df = bars.df.reset_index() if hasattr(bars, "df") else None
        if df is not None and not df.empty:
            for sym in STOCK_SYMBOLS:
                sym_df = df[df["symbol"] == sym] if "symbol" in df.columns else df
                if not sym_df.empty:
                    last_bar = sym_df.iloc[-1]
                    close  = float(last_bar["close"])
                    volume = int(last_bar["volume"]) if "volume" in last_bar else 0
                    if close > 0:
                        result[sym] = {"price": round(close, 2), "volume": volume}
    except Exception as e:
        print(f"Error velas acciones 5min: {e}")
    return result

def get_crypto_prices():
    prices = {}
    try:
        crypto_req = CryptoLatestQuoteRequest(symbol_or_symbols=list(CRYPTO_SYMBOLS.values()))
        crypto_quotes = crypto_data_client.get_crypto_latest_quote(crypto_req)
        for local_sym, alpaca_sym in CRYPTO_SYMBOLS.items():
            if alpaca_sym in crypto_quotes:
                q = crypto_quotes[alpaca_sym]
                mid = round((q.ask_price + q.bid_price) / 2, 1) if q.ask_price and q.bid_price else q.ask_price or q.bid_price
                if mid and mid > 0:
                    prices[local_sym] = mid
    except Exception as e:
        print(f"Error precios crypto: {e}")
    return prices

def place_alpaca_order(sym, qty, side):
    try:
        alpaca_sym = CRYPTO_SYMBOLS.get(sym, sym)
        order_data = MarketOrderRequest(
            symbol=alpaca_sym,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.GTC if sym in CRYPTO_SYMBOLS else TimeInForce.DAY
        )
        order = trading_client.submit_order(order_data)
        print(f"✅ Orden Alpaca: {side.upper()} {qty} {alpaca_sym} → ID {order.id}")
        return True
    except Exception as e:
        print(f"❌ Error orden Alpaca {side} {sym}: {e}")
        return False

def run_backtest(s, days=200):
    if not stock_data_client:
        print("⚠️ Backtest omitido — Alpaca no disponible")
        return

    print(f"🧠 Iniciando backtest con {days} días de datos históricos...")
    log(s, f"🧠 Iniciando backtest {days} días...", "think")

    end = datetime.now()
    start = end - timedelta(days=days)
    start_str = start.strftime("%Y-%m-%d")
    end_str   = end.strftime("%Y-%m-%d")

    historical_prices = {sym: [] for sym in BASE_PRICES}
    try:
        req = StockBarsRequest(
            symbol_or_symbols=STOCK_SYMBOLS,
            timeframe=TimeFrame.Day,
            start=start_str,
            end=end_str
        )
        bars_response = stock_data_client.get_stock_bars(req)
        bars_df = bars_response.df.reset_index() if hasattr(bars_response, 'df') else None
        if bars_df is not None and not bars_df.empty:
            for sym in STOCK_SYMBOLS:
                if "symbol" in bars_df.columns:
                    sym_data = bars_df[bars_df["symbol"] == sym]["close"].tolist()
                else:
                    sym_data = bars_df["close"].tolist()
                if sym_data:
                    historical_prices[sym] = [float(p) for p in sym_data]
                    print(f"  {sym}: {len(historical_prices[sym])} dias")
        print("✅ Datos históricos acciones descargados")
    except Exception as e:
        print(f"❌ Error descargando histórico acciones: {e}")

    try:
        crypto_req = CryptoBarsRequest(
            symbol_or_symbols=list(CRYPTO_SYMBOLS.values()),
            timeframe=TimeFrame.Day,
            start=start_str,
            end=end_str
        )
        crypto_response = crypto_data_client.get_crypto_bars(crypto_req)
        crypto_df = crypto_response.df.reset_index() if hasattr(crypto_response, 'df') else None
        if crypto_df is not None and not crypto_df.empty:
            for local_sym, alpaca_sym in CRYPTO_SYMBOLS.items():
                if "symbol" in crypto_df.columns:
                    sym_data = crypto_df[crypto_df["symbol"] == alpaca_sym]["close"].tolist()
                else:
                    sym_data = crypto_df["close"].tolist()
                if sym_data:
                    historical_prices[local_sym] = [float(p) for p in sym_data]
        print("✅ Datos históricos crypto descargados")
    except Exception as e:
        print(f"❌ Error descargando histórico crypto: {e}")

    available = {sym: len(v) for sym, v in historical_prices.items() if len(v) >= 2}
    if not available:
        print("⚠️ Datos históricos insuficientes para backtest")
        log(s, "⚠️ Datos históricos insuficientes", "warn")
        return
    min_days = max(available.values())

    bt_wins = bt_losses = bt_trades = 0

    for i in range(1, min_days):
        for sym in BASE_PRICES:
            prices_list = historical_prices.get(sym, [])
            if len(prices_list) > i:
                prev = prices_list[i - 1]
                curr = prices_list[i]
                move = round((curr - prev) / prev, 5) if prev else 0
                s["prices"][sym] = {"price": curr, "move": move, "trend": move, "volume": 0, "avg_volume": 0}

        T = THRESHOLDS[s["config"]["risk"]]
        for sym in BASE_PRICES:
            move = s["prices"][sym]["move"]
            vol  = VOLATILITY[sym]
            sc   = s["scores"][sym]["score"]
            mult = 1.4
            sig  = round((move / vol) * mult + ((sc - 50) / 50) * 0.4, 3)

            pos     = s["positions"].get(sym)
            has_pos = pos and pos.get("qty", 0) > 0
            price   = s["prices"][sym]["price"]

            if sig >= T["buy"] and not has_pos and s["cash"] > price * 0.001:
                budget = round(s["cash"] * 0.2, 2)
                qty    = round(budget / price, 3)
                cost   = round(qty * price, 2)
                if qty > 0 and cost <= s["cash"]:
                    s["cash"] -= cost
                    s["positions"][sym] = {"qty": qty, "avg_cost": price}
                    bt_trades += 1

            elif sig <= T["sell"] and has_pos:
                proceeds = round(pos["qty"] * price, 2)
                pnl = proceeds - pos["qty"] * pos["avg_cost"]
                won = pnl > 0
                ret = (price - pos["avg_cost"]) / pos["avg_cost"]
                s["cash"] = round(s["cash"] + proceeds, 2)
                update_brain(s, sym, won, ret)
                pos["qty"] = 0
                bt_trades += 1
                if won: bt_wins += 1
                else:   bt_losses += 1

        sl = s["config"]["sl"] / 100
        tp = s["config"]["tp"] / 100
        for sym in list(s["positions"].keys()):
            pos = s["positions"].get(sym)
            if not pos or pos.get("qty", 0) <= 0: continue
            cur = s["prices"][sym]["price"]
            ret = (cur - pos["avg_cost"]) / pos["avg_cost"]
            if ret <= -sl or ret >= tp:
                proceeds = round(pos["qty"] * cur, 2)
                pnl = proceeds - pos["qty"] * pos["avg_cost"]
                s["cash"] = round(s["cash"] + proceeds, 2)
                update_brain(s, sym, pnl > 0, ret)
                pos["qty"] = 0
                bt_trades += 1
                if pnl > 0: bt_wins += 1
                else:        bt_losses += 1

    for sym in list(s["positions"].keys()):
        pos = s["positions"].get(sym)
        if pos and pos.get("qty", 0) > 0:
            pos["qty"] = 0

    s["cash"] = s["start_cap"]
    s["wins"] = s["losses"] = 0
    s["positions"]  = {}
    s["backtest_done"] = True
    s["backtest_summary"] = {
        "dias": min_days, "trades_simulados": bt_trades,
        "wins": bt_wins, "losses": bt_losses,
        "wr": round(bt_wins / bt_trades * 100) if bt_trades > 0 else 0
    }
    wr = round(bt_wins / bt_trades * 100) if bt_trades > 0 else 0
    print(f"✅ Backtest completado: {min_days} días · {bt_trades} trades · WR {wr}%")
    log(s, f"✅ Backtest {min_days} días completado · {bt_trades} trades · WR {wr}%", "think")
    save_state(s)


# ---- DB ----

def get_db():
    return psycopg2.connect(DATABASE_URL, sslmode="require")

def init_db():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS agent_state (
                id INTEGER PRIMARY KEY DEFAULT 1,
                data JSONB NOT NULL,
                updated_at TIMESTAMP DEFAULT NOW()
            );
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS reports (
                id SERIAL PRIMARY KEY,
                texto TEXT NOT NULL,
                capital FLOAT,
                pnl_pct FLOAT,
                ops INTEGER,
                win_rate INTEGER,
                ciclos INTEGER,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"DB init error: {e}")

BASE_PRICES = {
    "NVDA": 875, "MSFT": 380, "META": 515, "GOOGL": 168,
    "AMZN": 192, "PLTR": 24,  "ARKK": 48,  "QQQ": 447,
    "MU":   90,  "TSM":  160, "CEG":  250, "GEV":  360
}
VOLATILITY = {
    "NVDA": 0.022, "MSFT": 0.014, "META": 0.020, "GOOGL": 0.015,
    "AMZN": 0.017, "PLTR": 0.035, "ARKK": 0.025, "QQQ":   0.013,
    "MU":   0.030, "TSM":  0.022, "CEG":  0.025, "GEV":   0.028
}
SECTORS = {
    "NVDA": "Chips IA",     "MSFT": "Cloud/IA",    "META": "IA Consumo",
    "GOOGL":"IA/Search",    "AMZN": "Cloud",        "PLTR": "Data IA",
    "ARKK": "ETF Tech",     "QQQ":  "ETF NASDAQ",   "MU":   "Chips/Memoria",
    "TSM":  "Chips IA",     "CEG":  "Energía",      "GEV":  "Energía"
}
THRESHOLDS = {
    "conservative": {"buy": 1.5,  "sell": -1.2},
    "balanced":     {"buy": 0.8,  "sell": -0.6},
    "aggressive":   {"buy": 0.35, "sell": -0.25}
}

def default_state():
    return {
        "cash": 1000.0, "start_cap": 1000.0,
        "positions": {}, "history": [], "decisions": [], "log": [],
        "scores":    {s: {"score": 50, "trades": 0, "wins": 0, "last": "hold"} for s in BASE_PRICES},
        "prices":    {s: {"price": p, "move": 0, "trend": 0, "volume": 0, "avg_volume": 0} for s, p in BASE_PRICES.items()},
        "price_history":  {s: [] for s in BASE_PRICES},
        "volume_history": {s: [] for s in BASE_PRICES},
        "patterns": [], "memory": [], "wins": 0, "losses": 0, "cycle": 0,
        "running": False, "last_cycle_time": 0,
        "config": {"freq": 300, "sl": 4, "tp": 6, "sz": 20, "risk": "balanced"},
        "mode": "beta"
    }

def load_state():
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("SELECT data FROM agent_state WHERE id = 1")
        row = cur.fetchone()
        cur.close()
        conn.close()
        if row:
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            if "price_history" not in data:
                data["price_history"] = {s: [] for s in BASE_PRICES}
            # Migración: agregar volume_history si no existe
            if "volume_history" not in data:
                data["volume_history"] = {s: [] for s in BASE_PRICES}
            valid = set(BASE_PRICES.keys())
            data["scores"] = {
                s: data["scores"].get(s, {"score": 50, "trades": 0, "wins": 0, "last": "hold"})
                for s in valid
            }
            data["prices"] = {
                s: data["prices"].get(s, {"price": BASE_PRICES[s], "move": 0, "trend": 0, "volume": 0, "avg_volume": 0})
                for s in valid
            }
            # Migración: agregar volume/avg_volume a precios existentes si faltan
            for s in valid:
                if "volume" not in data["prices"][s]:
                    data["prices"][s]["volume"] = 0
                if "avg_volume" not in data["prices"][s]:
                    data["prices"][s]["avg_volume"] = 0
            data["price_history"] = {
                s: data["price_history"].get(s, [])
                for s in valid
            }
            data["volume_history"] = {
                s: data["volume_history"].get(s, [])
                for s in valid
            }
            data["positions"] = {
                k: v for k, v in data.get("positions", {}).items() if k in valid
            }
            return data
    except Exception as e:
        print(f"Load state error: {e}")
    return default_state()

def save_state(s):
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO agent_state (id, data, updated_at)
            VALUES (1, %s, NOW())
            ON CONFLICT (id) DO UPDATE SET data = %s, updated_at = NOW()
        """, (json.dumps(s), json.dumps(s)))
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Save state error: {e}")

def ts():
    return datetime.now().strftime("%H:%M:%S")

def log(s, msg, t="think"):
    s["log"].insert(0, {"t": ts(), "msg": msg, "type": t})
    if len(s["log"]) > 300:
        s["log"] = s["log"][:300]

def simulate_prices(s):
    for sym in BASE_PRICES:
        vol   = VOLATILITY[sym]
        shock = (random.random() - 0.5) * 2
        trend = s["prices"][sym]["trend"] * 0.85 + shock * 0.15
        move  = (trend + (random.random() - 0.5) * 2) * vol
        prev  = s["prices"][sym]["price"]
        np_   = round(prev * (1 + move), 2)
        s["prices"][sym] = {"price": np_, "move": round(move, 5), "trend": round(trend, 5),
                            "volume": 0, "avg_volume": 0}

def _append_price_history(s):
    ph = s.setdefault("price_history",  {s2: [] for s2 in BASE_PRICES})
    vh = s.setdefault("volume_history", {s2: [] for s2 in BASE_PRICES})
    for sym in BASE_PRICES:
        # Precio
        ph.setdefault(sym, []).append(s["prices"][sym]["price"])
        if len(ph[sym]) > 100:
            ph[sym] = ph[sym][-100:]
        # Volumen (solo si tiene dato real)
        vol = s["prices"][sym].get("volume", 0)
        if vol > 0:
            vh.setdefault(sym, []).append(vol)
            if len(vh[sym]) > 100:
                vh[sym] = vh[sym][-100:]
        # Actualizar avg_volume en prices para fácil acceso
        if vh.get(sym):
            s["prices"][sym]["avg_volume"] = int(sum(vh[sym]) / len(vh[sym]))

def update_prices(s):
    mode = s.get("mode", "alpha")
    if mode != "beta" or not trading_client:
        simulate_prices(s)
        _append_price_history(s)
        return False, False

    market_open = is_market_open()
    got_real    = False

    crypto = get_crypto_prices()
    for local_sym, new_price in crypto.items():
        prev = s["prices"][local_sym]["price"]
        move = round((new_price - prev) / prev, 5) if prev else 0
        s["prices"][local_sym] = {"price": new_price, "move": move, "trend": move,
                                  "volume": 0, "avg_volume": s["prices"][local_sym].get("avg_volume", 0)}
    if crypto:
        got_real = True

    if market_open:
        stock_bars = get_stock_bar_prices()
        if stock_bars:
            for sym, bar in stock_bars.items():
                prev  = s["prices"][sym]["price"]
                price = bar["price"]
                vol   = bar["volume"]
                move  = round((price - prev) / prev, 5) if prev else 0
                s["prices"][sym] = {"price": price, "move": move, "trend": move,
                                    "volume": vol, "avg_volume": s["prices"][sym].get("avg_volume", 0)}
            # Símbolos sin vela: simular movimiento mínimo
            for sym in STOCK_SYMBOLS:
                if sym not in stock_bars:
                    vol_  = VOLATILITY[sym]
                    prev  = s["prices"][sym]["price"]
                    move  = (random.random() - 0.5) * 2 * vol_
                    s["prices"][sym] = {"price": round(prev * (1 + move), 2),
                                        "move": round(move, 5), "trend": round(move, 5),
                                        "volume": 0, "avg_volume": s["prices"][sym].get("avg_volume", 0)}
            got_real = True
        else:
            for sym in STOCK_SYMBOLS:
                vol_  = VOLATILITY[sym]
                prev  = s["prices"][sym]["price"]
                move  = (random.random() - 0.5) * 2 * vol_
                s["prices"][sym] = {"price": round(prev * (1 + move), 2),
                                    "move": round(move, 5), "trend": round(move, 5),
                                    "volume": 0, "avg_volume": s["prices"][sym].get("avg_volume", 0)}
        _append_price_history(s)

    return got_real, market_open

def gp(s, sym):
    return s["prices"][sym]["price"]

def update_brain(s, sym, won, ret):
    sc = s["scores"][sym]
    sc["trades"] += 1
    if won: sc["wins"] += 1; s["wins"] += 1
    else:   s["losses"] += 1
    sc["score"] = min(97, max(3, sc["score"] + ((7 + random.random()*3) if won else -(9 + random.random()*4))))
    s["memory"].insert(0, {"sym": sym, "won": won, "ret": round(ret*100,2), "sector": SECTORS[sym], "t": ts()})
    if len(s["memory"]) > 80: s["memory"] = s["memory"][:80]
    sm = [m for m in s["memory"] if m["sector"] == SECTORS[sym]]
    if len(sm) >= 3:
        wr  = round(len([m for m in sm if m["won"]]) / len(sm) * 100)
        idx = next((i for i,p in enumerate(s["patterns"]) if p["sector"]==SECTORS[sym]), -1)
        obj = {"sector": SECTORS[sym], "wr": wr, "ops": len(sm)}
        if idx >= 0: s["patterns"][idx] = obj
        else:        s["patterns"].append(obj)

def check_sl_tp(s):
    sl = s["config"]["sl"] / 100
    tp = s["config"]["tp"] / 100
    for sym in list(s["positions"].keys()):
        pos = s["positions"].get(sym)
        if not pos or pos.get("qty", 0) <= 0: continue
        cur    = gp(s, sym)
        ret    = (cur - pos["avg_cost"]) / pos["avg_cost"]
        reason = "stop-loss" if ret <= -sl else "take-profit" if ret >= tp else None
        if not reason: continue
        proceeds = round(pos["qty"] * cur, 2)
        pnl      = round(proceeds - pos["qty"] * pos["avg_cost"], 2)
        if s.get("mode") == "beta" and trading_client:
            place_alpaca_order(sym, pos["qty"], "sell")
        s["cash"] = round(s["cash"] + proceeds, 2)
        update_brain(s, sym, pnl > 0, ret)
        s["history"].insert(0,  {"t": ts(), "sym": sym, "type": reason, "qty": pos["qty"], "price": cur, "pnl": pnl})
        s["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "SL" if reason=="stop-loss" else "TP",
                                   "price": cur, "detail": f"ret {round(ret*100,2)}%", "won": pnl > 0})
        log(s, f"{reason.upper()} {sym} · ret {round(ret*100,2)}% · P&L ${pnl}", "buy" if pnl>0 else "sell")
        pos["qty"] = 0

# -------------------------------------------------------
# RSI — cálculo puro sin dependencias externas
# -------------------------------------------------------
def calc_rsi(prices, period=14):
    """
    Calcula el RSI sobre una lista de precios de cierre.
    Retorna valor entre 0 y 100, o None si no hay suficientes datos.
    RSI < 30 → sobrevendido (señal de compra)
    RSI > 70 → sobrecomprado (señal de venta)
    """
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(1, period + 1):
        delta = prices[-period - 1 + i] - prices[-period - 2 + i]
        if delta >= 0:
            gains.append(delta)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(delta))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return round(100 - (100 / (1 + rs)), 2)

# -------------------------------------------------------
# Factor de volumen
# -------------------------------------------------------
def calc_volume_factor(sym, s):
    """
    Compara el volumen actual con el promedio histórico.
    Retorna un multiplicador entre 0.7 y 1.5:
      - Volumen 2x el promedio  → factor 1.5  (señal más fuerte)
      - Volumen igual al promedio → factor 1.0 (neutro)
      - Volumen < 50% del promedio → factor 0.7 (señal más débil)
    Si no hay datos de volumen, devuelve 1.0 (sin efecto).
    """
    current_vol = s["prices"][sym].get("volume", 0)
    avg_vol     = s["prices"][sym].get("avg_volume", 0)
    if current_vol <= 0 or avg_vol <= 0:
        return 1.0
    ratio = current_vol / avg_vol
    # Clampear entre 0.7 y 1.5
    factor = max(0.7, min(1.5, 0.7 + ratio * 0.3))
    return round(factor, 3)

# -------------------------------------------------------
# RSI adjustment: cuánto suma/resta a la señal
# -------------------------------------------------------
def calc_rsi_adjustment(rsi):
    """
    Convierte el RSI en un ajuste aditivo para la señal:
      RSI < 30 (sobrevendido)  → hasta +0.4 (favorece compra)
      RSI > 70 (sobrecomprado) → hasta -0.4 (favorece venta)
      RSI entre 30-70          → ajuste proporcional pequeño
    """
    if rsi is None:
        return 0.0
    if rsi < 30:
        # Más sobrevendido → más bonus de compra
        return round((30 - rsi) / 30 * 0.4, 3)
    elif rsi > 70:
        # Más sobrecomprado → más penalización
        return round(-((rsi - 70) / 30 * 0.4), 3)
    else:
        # Zona neutral: ajuste leve centrado en 50
        return round((50 - rsi) / 50 * 0.1, 3)

def calc_signal(s, sym, mult):
    """
    Señal combinada: MA5/MA20 + RSI + Volumen + Sentiment.

    Componentes:
      - Base MA:      cruce MA5/MA20 normalizado por volatilidad  (principal)
      - RSI adjust:   hasta ±0.4 según zona sobrecompra/sobreventa
      - Vol factor:   multiplica la señal base entre 0.7x y 1.5x
      - Sent bonus:   hasta ±0.3 según noticias
    """
    hist  = s.get("price_history", {}).get(sym, [])
    move  = s["prices"][sym]["move"]
    vol   = VOLATILITY[sym]
    sc    = s["scores"][sym]["score"]

    # --- Señal base MA ---
    if len(hist) < 20:
        sig_base = round((move / vol) * mult + ((sc - 50) / 50) * 0.4, 3)
    else:
        ma5  = sum(hist[-5:])  / 5
        ma20 = sum(hist[-20:]) / 20
        cross    = (ma5 - ma20) / ma20
        sig_base = round((cross / vol) * mult + ((sc - 50) / 50) * 0.4, 3)

    # --- Factor de volumen (multiplica la señal base) ---
    vol_factor = calc_volume_factor(sym, s)
    sig_base   = round(sig_base * vol_factor, 3)

    # --- RSI ---
    rsi        = calc_rsi(hist) if len(hist) >= 15 else None
    rsi_adjust = calc_rsi_adjustment(rsi)

    # --- Sentiment ---
    sent_bonus = get_sentiment_bonus(sym)

    # Guardar indicadores en scores para el dashboard
    s["scores"][sym]["rsi"]        = rsi
    s["scores"][sym]["vol_factor"] = vol_factor

    return round(sig_base + rsi_adjust + sent_bonus, 3)

def run_cycle(s):
    now  = time.time()
    freq = s["config"].get("freq", 300)
    last = s.get("last_cycle_time", 0)
    if now - last < freq:
        return
    s["last_cycle_time"] = now
    s["cycle"] += 1

    mode = s.get("mode", "alpha")
    using_real, market_open = update_prices(s)

    if mode == "beta" and not market_open:
        hist_len = len(s.get("price_history", {}).get("NVDA", []))
        log(s, f"Ciclo #{s['cycle']} · 🔒 Mercado cerrado · historial pausado ({hist_len} pts)", "think")
        check_sl_tp(s)
        save_state(s)
        return

    source   = "📡 Alpaca velas 5min" if using_real else "🎲 Simulado"
    hist_len = len(s.get("price_history", {}).get("NVDA", []))
    source  += " · MA5/MA20 activa" if hist_len >= 20 else f" · acumulando historial ({hist_len}/20)"

    rsi_sample = s["scores"].get("NVDA", {}).get("rsi")
    source += f" · RSI:{rsi_sample}" if rsi_sample is not None else " · RSI:acumulando"

    sent_ready = len(sentiment_cache) > 0
    source += " · 📰 Sentiment activo" if sent_ready else " · ⏳ Sentiment cargando"

    log(s, f"Ciclo #{s['cycle']} · {source}", "think")

    check_sl_tp(s)
    T      = THRESHOLDS[s["config"]["risk"]]
    total  = s["cash"] + sum(pos["qty"]*gp(s,sym) for sym,pos in s["positions"].items() if pos.get("qty",0)>0)
    budget = round(total * s["config"]["sz"] / 100, 2)
    bought = sold = held = 0

    for sym in BASE_PRICES:
        risk = s["config"]["risk"]
        mult = 2.5 if risk=="aggressive" else 0.7 if risk=="conservative" else 1.4

        sig = calc_signal(s, sym, mult)

        pos     = s["positions"].get(sym)
        has_pos = pos and pos.get("qty", 0) > 0
        price   = gp(s, sym)
        s["scores"][sym]["last"] = "compra" if sig>=T["buy"] else "venta" if sig<=T["sell"] else "hold"

        with sentiment_lock:
            sent = sentiment_cache.get(sym)
        s["scores"][sym]["sentiment"] = sent["label"] if sent else "neutral"
        s["scores"][sym]["sent_score"] = sent["score"] if sent else 0.0

        if sig >= T["buy"] and not has_pos and s["cash"] > price * 0.001:
            spend = min(budget, s["cash"] * 0.9)
            qty   = round(spend/price, 6 if price>10000 else 5 if price>1000 else 3 if price>100 else 2)
            cost  = round(qty * price, 2)
            if qty > 0 and cost <= s["cash"] + 0.01:
                if s.get("mode") == "beta" and trading_client:
                    place_alpaca_order(sym, qty, "buy")
                s["cash"] = round(s["cash"] - cost, 2)
                s["positions"][sym] = {"qty": qty, "avg_cost": price}
                s["history"].insert(0,  {"t": ts(), "sym": sym, "type": "Compra", "qty": qty, "price": price, "pnl": None})
                s["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "COMPRA", "price": price,
                                           "detail": f"señal {sig} · RSI:{s['scores'][sym].get('rsi','?')} · vol:{s['scores'][sym].get('vol_factor','?')}x · sent:{s['scores'][sym]['sentiment']}"})
                log(s, f"COMPRA {qty} {sym} a ${price} · RSI:{s['scores'][sym].get('rsi','?')} · vol:{s['scores'][sym].get('vol_factor','?')}x · sent:{s['scores'][sym]['sentiment']}", "buy")
                bought += 1

        elif sig <= T["sell"] and has_pos:
            proceeds = round(pos["qty"]*price, 2)
            pnl      = round(proceeds - pos["qty"]*pos["avg_cost"], 2)
            if s.get("mode") == "beta" and trading_client:
                place_alpaca_order(sym, pos["qty"], "sell")
            s["cash"] = round(s["cash"] + proceeds, 2)
            update_brain(s, sym, pnl>0, (price-pos["avg_cost"])/pos["avg_cost"])
            s["history"].insert(0,  {"t": ts(), "sym": sym, "type": "Venta", "qty": pos["qty"], "price": price, "pnl": pnl})
            s["decisions"].insert(0, {"t": ts(), "sym": sym, "action": "VENTA", "price": price,
                                       "detail": f"señal {sig} · P&L ${pnl} · RSI:{s['scores'][sym].get('rsi','?')} · sent:{s['scores'][sym]['sentiment']}",
                                       "won": pnl>0})
            log(s, f"VENTA {pos['qty']} {sym} a ${price} · P&L ${pnl} · RSI:{s['scores'][sym].get('rsi','?')} · sent:{s['scores'][sym]['sentiment']}",
                "buy" if pnl>0 else "sell")
            pos["qty"] = 0
            sold += 1
        else:
            held += 1

    if len(s["history"])   > 500: s["history"]   = s["history"][:500]
    if len(s["decisions"]) > 200: s["decisions"] = s["decisions"][:200]
    save_state(s)


init_db()
init_alpaca()
state = load_state()

if ALPACA_API_KEY and ALPACA_SECRET_KEY:
    state["mode"] = "beta"

was_running = state.get("running", False)
if was_running:
    print("🔄 El agente estaba corriendo antes del reinicio — reanudando automáticamente")
    log(state, "🔄 Servidor reiniciado — agente reanudado automáticamente", "think")
else:
    print("⏸️  Agente detenido (estado guardado)")

if stock_data_client and not state.get("backtest_done") and len(state.get("memory", [])) == 0:
    print("🧠 Sin historial detectado — lanzando backtest automático en segundo plano...")
    bt_thread = threading.Thread(target=run_backtest, args=(state, 200), daemon=True)
    bt_thread.start()
else:
    if state.get("backtest_done"):
        print("✅ Backtest previo detectado — omitiendo")

def background_loop():
    print("🔄 Background loop iniciado — el agente corre 24/7")
    while True:
        try:
            if state["running"]:
                run_cycle(state)
        except Exception as e:
            print(f"❌ Error en background loop: {e}")
        time.sleep(5)

bg_thread = threading.Thread(target=background_loop, daemon=True)
bg_thread.start()

sent_thread = threading.Thread(target=sentiment_loop, daemon=True)
sent_thread.start()


# ---- ENDPOINTS ----

@app.route("/")
def index():
    return jsonify({
        "status": "TradingAgent API running",
        "cycle":  state["cycle"],
        "mode":   state.get("mode", "alpha"),
        "alpaca_connected":  trading_client is not None,
        "sentiment_symbols": len(sentiment_cache)
    })

@app.route("/dashboard")
def dashboard():
    return send_from_directory(".", "dashboard.html")

@app.route("/state")
def get_state():
    total = state["cash"] + sum(pos["qty"]*gp(state,sym) for sym,pos in state["positions"].items() if pos.get("qty",0)>0)
    pnl   = round(total - state["start_cap"], 2)
    wr    = round(state["wins"]/(state["wins"]+state["losses"])*100) if (state["wins"]+state["losses"])>0 else 0
    hist_len = len(state.get("price_history", {}).get("NVDA", []))
    rsi_ready = hist_len >= 15
    return jsonify({
        "running": state["running"], "cycle": state["cycle"],
        "cash":    round(state["cash"],2), "total": round(total,2),
        "pnl": pnl, "pnl_pct": round(pnl/state["start_cap"]*100,2),
        "wins": state["wins"], "losses": state["losses"], "win_rate": wr,
        "positions": {k:v for k,v in state["positions"].items() if v.get("qty",0)>0},
        "decisions": state["decisions"][:20], "log": state["log"][:100],
        "scores":   state["scores"], "prices": state["prices"],
        "patterns": state["patterns"], "config": state["config"],
        "history_count": len(state["history"]),
        "mode":    state.get("mode", "alpha"),
        "alpaca_connected": trading_client is not None,
        "ma_ready":    hist_len >= 20,
        "rsi_ready":   rsi_ready,
        "ma_progress": f"{hist_len}/20",
        "market_open": is_market_open() if state.get("mode") == "beta" else None,
        "sentiment_ready": len(sentiment_cache) > 0
    })

@app.route("/sentiment")
def get_sentiment():
    with sentiment_lock:
        data = dict(sentiment_cache)
    return jsonify({"sentiment": data, "symbols": len(data)})

@app.route("/sentiment/refresh", methods=["POST"])
def refresh_sentiment():
    def _refresh():
        update_sentiment_cache()
    t = threading.Thread(target=_refresh, daemon=True)
    t.start()
    return jsonify({"ok": True, "msg": "Actualización de sentiment iniciada"})

@app.route("/start", methods=["POST"])
def start():
    state["running"] = True
    save_state(state)
    mode = state.get("mode", "alpha")
    log(state, f"Agente iniciado · Modo: {mode.upper()}", "think")
    return jsonify({"ok": True, "running": True, "mode": mode})

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
    if "risk"  in data: state["config"]["risk"] = data["risk"]
    if "mode"  in data: state["mode"]           = data["mode"]
    save_state(state)
    return jsonify({"ok": True, "config": state["config"], "mode": state.get("mode")})

@app.route("/reset", methods=["POST"])
def reset():
    global state
    closed = []
    errors = []
    if trading_client:
        for sym, pos in state.get("positions", {}).items():
            if pos and pos.get("qty", 0) > 0:
                ok = place_alpaca_order(sym, pos["qty"], "sell")
                if ok:  closed.append(sym)
                else:   errors.append(sym)
    state = default_state()
    if ALPACA_API_KEY and ALPACA_SECRET_KEY:
        state["mode"] = "beta"
    save_state(state)
    return jsonify({"ok": True, "closed": closed, "errors": errors})

@app.route("/history")
def history():
    return jsonify({"history": state["history"][:100]})

@app.route("/backtest/status")
def backtest_status():
    return jsonify({
        "done":        state.get("backtest_done", False),
        "summary":     state.get("backtest_summary", None),
        "memory_size": len(state.get("memory", [])),
        "patterns":    state.get("patterns", [])
    })

@app.route("/alpaca/status")
def alpaca_status():
    if not trading_client:
        return jsonify({"connected": False, "reason": "No hay keys configuradas"})
    try:
        account = trading_client.get_account()
        return jsonify({
            "connected": True,
            "account_id":      str(account.id),
            "status":          str(account.status),
            "buying_power":    str(account.buying_power),
            "portfolio_value": str(account.portfolio_value),
            "paper": "paper" in ALPACA_BASE_URL
        })
    except Exception as e:
        return jsonify({"connected": False, "reason": str(e)})

@app.route("/save_report", methods=["POST"])
def save_report():
    data = request.json
    try:
        conn = get_db()
        cur  = conn.cursor()
        cur.execute("""
            INSERT INTO reports (texto, capital, pnl_pct, ops, win_rate, ciclos)
            VALUES (%s, %s, %s, %s, %s, %s) RETURNING id
        """, (data.get("texto",""), data.get("capital",0), data.get("pnl_pct",0),
              data.get("ops",0), data.get("win_rate",0), data.get("ciclos",0)))
        report_id = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM reports")
        total = cur.fetchone()[0]
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"ok": True, "id": report_id, "total": total})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})

@app.route("/reports")
def get_reports():
    try:
        conn = get_db()
        cur  = conn.cursor(cursor_factory=RealDictCursor)
        cur.execute("SELECT * FROM reports ORDER BY created_at DESC LIMIT 100")
        reports = []
        for row in cur.fetchall():
            reports.append({
                "id":     row["id"],
                "fecha":  row["created_at"].strftime("%d/%m/%Y %H:%M:%S"),
                "texto":  row["texto"],
                "resumen": {
                    "capital":  row["capital"],
                    "pnl_pct":  row["pnl_pct"],
                    "ops":      row["ops"],
                    "win_rate": row["win_rate"],
                    "ciclos":   row["ciclos"]
                }
            })
        cur.close()
        conn.close()
        return jsonify({"reports": reports, "total": len(reports)})
    except Exception as e:
        return jsonify({"reports": [], "total": 0, "error": str(e)})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
