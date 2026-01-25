from flask import Flask, request, jsonify, render_template_string
import requests, time, math, json, os
from threading import Lock

# ================= CONFIG =================
DEX_API = "https://api.dexscreener.com/latest/dex/tokens"

MIN_LIQ_USD = 2500
LOW_MC_MAX = 80000

CONF_BUY = 70
CONF_INVEST = 55
WEIRD_MOONSHOT = 75

CACHE_TTL = 90
AUTO_REFRESH_MINUTES = 5   # <<< CHANGE THIS IF YOU WANT

STATE_FILE = "state.json"

# ================= UTILS =================
def safe_float(x, d=0.0):
    try:
        return float(x)
    except:
        return d

# ================= STATE =================
class State:
    def __init__(self):
        self.lock = Lock()
        self.alpha_hist = []
        self.weird_hist = []
        self.social_hist = []
        self.load()

    def load(self):
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE) as f:
                    d = json.load(f)
                    self.alpha_hist = d.get("alpha", [])
                    self.weird_hist = d.get("weird", [])
                    self.social_hist = d.get("social", [])
            except:
                pass

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump({
                "alpha": self.alpha_hist[-1000:],
                "weird": self.weird_hist[-1000:],
                "social": self.social_hist[-1000:]
            }, f)

STATE = State()
CACHE = {}

# ================= DATA =================
def fetch_pair(ca):
    now = time.time()
    if ca in CACHE and now - CACHE[ca][1] < CACHE_TTL:
        return CACHE[ca][0]

    try:
        r = requests.get(f"{DEX_API}/{ca}", timeout=8)
        if r.status_code != 200:
            return None
        pairs = r.json().get("pairs", [])
        if not pairs:
            return None
        best = max(pairs, key=lambda p: safe_float(p.get("liquidity", {}).get("usd")))
        CACHE[ca] = (best, now)
        return best
    except:
        return None

# ================= METRICS =================
def alpha(v, tx):
    return math.log(v + 1) * 0.6 + math.log(tx + 1) * 0.4

def percentile(x, arr):
    if len(arr) < 20:
        return 50.0
    return round(sum(1 for v in arr if v <= x) / len(arr) * 100, 2)

def weirdness(v5, v30, tx5, liq, mc):
    s = 0
    if v30 > 0 and v5 / v30 > 3: s += 30
    if tx5 > 15 and liq / max(v5,1) < 1: s += 25
    if mc < 50000 and v5 > 3000: s += 25
    if liq < mc * 0.4: s += 20
    return min(s, 100)

def social_velocity(v5, v30, tx5, liq):
    s = 0
    if v30 > 0 and v5 / v30 > 2: s += 40
    if tx5 > 20: s += 30
    if liq > 0 and tx5 / liq > 0.002: s += 30
    return min(s, 100)

# ================= DECISION =================
def decide(conf, weird, social, mc):
    if weird >= WEIRD_MOONSHOT and social >= 60 and mc < LOW_MC_MAX:
        return "MOONSHOT", "0.25–0.5%", "High variance optionality"
    if conf >= CONF_BUY:
        return "BUY", "2–5%", "Momentum trade"
    if conf >= CONF_INVEST:
        return "INVEST", "1–2%", "Early structure"
    return "WAIT", "0%", "Ignore"

# ================= FLASK =================
app = Flask(__name__)

HTML = f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Meme Scanner</title>
<style>
body {{ background:#0f172a; color:#e5e7eb; font-family:sans-serif; padding:16px }}
.card {{ background:#111827; padding:16px; border-radius:12px; margin-bottom:12px }}
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
async function scanAll() {{
  const boxes = document.querySelectorAll(".ca");
  const results = document.getElementById("results");
  results.innerHTML = "";

  for (let box of boxes) {{
    const ca = box.value.trim();
    if (!ca) continue;

    const r = await fetch("/scan?ca=" + ca);
    const d = await r.json();

    const div = document.createElement("div");
    div.className = "card";
    div.innerHTML = "<pre>" + JSON.stringify(d, null, 2) + "</pre>";
    results.appendChild(div);
  }}
}}

setInterval(scanAll, {AUTO_REFRESH_MINUTES} * 60 * 1000);
</script>

</body>
</html>
"""

@app.route("/")
def home():
    return render_template_string(HTML)

@app.route("/scan")
def scan():
    ca = request.args.get("ca", "").strip()
    if len(ca) < 30:
        return jsonify({"error": "INVALID_CA"})

    pair = fetch_pair(ca)
    if not pair:
        return jsonify({"action": "WAIT", "reason": "NO_DATA"})

    v5 = safe_float(pair.get("volume", {}).get("m5"))
    v30 = safe_float(pair.get("volume", {}).get("m30"))
    tx5 = sum(pair.get("txns", {}).get("m5", {}).values())
    liq = safe_float(pair.get("liquidity", {}).get("usd"))
    fdv = safe_float(pair.get("fdv"))
    mc = fdv if fdv > 0 else liq * 2

    if liq < MIN_LIQ_USD:
        return jsonify({"action": "WAIT", "reason": "LOW_LIQUIDITY"})

    a = alpha(v5, tx5)
    w = weirdness(v5, v30, tx5, liq, mc)
    s = social_velocity(v5, v30, tx5, liq)

    with STATE.lock:
        STATE.alpha_hist.append(a)
        STATE.weird_hist.append(w)
        STATE.social_hist.append(s)

        conf = percentile(a, STATE.alpha_hist)
        action, size, note = decide(conf, w, s, mc)
        STATE.save()

    return jsonify({
        "token": pair.get("baseToken", {}).get("name", "Unknown"),
        "action": action,
        "suggested_position": size,
        "confidence": conf,
        "weirdness": w,
        "social_velocity": s,
        "market_cap": round(mc, 2),
        "note": note
    })

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
    
