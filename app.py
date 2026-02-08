import os, io, time, datetime, base64, requests, math
import pandas as pd
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier
from scipy.stats import spearmanr

# ===================== ENV =====================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = os.environ.get("GITHUB_REPO")
GITHUB_FILE  = os.environ.get("GITHUB_FILE", "coin_data.csv")

API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}",
           "Accept": "application/vnd.github.v3+json"}

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
AUTO_REFRESH = 10
LABEL_AFTER_HOURS = 72
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
    r = requests.get(API, headers=HEADERS)
    if r.status_code == 404:
        return pd.DataFrame(columns=CSV_HEADER), None
    j = r.json()
    content = base64.b64decode(j["content"]).decode()
    return pd.read_csv(io.StringIO(content)), j["sha"]

def save_csv(df, sha):
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    content = base64.b64encode(buf.getvalue().encode()).decode()
    payload = {"message": "update data", "content": content, "sha": sha}
    requests.put(API, headers=HEADERS, json=payload)

# ===================== HELPERS =====================
def rsi(buys, sells):
    rs = buys / max(sells,1)
    return round(100 - (100/(1+rs)),2)

def fetch_dex(ca):
    r = requests.get(DEX_URL+ca,timeout=10).json()
    if not r.get("pairs"): return None
    p = r["pairs"][0]

    tx5 = p.get("txns",{}).get("m5",{})
    tx1 = p.get("txns",{}).get("h1",{})
    vol = p.get("volume",{})
    age = int((time.time()*1000 - p.get("pairCreatedAt",0))/60000)

    return {
        "symbol":p["baseToken"]["symbol"],
        "chain":p["chainId"],
        "mc":float(p.get("fdv") or 0),
        "liq":float(p.get("liquidity",{}).get("usd") or 0),
        "buys5":tx5.get("buys",0),"sells5":tx5.get("sells",0),
        "buys1":tx1.get("buys",0),"sells1":tx1.get("sells",0),
        "vol5":float(vol.get("m5",0)),
        "vol1":float(vol.get("h1",0)),
        "age":age
    }

# ===================== FAST RUG =====================
def fast_rug_check(df, ca, current_mc):
    now = datetime.datetime.utcnow()
    past = df[df["ca"] == ca]

    for _, row in past.iterrows():
        try:
            ts = datetime.datetime.fromisoformat(row["timestamp"])
        except:
            continue

        minutes = (now - ts).total_seconds() / 60
        old_mc = row["market_cap"]
        if old_mc <= 0: continue

        drop = current_mc / old_mc

        if minutes <= 15 and drop <= 0.3:
            return "FAST_RUG_15M"
        if minutes <= 60 and drop <= 0.3:
            return "FAST_RUG_1H"

    return ""

# ===================== STRUCTURE =====================
def structural_projection(d):
    if d["liq"]<=0 or d["mc"]<=0:
        return d["mc"],0

    net = d["buys5"]-d["sells5"]
    total = max(d["buys5"]+d["sells5"],1)
    flow = net/total

    liq_pressure = d["vol5"]/max(d["liq"],1)
    liq_ratio = d["liq"]/d["mc"]
    age_boost = 1.8 if d["age"]<10 else 1.4 if d["age"]<30 else 1.1

    projected = d["mc"]*(1+flow*liq_pressure*liq_ratio*age_boost*4)
    if flow<0 and liq_pressure>0.7:
        projected=0

    pct=((projected-d["mc"])/d["mc"])*100
    return int(max(projected,0)),round(pct,2)

# ===================== ML =====================
MULT={"RUG":0.2,"FAST_RUG_15M":0.15,"FAST_RUG_1H":0.2,
      "FLAT":1,"2X":3,"5X":7,"10X":15,"20X":30}

def train_ml(df):
    df=df.dropna(subset=["label_outcome"])
    if len(df)<MIN_TRAIN_ROWS: return None
    X=df[["liq_to_mc","buy_sell_ratio","buys_5m","buys_1h",
          "volume_5m","volume_1h","age_minutes","rsi_5m","rsi_15m"]]
    y=df["label_outcome"]
    m=RandomForestClassifier(n_estimators=300,max_depth=10,min_samples_leaf=10)
    m.fit(X,y)
    return m

def ml_predict(m,row):
    if m is None:
        return row["market_cap"],0
    X=[[row["liq_to_mc"],row["buy_sell_ratio"],
        row["buys_5m"],row["buys_1h"],
        row["volume_5m"],row["volume_1h"],
        row["age_minutes"],row["rsi_5m"],row["rsi_15m"]]]
    probs=dict(zip(m.classes_,m.predict_proba(X)[0]))
    ev=sum(probs[k]*MULT.get(k,1) for k in probs)
    return int(row["market_cap"]*ev),min(100,int(ev*6))

# ===================== MANUAL AUTO LABEL =====================
def manual_auto_label(df):
    now = datetime.datetime.utcnow()
    checked = labeled = 0

    for idx,row in df.iterrows():
        if pd.notna(row["label_outcome"]): continue
        try:
            ts = datetime.datetime.fromisoformat(row["timestamp"])
        except:
            continue

        if (now-ts).total_seconds() < LABEL_AFTER_HOURS*3600:
            continue

        checked += 1
        d = fetch_dex(row["ca"])
        if not d or d["mc"]<=0: continue

        ratio = d["mc"]/max(row["market_cap"],1)
        label = ("RUG" if ratio<=0.3 else
                 "FLAT" if ratio<=0.9 else
                 "2X" if ratio<=2 else
                 "5X" if ratio<=5 else
                 "10X" if ratio<=10 else "20X")

        df.at[idx,"label_outcome"]=label
        df.at[idx,"mc_after_3d"]=int(d["mc"])
        labeled += 1

    return df,checked,labeled

# ===================== UI =====================
HTML = """
<meta http-equiv="refresh" content="{{refresh}}">
<h2 style="font-size:26px;">📊 Meme Scanner</h2>
<p style="font-size:18px;">
Scanned: {{sc}} | Labeled: {{lb}}
</p>

<form method="post">
<textarea name="cas" style="width:100%;height:120px;font-size:16px;"></textarea><br>
<button style="padding:16px 32px;font-size:20px;">🚀 Scan</button>
</form>

<form method="post" action="/auto_label">
<button style="padding:14px 28px;font-size:18px;margin-top:8px;">🧠 Auto Label (72h)</button>
</form>

<form method="get" action="/backtest">
<button style="padding:14px 28px;font-size:18px;margin-top:8px;">📊 Backtest</button>
</form>

<table border="1" cellpadding="8" style="font-size:16px;margin-top:10px;">
<tr>
<th>Coin</th><th>MC</th><th>Liq</th>
<th>Struct MC</th><th>Struct %</th>
<th>ML MC</th><th>Conf</th>
</tr>
{% for r in results %}
<tr>
<td>{{r.symbol}}</td>
<td>${{r.mc}}</td>
<td>${{r.liq}}</td>
<td>${{r.struct_mc}}</td>
<td>{{r.struct_pct}}%</td>
<td>${{r.ml_mc}}</td>
<td>{{r.conf}}</td>
</tr>
{% endfor %}
</table>
"""

@app.route("/",methods=["GET","POST"])
def index():
    df,sha=load_csv()
    model=train_ml(df)
    results=[]

    if request.method=="POST":
        for ca in request.form.get("cas","").splitlines():
            d=fetch_dex(ca.strip())
            if not d: continue

            struct_mc,struct_pct=structural_projection(d)
            fast_label=fast_rug_check(df,ca.strip(),d["mc"])

            row={
                "timestamp":datetime.datetime.utcnow().isoformat(),
                "ca":ca,"symbol":d["symbol"],"chain":d["chain"],
                "price":0,"market_cap":d["mc"],"liquidity":d["liq"],
                "buys_5m":d["buys5"],"sells_5m":d["sells5"],
                "buys_1h":d["buys1"],"sells_1h":d["sells1"],
                "txns_24h":0,
                "volume_5m":d["vol5"],"volume_1h":d["vol1"],"volume_24h":0,
                "age_minutes":d["age"],
                "rsi_5m":50,"rsi_15m":50,
                "liq_to_mc":d["liq"]/d["mc"]*100 if d["mc"] else 0,
                "buy_sell_ratio":d["buys1"]/max(d["sells1"],1),
                "label_outcome":fast_label,
                "mc_after_3d":"",
                "ml_predicted_mc":"",
                "ml_confidence":""
            }

            ml_mc,conf=ml_predict(model,row)
            row["ml_predicted_mc"]=ml_mc
            row["ml_confidence"]=conf
            df.loc[len(df)]=row

            results.append({
                "symbol":d["symbol"],
                "mc":int(d["mc"]),
                "liq":int(d["liq"]),
                "struct_mc":struct_mc,
                "struct_pct":struct_pct,
                "ml_mc":ml_mc,
                "conf":conf
            })

        save_csv(df,sha)

    return render_template_string(
        HTML,
        results=results,
        sc=len(df),
        lb=df["label_outcome"].notna().sum(),
        refresh=AUTO_REFRESH
    )

@app.route("/auto_label",methods=["POST"])
def auto_label():
    df,sha=load_csv()
    df,checked,labeled=manual_auto_label(df)
    save_csv(df,sha)
    return f"<h3>Auto Label Complete</h3>Checked:{checked}<br>Labeled:{labeled}<br><a href='/'>Back</a>"

@app.route("/backtest")
def backtest():
    df,_=load_csv()
    bt=df.dropna(subset=["mc_after_3d","ml_predicted_mc"])
    if len(bt)<20:
        return "Not enough labeled data"
    corr,_=spearmanr(bt["ml_predicted_mc"],bt["mc_after_3d"])
    return f"<h3>Backtest</h3>Samples:{len(bt)}<br>Correlation:{round(corr,3)}<br><a href='/'>Back</a>"

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
