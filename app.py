import os, io, time, datetime, base64, requests
import pandas as pd
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier
from scipy.stats import spearmanr

# ===================== GITHUB STORAGE =====================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = os.environ.get("GITHUB_REPO")
GITHUB_FILE  = os.environ.get("GITHUB_FILE", "coin_data.csv")

if not GITHUB_TOKEN or not GITHUB_REPO:
    raise RuntimeError("GitHub env vars missing")

GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

# ===================== CONFIG =====================
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
MIN_TRAIN_ROWS = 50

CSV_HEADER = [
    "timestamp","ca","symbol","chain",
    "price","market_cap","liquidity",
    "buys_5m","sells_5m","buys_1h","sells_1h",
    "txns_24h","volume_5m","volume_1h","volume_24h",
    "age_minutes","rsi_5m","rsi_15m",
    "liq_to_mc","buy_sell_ratio",
    "label_outcome","mc_after_3d",
    "ml_predicted_mc","ml_confidence"
]

app = Flask(__name__)

# ===================== CSV =====================
def load_csv():
    r = requests.get(GITHUB_API, headers=HEADERS)
    if r.status_code == 404:
        return pd.DataFrame(columns=CSV_HEADER), None
    data = r.json()
    content = base64.b64decode(data["content"]).decode()
    return pd.read_csv(io.StringIO(content)), data["sha"]

def save_csv(df, sha):
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    encoded = base64.b64encode(buf.getvalue().encode()).decode()
    payload = {"message": "update data", "content": encoded, "sha": sha}
    requests.put(GITHUB_API, headers=HEADERS, json=payload)

# ===================== INDICATORS =====================
def rsi(buys, sells):
    rs = buys / max(sells,1)
    return round(100 - (100/(1+rs)),2)

# ===================== FETCH =====================
def fetch_dex(ca):
    r = requests.get(DEX_URL+ca,timeout=10).json()
    if not r.get("pairs"): return None
    p = r["pairs"][0]

    tx5 = p.get("txns",{}).get("m5",{})
    tx1 = p.get("txns",{}).get("h1",{})
    tx24 = p.get("txns",{}).get("h24",{})
    vol = p.get("volume",{})

    created = p.get("pairCreatedAt",int(time.time()*1000))
    age = int((time.time()*1000-created)/60000)

    return {
        "symbol":p["baseToken"]["symbol"],
        "chain":p["chainId"],
        "price":float(p.get("priceUsd") or 0),
        "mc":float(p.get("fdv") or 0),
        "liq":float(p.get("liquidity",{}).get("usd") or 0),
        "buys5":tx5.get("buys",0),
        "sells5":tx5.get("sells",0),
        "buys1":tx1.get("buys",0),
        "sells1":tx1.get("sells",0),
        "tx24":tx24.get("buys",0)+tx24.get("sells",0),
        "vol5":float(vol.get("m5",0)),
        "vol1":float(vol.get("h1",0)),
        "vol24":float(vol.get("h24",0)),
        "age":age
    }

# ===================== ORACLE =====================
def insider_proxy(d):
    s=0
    if d["buys1"]>d["sells1"]*2: s+=35
    if d["liq"]>d["mc"]*0.08: s+=35
    if d["age"]<120: s+=20
    return min(s,100)

def narrative_proxy(d):
    s=0
    if d["vol1"]>d["vol5"]*2: s+=35
    if d["tx24"]>100: s+=35
    if 40<rsi(d["buys5"],d["sells5"])<70: s+=20
    return min(s,100)

# ===================== ML =====================
MULT={"RUG":0.2,"FLAT":1,"2X":3,"5X":7,"10X":15,"20X":30,"50X":60,"100X":120}

def train_ml(df):
    df=df.dropna(subset=["label_outcome"])
    if len(df)<MIN_TRAIN_ROWS: return None
    X=df[["liq_to_mc","buy_sell_ratio","buys_5m","buys_1h","volume_5m","volume_1h","age_minutes","rsi_5m","rsi_15m"]]
    y=df["label_outcome"]
    m=RandomForestClassifier(n_estimators=300,max_depth=10,min_samples_leaf=10)
    m.fit(X,y)
    return m

def ml_predict(m,row):
    if m is None: return {},row["market_cap"],0
    X=[[row["liq_to_mc"],row["buy_sell_ratio"],row["buys_5m"],row["buys_1h"],row["volume_5m"],row["volume_1h"],row["age_minutes"],row["rsi_5m"],row["rsi_15m"]]]
    probs=dict(zip(m.classes_,m.predict_proba(X)[0]))
    ev=sum(probs[k]*MULT[k] for k in probs)
    return probs,int(row["market_cap"]*ev),min(100,int(ev*6))

# ===================== REGIME =====================
def market_regime(df):
    recent=df.tail(50)
    if len(recent)<20: return "NEUTRAL"
    win_rate=(recent["mc_after_3d"]>recent["market_cap"]*2).mean()
    rug_rate=(recent["label_outcome"]=="RUG").mean()
    if win_rate>0.25: return "🔥 HOT"
    if rug_rate>0.4: return "❄️ COLD"
    return "NEUTRAL"

# ===================== POSITION SIZING =====================
def position_size(conf,rug,regime):
    base=min(conf/100*10,5)
    if rug>30: base*=0.5
    if regime=="❄️ COLD": base*=0.6
    if regime=="🔥 HOT": base*=1.2
    return round(min(base,5),2)

# ===================== EXIT LOGIC =====================
def exit_signal(conf,rug,liq):
    if rug>40: return "EXIT IMMEDIATELY"
    if conf>70: return "HOLD / TRAIL"
    if conf<30: return "SMALL / QUICK EXIT"
    return "MONITOR"

# ===================== UI =====================
HTML="""
<meta http-equiv="refresh" content="10">
<h2>📱 Meme Trading Tool</h2>
<p>Market Regime: <b>{{regime}}</b></p>

<form method="post">
<textarea name="cas" style="width:100%;height:120px;"></textarea><br>
<button style="padding:14px 28px;font-size:18px;">🚀 Scan</button>
</form>

{% for r in results %}
<hr>
<b>{{r.symbol}}</b><br>
MC: ${{r.mc}} | Pred MC: ${{r.pmc}} | Liquidity: ${{r.liq}}<br>
ML Conf: {{r.conf}} | Insider: {{r.ins}} | Narrative: {{r.nar}}<br>
📏 Position Size: <b>{{r.size}}%</b><br>
🚪 Exit Signal: <b>{{r.exit}}</b>
{% endfor %}
"""

@app.route("/",methods=["GET","POST"])
def index():
    df,sha=load_csv()
    model=train_ml(df)
    regime=market_regime(df)
    results=[]

    if request.method=="POST":
        for ca in request.form.get("cas","").splitlines():
            d=fetch_dex(ca.strip())
            if not d: continue

            row={
                "timestamp":datetime.datetime.utcnow().isoformat(),
                "ca":ca,"symbol":d["symbol"],"chain":d["chain"],
                "price":d["price"],"market_cap":d["mc"],"liquidity":d["liq"],
                "buys_5m":d["buys5"],"sells_5m":d["sells5"],
                "buys_1h":d["buys1"],"sells_1h":d["sells1"],
                "txns_24h":d["tx24"],
                "volume_5m":d["vol5"],"volume_1h":d["vol1"],"volume_24h":d["vol24"],
                "age_minutes":d["age"],
                "rsi_5m":rsi(d["buys5"],d["sells5"]),
                "rsi_15m":rsi(d["buys1"],d["sells1"]),
                "liq_to_mc":d["liq"]/d["mc"]*100 if d["mc"] else 0,
                "buy_sell_ratio":d["buys1"]/max(d["sells1"],1),
                "label_outcome":"","mc_after_3d":"","ml_predicted_mc":"","ml_confidence":""
            }

            probs,pmc,conf=ml_predict(model,row)
            rug=int(probs.get("RUG",0)*100)
            size=position_size(conf,rug,regime)
            exit_sig=exit_signal(conf,rug,d["liq"])

            row["ml_predicted_mc"]=pmc
            row["ml_confidence"]=conf
            df.loc[len(df)]=row

            results.append({
                "symbol":d["symbol"],"mc":int(d["mc"]),"pmc":pmc,"liq":int(d["liq"]),
                "conf":conf,"ins":insider_proxy(d),"nar":narrative_proxy(d),
                "size":size,"exit":exit_sig
            })

        save_csv(df,sha)

    return render_template_string(HTML,results=results,regime=regime)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
