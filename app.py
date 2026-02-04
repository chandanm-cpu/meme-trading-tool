import os, csv, time, datetime, requests
import pandas as pd, joblib
from flask import Flask, request, render_template_string
from sklearn.ensemble import RandomForestClassifier
from scipy.stats import spearmanr

# ================= DISK LOCK =================
DISK_PATH = "/data"
if not os.path.exists(DISK_PATH):
    raise RuntimeError("❌ Persistent disk not mounted at /data")

# ================= PATHS =================
DATA_FILE = "/data/coin_data.csv"
MODEL_FILE = "/data/ml_model.pkl"
STATE_FILE = "/data/state.txt"

# ================= CONFIG =================
DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
LABEL_AFTER_DAYS = 3
MIN_ROWS_TO_TRAIN = 50

# ================= CSV SCHEMA =================
CSV_HEADER = [
    "timestamp","ca",
    "mc_at_scan","liq","lmc",
    "buys","sells","accel",
    "age_minutes","tx_count_24h","rsi_15m",
    "predicted_mc_dd",
    "total_scanned_at_time","total_labeled_at_time",
    "tier","mc_latest","outcome","labeled_at"
]

# ================= SAFE CSV INIT =================
if not os.path.exists(DATA_FILE):
    with open(DATA_FILE, "w", newline="") as f:
        csv.writer(f).writerow(CSV_HEADER)

app = Flask(__name__)

# ================= STATUS =================
def disk_status():
    return "OK" if os.path.exists(DISK_PATH) else "MISSING"

def csv_status():
    try:
        with open(DATA_FILE, "r") as f:
            reader = csv.reader(f)
            header = next(reader)
            rows = sum(1 for _ in reader)
        return header == CSV_HEADER, rows
    except:
        return False, 0

# ================= DATA =================
def fetch_dex(ca):
    try:
        r = requests.get(DEX_URL + ca, timeout=10).json()
        if not r.get("pairs"):
            return None
        p = r["pairs"][0]
        created = p.get("pairCreatedAt", int(time.time()*1000))
        age = int((time.time()*1000 - created) / 60000)
        tx = p.get("txns", {}).get("h24", {})
        buys, sells = tx.get("buys",0), tx.get("sells",0)
        return {
            "symbol": p["baseToken"]["symbol"],
            "mc": float(p.get("fdv") or 0),
            "liq": float(p.get("liquidity",{}).get("usd") or 0),
            "buys": buys,
            "sells": sells,
            "tx": buys + sells,
            "age": age
        }
    except:
        return None

def rsi(buys,sells):
    rs = buys / max(sells,1)
    return round(100 - (100/(1+rs)),2)

# ================= ML =================
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

# ================= STATS =================
def stats():
    df = pd.read_csv(DATA_FILE)
    scanned = max(len(df) - 1, 0)     # <-- FIXED
    labeled = df["outcome"].notna().sum()
    return scanned, labeled

# ================= SAVE =================
def save_snapshot(ca,d,pmc):
    df = pd.read_csv(DATA_FILE)
    scanned, labeled = stats()
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
            scanned, labeled,
            "NA","","",""
        ])

# ================= LABEL + TRAIN =================
def auto_label():
    df = pd.read_csv(DATA_FILE)
    now = datetime.datetime.utcnow()
    for i,r in df.iterrows():
        if pd.notna(r["outcome"]): continue
        if (now-datetime.datetime.fromisoformat(r["timestamp"])).days < LABEL_AFTER_DAYS:
            continue
        d = fetch_dex(r["ca"])
        mc2 = d["mc"] if d else 0
        ratio = mc2/r["mc_at_scan"] if r["mc_at_scan"] else 0
        out = "RUG" if ratio<0.5 else "FLAT" if ratio<1.5 else "2X_5X" if ratio<5 else "5X_20X" if ratio<20 else "20X_PLUS"
        df.at[i,"mc_latest"]=mc2
        df.at[i,"outcome"]=out
        df.at[i,"labeled_at"]=now.isoformat()
    df.to_csv(DATA_FILE,index=False)

def train():
    df = pd.read_csv(DATA_FILE).dropna(subset=["outcome"])
    if len(df) < MIN_ROWS_TO_TRAIN: return
    X = df[["lmc","buys","sells","accel","age_minutes","tx_count_24h","rsi_15m"]]
    y = df["outcome"]
    model = RandomForestClassifier(n_estimators=300,max_depth=10,min_samples_leaf=15)
    model.fit(X,y)
    joblib.dump(model,MODEL_FILE)

def lazy():
    today = datetime.date.today().isoformat()
    if os.path.exists(STATE_FILE) and open(STATE_FILE).read().strip() == today:
        return
    auto_label()
    train()
    open(STATE_FILE,"w").write(today)

# ================= UI =================
HTML = """
<meta http-equiv="refresh" content="15">
<h2>ML Scanner</h2>

<p>
🧠 Disk: <b>{{disk}}</b> |
📄 CSV Rows: <b>{{rows}}</b> |
📑 Header: <b>{{header}}</b>
</p>

<a href="/backtest"><button>📊 Backtest</button></a>

<p><b>Scanned:</b> {{sc}} | <b>Labeled:</b> {{lb}}</p>

<form method="post">
<textarea name="cas" style="width:100%;height:120px;">{{cas}}</textarea><br>
<button style="padding:14px 28px;font-size:18px;">🚀 Scan</button>
</form>

{% if results %}
<hr>
{% for r in results %}
<b>{{r.symbol}}</b> | MC ${{r.mc}} | Pred MC ${{r.pmc}} | Size {{r.size}}%<br>
{{r.ml}}<br><br>
{% endfor %}
{% endif %}
"""

@app.route("/", methods=["GET","POST"])
def index():
    lazy()
    sc, lb = stats()
    header_ok, rows = csv_status()
    results = []
    cas_text = ""

    if request.method == "POST":
        cas_text = request.form.get("cas","")
        for ca in cas_text.splitlines():
            d = fetch_dex(ca.strip())
            if not d: continue
            lmc = (d["liq"]/d["mc"])*100 if d["mc"] else 0
            probs, pmc, score = ml_predict(
                d["mc"], lmc, d["buys"], d["sells"],
                d["buys"], d["age"], d["tx"], rsi(d["buys"],d["sells"])
            )
            if probs:
                size = min(int(score*10),5)
                ml_txt = ", ".join(f"{k}:{int(v*100)}%" for k,v in probs.items())
            else:
                size = 0
                ml_txt = "ML collecting data"
            save_snapshot(ca,d,pmc)
            results.append({
                "symbol": d["symbol"],
                "mc": int(d["mc"]),
                "pmc": pmc,
                "size": size,
                "ml": ml_txt
            })

    return render_template_string(
        HTML,
        disk=disk_status(),
        rows=rows,
        header="OK" if header_ok else "INVALID",
        sc=sc, lb=lb,
        results=results,
        cas=cas_text
    )

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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",10000)))
