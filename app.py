from flask import Flask, request, render_template_string
import requests
import time
import math
import os

app = Flask(__name__)

PREV = {}
SEEN = {}

TIER_A_TTL = 20 * 60
TIER_B_TTL = 40 * 60
TIER_C_TTL = 10 * 60

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>Low MC 100× Filter</title>
<style>
body { font-family: Arial; background:#0f172a; color:#e5e7eb; margin:0 }
.container { padding:16px; max-width:620px; margin:auto }
textarea { width:100%; height:120px; border-radius:8px; padding:10px }
button { width:100%; padding:12px; background:#22c55e; border:none; border-radius:8px; margin-top:10px }
.card { background:#1e293b; padding:12px; margin-top:12px; border-radius:10px }
.ca { font-size:12px; word-break:break-all; color:#94a3b8 }
.a { color:#4ade80 }
.b { color:#facc15 }
.c { color:#60a5fa }
.d { color:#f87171 }
.small { font-size:12px; color:#94a3b8 }
</style>
</head>
<body>
<div class="container">
<h2>🔍 Low MC 100× Structural Filter</h2>
<p class="small">Auto refresh: 10s • Focus: 20k–100k MC</p>

<form method="post">
<textarea name="cas">{{cas}}</textarea>
<button type="submit">Analyze</button>
</form>

{% for r in results %}
<div class="card">
<div class="{{r.cls}}"><b>{{ r.tier }}</b></div>
<div><b>{{ r.name }}</b> ({{ r.symbol }})</div>
<div class="ca">{{ r.ca }}</div>

<div class="small">MC: ${{ r.mc }} | Liquidity: ${{ r.liq }}</div>
<div class="small">Scarcity: {{ r.scarcity }}% | Demand: {{ r.demand }}%</div>
<div class="small">Demand Accel: {{ r.accel }} | Liquidity Δ: {{ r.liq_delta }}</div>

{% if r.ml_prob is not none %}
<div class="small">🧠 ML Extreme Prob: {{ r.ml_prob }}%</div>
{% endif %}

<div class="small">⏳ Time Remaining: {{ r.time_left }} min</div>
<div class="small">Buys / Sells (24h): {{ r.buys }} / {{ r.sells }}</div>
</div>
{% endfor %}
</div>
</body>
</html>
"""

def fetch(ca):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    r = requests.get(url, timeout=10)
    j = r.json()
    if "pairs" not in j or not j["pairs"]:
        return None

    p = j["pairs"][0]
    tx = p.get("txns", {}).get("h24", {})

    return {
        "name": p.get("baseToken", {}).get("name", "Unknown"),
        "symbol": p.get("baseToken", {}).get("symbol", "UNK"),
        "fdv": float(p.get("fdv") or 0),
        "liq": float(p.get("liquidity", {}).get("usd") or 0),
        "buys": tx.get("buys", 0),
        "sells": tx.get("sells", 0),
    }

def scarcity_score(fdv, liq, buys, sells):
    if fdv == 0:
        return 0
    s = 0
    ratio = liq / fdv
    if ratio < 0.10: s += 40
    if ratio < 0.05: s += 30
    if sells < buys * 0.8: s += 20
    if sells < 20: s += 10
    return min(s, 100)

def demand_score(buys, sells):
    d = 0
    total = buys + sells
    ratio = buys / max(sells, 1)
    if ratio > 1.3: d += 30
    if ratio > 1.6: d += 30
    if buys > 80: d += 20
    if total > 150: d += 20
    return min(d, 100)

def ml_probability(scarcity, demand, accel):
    x = 0.04 * scarcity + 0.05 * demand + 0.08 * accel - 10
    return round((1 / (1 + math.exp(-x))) * 100, 1)

@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    cas_text = request.form.get("cas", "")
    now = time.time()

    for ca in cas_text.splitlines():
        ca = ca.strip()
        if not ca:
            continue

        d = fetch(ca)
        if not d or d["fdv"] < 20000:
            continue

        mc = int(d["fdv"])
        sc = scarcity_score(d["fdv"], d["liq"], d["buys"], d["sells"])
        dm = demand_score(d["buys"], d["sells"])

        prev = PREV.get(ca)
        accel = dm - prev["demand"] if prev else 0
        liq_delta = d["liq"] - prev["liq"] if prev else 0
        PREV[ca] = {"demand": dm, "liq": d["liq"]}

        if ca not in SEEN:
            SEEN[ca] = now
        age = now - SEEN[ca]

        if mc < 100000:
            sc_t, dm_t = 75, 50
        else:
            sc_t, dm_t = 70, 70

        ml_prob = None
        if sc >= sc_t and dm >= dm_t and accel >= 15 and liq_delta >= 0:
            tier, cls, ttl = "🚀 Tier A — Structural + Acceleration", "a", TIER_A_TTL
            ml_prob = ml_probability(sc, dm, accel)
        elif sc >= sc_t:
            tier, cls, ttl = "👀 Tier B — Scarcity (waiting demand)", "b", TIER_B_TTL
        elif dm >= dm_t:
            tier, cls, ttl = "👀 Tier C — Demand (weak structure)", "c", TIER_C_TTL
        else:
            tier, cls, ttl = "❌ Tier D — Noise / Avoid", "d", 0

        if ttl > 0 and age > ttl:
            tier, cls = "⌛ EXPIRED — Missed Window", "d"
            ml_prob = None

        time_left = max(0, int((ttl - age) / 60)) if ttl > 0 else 0

        results.append({
            "ca": ca,
            "name": d["name"],
            "symbol": d["symbol"],
            "mc": mc,
            "liq": int(d["liq"]),
            "scarcity": sc,
            "demand": dm,
            "accel": accel,
            "liq_delta": int(liq_delta),
            "buys": d["buys"],
            "sells": d["sells"],
            "tier": tier,
            "cls": cls,
            "ml_prob": ml_prob,
            "time_left": time_left,
        })

    return render_template_string(HTML, results=results, cas=cas_text)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
