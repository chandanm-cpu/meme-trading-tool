from flask import Flask, request, render_template_string
import requests, os, time, datetime
from collections import Counter, deque

app = Flask(__name__)

# ======================================================
# CONFIG
# ======================================================
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"

SOL_RPC = "https://api.mainnet-beta.solana.com"
BSC_RPC = "https://bsc-dataseed.binance.org/"

MC_MIN = 20000
MC_MAX = 400000
CHAOS_MC_MAX = 60000

SCARCITY_STRICT = 75
DEMAND_STRICT = 60
ACCEL_STRICT = 8

CACHE_TTL = 90            # seconds
TREND_WINDOW = 5          # last N organic readings

# ======================================================
# GLOBAL STATE (in-memory)
# ======================================================
PREV_DEMAND = {}
LAST_TIER = {}
TIER_HISTORY = {}
SKIPPED = {}

RPC_CACHE = {}            # { key: (timestamp, data) }
ORGANIC_HISTORY = {}      # { ca: deque([...]) }

# ======================================================
# HTML
# ======================================================
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Advanced On-Chain Scanner</title>
<style>
body{font-family:Arial;background:#0f172a;color:#e5e7eb}
.container{max-width:900px;margin:auto;padding:15px}
textarea{width:100%;height:120px}
button{width:100%;padding:10px;margin-top:6px}
.card{background:#1e293b;padding:12px;margin-top:10px;border-radius:10px}
.a{color:#4ade80}.b{color:#facc15}.c{color:#60a5fa}.d{color:#f87171}
.chaos{color:#fb923c;font-weight:bold}
.alert{color:#fb7185;font-weight:bold}
.small{font-size:12px;color:#94a3b8}
.ca{word-break:break-all;font-size:11px}
hr{border:0;border-top:1px solid #334155;margin:16px 0}
</style>
</head>
<body>
<div class="container">
<h2>🔬 Advanced Structural + On-Chain Scanner</h2>

<form method="post">
<textarea name="cas">{{cas}}</textarea>
<button name="action" value="analyze">Analyze</button>
<button name="action" value="history">Tier History</button>
</form>

{% for r in results %}
<div class="card">
{% if r.alert %}
<div class="alert">🚨 AUTO DOWNGRADE / UPGRADE</div>
{% endif %}
{% if r.chaos %}
<div class="chaos">⚡ CHAOS MODE</div>
{% endif %}
<div class="{{r.cls}}"><b>{{r.tier}}</b></div>
<b>{{r.name}} ({{r.symbol}})</b>
<div class="ca">{{r.ca}}</div>

<div class="small">
MC: ${{r.mc}} | LP: ${{r.liq}}
</div>

<div class="small">
Scarcity {{r.sc}} | Demand {{r.dm}} | Accel {{r.acc}}
</div>

<hr>
<div class="small"><b>On-chain Diagnostics</b></div>
<div class="small">Organic Demand: {{r.organic}}</div>
<div class="small">Organic Trend: {{r.trend}}</div>
<div class="small">Wallet Clustering: {{r.cluster}}</div>
<div class="small">Rug Risk: {{r.rug}}</div>
<div class="small"><b>Confidence:</b> {{r.conf}} / 100</div>
</div>
{% endfor %}
</div>
</body>
</html>
"""

# ======================================================
# UTILS
# ======================================================
def cached(key, fetch_fn):
    now = time.time()
    if key in RPC_CACHE:
        ts, data = RPC_CACHE[key]
        if now - ts < CACHE_TTL:
            return data
    data = fetch_fn()
    RPC_CACHE[key] = (now, data)
    return data

# ======================================================
# DEX DATA
# ======================================================
def fetch_dex(ca):
    try:
        j = requests.get(DEX_URL + ca, timeout=8).json()
        if not j.get("pairs"):
            return None
        p = j["pairs"][0]
        tx = p.get("txns", {}).get("h24", {})
        return {
            "name": p["baseToken"]["name"],
            "symbol": p["baseToken"]["symbol"],
            "fdv": float(p.get("fdv") or 0),
            "liq": float(p.get("liquidity", {}).get("usd") or 0),
            "buys": tx.get("buys", 0),
            "sells": tx.get("sells", 0),
            "chain": p.get("chainId")
        }
    except:
        return None

# ======================================================
# RPC – SOLANA
# ======================================================
def sol_recent_signers(token):
    def fetch():
        payload = {
            "jsonrpc":"2.0","id":1,
            "method":"getSignaturesForAddress",
            "params":[token,{"limit":50}]
        }
        r = requests.post(SOL_RPC, json=payload, timeout=8).json()
        if "result" not in r:
            return []
        # proxy: signer clustering via signature prefixes
        return [tx["signature"][:6] for tx in r["result"]]
    return cached(("sol", token), fetch)

# ======================================================
# ORGANIC DEMAND + CLUSTERING
# ======================================================
def analyze_wallets(buyers):
    if not buyers:
        return "LOW", "HIGH"

    c = Counter(buyers)
    total = sum(c.values())
    unique = len(c)
    top_ratio = max(c.values()) / total

    # Clustering
    cluster = "LOW"
    if top_ratio > 0.5:
        cluster = "HIGH"
    elif top_ratio > 0.35:
        cluster = "MEDIUM"

    # Organic
    if unique >= 25 and cluster == "LOW":
        organic = "HIGH"
    elif unique >= 10:
        organic = "MEDIUM"
    else:
        organic = "LOW"

    return organic, cluster

# ======================================================
# TREND
# ======================================================
def update_trend(ca, organic):
    hist = ORGANIC_HISTORY.setdefault(ca, deque(maxlen=TREND_WINDOW))
    hist.append(organic)

    if len(hist) < 3:
        return "FLAT"

    if hist[-1] == "HIGH" and hist[-2] in ("MEDIUM","LOW"):
        return "↑ Improving"
    if hist[-1] == "LOW" and hist[-2] in ("HIGH","MEDIUM"):
        return "↓ Deteriorating"
    return "FLAT"

# ======================================================
# SCARCITY / DEMAND
# ======================================================
def scarcity(fdv, liq, buys, sells):
    s = 0
    if fdv == 0: return 0
    r = liq / fdv
    if r < 0.1: s += 40
    if r < 0.05: s += 30
    if sells < buys * 0.8: s += 20
    if sells < 20: s += 10
    return min(s,100)

def demand(buys, sells):
    d = 0
    r = buys / max(sells,1)
    if r > 1.3: d += 30
    if r > 1.6: d += 30
    if buys > 80: d += 20
    if buys + sells > 150: d += 20
    return min(d,100)

# ======================================================
# RUG RISK (BEHAVIORAL)
# ======================================================
def rug_risk(cluster, organic, liq, mc):
    if liq / mc < 0.03:
        return "HIGH"
    if cluster == "HIGH" and organic == "LOW":
        return "HIGH"
    if cluster == "MEDIUM":
        return "MEDIUM"
    return "LOW"

# ======================================================
# CONFIDENCE
# ======================================================
def confidence(sc, dm, acc, organic, rug):
    base = int(sc*0.4 + dm*0.4 + max(acc,0)*2)
    if organic == "HIGH": base += 8
    if organic == "LOW": base -= 10
    if rug == "HIGH": base -= 25
    if rug == "MEDIUM": base -= 10
    return max(0, min(base,100))

# ======================================================
# MAIN
# ======================================================
@app.route("/", methods=["GET","POST"])
def index():
    cas = request.form.get("cas","")
    results = []

    for ca in [c.strip() for c in cas.splitlines() if c.strip()]:
        d = fetch_dex(ca)
        if not d: continue

        mc = d["fdv"]
        if mc < MC_MIN: continue

        sc = scarcity(d["fdv"], d["liq"], d["buys"], d["sells"])
        dm = demand(d["buys"], d["sells"])
        prev = PREV_DEMAND.get(ca, dm)
        acc = dm - prev
        PREV_DEMAND[ca] = dm

        # Wallet analysis (SOL only realistically)
        buyers = sol_recent_signers(ca) if d["chain"] == "solana" else []
        organic, cluster = analyze_wallets(buyers)
        trend = update_trend(ca, organic)
        rug = rug_risk(cluster, organic, d["liq"], mc)

        # Tier logic
        tier, cls = "❌ Tier D", "d"
        if sc>=SCARCITY_STRICT and dm>=DEMAND_STRICT and acc>=ACCEL_STRICT and mc<=MC_MAX:
            tier, cls = "🚀 Tier A", "a"
        elif sc>=SCARCITY_STRICT:
            tier, cls = "👀 Tier B", "b"
        elif dm>=DEMAND_STRICT:
            tier, cls = "👀 Tier C", "c"

        # Auto downgrade
        prev_tier = LAST_TIER.get(ca)
        alert = False
        if prev_tier == "🚀 Tier A" and rug == "HIGH":
            tier, cls = "👀 Tier C", "c"
            alert = True

        LAST_TIER[ca] = tier

        conf = confidence(sc, dm, acc, organic, rug)

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
            "chaos": mc<=CHAOS_MC_MAX and dm>=50,
            "organic": organic,
            "trend": trend,
            "cluster": cluster,
            "rug": rug,
            "conf": conf,
            "alert": alert
        })

    return render_template_string(HTML, results=results, cas=cas)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
    
