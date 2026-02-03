import os
import csv
import datetime
import requests
import pandas as pd
import joblib
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier

# ================= BASIC SETUP =================
app = Flask(__name__)

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
DATA_FILE = "coin_data.csv"
MODEL_FILE = "ml_model.pkl"
STATE_FILE = "state.txt"

LABEL_AFTER_DAYS = 3
MIN_ROWS_TO_TRAIN = 50
DECAY_DAYS = 14

# Tier thresholds
MC_MIN = 20_000
MC_MAX = 400_000
LMC_MIN = 3
LMC_MAX = 10
BUYSELL_STRONG = 1.3
ACCEL_STRONG = 8

# Viral madness thresholds
VIRAL_MC_MAX = 200_000
VIRAL_BUY_RATIO = 1.5
VIRAL_TX_MIN = 30

# ================= INIT CSV =================
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp","ca",
            "mc_at_scan","liq","lmc",
            "buys","sells","accel",
            "tier","mc_latest","outcome","labeled_at"
        ])

# ================= HELPERS =================
def fetch_dex(ca):
    try:
        r = requests.get(DEX_URL + ca, timeout=10).json()
        if not r.get("pairs"):
            return None
        p = r["pairs"][0]
        tx = p.get("txns", {}).get("h24", {})
        return {
            "name": p["baseToken"]["name"],
            "symbol": p["baseToken"]["symbol"],
            "mc": float(p.get("fdv") or 0),
            "liq": float(p.get("liquidity", {}).get("usd") or 0),
            "buys": tx.get("buys", 0),
            "sells": tx.get("sells", 0),
        }
    except:
        return None

# ================= TIER LOGIC =================
def tier_logic(mc, liq, buys, sells, accel):
    if mc < MC_MIN or mc > MC_MAX:
        return "❌ Tier D (MC out of range)"

    lmc = (liq / mc) * 100 if mc else 0
    bs = buys / max(sells, 1)

    p1 = LMC_MIN <= lmc <= LMC_MAX
    p2 = bs > BUYSELL_STRONG
    p3 = accel >= ACCEL_STRONG

    if p1 and p2 and p3:
        return "🟢 Tier A (Confirmed)"
    if p1 and (p2 or p3):
        return "🔵 Tier A (Early)"
    if p1:
        return "👀 Tier B"
    if p2 or p3:
        return "👀 Tier C"
    return "❌ Tier D"

# ================= VIRAL MADNESS =================
def viral_madness(mc, buys, sells):
    tx = buys + sells
    bs = buys / max(sells, 1)

    if (
        mc <= VIRAL_MC_MAX and
        tx >= VIRAL_TX_MIN and
        bs >= VIRAL_BUY_RATIO
    ):
        return True
    return False

# ================= SAVE SNAPSHOT =================
def save_snapshot(ca, d, tier):
    mc = d["mc"]
    liq = d["liq"]
    lmc = round((liq / mc) * 100, 2) if mc else 0
    buys = d["buys"]
    sells = max(d["sells"], 1)
    accel = buys

    with open(DATA_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.datetime.utcnow().isoformat(),
            ca, mc, liq, lmc,
            buys, sells, accel,
            tier, "", "", ""
        ])

# ================= AUTO LABEL =================
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

# ================= TRAIN ML =================
def train_model():
    df = pd.read_csv(DATA_FILE)
    df = df.dropna(subset=["outcome"])

    if len(df) < MIN_ROWS_TO_TRAIN:
        return False

    X = df[["lmc","buys","sells","accel"]]
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

# ================= LAZY CRON =================
def lazy_cron():
    today = datetime.date.today().isoformat()
    if os.path.exists(STATE_FILE) and open(STATE_FILE).read().strip() == today:
        return
    auto_label()
    train_model()
    with open(STATE_FILE, "w") as f:
        f.write(today)

# ================= ML PREDICTION =================
def ml_predict(lmc, buys, sells, accel):
    if not os.path.exists(MODEL_FILE):
        return "ML not ready"
    model = joblib.load(MODEL_FILE)
    probs = model.predict_proba([[lmc, buys, sells, accel]])[0]
    labels = model.classes_
    return ", ".join(f"{k}:{int(v*100)}%" for k,v in sorted(dict(zip(labels, probs)).items(), key=lambda x: -x[1]))

def ml_confidence():
    if not os.path.exists(STATE_FILE):
        return 0, "Never"
    last = datetime.date.fromisoformat(open(STATE_FILE).read().strip())
    days = (datetime.date.today() - last).days
    freshness = max(0, 1 - (days / DECAY_DAYS))
    return int(freshness * 100), ("Today" if days == 0 else f"{days} days ago")

# ================= UI =================
HTML = """
<h2>🧠 ML + Tier + Viral Scanner</h2>
<p><b>Last Learned:</b> {{last}} | <b>ML Confidence:</b> {{conf}}%</p>

<form method="post">
<textarea name="cas" style="width:100%;height:120px"></textarea><br>
<button>Scan Coins</button>
</form>

{% for r in results %}
<hr>
<b>{{r.name}} ({{r.symbol}})</b><br>
CA: {{r.ca}}<br>
MC: ${{r.mc}} | LP: ${{r.liq}}<br>
Buys/Sells: {{r.buys}} / {{r.sells}}<br>
<b>{{r.tier}}</b><br>
{% if r.viral %}
🔥 <b>Viral Madness Detected</b><br>
{% endif %}
<b>ML Prediction:</b> {{r.ml}}
{% endfor %}
"""

@app.route("/", methods=["GET","POST"])
def index():
    lazy_cron()
    conf, last = ml_confidence()

    results = []
    if request.method == "POST":
        for ca in request.form.get("cas","").splitlines():
            ca = ca.strip()
            if not ca:
                continue
            d = fetch_dex(ca)
            if not d:
                continue

            tier = tier_logic(d["mc"], d["liq"], d["buys"], d["sells"], d["buys"])
            viral = viral_madness(d["mc"], d["buys"], d["sells"])

            save_snapshot(ca, d, tier)

            lmc = (d["liq"] / d["mc"]) * 100 if d["mc"] else 0
            ml = ml_predict(lmc, d["buys"], d["sells"], d["buys"])

            results.append({
                **d,
                "ca": ca,
                "tier": tier,
                "viral": viral,
                "ml": ml
            })

    return render_template_string(
        HTML,
        results=results,
        conf=conf,
        last=last
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
