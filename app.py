from flask import Flask, request, render_template_string
import requests, time, math, os

app = Flask(__name__)

# ---------------- MEMORY ----------------
PREV = {}
SEEN = {}
RPC_PREV = {}

# ---------------- RPC ENDPOINTS ----------------
SOL_RPC = "https://api.mainnet-beta.solana.com"
BSC_RPC = "https://bsc-dataseed.binance.org"

# ---------------- LIMITS ----------------
MAX_RPC_CALLS = 10  # rate-limit safety

# ---------------- TTL ----------------
TIER_A_TTL = 20 * 60
TIER_B_TTL = 40 * 60
TIER_C_TTL = 10 * 60

# ---------------- UI ----------------
HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="15">
<title>20k–400k MC Prediction Filter</title>
<style>
body { font-family: Arial; background:#0f172a; color:#e5e7eb }
.container { max-width:650px; margin:auto; padding:15px }
textarea { width:100%; height:120px }
button { width:100%; padding:10px; margin-top:8px }
.card { background:#1e293b; padding:12px; margin-top:10px; border-radius:10px }
.a{color:#4ade80} .b{color:#facc15} .c{color:#60a5fa} .d{color:#f87171}
.small{font-size:12px;color:#94a3b8}
.ca{word-break:break-all;font-size:11px}
</style>
</head>
<body>
<div class="container">
<h2>🔍 Structural + RPC Filter</h2>
<form method="post">
<textarea name="cas">{{cas}}</textarea>
<button>Analyze</button>
</form>

{% for r in results %}
<div class="card">
<div class="{{r.cls}}"><b>{{r.tier}}</b></div>
<b>{{r.name}} ({{r.symbol}})</b>
<div class="ca">{{r.ca}}</div>
<div class="small">MC: ${{r.mc}} | Liq: ${{r.liq}}</div>
<div class="small">Scarcity: {{r.sc}} | Demand: {{r.dm}} | Accel: {{r.acc}}</div>
<div class="small">RPC/min: {{r.rpc_rate}} | RPC Δ: {{r.rpc_delta}}</div>
{% if r.ml %}
<div class="small">🧠 ML Prob: {{r.ml}}%</div>
{% endif %}
</div>
{% endfor %}
</div>
</body>
</html>
"""

# ---------------- DEXSCREENER ----------------
def dex(ca):
    try:
        j = requests.get(
            f"https://api.dexscreener.com/latest/dex/tokens/{ca}",
            timeout=8
        ).json()
        if not j.get("pairs"): return None
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

# ---------------- RPC ACTIVITY ----------------
def rpc_activity(ca, chain):
    try:
        if chain == "solana":
            payload = {
                "jsonrpc":"2.0","id":1,
                "method":"getSignaturesForAddress",
                "params":[ca,{"limit":15}]
            }
            r = requests.post(SOL_RPC, json=payload, timeout=6).json()
            return len(r.get("result", []))
        elif chain == "bsc":
            payload = {
                "jsonrpc":"2.0","id":1,
                "method":"eth_getTransactionCount",
                "params":[ca,"latest"]
            }
            r = requests.post(BSC_RPC, json=payload, timeout=6).json()
            return int(r.get("result","0x0"),16)
    except:
        return 0

# ---------------- SCORES ----------------
def scarcity(fdv, liq, buys, sells):
    if fdv == 0: return 0
    s = 0
    if liq/fdv < 0.1: s+=40
    if liq/fdv < 0.05: s+=30
    if sells < buys*0.8: s+=20
    if sells < 20: s+=10
    return min(s,100)

def demand(buys, sells, rpc_rate):
    d = 0
    if buys/max(sells,1) > 1.4: d+=40
    if buys > 60: d+=30
    if rpc_rate >= 8: d+=30
    return min(d,100)

def ml_prob(sc, dm, acc):
    return round((1/(1+math.exp(-(0.04*sc+0.05*dm+0.08*acc-10))))*100,1)

# ---------------- MAIN ----------------
@app.route("/", methods=["GET","POST"])
def index():
    results=[]
    cas=request.form.get("cas","")
    now=time.time()
    rpc_calls=0

    for ca in cas.splitlines():
        ca=ca.strip()
        if not ca: continue

        d=dex(ca)
        if not d or d["fdv"]<20000 or d["fdv"]>400000:
            continue

        rpc_rate=0
        rpc_delta=0
        if rpc_calls<MAX_RPC_CALLS:
            rpc_rate=rpc_activity(ca,d["chain"])
            prev=RPC_PREV.get(ca,0)
            rpc_delta=rpc_rate-prev
            RPC_PREV[ca]=rpc_rate
            rpc_calls+=1

        sc=scarcity(d["fdv"],d["liq"],d["buys"],d["sells"])
        dm=demand(d["buys"],d["sells"],rpc_rate)

        prev=PREV.get(ca,{"dm":dm})
        acc=dm-prev["dm"]
        PREV[ca]={"dm":dm}

        tier,cls,ml="❌ Tier D","d",None
        if sc>=75 and dm>=50 and acc>=15:
            tier,cls="🚀 Tier A","a"
            ml=ml_prob(sc,dm,acc)
        elif sc>=75:
            tier,cls="👀 Tier B","b"
        elif dm>=60:
            tier,cls="👀 Tier C","c"

        results.append({
            "ca":ca,"name":d["name"],"symbol":d["symbol"],
            "mc":int(d["fdv"]),"liq":int(d["liq"]),
            "sc":sc,"dm":dm,"acc":acc,
            "rpc_rate":rpc_rate,"rpc_delta":rpc_delta,
            "tier":tier,"cls":cls,"ml":ml
        })

    return render_template_string(HTML,results=results,cas=cas)

if __name__=="__main__":
    port=int(os.environ.get("PORT",10000))
    app.run(host="0.0.0.0",port=port)
