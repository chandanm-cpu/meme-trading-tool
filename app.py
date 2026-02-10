import os, io, time, datetime, base64, requests
import pandas as pd
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier, GradientBoostingRegressor
from scipy.stats import spearmanr

# ================= CONFIG =================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = os.environ.get("GITHUB_REPO")
GITHUB_FILE  = os.environ.get("GITHUB_FILE","coin_data.csv")

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
LABEL_AFTER_HOURS = 72
MIN_TRAIN_ROWS = 30

API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}

CSV_HEADER = [
    "timestamp","ca","symbol","chain",
    "market_cap","liquidity",
    "buys_1h","sells_1h",
    "volume_5m","volume_1h",
    "age_minutes",
    "liq_to_mc","buy_sell_ratio",
    "label_outcome","mc_after_3d"
]

app = Flask(__name__)

# ================= FAST HEALTH =================
@app.route("/health")
def health():
    return "OK"

# ================= CSV =================
def load_csv():
    r = requests.get(API, headers=HEADERS, timeout=10)
    if r.status_code == 404:
        return pd.DataFrame(columns=CSV_HEADER), None
    j = r.json()
    content = base64.b64decode(j["content"]).decode()
    return pd.read_csv(io.StringIO(content)), j["sha"]

def save_csv(df, sha):
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    payload = {
        "message":"update csv",
        "content":base64.b64encode(buf.getvalue().encode()).decode(),
        "sha":sha
    }
    requests.put(API, headers=HEADERS, json=payload, timeout=10)

# ================= DEX =================
def fetch_dex(ca):
    try:
        r = requests.get(DEX_URL+ca,timeout=10).json()
        if not r.get("pairs"):
            return None
        p = r["pairs"][0]
        tx = p.get("txns",{}).get("h1",{})
        vol = p.get("volume",{})
        age = int((time.time()*1000 - p.get("pairCreatedAt",0))/60000)
        return {
            "symbol":p["baseToken"]["symbol"],
            "chain":p["chainId"],
            "mc":float(p.get("fdv") or 0),
            "liq":float(p.get("liquidity",{}).get("usd") or 0),
            "buys1":tx.get("buys",0),
            "sells1":tx.get("sells",0),
            "vol5":float(vol.get("m5",0)),
            "vol1":float(vol.get("h1",0)),
            "age":age
        }
    except:
        return None

# ================= ML =================
def train_survivor(df):
    df=df.dropna(subset=["label_outcome"])
    if len(df)<MIN_TRAIN_ROWS:
        return None
    df["is_survivor"]=~df["label_outcome"].str.contains("RUG")
    X=df[["liq_to_mc","buy_sell_ratio","volume_5m","volume_1h","age_minutes"]]
    y=df["is_survivor"]
    m=RandomForestClassifier(n_estimators=250,max_depth=8)
    m.fit(X,y)
    return m

def survivor_prob(m,row):
    if m is None:
        return 0
    X=[[row["liq_to_mc"],row["buy_sell_ratio"],
        row["volume_5m"],row["volume_1h"],row["age_minutes"]]]
    return int(m.predict_proba(X)[0][1]*100)

def train_upside(df,q):
    df=df[df["label_outcome"].notna() & ~df["label_outcome"].str.contains("RUG")]
    if len(df)<MIN_TRAIN_ROWS:
        return None
    X=df[["liq_to_mc","buy_sell_ratio","volume_5m","volume_1h","age_minutes"]]
    y=df["mc_after_3d"]/df["market_cap"]
    m=GradientBoostingRegressor(loss="quantile",alpha=q,n_estimators=150)
    m.fit(X,y)
    return m

def upside_predict(m,row):
    if m is None:
        return 0
    X=[[row["liq_to_mc"],row["buy_sell_ratio"],
        row["volume_5m"],row["volume_1h"],row["age_minutes"]]]
    return int((m.predict(X)[0]-1)*100)

def rank_score(surv,mid,up,conf):
    return min(100,int(0.4*surv + 0.3*max(mid,0) + 0.2*max(up,0)/2 + 0.1*conf))

# ================= AUTO LABEL =================
def auto_label(df):
    now=datetime.datetime.utcnow()
    checked=labeled=0
    for i,r in df.iterrows():
        if pd.notna(r["label_outcome"]):
            continue
        ts=datetime.datetime.fromisoformat(r["timestamp"])
        if (now-ts).total_seconds()<LABEL_AFTER_HOURS*3600:
            continue
        checked+=1
        d=fetch_dex(r["ca"])
        if not d or d["mc"]<=0:
            df.at[i,"label_outcome"]="RUG"
            df.at[i,"mc_after_3d"]=0
            labeled+=1
            continue
        ratio=d["mc"]/max(r["market_cap"],1)
        label="RUG" if ratio<=0.3 else "FLAT" if ratio<=0.9 else \
              "2X" if ratio<=2 else "5X" if ratio<=5 else "10X"
        df.at[i,"label_outcome"]=label
        df.at[i,"mc_after_3d"]=int(d["mc"])
        labeled+=1
    return df,checked,labeled

# ================= ROUTES =================
@app.route("/",methods=["GET","POST"])
def index():
    df,sha=load_csv()
    scanned=len(df)
    labeled=df["label_outcome"].notna().sum()

    surv_model=train_survivor(df)
    q10=train_upside(df,0.1)
    q50=train_upside(df,0.5)
    q90=train_upside(df,0.9)

    results=[]

    if request.method=="POST":
        for ca in request.form.get("cas","").splitlines():
            ca=ca.strip()
            if not ca or ca in df["ca"].values:
                continue
            d=fetch_dex(ca)
            if not d:
                continue

            row={
                "timestamp":datetime.datetime.utcnow().isoformat(),
                "ca":ca,
                "symbol":d["symbol"],
                "chain":d["chain"],
                "market_cap":d["mc"],
                "liquidity":d["liq"],
                "buys_1h":d["buys1"],
                "sells_1h":d["sells1"],
                "volume_5m":d["vol5"],
                "volume_1h":d["vol1"],
                "age_minutes":d["age"],
                "liq_to_mc":d["liq"]/d["mc"]*100 if d["mc"] else 0,
                "buy_sell_ratio":d["buys1"]/max(d["sells1"],1),
                "label_outcome":None,
                "mc_after_3d":None
            }

            df=pd.concat([df,pd.DataFrame([row])],ignore_index=True)

            surv=survivor_prob(surv_model,row)
            mid=upside_predict(q50,row) if surv>=50 else None
            up=upside_predict(q90,row) if surv>=50 else None
            down=upside_predict(q10,row) if surv>=50 else None

            score=rank_score(surv,mid or 0,up or 0,0)

            results.append({
                "symbol":d["symbol"],
                "mc":int(d["mc"]),
                "liq":int(d["liq"]),
                "surv":surv,
                "down":down,
                "mid":mid,
                "up":up,
                "rank":score
            })

        save_csv(df,sha)

    html="""
    <h2 style="font-size:42px;">📊 ABC Meme Oracle</h2>
    <p style="font-size:26px;">Scanned: {{scanned}} | Labeled: {{labeled}}</p>

    <form method="post">
      <textarea name="cas" style="width:100%;height:140px;font-size:26px;"></textarea><br>
      <button style="font-size:34px;padding:20px;">🚀 Scan</button>
    </form>

    <form method="post" action="/auto_label">
      <button style="font-size:20px;padding:10px;">🧠 Auto Label</button>
    </form>

    <form method="get" action="/backtest">
      <button style="font-size:20px;padding:10px;">📊 Backtest</button>
    </form>

    <table border="1" cellpadding="18" style="font-size:48px;margin-top:20px;">
      <tr>
        <th>Coin</th><th>MC</th><th>Liq</th>
        <th>Survivor%</th>
        <th>Down%</th><th>Mid%</th><th>Up%</th>
        <th>Rank</th>
      </tr>
      {% for r in results %}
      <tr>
        <td>{{r.symbol}}</td><td>${{r.mc}}</td><td>${{r.liq}}</td>
        <td>{{r.surv}}</td>
        <td>{{r.down if r.down is not none else "-"}}</td>
        <td>{{r.mid if r.mid is not none else "-"}}</td>
        <td>{{r.up if r.up is not none else "-"}}</td>
        <td>{{r.rank}}</td>
      </tr>
      {% endfor %}
    </table>
    """
    return render_template_string(html,results=results,scanned=scanned,labeled=labeled)

@app.route("/auto_label",methods=["POST"])
def label():
    df,sha=load_csv()
    df,checked,labeled=auto_label(df)
    save_csv(df,sha)
    return f"Labeled {labeled} of {checked} checked<br><a href='/'>Back</a>"

@app.route("/backtest")
def backtest():
    df,_=load_csv()
    bt=df.dropna(subset=["mc_after_3d"])
    if len(bt)<10:
        return "Not enough data"
    corr,_=spearmanr(bt["market_cap"],bt["mc_after_3d"])
    return f"Samples: {len(bt)} | Correlation: {round(corr,3)}<br><a href='/'>Back</a>"

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
