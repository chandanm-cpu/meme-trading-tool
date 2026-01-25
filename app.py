from flask import Flask, request, jsonify, render_template_string
import requests, time, os, json, math, logging
from threading import Lock
from typing import Dict, Any

# ======================================================
# CONFIG
# ======================================================
MIN_LIQ_USD = 3000

CONF_INVEST = 80
CONF_BUY = 65

LOW_MC_MIN = 15000
LOW_MC_MAX = 80000

DECAY_CONF_DROP = 15
DECAY_MC_DROP = 0.12
CR_MAX = 60

CACHE_TTL = 120
STATE_SAVE_INTERVAL = 20

STATE_FILE = "state.json"
DEX_API = "https://api.dexscreener.com/latest/dex/tokens"

AUTO_REFRESH_MINUTES = 5

logging.basicConfig(level=logging.INFO)

# ======================================================
# HELPERS
# ======================================================
def safe_float(x, default=0.0):
    try:
        return float(x)
    except:
        return default

# ======================================================
# STATE
# ======================================================
class TradingState:
    def __init__(self):
        self.lock = Lock()
        self.last_save = 0
        self.memory: Dict[str, Dict[str, Any]] = {}
        self.alpha_history = []
        self.conf_history = {}
        self.load()

    def load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    d = json.load(f)
                    self.memory = d.get("memory", {})
                    self.alpha_history = d.get("alpha_history", [])
                    self.conf_history = d.get("conf_history", {})
            except:
                pass

    def save(self):
        if time.time() - self.last_save < STATE_SAVE_INTERVAL:
            return
        self.last_save = time.time()
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "memory": self.memory,
                "alpha_history": self.alpha_history,
                "conf_history": self.conf_history
            }, f)
        os.replace(tmp, STATE_FILE)

STATE = TradingState()

# ======================================================
# CACHE
# ======================================================
PAIR_CACHE = {}

def fetch_pair(ca):
    now = time.time()
    if ca in PAIR_CACHE:
        pair, ts = PAIR_CACHE[ca]
        if now - ts < CACHE_TTL:
            return pair

    try:
        r = requests.get(f"{DEX_API}/{ca}", timeout=8)
        if r.status_code != 200:
            return None
        data = r.json()
        pairs = data.get("pairs", [])
        if not pairs:
            return None
        best = max(pairs, key=lambda p: safe_float(p.get("liquidity", {}).get("usd")))
        PAIR_CACHE[ca] = (best, now)
        return best
    except:
        return None

# ======================================================
# METRICS
# ======================================================
def alpha_score(v5, tx5):
    return math.log(v5 + 1) * 0.6 + math.log(tx5 + 1) * 0.4

def alpha_percentile(alpha):
    recent = [a for t, a in STATE.alpha_history if time.time() - t <= 3600]
    if len(recent) < 15:
        return 50.0
    below = sum(1 for v in recent if v <= alpha)
    return round((below / len(recent)) * 100, 2)

def time_weighted_conf(hist):
    if not hist:
        return 0
    now = time.time()
    weights, values = [], []
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

HTML = f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Meme Scanner</title>
<style>
body {{ font-family: Arial; background:#0f172a; color:#e5e7eb; padding:16px }}
.card {{ background:#111827; padding:16px; border-radius:12px; margin-bottom:16px; border:2px solid transparent }}
.card.up {{ border-color:#16a34a }}
.card.down {{ border-color:#dc2626 }}
input,button {{ width:100%; padding:12px; font-size:16px; margin-top:8px }}
button {{ background:#2563eb; color:white; border:none; border-radius:8px }}
pre {{ white-space:pre-wrap }}
</style>
</head>
<body>

<div class="card">
<h3>Paste Contract Addresses</h3>
<input class="ca" placeholder="CA 1">
<input class="ca" placeholder="CA 2">
<input class="ca" placeholder="CA 3">
<input class="ca" placeholder="CA 4">
<input class="ca" placeholder="CA 5">
<button onclick="scanAll()">Scan Now</button>
<p>Auto refresh every {AUTO_REFRESH_MINUTES} minutes</p>
</div>

<div id="results"></div>

<script>
let lastResults = {{}};

async function scanAll() {{
  const cas = [];
  document.querySelectorAll(".ca").forEach(b => {{
    if (b.value.trim()) cas.push(b.value.trim());
  }});

  if (cas.length === 0) return;

  const r = await fetch("/scan_batch", {{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body: JSON.stringify({{cas}})
  }});
  const data = await r.json();

  const results = document.getElementById("results");
  results.innerHTML = "";

  data.forEach(d => {{
    const div = document.createElement("div");
    div.className = "card";

    if (d.status_changed) {{
      if (["INVEST","BUY"].includes(d.action)) div.classList.add("up");
      else div.classList.add("down");
    }}

    div.innerHTML = "<pre>"+JSON.stringify(d,null,2)+"</pre>";
    results.appendChild(div);
  }});
}}

setInterval(scanAll, {AUTO_REFRESH_MINUTES} * 60 * 1000);
</script>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

# ======================================================
# CORE SCAN
# ======================================================
def scan_single(ca: str):
    pair = fetch_pair(ca)
    if not pair:
        return {"ca": ca, "action":"WAIT", "reason":"NO_MARKET_DATA"}

    token_name = pair.get("baseToken",{}).get("name","Unknown")
    token_symbol = pair.get("baseToken",{}).get("symbol","NA")

    v5 = safe_float(pair.get("volume",{}).get("m5"))
    tx5 = sum(pair.get("txns",{}).get("m5",{}).values())
    liq = safe_float(pair.get("liquidity",{}).get("usd"))
    pc = safe_float(pair.get("priceChange",{}).get("m5"))

    if liq < MIN_LIQ_USD:
        return {"ca": ca, "action":"WAIT", "reason":"LOW_LIQUIDITY"}

    fdv = safe_float(pair.get("fdv"))
    mc = fdv if fdv > 0 else liq * 2
    low_mc = LOW_MC_MIN <= mc <= LOW_MC_MAX

    alpha = alpha_score(v5, tx5)

    with STATE.lock:
        STATE.alpha_history.append((time.time(),alpha))
        STATE.alpha_history = [(t,a) for t,a in STATE.alpha_history if time.time()-t <= 3600]

        raw_conf = alpha_percentile(alpha)
        hist = STATE.conf_history.get(ca,[])
        hist.append((time.time(),raw_conf))
        hist = hist[-10:]
        STATE.conf_history[ca] = hist
        conf = time_weighted_conf(hist)

        prev = STATE.memory.get(ca,{})
        prev_action = prev.get("action")

        decay = False
        if prev.get("mc") and mc < prev["mc"]*(1-DECAY_MC_DROP):
            decay = True
        if prev.get("conf") and prev["conf"]-conf >= DECAY_CONF_DROP:
            decay = True

        concentration = 0
        if pc < -15: concentration += 40
        if liq / max(v5,1) < 0.7: concentration += 20
        if low_mc: concentration += 10

        action = "WAIT"
        if not decay and conf >= CONF_INVEST and concentration < CR_MAX:
            action = "INVEST"
        elif not decay and conf >= CONF_BUY and concentration < CR_MAX:
            action = "BUY"

        status_changed = prev_action and prev_action != action

        STATE.memory[ca] = {
            "mc": mc,
            "conf": conf,
            "action": action
        }
        STATE.save()

    return {
        "ca": ca,
        "token": f"{token_name} ({token_symbol})",
        "action": action,
        "previous_action": prev_action,
        "status_changed": status_changed,
        "confidence_tw": conf,
        "market_cap": round(mc,2),
        "decay": decay
    }

@app.route("/scan_batch", methods=["POST"])
def scan_batch():
    cas = request.json.get("cas", [])
    results = [scan_single(ca) for ca in cas]
    return jsonify(results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
                      
