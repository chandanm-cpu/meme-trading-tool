from flask import Flask, request, jsonify
import requests
import time
import os
import json
import math
import logging
from typing import Dict, Any, Optional, Tuple
from threading import Lock

# ======================================================
# CONFIGURATION
# ======================================================
MIN_LIQ_USD = 3000

CONF_INVEST = 80
CONF_BUY = 65

CR_MAX = 60
DECAY_CONF_DROP = 15
DECAY_MC_DROP = 0.10      # 10% MC drop
LIQ_VOL_MIN = 0.7

LOW_MC_MIN = 20000
LOW_MC_MAX = 50000

INVEST_CONFIRM_GAP = 300   # seconds
STATE_SAVE_INTERVAL = 15
CACHE_TTL = 60

STATE_FILE = "state.json"
LOG_FILE = "app.log"
DEX_API = "https://api.dexscreener.com/latest/dex/tokens"

# ======================================================
# LOGGING
# ======================================================
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

# ======================================================
# HELPERS
# ======================================================
def safe_float(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default

# ======================================================
# STATE
# ======================================================
class TradingState:
    def __init__(self):
        self.lock = Lock()
        self.last_save = 0.0

        self.memory: Dict[str, Dict[str, Any]] = {}
        self.conf_history: Dict[str, list] = {}
        self.alpha_history: list = []   # (ts, alpha)

        self.load()

    def load(self):
        if not os.path.exists(STATE_FILE):
            return
        try:
            with open(STATE_FILE, "r") as f:
                d = json.load(f)
                self.memory = d.get("memory", {})
                self.conf_history = d.get("conf_history", {})
                self.alpha_history = d.get("alpha_history", [])
        except Exception as e:
            logging.error(f"State load failed: {e}")

    def maybe_save(self):
        now = time.time()
        if now - self.last_save < STATE_SAVE_INTERVAL:
            return
        self.last_save = now
        try:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump({
                    "memory": self.memory,
                    "conf_history": self.conf_history,
                    "alpha_history": self.alpha_history
                }, f)
            os.replace(tmp, STATE_FILE)
        except Exception as e:
            logging.error(f"State save failed: {e}")

STATE = TradingState()

# ======================================================
# CACHE
# ======================================================
PAIR_CACHE = {}

def cached_fetch_pair(ca: str):
    now = time.time()
    if ca in PAIR_CACHE:
        pair, ts = PAIR_CACHE[ca]
        if now - ts < CACHE_TTL:
            return pair, None

    try:
        r = requests.get(f"{DEX_API}/{ca}", timeout=8)
        if r.status_code != 200:
            return None, "HTTP_ERROR"

        data = r.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return None, "NO_MARKET"

        best = max(pairs, key=lambda p: safe_float(p.get("liquidity", {}).get("usd")))
        PAIR_CACHE[ca] = (best, now)
        return best, None
    except Exception:
        return None, "FETCH_FAILED"

# ======================================================
# METRICS
# ======================================================
def normalized_alpha(v5, tx5):
    return math.log(v5 + 1) * 0.6 + math.log(tx5 + 1) * 0.4

def alpha_percentile(alpha, history):
    recent = [a for t, a in history if time.time() - t <= 3600]
    if len(recent) < 15:
        return 50.0
    below = sum(1 for v in recent if v <= alpha)
    return round((below / len(recent)) * 100, 2)

def time_weighted_confidence(hist):
    if len(hist) < 2:
        return hist[-1][1]
    weights = []
    values = []
    now = time.time()
    for t, c in hist[-6:]:
        age = (now - t) / 1800
        w = math.exp(-3 * age)
        weights.append(w)
        values.append(c)
    return round(sum(w * v for w, v in zip(weights, values)) / sum(weights), 2)

# ======================================================
# FLASK APP
# ======================================================
app = Flask(__name__)

@app.route("/")
def home():
    return "Trading engine live. Use /scan?ca=CONTRACT_ADDRESS"

@app.route("/scan")
def scan():
    ca = request.args.get("ca", "").strip()
    if len(ca) < 30:
        return jsonify({"action": "WAIT", "reason": "INVALID_CA"})

    pair, err = cached_fetch_pair(ca)
    if err or not pair:
        return jsonify({"action": "WAIT", "reason": err})

    v5 = safe_float(pair.get("volume", {}).get("m5"))
    tx5 = safe_float(sum(pair.get("txns", {}).get("m5", {}).values()))
    liq = safe_float(pair.get("liquidity", {}).get("usd"))
    price_change = safe_float(pair.get("priceChange", {}).get("m5"))

    if liq < MIN_LIQ_USD:
        return jsonify({"action": "WAIT", "reason": "LOW_LIQUIDITY"})

    fdv = safe_float(pair.get("fdv"))
    mc = fdv if fdv > 0 else liq * 2
    low_mc = LOW_MC_MIN <= mc <= LOW_MC_MAX

    alpha = normalized_alpha(v5, tx5)

    with STATE.lock:
        STATE.alpha_history.append((time.time(), alpha))
        STATE.alpha_history = [(t, a) for t, a in STATE.alpha_history if time.time() - t <= 3600]

        raw_conf = alpha_percentile(alpha, STATE.alpha_history)

        hist = STATE.conf_history.get(ca, [])
        hist.append((time.time(), raw_conf))
        hist = hist[-10:]
        STATE.conf_history[ca] = hist

        conf = time_weighted_confidence(hist)

        prev = STATE.memory.get(ca, {})
        prev_mc = prev.get("mc")
        prev_conf = prev.get("conf")

        decay_alert = False
        if prev_mc and mc < prev_mc * (1 - DECAY_MC_DROP):
            decay_alert = True
        if prev_conf and prev_conf - conf >= DECAY_CONF_DROP:
            decay_alert = True

        concentration = 0
        if price_change < -15:
            concentration += 40
        if liq / max(v5, 1) < LIQ_VOL_MIN:
            concentration += 20
        if low_mc:
            concentration += 10

        action = "WAIT"

        if not decay_alert and conf >= CONF_INVEST and concentration < CR_MAX and mc >= (prev_mc or mc):
            action = "INVEST"
        elif not decay_alert and conf >= CONF_BUY and concentration < CR_MAX and not low_mc:
            action = "BUY"

        STATE.memory[ca] = {
            "alpha": alpha,
            "conf": conf,
            "mc": mc,
            "time": time.time()
        }

        STATE.maybe_save()

    return jsonify({
        "action": action,
        "confidence_raw": raw_conf,
        "confidence_time_weighted": conf,
        "alpha": round(alpha, 3),
        "market_cap": round(mc, 2),
        "low_mc_mode": low_mc,
        "concentration_score": concentration,
        "decay_alert": decay_alert,
        "entry_statement": (
            "Early accumulation with MC stability."
            if action == "INVEST"
            else "Momentum trade only."
            if action == "BUY"
            else "No entry. Demand not confirmed."
        ),
        "exit_statement": (
            "Exit immediately on MC drop or decay."
            if action in ["INVEST", "BUY"]
            else "Stay sidelined."
        )
    })

if __name__ == "__main__":
    logging.info("Starting trading engine")
    app.run(host="0.0.0.0", port=5000)
