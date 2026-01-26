from flask import Flask, request, jsonify, render_template_string
import requests, time, math, json, os
from threading import Lock

# ================= CONFIG =================
DEX_API = "https://api.dexscreener.com/latest/dex/tokens"
STATE_FILE = "oracle_state.json"

CACHE_TTL = 90
AUTO_REFRESH_MIN = 5

MIN_LIQ = 2500
LOW_MC_MAX = 80000

# ================= UTIL =================
def sf(x, d=0.0):
    try: return float(x)
    except: return d

def pct(x, arr):
    if len(arr) < 20: return 50.0
    return round(sum(1 for v in arr if v <= x) / len(arr) * 100, 2)

# ================= STATE =================
class OracleState:
    def __init__(self):
        self.lock = Lock()
        self.history = []
        self.paper = []
        self.load()

    def load(self):
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE) as f:
                d = json.load(f)
                self.history = d.get("history", [])
                self.paper = d.get("paper", [])

    def save(self):
        with open(STATE_FILE, "w") as f:
            json.dump({
                "history": self.history[-500:],
                "paper": self.paper[-500:]
            }, f)

STATE = OracleState()
CACHE = {}

# ================= DATA =================
def fetch_pair(ca):
    now = time.time()
    if ca in CACHE and now - CACHE[ca][1] < CACHE_TTL:
        return CACHE[ca][0]

    r = requests.get(f"{DEX_API}/{ca}", timeout=8)
    if r.status_code != 200: return None
    pairs = r.json().get("pairs", [])
    if not pairs: return None
    best = max(pairs, key=lambda p: sf(p.get("liquidity", {}).get("usd")))
    CACHE[ca] = (best, now)
    return best

# ================= FEATURES =================
def volume_accel(v5, v30): return v5 / max(v30,1)
def alpha(v, tx): return math.log(v+1)*0.6 + math.log(tx+1)*0.4

def detect_regime(v5,v30,tx5,liq,mc,pc):
    va = volume_accel(v5,v30)
    if v5<1000 or tx5<5: return "DEAD"
    if va>=1.8 and tx5>=10 and abs(pc)<3 and mc<LOW_MC_MAX: return "ACCUMULATION"
    if va>=2.5 and tx5>=20 and pc>2: return "IGNITION"
    if pc>8 and v5>10000: return "EXPANSION"
    if v5>15000 and tx5<15: return "DISTRIBUTION"
    return "UNKNOWN"

def regime_score(r):
    return {"ACCUMULATION":70,"IGNITION":85,"EXPANSION":75}.get(r,0)

# ================= FLASK =================
app = Flask(__name__)

HTML = f"""
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Decision Oracle</title>
<style>
body {{background:#0f172a;color:#e5e7eb;font-family:sans-serif;padding:16px}}
.card {{background:#111827;padding:16px;border-radius:12px;margin-bottom:12px}}
table {{width:100%;border-collapse:collapse}}
th,td {{padding:8px;border-bottom:1px solid #333}}
button,input {{padding:10px;width:100%;margin-top:6px}}
</style>
</head>
<body>

<div class="card">
<h3>Scan Contracts</h3>
<input class="ca" placeholder="CA 1">
<input class="ca" placeholder="CA 2">
<input class="ca" placeholder="CA 3">
<button onclick="scanAll()">Scan</button>
</div>

<div class="card">
<h3>Ranking Board</h3>
<table id="rank">
<tr><th>Rank</th><th>Token</th><th>Regime</th><th>Conf</th><th>Score</th><th>Action</th></tr>
</table>
</div>

<script>
async function scanAll() {{
  let rows=[];
  for (let b of document.querySelectorAll(".ca")) {{
    if (!b.value) continue;
    let r=await fetch("/scan?ca="+b.value);
    let d=await r.json();
    rows.push(d);
  }}
  rows.sort((a,b)=>b.score-a.score);
  let t=document.getElementById("rank");
  t.innerHTML="<tr><th>Rank</th><th>Token</th><th>Regime</th><th>Conf</th><th>Score</th><th>Action</th></tr>";
  rows.forEach((r,i)=>{
    t.innerHTML+=`<tr><td>${i+1}</td><td>${r.token}</td><td>${r.regime}</td><td>${r.confidence}</td><td>${r.score}</td><td>${r.action}</td></tr>`;
  });
}}
setInterval(scanAll,{AUTO_REFRESH_MIN}*60000);
</script>
</body>
</html>
"""

@app.route("/")
def home(): return render_template_string(HTML)

@app.route("/scan")
def scan():
    ca=request.args.get("ca","")
    p=fetch_pair(ca)
    if not p: return jsonify({"score":0})

    v5=sf(p["volume"]["m5"]); v30=sf(p["volume"]["m30"])
    tx5=sum(p["txns"]["m5"].values())
    liq=sf(p["liquidity"]["usd"])
    mc=sf(p.get("fdv",liq*2))
    pc=sf(p.get("priceChange",{}).get("m5"))

    reg=detect_regime(v5,v30,tx5,liq,mc,pc)
    conf=min(100,alpha(v5,tx5)*20)
    score=round(0.4*regime_score(reg)+0.6*conf,2)

    action={"ACCUMULATION":"OPTIONAL","IGNITION":"EARLY","EXPANSION":"MOMENTUM"}.get(reg,"WAIT")

    return jsonify({
        "token":p["baseToken"]["name"],
        "regime":reg,
        "confidence":round(conf,1),
        "score":score,
        "action":action
    })

if __name__=="__main__":
    app.run(host="0.0.0.0",port=5000)
    
