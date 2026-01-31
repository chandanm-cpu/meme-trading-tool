from flask import Flask, request, render_template_string
import requests, time, math, os

app = Flask(__name__)

PREV = {}
SEEN = {}
LAST_SKIPPED = {}
LAST_TIER = {}   # 🔔 used for B → A alert

TIER_A_TTL = 20 * 60
TIER_B_TTL = 40 * 60
TIER_C_TTL = 10 * 60

AUTO_RECHECK_SECONDS = 120

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>20k–400k MC Structural Filter</title>
<style>
body { font-family: Arial; background:#0f172a; color:#e5e7eb; margin:0 }
.container { max-width:780px; margin:auto; padding:15px }
textarea { width:100%; height:120px; border-radius:6px; padding:8px }
button { width:100%; padding:10px; margin-top:8px; border-radius:6px }
.card { background:#1e293b; padding:12px; margin-top:12px; border-radius:10px }
.a{color:#4ade80} .b{color:#facc15} .c{color:#60a5fa} .d{color:#f87171}
.alert{color:#fb7185;font-weight:bold}
.new{color:#22c55e;font-weight:bold}
.small{font-size:12px;color:#94a3b8}
.ca{word-break:break-all;font-size:11px;color:#94a3b8}
ul{margin:6px 0 0 16px;padding:0}
hr{border:0;border-top:1px solid #334155;margin:20px 0}
</style>
</head>
<body>
<div class="container">
<h2>🔍 Structural Filter (20k–400k MC)</h2>

<form method="post">
<textarea name="cas">{{cas}}</textarea>
<button name="action" value="analyze">Analyze</button>
<button name="action" value="recheck">Recheck Skipped Coins</button>
</form>

{% for r in results %}
<div class="card">
{% if r.alert %}
<div class="alert">🚨 ALERT: Tier B → Tier A</div>
{% endif %}
{% if r.new %}
<div class="new">🆕 BECAME ELIGIBLE</div>
{% endif %}
<div class="{{r.cls}}"><b>{{r.tier}}</b></div>
<b>{{r.name}} ({{r.symbol}})</b>
<div class="ca">{{r.ca}}</div>

<div class="small">MC: ${{r.mc}} | Liquidity: ${{r.liq}}</div>
<div class="small">Scarcity: {{r.sc}} | Demand: {{r.dm}} | Accel: {{r.acc}}</div>

{% if r.conf %}
<div class="small"><b>Confidence:</b> {{r.conf}} / 100</div>
{% endif %}

{% if r.why %}
<div class="small"><b>Why this tier:</b></div>
<ul class="small">
{% for w in r.why %}<li>{{w}}</li>{% endfor %}
</ul>
{% endif %}
</div>
{% endfor %}

{% if skipped %}
<hr>
<h3>⚠️ Skipped Coins</h3>
{% for s in skipped %}
<div class="card">
<div class="d"><b>Skipped</b></div>
<div class="ca">{{s.ca}}</div>
<div class="small">Reason: {{s.reason}}</div>
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
            return None, "No active DexScreener pair yet"

        p = j["pairs"][0]
        tx = p.get("txns", {}).get("h24", {})

        if p.get("fdv") is None:
            return None, "Market cap data unavailable"
        if p.get("liquidity", {}).get("usd") is None:
            return None, "Liquidity data unavailable"
        if not tx:
            return None, "Buy/Sell data unavailable"

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
    r = liq / fdv
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

def confidence(sc, dm, acc, st, dt, at):
    return min(100, int((sc/st)*30 + (dm/dt)*40 + (acc/at)*30))

# ---------------- MAIN ----------------
@app.route("/", methods=["GET","POST"])
def index():
    action = request.form.get("action", "analyze")
    cas = request.form.get("cas", "")

    results, skipped = [], []
    targets = list(LAST_SKIPPED.keys()) if action == "recheck" else [c.strip() for c in cas.splitlines() if c.strip()]

    for ca in targets:
        d, err = fetch_dex(ca)
        if err:
            skipped.append({"ca": ca, "reason": err})
            LAST_SKIPPED[ca] = err
            continue

        if d["fdv"] < 20000 or d["fdv"] > 400000:
            skipped.append({"ca": ca, "reason": "Market cap outside 20k–400k"})
            LAST_SKIPPED[ca] = "MC outside range"
            continue

        LAST_SKIPPED.pop(ca, None)

        sc = scarcity(d["fdv"], d["liq"], d["buys"], d["sells"])
        dm = demand(d["buys"], d["sells"])
        acc = dm - PREV.get(ca, {"dm": dm})["dm"]
        PREV[ca] = {"dm": dm}

        mc = d["fdv"]
        if mc <= 100000:
            st, dt, at = 75, 50, 15
        elif mc <= 250000:
            st, dt, at = 80, 65, 20
        else:
            st, dt, at = 85, 75, 25

        tier, cls, why, conf = "❌ Tier D", "d", None, None

        if sc >= st and dm >= dt and acc >= at:
            tier, cls = "🚀 Tier A", "a"
            conf = confidence(sc, dm, acc, st, dt, at)
            why = ["Scarcity + demand + acceleration aligned"]
        elif sc >= st:
            tier, cls = "👀 Tier B", "b"
            why = ["Scarcity present, demand pending"]
        elif dm >= dt:
            tier, cls = "👀 Tier C", "c"
            why = ["Demand present, structure weak"]

        alert = LAST_TIER.get(ca) == "👀 Tier B" and tier == "🚀 Tier A"
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
            "why": why,
            "conf": conf,
            "alert": alert,
            "new": False
        })

    return render_template_string(HTML, results=results, skipped=skipped, cas=cas)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
