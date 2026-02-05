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

# ===================== CSV HEADER (LOCKED) =====================
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

# ===================== CSV HELPERS =====================
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
    rs = buys / max(sells, 1)
    return round(100 - (100 / (1 + rs)), 2)

# ===================== FETCH DEX DATA =====================
def fetch_dex(ca):
    r = requests.get(DEX_URL + ca, timeout=10).json()
    if not r.get("pairs"):
        return None
    p = r["pairs"][0]

    tx5 = p.get("txns", {}).get("m5", {})
    tx1h = p.get("txns", {}).get("h1", {})
    tx24 = p.get("txns", {}).get("h24", {})
    vol = p.get("volume", {})

    created = p.get("pairCreatedAt", int(time.time() * 1000))
    age = int((time.time() * 1000 - created) / 60000)

    return {
        "symbol": p["baseToken"]["symbol"],
        "chain": p["chainId"],
        "price": float(p.get("priceUsd") or 0),
        "mc": float(p.get("fdv") or 0),
        "liq": float(p.get("liquidity", {}).get("usd") or 0),
        "buys5": tx5.get("buys", 0),
        "sells5": tx5.get("sells", 0),
        "buys1h": tx1h.get("buys", 0),
        "sells1h": tx1h.get("sells", 0),
        "tx24": tx24.get("buys", 0) + tx24.get("sells", 0),
        "vol5": float(vol.get("m5", 0)),
        "vol1h": float(vol.get("h1", 0)),
        "vol24": float(vol.get("h24", 0)),
        "age": age
    }

# ===================== ORACLE SCORES =====================
def insider_proxy(d):
    score = 0
    if d["buys1h"] > d["sells1h"] * 2:
        score += 35
    if d["liq"] > d["mc"] * 0.08:
        score += 35
    if d["age"] < 120:
        score += 20
    return min(score, 100)

def narrative_proxy(d):
    score = 0
    if d["vol1h"] > d["vol5"] * 2:
        score += 35
    if d["tx24"] > 100:
        score += 35
    if 40 < rsi(d["buys5"], d["sells5"]) < 70:
        score += 20
    return min(score, 100)

# ===================== ML =====================
MULT = {
    "RUG": 0.2,
    "FLAT": 1,
    "2X": 3,
    "5X": 7,
    "10X": 15,
    "20X": 30,
    "50X": 60,
    "100X": 120
}

def train_ml(df):
    df = df.dropna(subset=["label_outcome"])
    if len(df) < MIN_TRAIN_ROWS:
        return None

    X = df[[
        "liq_to_mc","buy_sell_ratio",
        "buys_5m","buys_1h",
        "volume_5m","volume_1h",
        "age_minutes","rsi_5m","rsi_15m"
    ]]
    y = df["label_outcome"]

    model = RandomForestClassifier(
        n_estimators=300,
        max_depth=10,
        min_samples_leaf=10,
        random_state=42
    )
    model.fit(X, y)
    return model

def ml_predict(model, row):
    if model is None:
        return {}, row["market_cap"], 0

    X = [[
        row["liq_to_mc"], row["buy_sell_ratio"],
        row["buys_5m"], row["buys_1h"],
        row["volume_5m"], row["volume_1h"],
        row["age_minutes"], row["rsi_5m"], row["rsi_15m"]
    ]]

    probs = dict(zip(model.classes_, model.predict_proba(X)[0]))
    ev = sum(probs[k] * MULT[k] for k in probs)
    confidence = min(100, int(ev * 6))

    return probs, int(row["market_cap"] * ev), confidence

# ===================== UI =====================
HTML = """
<meta http-equiv="refresh" content="10">

<h2>
📱 Meme Trading Tool
&nbsp;&nbsp;
<a href="/backtest">📊 Backtest</a>
</h2>

<p>
Scanned: <b>{{sc}}</b> | Labeled: <b>{{lb}}</b>
</p>

<form method="post">
<textarea name="cas" style="width:100%;height:120px;">{{cas}}</textarea><br>
<button style="padding:14px 28px;font-size:18px;">🚀 Scan</button>
</form>

{% if results %}
<hr>
{% for r in results %}
<b>{{r.symbol}}</b> ({{r.chain}})<br>
Pred MC: ${{r.pmc}} | ML Confidence: {{r.conf}} / 100<br>

Insider Proxy: {{r.ins}} / 100<br>
Narrative Readiness: {{r.nar}} / 100<br>

Rug {{r.p.get("RUG",0)}}% |
1–2x {{r.p.get("FLAT",0)}}% |
5–10x {{r.p.get("5X",0)}}% |
10–20x {{r.p.get("10X",0)}}% |
20–50x {{r.p.get("20X",0)}}% |
100x {{r.p.get("100X",0)}}%
<hr>
{% endfor %}
{% endif %}
"""

@app.route("/", methods=["GET","POST"])
def index():
    df, sha = load_csv()
    model = train_ml(df)

    results = []
    cas_text = ""

    if request.method == "POST":
        cas_text = request.form.get("cas","")
        for ca in cas_text.splitlines():
            d = fetch_dex(ca.strip())
            if not d:
                continue

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
                "rsi_5m": rsi(d["buys5"], d["sells5"]),
                "rsi_15m": rsi(d["buys1h"], d["sells1h"]),
                "liq_to_mc": d["liq"]/d["mc"]*100 if d["mc"] else 0,
                "buy_sell_ratio": d["buys1h"]/max(d["sells1h"],1),
                "label_outcome": "",
                "mc_after_3d": "",
                "ml_predicted_mc": "",
                "ml_confidence": ""
            }

            probs, pmc, conf = ml_predict(model, row)
            row["ml_predicted_mc"] = pmc
            row["ml_confidence"] = conf
            df.loc[len(df)] = row

            results.append({
                "symbol": d["symbol"],
                "chain": d["chain"],
                "pmc": pmc,
                "conf": conf,
                "ins": insider_proxy(d),
                "nar": narrative_proxy(d),
                "p": {k: int(v * 100) for k, v in probs.items()}
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
    df,_ = load_csv()
    bt = df.dropna(subset=["mc_after_3d","ml_predicted_mc"])
    if len(bt) < 20:
        return "Not enough data for backtest yet"
    corr,_ = spearmanr(bt["ml_predicted_mc"], bt["mc_after_3d"])
    return f"""
    <h2>📊 Backtest</h2>
    Samples: {len(bt)}<br>
    Correlation: {round(corr,3)}<br>
    <a href="/">⬅ Back</a>
    """

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
