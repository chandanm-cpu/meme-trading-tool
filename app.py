from flask import Flask, request, render_template_string
import requests, time, math, os, datetime

app = Flask(__name__)

PREV = {}
LAST_TIER = {}
LAST_SKIPPED = {}
TIER_HISTORY = []   # 👈 stores tier transitions

TIER_A_TTL = 20 * 60
TIER_B_TTL = 40 * 60
TIER_C_TTL = 10 * 60

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Structural Monitor</title>
<style>
body { font-family: Arial; background:#0f172a; color:#e5e7eb }
.container { max-width:820px; margin:auto; padding:15px }
textarea { width:100%; height:120px }
button { width:100%; padding:10px; margin-top:8px }
.card { background:#1e293b; padding:12px; margin-top:10px; border-radius:10px }
.a{color:#4ade80} .b{color:#facc15} .c{color:#60a5fa} .d{color:#f87171}
.alert{color:#fb7185;font-weight:bold}
.small{font-size:12px;color:#94a3b8}
.ca{font-size:11px;word-break:break-all}
hr{border:0;border-top:1px solid #334155;margin:20px 0}
</style>
</head>
<body>
<div class="container">
<h2>🔍 Structural Monitor</h2>

<form method="post">
<textarea name="cas">{{cas}}</textarea>
<button name="action" value="analyze">Analyze</button>
<button name="action" value="recheck">Recheck Skipped</button>
<button name="action" value="history">Show Tier Changes</button>
</form>

{% if action == "history" %}
<hr>
<h3>📜 Tier Change History</h3>
{% if history %}
{% for h in history %}
<div class="card">
<div class="small"><b>CA:</b> {{h.ca}}</div>
<div class="small">From: {{h.prev}} → To: {{h.curr}}</div>
<div class="small">Time: {{h.time}}</div>
</div>
{% endfor %}
{% else %}
<div class="small">No tier changes recorded yet.</div>
{% endif %}
{% endif %}

{% if action != "history" %}
{% for r in results %}
<div class="card">
{% if r.alert %}
<div class="alert">🚨 ALERT: Tier B → Tier A</div>
{% endif %}
<div class="{{r.cls}}"><b>{{r.tier}}</b></div>
<b>{{r.name}} ({{r.symbol}})</b>
<div class="ca">{{r.ca}}</div>
<div class="small">MC: ${{r.mc}} | Liq: ${{r.liq}}</div>
<div class="small">Scarcity: {{r.sc}} | Demand: {{r.dm}} | Accel: {{r.acc}}</div>
</div>
{% endfor %}
{% endif %}

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

def fetch_dex(ca):
    try:
        j = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
            timeout=8
        ).json()
        if not j.get("pairs"):
            return None, "No pair yet"

        p = j["pairs"][0]
        tx = p.get("txns", {}).get("h24", {})

        return {
            "name": p["baseToken"]["name"],
            "symbol": p["baseToken"]["symbol"],
            "fdv": float(p.get("fdv") or 0),
            "liq": float(p.get("liquidity", {}).get("usd") or 0),
            "buys": tx.get("buys", 0),
            "sells": tx.get("sells", 0),
        }, None
    except:
        return None, "API error"

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

    targets = [c.strip() for c in cas.splitlines() if c.strip()]

    for ca in targets:
        d, err = fetch_dex(ca)
        if err:
            skipped.append({"ca": ca, "reason": err})
            continue

        if d["fdv"] < 20000 or d["fdv"] > 400000:
            skipped.append({"ca": ca, "reason": "MC outside range"})
            continue

        sc = scarcity(d["fdv"], d["liq"], d["buys"], d["sells"])
        dm = demand(d["buys"], d["sells"])
        acc = dm - PREV.get(ca, {"dm": dm})["dm"]
        PREV[ca] = {"dm": dm}

        if sc >= 75 and dm >= 50 and acc >= 15:
            tier, cls = "🚀 Tier A", "a"
        elif sc >= 75:
            tier, cls = "👀 Tier B", "b"
        elif dm >= 60:
            tier, cls = "👀 Tier C", "c"
        else:
            tier, cls = "❌ Tier D", "d"

        prev = LAST_TIER.get(ca)
        if prev and prev != tier:
            TIER_HISTORY.append({
                "ca": ca,
                "prev": prev,
                "curr": tier,
                "time": datetime.datetime.now().strftime("%H:%M:%S")
            })

        LAST_TIER[ca] = tier

        alert = prev == "👀 Tier B" and tier == "🚀 Tier A"

        results.append({
            "ca": ca,
            "name": d["name"],
            "symbol": d["symbol"],
            "mc": int(d["fdv"]),
            "liq": int(d["liq"]),
            "sc": sc,
            "dm": dm,
            "acc": acc,
            "tier": tier,
            "cls": cls,
            "alert": alert
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
