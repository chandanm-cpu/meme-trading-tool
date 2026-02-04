import os, csv, time, datetime, requests
import pandas as pd, joblib, numpy as np
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier
from scipy.stats import spearmanr

app = Flask(__name__)

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
DATA_FILE = "coin_data.csv"
MODEL_FILE = "ml_model.pkl"
STATE_FILE = "state.txt"

LABEL_AFTER_DAYS = 3
MIN_ROWS_TO_TRAIN = 50

CSV_HEADER = [
    "timestamp","ca",
    "mc_at_scan","liq","lmc",
    "buys","sells","accel",
    "age_minutes","tx_count_24h","rsi_15m",
    "predicted_mc_dd",
    "total_scanned_at_time","total_labeled_at_time",
    "tier","mc_latest","outcome","labeled_at"
]

if not os.path.exists(DATA_FILE):
    with open(DATA_FILE,"w",newline="") as f:
        csv.writer(f).writerow(CSV_HEADER)

# ---------------- DATA ----------------
def fetch_dex(ca):
    r = requests.get(DEX_URL + ca, timeout=10).json()
    if not r.get("pairs"):
        return None
    p = r["pairs"][0]
    created = p.get("pairCreatedAt", int(time.time()*1000))
    age = int((time.time()*1000 - created) / 60000)
    tx = p.get("txns", {}).get("h24", {})
    buys, sells = tx.get("buys",0), tx.get("sells",0)
    return {
        "name": p["baseToken"]["name"],
        "symbol": p["baseToken"]["symbol"],
        "mc": float(p.get("fdv") or 0),
        "liq": float(p.get("liquidity",{}).get("usd") or 0),
        "buys": buys,
        "sells": sells,
        "tx": buys + sells,
        "age": age
    }

def rsi(buys,sells):
    rs = buys / max(sells,1)
    return round(100 - (100/(1+rs)),2)

# ---------------- ML ----------------
MULT = {"RUG":0.2,"FLAT":1.0,"2X_5X":3.5,"5X_20X":10,"20X_PLUS":30}
RUG_PENALTY = 2.5

def ml_predict(mc,lmc,buys,sells,accel,age,tx,rsi_v):
    if not os.path.exists(MODEL_FILE):
        return None, mc, 0
    m = joblib.load(MODEL_FILE)
    probs = dict(zip(
        m.classes_,
        m.predict_proba([[lmc,buys,sells,accel,age,tx,rsi_v]])[0]
    ))
    ev = sum(probs[k]*MULT[k] for k in probs)
    score = ev - probs.get("RUG",0)*RUG_PENALTY
    return probs, int(mc*ev), round(score,2)

# ---------------- REGIME ----------------
def market_regime():
    df = pd.read_csv(DATA_FILE).dropna(subset=["outcome"])
    if len(df) < 50:
        return "NEUTRAL", 0.7
    rug_rate = (df["outcome"]=="RUG").mean()
    if rug_rate > 0.45:
        return "TOXIC", 0.3
    if rug_rate < 0.25:
        return "HOT", 1.0
    return "NEUTRAL", 0.7

def position_size(score, rug_p, reg_mult):
    if rug_p > 0.4 or score <= 0:
        return 0.0
    base = min(score/10, 0.05)
    return round(base * reg_mult, 3)

# ---------------- STORAGE ----------------
def stats():
    df = pd.read_csv(DATA_FILE)
    return len(df), df["outcome"].notna().sum()

def save(ca,d,pmc):
    df = pd.read_csv(DATA_FILE)
    scanned,labeled = len(df), df["outcome"].notna().sum()
    lmc = (d["liq"]/d["mc"])*100 if d["mc"] else 0
    accel = d["buys"]
    with open(DATA_FILE,"a",newline="") as f:
        csv.writer(f).writerow([
            datetime.datetime.utcnow().isoformat(),
            ca,
            d["mc"], d["liq"], round(lmc,2),
            d["buys"], d["sells"], accel,
            d["age"], d["tx"], rsi(d["buys"],d["sells"]),
            pmc,
            scanned,labeled,
            "NA","","",""
        ])

# ---------------- LABEL + TRAIN ----------------
def auto_label():
    df = pd.read_csv(DATA_FILE)
    now = datetime.datetime.utcnow()
    for i,r in df.iterrows():
        if pd.notna(r["outcome"]):
            continue
        if (now-datetime.datetime.fromisoformat(r["timestamp"])).days < LABEL_AFTER_DAYS:
            continue
        d = fetch_dex(r["ca"])
        mc2 = d["mc"] if d else 0
        ratio = mc2/r["mc_at_scan"] if r["mc_at_scan"] else 0
        out = "RUG" if ratio<0.5 else "FLAT" if ratio<1.5 else "2X_5X" if ratio<5 else "5X_20X" if ratio<20 else "20X_PLUS"
        df.at[i,"mc_latest"] = mc2
        df.at[i,"outcome"] = out
        df.at[i,"labeled_at"] = now.isoformat()
    df.to_csv(DATA_FILE,index=False)

def train():
    df = pd.read_csv(DATA_FILE).dropna(subset=["outcome"])
    if len(df) < MIN_ROWS_TO_TRAIN:
        return
    X = df[["lmc","buys","sells","accel","age_minutes","tx_count_24h","rsi_15m"]]
    y = df["outcome"]
    m = RandomForestClassifier(n_estimators=300,max_depth=10,min_samples_leaf=15)
    m.fit(X,y)
    joblib.dump(m,MODEL_FILE)

def lazy():
    t = datetime.date.today().isoformat()
    if os.path.exists(STATE_FILE) and open(STATE_FILE).read().strip()==t:
        return
    auto_label()
    train()
    open(STATE_FILE,"w").write(t)

# ---------------- UI ----------------
@app.route("/")
def index():
    lazy()
    sc,lb = stats()
    regime,_ = market_regime()
    return render_template_string("""
    <meta http-equiv="refresh" content="15">
    <h2>ML Scanner</h2>
    <p>Scanned: {{sc}} | Labeled: {{lb}} | Regime: {{reg}}</p>
    <form method="post" action="/scan">
      <textarea name="cas" style="width:100%;height:120px"></textarea><br>
      <button>Scan</button>
    </form>
    <p><a href="/backtest">Backtest</a></p>
    """, sc=sc, lb=lb, reg=regime)

@app.route("/scan", methods=["POST"])
def scan():
    lazy()
    regime, reg_mult = market_regime()
    out = []

    for ca in request.form.get("cas","").splitlines():
        d = fetch_dex(ca.strip())
        if not d:
            continue

        lmc = (d["liq"]/d["mc"])*100 if d["mc"] else 0
        probs, pmc, score = ml_predict(
            d["mc"], lmc, d["buys"], d["sells"],
            d["buys"], d["age"], d["tx"], rsi(d["buys"],d["sells"])
        )

        if probs:
            size = position_size(score, probs.get("RUG",0), reg_mult)
            ml_txt = ", ".join(f"{k}:{int(v*100)}%" for k,v in probs.items())
        else:
            size = 0.0
            ml_txt = "ML collecting data"

        save(ca, d, pmc)

        out.append(
            f"<b>{d['symbol']}</b> | MC ${int(d['mc'])} | "
            f"Pred MC ${pmc} | Size {int(size*100)}%<br>"
            f"{ml_txt}<br>"
        )

    return "<hr>".join(out) + '<br><a href="/">Back</a>'

@app.route("/backtest")
def backtest():
    df = pd.read_csv(DATA_FILE).dropna(subset=["predicted_mc_dd","mc_latest"])
    if len(df) < 30:
        return "Not enough data"
    corr,_ = spearmanr(df["predicted_mc_dd"], df["mc_latest"])
    df["err"] = df["mc_latest"]/df["predicted_mc_dd"]
    return f"""
    <h2>Backtest</h2>
    Correlation: {round(corr,3)}<br>
    Median Error: {round(df["err"].median(),2)}<br>
    <a href="/">Back</a>
    """

if __name__=="__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
