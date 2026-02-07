import os, io, time, datetime, base64, requests, math
import pandas as pd
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier
from scipy.stats import spearmanr

# ===================== ENV =====================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = os.environ.get("GITHUB_REPO")
GITHUB_FILE  = os.environ.get("GITHUB_FILE", "coin_data.csv")

if not GITHUB_TOKEN or not GITHUB_REPO:
    raise RuntimeError("GitHub env vars missing")

API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE}"
HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
MIN_TRAIN_ROWS = 50
AUTO_REFRESH = 10
LABEL_AFTER_HOURS = 72

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

def fetch_current_mc(ca):
    try:
        r = requests.get(DEX_URL + ca, timeout=10).json()
        if not r.get("pairs"):
            return None
        return float(r["pairs"][0].get("fdv") or 0)
    except:
        return None

# ===================== AUTO RECHECK (72h) =====================
def auto_recheck_and_label(df):
    now = datetime.datetime.utcnow()
    updated = False

    for idx, row in df.iterrows():
        if pd.notna(row["label_outcome"]):
            continue

        try:
            ts = datetime.datetime.fromisoformat(row["timestamp"])
        except:
            continue

        if (now - ts).total_seconds() < LABEL_AFTER_HOURS * 3600:
            continue

        current_mc = fetch_current_mc(row["ca"])
        if not current_mc or current_mc <= 0:
            continue

        ratio = current_mc / max(row["market_cap"], 1)

        if ratio <= 0.3:
            label = "RUG"
        elif ratio <= 0.9:
            label = "FLAT"
        elif ratio <= 2:
            label = "2X"
        elif ratio <= 5:
            label = "5X"
        elif ratio <= 10:
            label = "10X"
        else:
            label = "20X"

        df.at[idx, "label_outcome"] = label
        df.at[idx, "mc_after_3d"] = int(current_mc)
        updated = True

    return df, updated

# ===================== ML =====================
MULT={"RUG":0.2,"FLAT":1,"2X":3,"5X":7,"10X":15,"20X":30}

def train_ml(df):
    df=df.dropna(subset=["label_outcome"])
    if len(df)<MIN_TRAIN_ROWS:
        return None
    X=df[["liq_to_mc","buy_sell_ratio","buys_5m","buys_1h",
          "volume_5m","volume_1h","age_minutes","rsi_5m","rsi_15m"]]
    y=df["label_outcome"]
    m=RandomForestClassifier(n_estimators=300,max_depth=10,min_samples_leaf=10)
    m.fit(X,y)
    return m

def ml_predict(m,row):
    if m is None:
        return row["market_cap"], 0
    X=[[row["liq_to_mc"],row["buy_sell_ratio"],
        row["buys_5m"],row["buys_1h"],
        row["volume_5m"],row["volume_1h"],
        row["age_minutes"],row["rsi_5m"],row["rsi_15m"]]]
    probs=dict(zip(m.classes_,m.predict_proba(X)[0]))
    ev=sum(probs[k]*MULT[k] for k in probs)
    return int(row["market_cap"]*ev), min(100,int(ev*6))

# ===================== HTML =====================
HTML = """
<meta http-equiv="refresh" content="{{refresh}}">
<h2>📱 Meme Trading Tool (Auto-Recheck Enabled)</h2>

<p>
Scanned: <b>{{sc}}</b> |
Labeled: <b>{{lb}}</b> |
ML Ready: <b>{{ready}}%</b>
</p>

<form method="post">
<textarea name="cas" style="width:100%;height:120px;"></textarea><br>
<button style="padding:14px 28px;font-size:18px;">🚀 Scan</button>
</form>

{% for r in results %}
<hr>
<b>{{r.symbol}}</b><br>
Current MC: ${{r.mc}}<br>
Structural: ${{r.struct}}<br>
ML Expected: ${{r.ml}} (Conf {{r.conf}})
{% endfor %}
"""

# ===================== ROUTE =====================
@app.route("/", methods=["GET","POST"])
def index():
    df, sha = load_csv()

    # AUTO RECHECK HERE
    df, updated = auto_recheck_and_label(df)
    if updated:
        save_csv(df, sha)
        df, sha = load_csv()

    labeled = df["label_outcome"].notna().sum()
    ready = min(100, int(labeled / MIN_TRAIN_ROWS * 100))
    model = train_ml(df)

    results = []

    if request.method == "POST":
        for ca in request.form.get("cas","").splitlines():
            mc = fetch_current_mc(ca.strip())
            if not mc:
                continue

            row = {
                "market_cap": mc,
                "liq_to_mc": 0,
                "buy_sell_ratio": 1,
                "buys_5m": 0,
                "buys_1h": 0,
                "volume_5m": 0,
                "volume_1h": 0,
                "age_minutes": 60,
                "rsi_5m": 50,
                "rsi_15m": 50
            }

            ml_mc, conf = ml_predict(model, row)

            results.append({
                "symbol": ca[:6],
                "mc": int(mc),
                "struct": "see above",
                "ml": ml_mc,
                "conf": conf
            })

    return render_template_string(
        HTML,
        results=results,
        sc=len(df),
        lb=labeled,
        ready=ready,
        refresh=AUTO_REFRESH
    )

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
