from flask import Flask, request, render_template_string
import requests, os, time, datetime
from collections import Counter, deque

app = Flask(__name__)

# =====================================================
# CONFIG
# =====================================================
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
SOL_RPC = "https://api.mainnet-beta.solana.com"

MC_MIN = 20000
MC_MAX = 400000
CHAOS_MC_MAX = 60000

LMC_MIN = 3      # %
LMC_MAX = 10     # %

BUYSELL_STRONG = 1.3
ACCEL_STRONG = 8

CACHE_TTL = 90
TREND_WINDOW = 5

# =====================================================
# GLOBAL STATE
# =====================================================
PREV_BUYS = {}
LAST_TIER = {}
TIER_HISTORY = {}
RPC_CACHE = {}
ORGANIC_HISTORY = {}
LAST_LIQ = {}

# =====================================================
# HTML
# =====================================================
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>Advanced Tier Engine</title>
<style>
body{font-family:Arial;background:#0f172a;color:#e5e7eb}
.container{max-width:920px;margin:auto;padding:15px}
textarea{width:100%;height:120px}
button{width:100%;padding:10px;margin-top:6px}
.card{background:#1e293b;padding:12px;margin-top:10px;border-radius:10px}
.early{color:#60a5fa}
.confirmed{color:#4ade80}
.b{color:#facc15}
.c{color:#fb923c}
.d{color:#f87171}
.chaos{color:#fb923c;font-weight:bold}
.alert{color:#fb7185;font-weight:bold}
.small{font-size:12px;color:#94a3b8}
.ca{word-break:break-all;font-size:11px}
hr{border:0;border-top:1px solid #334155;margin:14px 0}
</style>
</head>
<body>
<div class="container">
<h2>🔬 Advanced Structural Scanner</h2>

<form method="post">
<textarea name="cas">{{cas}}</textarea>
<button>Analyze</button>
</form>

{% for r in results %}
<div class="card">
{% if r.alert %}
<div class="alert">🚨 AUTO DOWNGRADE / UPGRADE</div>
{% endif %}
{% if r.chaos %}
<div class="chaos">⚡ CHAOS MODE (High Risk)</div>
{% endif %}

<div class="{{r.cls}}"><b>{{r.tier}}</b></div>
<b>{{r.name}} ({{r.symbol}})</b>
<div class="ca">{{r.ca}}</div>

<div class="small">
MC: ${{r.mc}} | LP: ${{r.liq}} | L/MC: {{r.lmc}}%
</div>
<div class="small">
Buy/Sell: {{r.bs}} | Accel: {{r.acc}}
</div>

<hr>
<div class="small"><b>Diagnostics</b></div>
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

# =====================================================
# HELPERS
# =====================================================
def cached(key, fn):
    now = time.time()
    if key in RPC_CACHE:
        ts, data = RPC_CACHE[key]
        if now - ts < CACHE_TTL:
            return data
    data = fn()
    RPC_CACHE[key] = (now, data)
    return data

# =====================================================
# DEX DATA
# =====================================================
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

# =====================================================
# SOLANA BUYER PROXY
# =====================================================
def sol_signers(token):
    def fetch():
        payload = {
            "jsonrpc":"2.0","id":1,
            "method":"getSignaturesForAddress",
            "params":[token,{"limit":50}]
        }
        r = requests.post(SOL_RPC, json=payload, timeout=8).json()
        if "result" not in r:
            return []
        return [tx["signature"][:6] for tx in r["result"]]
    return cached(("sol", token), fetch)

# =====================================================
# ORGANIC + CLUSTER
# =====================================================
def analyze_wallets(buyers):
    if not buyers:
        return "LOW", "HIGH"
    c = Counter(buyers)
    total = sum(c.values())
    unique = len(c)
    top_ratio = max(c.values()) / total

    cluster = "LOW"
    if top_ratio > 0.5: cluster = "HIGH"
    elif top_ratio > 0.35: cluster = "MEDIUM"

    if unique >= 25 and cluster == "LOW":
        organic = "HIGH"
    elif unique >= 10:
        organic = "MEDIUM"
    else:
        organic = "LOW"

    return organic, cluster

def update_trend(ca, organic):
    hist = ORGANIC_HISTORY.setdefault(ca, deque(maxlen=TREND_WINDOW))
    hist.append(organic)
    if len(hist) < 3:
        return "FLAT"
    if hist[-1] == "HIGH" and hist[-2] != "HIGH":
        return "↑ Improving"
    if hist[-1] == "LOW" and hist[-2] != "LOW":
        return "↓ Deteriorating"
    return "FLAT"

# =====================================================
# RISK + CONFIDENCE
# =====================================================
def rug_risk(cluster, organic, liq, mc):
    if liq / mc < 0.03:
        return "HIGH"
    if cluster == "HIGH" and organic == "LOW":
        return "HIGH"
    if cluster == "MEDIUM":
        return "MEDIUM"
    return "LOW"

def confidence(lmc_ok, p3, p4, organic, rug):
    score = 50
    if lmc_ok: score += 15
    if p3: score += 10
    if p4: score += 10
    if organic == "HIGH": score += 5
    if organic == "LOW": score -= 10
    if rug == "HIGH": score -= 25
    if rug == "MEDIUM": score -= 10
    return max(0, min(score, 100))

# =====================================================
# MAIN
# =====================================================
@app.route("/", methods=["GET","POST"])
def index():
    cas = request.form.get("cas","")
    results = []

    for ca in [c.strip() for c in cas.splitlines() if c.strip()]:
        d = fetch_dex(ca)
        if not d or d["fdv"] == 0:
            continue

        mc = d["fdv"]
        if mc < MC_MIN or mc > 5_000_000:
            continue

        liq = d["liq"]
        lmc = round((liq / mc) * 100, 2)

        buys = d["buys"]
        sells = max(d["sells"], 1)
        bs_ratio = round(buys / sells, 2)

        prev = PREV_BUYS.get(ca, buys)
        acc = buys - prev
        PREV_BUYS[ca] = buys

        # ---- P CONDITIONS ----
        p1 = LMC_MIN <= lmc <= LMC_MAX
        p2 = liq > 0 and LAST_LIQ.get(ca, liq) <= liq
        p3 = bs_ratio > BUYSELL_STRONG
        p4 = acc >= ACCEL_STRONG
        LAST_LIQ[ca] = liq

        # ---- WALLET LAYER ----
        buyers = sol_signers(ca) if d["chain"] == "solana" else []
        organic, cluster = analyze_wallets(buyers)
        trend = update_trend(ca, organic)
        rug = rug_risk(cluster, organic, liq, mc)

        # ---- TIER LOGIC (EXACT) ----
        if p1 and p2 and p3 and p4:
            tier = "🟢 Tier A (Confirmed)"
            cls = "confirmed"
        elif p1 and p2 and (p3 or p4):
            tier = "🔵 Tier A (Early)"
            cls = "early"
        elif p1 and p2:
            tier = "👀 Tier B"
            cls = "b"
        elif p3 or p4:
            tier = "👀 Tier C"
            cls = "c"
        else:
            tier = "❌ Tier D"
            cls = "d"

        prev_tier = LAST_TIER.get(ca)
        alert = False
        if prev_tier and prev_tier != tier:
            alert = True
            TIER_HISTORY.setdefault(ca, []).append({
                "from": prev_tier,
                "to": tier,
                "time": datetime.datetime.now().strftime("%H:%M:%S")
            })
        LAST_TIER[ca] = tier

        conf = confidence(p1 and p2, p3, p4, organic, rug)

        results.append({
            "ca": ca,
            "name": d["name"],
            "symbol": d["symbol"],
            "mc": int(mc),
            "liq": int(liq),
            "lmc": lmc,
            "bs": bs_ratio,
            "acc": acc,
            "tier": tier,
            "cls": cls,
            "organic": organic,
            "trend": trend,
            "cluster": cluster,
            "rug": rug,
            "conf": conf,
            "chaos": mc <= CHAOS_MC_MAX and p3,
            "alert": alert
        })

    return render_template_string(HTML, results=results, cas=cas)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
