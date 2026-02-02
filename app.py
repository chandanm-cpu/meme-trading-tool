from flask import Flask, request, render_template_string
import requests, time, os, datetime

app = Flask(__name__)

# ---------------- STATE ----------------
PREV_DEMAND = {}
LAST_TIER = {}
LAST_SKIPPED = {}
TIER_HISTORY = []

# ---------------- CONFIG ----------------
MC_MIN = 20000
MC_MAX = 400000

# Loosened Tier A (structural)
ACCEL_STRICT = 8        # was 15
DEMAND_STRICT = 60
SCARCITY_STRICT = 75

# Chaos mode
CHAOS_MC_MAX = 60000

# ---------------- HTML ----------------
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Structural + Chaos Scanner</title>
<style>
body{font-family:Arial;background:#0f172a;color:#e5e7eb;margin:0}
.container{max-width:820px;margin:auto;padding:15px}
textarea{width:100%;height:120px;border-radius:6px;padding:8px}
button{width:100%;padding:10px;margin-top:8px;border-radius:6px}
.card{background:#1e293b;padding:12px;margin-top:12px;border-radius:10px}
.a{color:#4ade80}.b{color:#facc15}.c{color:#60a5fa}.d{color:#f87171}
.chaos{color:#fb923c;font-weight:bold}
.alert{color:#fb7185;font-weight:bold}
.small{font-size:12px;color:#94a3b8}
.ca{word-break:break-all;font-size:11px;color:#94a3b8}
hr{border:0;border-top:1px solid #334155;margin:20px 0}
</style>
</head>
<body>
<div class="container">
<h2>🔍 Structural + Chaos Scanner</h2>

<form method="post">
<textarea name="cas">{{cas}}</textarea>
<button name="action" value="analyze">Analyze</button>
<button name="action" value="recheck">Recheck Skipped</button>
<button name="action" value="history">Show Tier Changes</button>
</form>

{% if action == "history" %}
<hr>
<h3>📜 Tier Change History</h3>
{% for h in history %}
<div class="card small">
<b>{{h.ca}}</b><br>
{{h.prev}} → {{h.curr}}<br>
{{h.time}}
</div>
{% endfor %}
{% endif %}

{% if action != "history" %}
{% for r in results %}
<div class="card">
{% if r.alert %}
<div class="alert">🚨 ALERT: Tier B → Tier A</div>
{% endif %}
{% if r.chaos %}
<div class="chaos">⚡ CHAOS MODE (High Risk)</div>
{% endif %}
<div class="{{r.cls}}"><b>{{r.tier}}</b></div>
<b>{{r.name}} ({{r.symbol}})</b>
<div class="ca">{{r.ca}}</div>
<div class="small">MC: ${{r.mc}} | Liquidity: ${{r.liq}}</div>
<div class="small">Scarcity: {{r.sc}} | Demand: {{r.dm}} | Accel: {{r.acc}}</div>
</div>
{% endfor %}
{% endif %}

{% if skipped %}
<hr>
<h3>⚠️ Skipped Coins</h3>
{% for s in skipped %}
<div class="card small">
<b>{{s.ca}}</b><br>
Reason: {{s.reason}}
</div>
{% endfor %}
{% endif %}
</div>
</body>
</html>
"""

# ---------------- DATA ----------------
def fetch_dex(ca):
    try:
        j = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
            timeout=8
        ).json()

        if not j.get("pairs"):
            return None, "No DexScreener pair yet"

        p = j["pairs"][0]
        tx = p.get("txns", {}).get("h24", {})

        if p.get("fdv") is None or p.get("liquidity", {}).get("usd") is None:
            return None, "Missing market or liquidity data"

        return {
            "name": p["baseToken"]["name"],
            "symbol": p["baseToken"]["symbol"],
            "fdv": float(p["fdv"]),
            "liq": float(p["liquidity"]["usd"]),
            "buys": tx.get("buys", 0),
            "sells": tx.get("sells", 0),
        }, None
    except:
        return None, "API error / rate limit"

# ---------------- SCORES ----------------
def scarcity(fdv, liq, buys, sells):
    s = 0
    r = liq / fdv if fdv else 1
    if r < 0.10: s += 40
    if r < 0.05: s += 30
    if sells < buys * 0.8: s += 20
    if sells < 20: s += 10
    return min(s, 100)

def demand(buys, sells):
    d = 0
    r = buys / max(sells, 1)
    if r > 1.3: d += 30
    if r > 1.6: d += 30
    if buys > 80: d += 20
    if buys + sells > 150: d += 20
    return min(d, 100)

# ---------------- MAIN ----------------
@app.route("/", methods=["GET","POST"])
def index():
    action = request.form.get("action", "analyze")
    cas = request.form.get("cas", "")
    results, skipped = [], []

    if action == "history":
        return render_template_string(
            HTML,
            action=action,
            history=TIER_HISTORY[::-1],
            cas=cas
        )

    targets = list(LAST_SKIPPED.keys()) if action == "recheck" else [
        c.strip() for c in cas.splitlines() if c.strip()
    ]

    for ca in targets:
        d, err = fetch_dex(ca)
        if err:
            skipped.append({"ca": ca, "reason": err})
            LAST_SKIPPED[ca] = err
            continue

        mc = d["fdv"]
        if mc < MC_MIN:
            skipped.append({"ca": ca, "reason": "MC below 20k"})
            LAST_SKIPPED[ca] = "MC below 20k"
            continue

        LAST_SKIPPED.pop(ca, None)

        sc = scarcity(d["fdv"], d["liq"], d["buys"], d["sells"])
        dm = demand(d["buys"], d["sells"])
        prev_dm = PREV_DEMAND.get(ca, dm)
        acc = dm - prev_dm
        PREV_DEMAND[ca] = dm

        chaos = mc <= CHAOS_MC_MAX and dm >= 50 and acc >= 5 and sc < SCARCITY_STRICT

        tier, cls = "❌ Tier D", "d"
        if sc >= SCARCITY_STRICT and dm >= DEMAND_STRICT and acc >= ACCEL_STRICT and mc <= MC_MAX:
            tier, cls = "🚀 Tier A", "a"
        elif sc >= SCARCITY_STRICT:
            tier, cls = "👀 Tier B", "b"
        elif dm >= DEMAND_STRICT:
            tier, cls = "👀 Tier C", "c"

        prev = LAST_TIER.get(ca)
        if prev and prev != tier:
            TIER_HISTORY.append({
                "ca": ca,
                "prev": prev,
                "curr": tier,
                "time": datetime.datetime.now().strftime("%H:%M:%S")
            })

        alert = prev == "👀 Tier B" and tier == "🚀 Tier A"
        LAST_TIER[ca] = tier

        results.append({
            "ca": ca,
            "name": d["name"],
            "symbol": d["symbol"],
            "mc": int(mc),
            "liq": int(d["liq"]),
            "sc": sc,
            "dm": dm,
            "acc": acc,
            "tier": tier,
            "cls": cls,
            "alert": alert,
            "chaos": chaos
        })

    return render_template_string(
        HTML,
        action=action,
        results=results,
        skipped=skipped,
        cas=cas
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
