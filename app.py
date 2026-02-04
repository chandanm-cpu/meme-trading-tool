import os, io, csv, time, datetime, base64, requests
import numpy as np
import pandas as pd
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier
from scipy.stats import spearmanr

# =====================================================
# GITHUB CONFIG (FREE PERSISTENT STORAGE)
# =====================================================
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

# =====================================================
# CONFIG
# =====================================================
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
LABEL_AFTER_DAYS = 3
MIN_TRAIN_ROWS = 50

# =====================================================
# FINAL CSV SCHEMA (LOCKED)
# =====================================================
CSV_HEADER = [
    "timestamp","ca","symbol","chain",
    "price","market_cap","liquidity",
    "buys_5m","sells_5m","buys_1h","sells_1h",
    "txns_24h",
    "volume_5m","volume_1h","volume_24h",
    "age_minutes",
    "rsi_5m","rsi_15m",
    "liq_to_mc","buy_sell_ratio",
    "label_outcome","mc_after_3d",
    "ml_predicted_mc","ml_confidence"
]

# =====================================================
# APP
# =====================================================
app = Flask(__name__)

# =====================================================
# GITHUB CSV HELPERS
# =====================================================
def load_csv():
    r = requests.get(GITHUB_API, headers=HEADERS)
    if r.status_code == 404:
        return pd.DataFrame(columns=CSV_HEADER), None
    data = r.json()
    content = base64.b64decode(data["content"]).decode()
    df = pd.read_csv(io.StringIO(content))
    return df, data["sha"]

def save_csv(df, sha):
    buf = io.StringIO()
    df.to_csv(buf, index=False)
    encoded = base64.b64encode(buf.getvalue().encode()).decode()
    payload = {
        "message": "update coin data",
        "content": encoded,
        "sha": sha
    }
    r = requests.put(GITHUB_API, headers=HEADERS, json=payload)
    if r.status_code not in (200,201):
        raise RuntimeError("GitHub CSV save failed")

# =====================================================
# INDICATORS
# =====================================================
def rsi_from_buys_sells(buys, sells):
    rs = buys / max(sells, 1)
    return round(100 - (100 / (1 + rs)), 2)

# =====================================================
# FETCH DEX DATA
# =====================================================
def fetch_dex(ca):
    r = requests.get(DEX_URL + ca, timeout=10).json()
    if not r.get("pairs"):
        return None

    p = r["pairs"][0]
    tx5 = p.get("txns", {}).get("m5", {})
    tx1h = p.get("txns", {}).get("h1", {})
    tx24 = p.get("txns", {}).get("h24", {})
    vol = p.get("volume", {})

    created = p.get("pairCreatedAt", int(time.time()*1000))
    age = int((time.time()*1000 - created) / 60000)

    return {
        "symbol": p["baseToken"]["symbol"],
        "chain": p["chainId"],
        "price": float(p.get("priceUsd") or 0),
        "mc": float(p.get("fdv") or 0),
        "liq": float(p.get("liquidity",{}).get("usd") or 0),
        "buys5": tx5.get("buys",0),
        "sells5": tx5.get("sells",0),
        "buys1h": tx1h.get("buys",0),
        "sells1h": tx1h.get("sells",0),
        "tx24": tx24.get("buys",0) + tx24.get("sells",0),
        "vol5": float(vol.get("m5",0)),
        "vol1h": float(vol.get("h1",0)),
        "vol24": float(vol.get("h24",0)),
        "age": age
    }

# =====================================================
# AUTO LABEL
# =====================================================
def auto_label(df):
    now = datetime.datetime.utcnow()
    for i,r in df.iterrows():
        if pd.notna(r["label_outcome"]):
            continue
        t = datetime.datetime.fromisoformat(r["timestamp"])
        if (now - t).days < LABEL_AFTER_DAYS:
            continue

        d = fetch_dex(r["ca"])
        mc2 = d["mc"] if d else 0
        ratio = mc2 / r["market_cap"] if r["market_cap"] else 0

        if ratio < 0.5:
            out = "RUG"
        elif ratio < 1.5:
            out = "FLAT"
        elif ratio < 5:
            out = "2X"
        elif ratio < 20:
            out = "5X"
        else:
            out = "20X"

        df.at[i,"label_outcome"] = out
        df.at[i,"mc_after_3d"] = mc2

    return df

# =====================================================
# ML TRAIN / PREDICT
# =====================================================
MODEL_PATH = "ml_model.pkl"

def train_ml(df):
    train_df = df.dropna(subset=["label_outcome"])
    if len(train_df) < MIN_TRAIN_ROWS:
        return None

    X = train_df[[
        "liq_to_mc","buy_sell_ratio",
        "buys_5m","buys_1h",
        "volume_5m","volume_1h",
        "age_minutes","rsi_5m","rsi_15m"
    ]]
    y = train_df["label_outcome"]

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=10,
        min_samples_leaf=10,
        random_state=42
    )
    model.fit(X,y)
    return model

MULT = {"RUG":0.2,"FLAT":1,"2X":3,"5X":10,"20X":30}

def ml_predict(model, row):
    if model is None:
        return row["market_cap"], 0

    X = [[
        row["liq_to_mc"],row["buy_sell_ratio"],
        row["buys_5m"],row["buys_1h"],
        row["volume_5m"],row["volume_1h"],
        row["age_minutes"],row["rsi_5m"],row["rsi_15m"]
    ]]
    probs = dict(zip(model.classes_, model.predict_proba(X)[0]))
    ev = sum(probs[k]*MULT[k] for k in probs)
    return int(row["market_cap"] * ev), round(ev,2)

# =====================================================
# UI
# =====================================================
HTML = """
<meta http-equiv="refresh" content="15">
<h2>📱 Meme Trading Tool (Free)</h2>

<p>
📦 Storage: GitHub |
📄 Total Scanned: <b>{{sc}}</b> |
🏷 Labeled: <b>{{lb}}</b>
</p>

<form method="post">
<textarea name="cas" style="width:100%;height:120px;">{{cas}}</textarea><br>
<button style="padding:14px 28px;font-size:18px;">🚀 Scan</button>
</form>

{% if results %}
<hr>
{% for r in results %}
<b>{{r.symbol}}</b> ({{r.chain}})<br>
MC: ${{r.mc}} | Liq: ${{r.liq}} | Age: {{r.age}}m<br>
RSI(5m): {{r.rsi5}} | RSI(15m): {{r.rsi15}}<br>
Pred MC: ${{r.pmc}} | Confidence: {{r.conf}}<br>
<hr>
{% endfor %}
{% endif %}

<p><a href="/backtest">📊 Backtest</a></p>
"""

@app.route("/", methods=["GET","POST"])
def index():
    df, sha = load_csv()
    df = auto_label(df)
    model = train_ml(df)

    results = []
    cas_text = ""

    if request.method == "POST":
        cas_text = request.form.get("cas","")
        for ca in cas_text.splitlines():
            d = fetch_dex(ca.strip())
            if not d: continue

            rsi5 = rsi_from_buys_sells(d["buys5"], d["sells5"])
            rsi15 = rsi_from_buys_sells(d["buys1h"], d["sells1h"])

            row = {
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "ca": ca,
                "symbol": d["symbol"],
                "chain": d["chain"],
                "price": d["price"],
                "market_cap": d["mc"],
                "liquidity": d["liq"],
                "buys_5m": d["buys5"],
                "sells_5m": d["sells5"],
                "buys_1h": d["buys1h"],
                "sells_1h": d["sells1h"],
                "txns_24h": d["tx24"],
                "volume_5m": d["vol5"],
                "volume_1h": d["vol1h"],
                "volume_24h": d["vol24"],
                "age_minutes": d["age"],
                "rsi_5m": rsi5,
                "rsi_15m": rsi15,
                "liq_to_mc": round((d["liq"]/d["mc"])*100 if d["mc"] else 0,2),
                "buy_sell_ratio": round(d["buys1h"]/max(d["sells1h"],1),2),
                "label_outcome": "",
                "mc_after_3d": "",
                "ml_predicted_mc": "",
                "ml_confidence": ""
            }

            pmc, conf = ml_predict(model, row)
            row["ml_predicted_mc"] = pmc
            row["ml_confidence"] = conf

            df.loc[len(df)] = row

            results.append({
                "symbol": d["symbol"],
                "chain": d["chain"],
                "mc": int(d["mc"]),
                "liq": int(d["liq"]),
                "age": d["age"],
                "rsi5": rsi5,
                "rsi15": rsi15,
                "pmc": pmc,
                "conf": conf
            })

        save_csv(df, sha)

    return render_template_string(
        HTML,
        sc=len(df),
        lb=df["label_outcome"].notna().sum(),
        results=results,
        cas=cas_text
    )

@app.route("/backtest")
def backtest():
    df, _ = load_csv()
    bt = df.dropna(subset=["mc_after_3d","ml_predicted_mc"])
    if len(bt) < 20:
        return "Not enough data for backtest yet"
    corr,_ = spearmanr(bt["ml_predicted_mc"], bt["mc_after_3d"])
    return f"""
    <h2>Backtest</h2>
    Samples: {len(bt)}<br>
    Correlation: {round(corr,3)}<br>
    <a href="/">Back</a>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
