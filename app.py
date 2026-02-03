import sys
import os
import csv
import time
import datetime
import requests
import pandas as pd
import joblib
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier

# =====================================================
# BASIC SETUP
# =====================================================
app = Flask(__name__)

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
DATA_FILE = "coin_data.csv"
MODEL_FILE = "ml_model.pkl"

LABEL_AFTER_DAYS = 3
MIN_ROWS_TO_TRAIN = 50

IS_CRON = "--cron" in sys.argv

# =====================================================
# CREATE CSV IF NOT EXISTS
# =====================================================
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp",
            "ca",
            "mc_at_scan",
            "liq",
            "lmc",
            "buys",
            "sells",
            "accel",
            "tier",
            "mc_latest",
            "outcome",
            "labeled_at"
        ])

# =====================================================
# HELPERS
# =====================================================
def fetch_dex(ca):
    try:
        r = requests.get(DEX_URL + ca, timeout=10).json()
        if not r.get("pairs"):
            return None
        p = r["pairs"][0]
        tx = p.get("txns", {}).get("h24", {})
        return {
            "mc": float(p.get("fdv") or 0),
            "liq": float(p.get("liquidity", {}).get("usd") or 0),
            "buys": tx.get("buys", 0),
            "sells": tx.get("sells", 0),
        }
    except:
        return None

def save_snapshot(ca, data):
    mc = data["mc"]
    liq = data["liq"]
    lmc = round((liq / mc) * 100, 2) if mc else 0
    buys = data["buys"]
    sells = max(data["sells"], 1)
    accel = buys  # simple for now
    tier = "UNKNOWN"

    with open(DATA_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.datetime.utcnow().isoformat(),
            ca,
            mc,
            liq,
            lmc,
            buys,
            sells,
            accel,
            tier,
            "",
            "",
            ""
        ])

# =====================================================
# AUTO LABEL (RUN BY CRON)
# =====================================================
def auto_label():
    df = pd.read_csv(DATA_FILE)
    now = datetime.datetime.utcnow()

    changed = False

    for i, row in df.iterrows():
        if pd.notna(row["outcome"]):
            continue

        scan_time = datetime.datetime.fromisoformat(row["timestamp"])
        if (now - scan_time).days < LABEL_AFTER_DAYS:
            continue

        d = fetch_dex(row["ca"])
        if not d:
            outcome = "RUG"
            mc_new = 0
        else:
            mc_new = d["mc"]
            ratio = mc_new / row["mc_at_scan"] if row["mc_at_scan"] else 0

            if ratio < 0.5:
                outcome = "RUG"
            elif ratio < 1.5:
                outcome = "FLAT"
            elif ratio < 5:
                outcome = "2X_5X"
            elif ratio < 20:
                outcome = "5X_20X"
            else:
                outcome = "20X_PLUS"

        df.at[i, "mc_latest"] = mc_new
        df.at[i, "outcome"] = outcome
        df.at[i, "labeled_at"] = now.isoformat()
        changed = True

    if changed:
        df.to_csv(DATA_FILE, index=False)

# =====================================================
# TRAIN ML (RUN BY CRON)
# =====================================================
def train_model():
    df = pd.read_csv(DATA_FILE)
    df = df.dropna(subset=["outcome"])

    if len(df) < MIN_ROWS_TO_TRAIN:
        return False

    X = df[["lmc", "buys", "sells", "accel"]]
    y = df["outcome"]

    model = RandomForestClassifier(
        n_estimators=200,
        max_depth=8,
        min_samples_leaf=20,
        random_state=42
    )
    model.fit(X, y)
    joblib.dump(model, MODEL_FILE)
    return True

# =====================================================
# CRON TASKS
# =====================================================
def cron_tasks():
    print("CRON STARTED")
    auto_label()
    trained = train_model()
    if trained:
        print("MODEL TRAINED")
    else:
        print("NOT ENOUGH DATA")
    print("CRON FINISHED")

# =====================================================
# WEB APP (VERY SIMPLE)
# =====================================================
HTML = """
<h2>ML Scanner</h2>
<form method="post">
<textarea name="cas" style="width:100%;height:120px"></textarea><br>
<button>Scan</button>
</form>
<pre>{{msg}}</pre>
"""

@app.route("/", methods=["GET", "POST"])
def index():
    msg = ""
    if request.method == "POST":
        cas = request.form.get("cas", "")
        for ca in cas.splitlines():
            ca = ca.strip()
            if not ca:
                continue
            d = fetch_dex(ca)
            if d:
                save_snapshot(ca, d)
                msg += f"Saved {ca}\n"
    return render_template_string(HTML, msg=msg)

# =====================================================
# ENTRY POINT
# =====================================================
if __name__ == "__main__":
    if IS_CRON:
        cron_tasks()
    else:
        port = int(os.environ.get("PORT", 10000))
        app.run(host="0.0.0.0", port=port)
