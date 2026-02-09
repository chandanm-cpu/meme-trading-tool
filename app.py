import os, io, time, datetime, base64, requests, traceback
import pandas as pd
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier, GradientBoostingRegressor
from lifelines import CoxPHFitter
from scipy.stats import spearmanr

# ===================== ENV =====================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = os.environ.get("GITHUB_REPO")
GITHUB_FILE  = os.environ.get("GITHUB_FILE", "coin_data.csv")

API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"}

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
    if sha is None: return
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    payload = {
        "message": "update data",
        "content": base64.b64encode(buf.getvalue().encode()).decode(),
        "sha": sha
    }
    requests.put(API, headers=HEADERS, json=payload)

# ===================== DEX =====================
def fetch_dex(ca):
    try:
        r = requests.get(DEX_URL+ca, timeout=10).json()
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
    except:
        return None

# ===================== MODELS =====================
def train_rf(df):
    df=df.dropna(subset=["label_outcome"])
    if len(df)<MIN_TRAIN_ROWS: return None
    X=df[["liq_to_mc","buy_sell_ratio","volume_5m","volume_1h","age_minutes"]]
    y=df["label_outcome"]
    m=RandomForestClassifier(n_estimators=300,max_depth=10)
    m.fit(X,y)
    return m

def rf_confidence(m,row):
    if m is None: return 0
    X=[[row["liq_to_mc"],row["buy_sell_ratio"],row["volume_5m"],row["volume_1h"],row["age_minutes"]]]
    p=m.predict_proba(X)[0]
    return int(max(p)*100)

def train_survival(df):
    df=df.dropna(subset=["label_outcome"])
    if len(df)<MIN_TRAIN_ROWS: return None
    df["event"] = df["label_outcome"].str.contains("RUG").astype(int)
    df["duration"] = df["age_minutes"]
    cph=CoxPHFitter()
    cph.fit(df[["duration","event","liq_to_mc","buy_sell_ratio","volume_5m","volume_1h"]],
            duration_col="duration",event_col="event")
    return cph

def survival_prob(cph,row,hours):
    if cph is None: return 0
    df=pd.DataFrame([{
        "liq_to_mc":row["liq_to_mc"],
        "buy_sell_ratio":row["buy_sell_ratio"],
        "volume_5m":row["volume_5m"],
        "volume_1h":row["volume_1h"]
    }])
    surv=cph.predict_survival_function(df, times=[hours*60])
    return int(surv.iloc[0,0]*100)

def train_quantile(df,q):
    df=df.dropna(subset=["mc_after_3d"])
    if len(df)<MIN_TRAIN_ROWS: return None
    X=df[["liq_to_mc","buy_sell_ratio","volume_5m","volume_1h","age_minutes"]]
    y=df["mc_after_3d"]/df["market_cap"]
    m=GradientBoostingRegressor(loss="quantile",alpha=q,n_estimators=200)
    m.fit(X,y)
    return m

def quantile_predict(m,row):
    if m is None: return 0
    X=[[row["liq_to_mc"],row["buy_sell_ratio"],row["volume_5m"],row["volume_1h"],row["age_minutes"]]]
    return int((m.predict(X)[0]-1)*100)

# ===================== ROUTES =====================
@app.route("/",methods=["GET","POST"])
def index():
    df,sha=load_csv()
    rf=train_rf(df)
    surv=train_survival(df)
    q10=train_quantile(df,0.1)
    q50=train_quantile(df,0.5)
    q90=train_quantile(df,0.9)

    results=[]
    if request.method=="POST":
        for ca in request.form.get("cas","").splitlines():
            d=fetch_dex(ca.strip())
            if not d: continue

            row={
                "liq_to_mc":d["liq"]/d["mc"]*100 if d["mc"] else 0,
                "buy_sell_ratio":d["buys1"]/max(d["sells1"],1),
                "volume_5m":d["vol5"],
                "volume_1h":d["vol1"],
                "age_minutes":d["age"],
                "market_cap":d["mc"]
            }

            results.append({
                "symbol":d["symbol"],
                "mc":int(d["mc"]),
                "liq":int(d["liq"]),
                "surv24":survival_prob(surv,row,24),
                "surv72":survival_prob(surv,row,72),
                "q10":quantile_predict(q10,row),
                "q50":quantile_predict(q50,row),
                "q90":quantile_predict(q90,row),
                "conf":rf_confidence(rf,row)
            })

    HTML="""
    <meta http-equiv="refresh" content="{{refresh}}">
    <h2 style="font-size:36px;">📊 ML Meme Oracle</h2>
    <form method="post">
    <textarea name="cas" style="width:100%;height:120px;font-size:24px;"></textarea><br>
    <button style="font-size:28px;padding:16px;">Scan</button>
    </form>

    <table border="1" cellpadding="16" style="font-size:48px;margin-top:20px;">
    <tr>
    <th>Coin</th><th>MC</th><th>Liq</th>
    <th>Surv 24h</th><th>Surv 72h</th>
    <th>Down %</th><th>Median %</th><th>Up %</th>
    <th>Conf</th>
    </tr>
    {% for r in results %}
    <tr>
    <td>{{r.symbol}}</td>
    <td>${{r.mc}}</td>
    <td>${{r.liq}}</td>
    <td>{{r.surv24}}%</td>
    <td>{{r.surv72}}%</td>
    <td>{{r.q10}}%</td>
    <td>{{r.q50}}%</td>
    <td>{{r.q90}}%</td>
    <td>{{r.conf}}</td>
    </tr>
    {% endfor %}
    </table>
    """

    return render_template_string(HTML,results=results,refresh=AUTO_REFRESH)

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
