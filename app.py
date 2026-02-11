import os, io, time, datetime, base64, requests
import pandas as pd
from flask import Flask, request, render_template_string
from scipy.stats import spearmanr

# ================= CONFIG =================
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPO  = os.environ.get("GITHUB_REPO")
GITHUB_FILE  = os.environ.get("GITHUB_FILE","coin_data.csv")

DEX_URL = "https://api.dexscreener.com/latest/dex/tokens/"
LABEL_AFTER_HOURS = 72

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
        r = requests.get(DEX_URL+ca, timeout=10).json()
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

# ================= BUCKETING =================
def bucket_age(m):
    if m < 15: return "<15m"
    if m < 60: return "15-60m"
    if m < 360: return "1-6h"
    return ">6h"

def bucket_liq(x):
    if x < 5: return "<5%"
    if x < 15: return "5-15%"
    if x < 30: return "15-30%"
    return ">30%"

def bucket_ratio(x):
    if x < 0.7: return "<0.7"
    if x < 1.2: return "0.7-1.2"
    if x < 2: return "1.2-2"
    return ">2"

# ================= ORACLE CORE =================
def oracle_stats(df, row):
    if len(df) < 20:
        return None

    df2 = df.copy()
    df2["age_b"] = df2["age_minutes"].apply(bucket_age)
    df2["liq_b"] = df2["liq_to_mc"].apply(bucket_liq)
    df2["ratio_b"] = df2["buy_sell_ratio"].apply(bucket_ratio)

    mask = (
        (df2["age_b"] == bucket_age(row["age_minutes"])) &
        (df2["liq_b"] == bucket_liq(row["liq_to_mc"])) &
        (df2["ratio_b"] == bucket_ratio(row["buy_sell_ratio"]))
    )

    sample = df2[mask]
    if len(sample) < 5:
        return None

    non_rug = sample[~sample["label_outcome"].fillna("").str.contains("RUG")]
    surv = int(len(non_rug) / len(sample) * 100)

    upside = None
    if len(non_rug) >= 3:
        mult = non_rug["mc_after_3d"] / non_rug["market_cap"]
        upside = {
            "median": round(mult.median(),2),
            "p80": round(mult.quantile(0.8),2),
            "max": round(mult.max(),2)
        }

    rarity = int((1 - len(sample)/max(len(df2),1)) * 100)

    score = int(
        0.45 * surv +
        0.30 * (upside["median"]*10 if upside else 0) +
        0.15 * rarity +
        0.10 * min(row["buy_sell_ratio"]*20,100)
    )
    score = max(0,min(100,score))

    return surv, upside, rarity, score

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

            stats = oracle_stats(df, row)
            if stats:
                surv, up, rarity, score = stats
            else:
                surv, up, rarity, score = 0, None, 0, 0

            results.append({
                "symbol":d["symbol"],
                "mc":int(d["mc"]),
                "liq":int(d["liq"]),
                "surv":surv,
                "median": up["median"] if up else "-",
                "p80": up["p80"] if up else "-",
                "max": up["max"] if up else "-",
                "rarity":rarity,
                "score":score
            })

        save_csv(df,sha)

    html="""
    <h2 style="font-size:42px;">🧠 Lightweight Meme Oracle</h2>
    <p style="font-size:26px;">Scanned: {{scanned}} | Labeled: {{labeled}}</p>

    <form method="post">
      <textarea name="cas" style="width:100%;height:140px;font-size:26px;"></textarea><br>
      <button style="font-size:34px;padding:20px;">🚀 Scan</button>
    </form>

    <form method="post" action="/auto_label">
      <button style="font-size:20px;padding:10px;">🧠 Auto Label</button>
    </form>

    <table border="1" cellpadding="18" style="font-size:44px;margin-top:20px;">
      <tr>
        <th>Coin</th><th>MC</th><th>Liq</th>
        <th>Surv%</th>
        <th>Median×</th><th>80%×</th><th>Max×</th>
        <th>Rarity</th><th>Oracle</th>
      </tr>
      {% for r in results %}
      <tr>
        <td>{{r.symbol}}</td><td>${{r.mc}}</td><td>${{r.liq}}</td>
        <td>{{r.surv}}</td>
        <td>{{r.median}}</td><td>{{r.p80}}</td><td>{{r.max}}</td>
        <td>{{r.rarity}}</td>
        <td>{{r.score}}</td>
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

if __name__=="__main__":
    app.run(host="0.0.0.0",port=int(os.environ.get("PORT",10000)))
