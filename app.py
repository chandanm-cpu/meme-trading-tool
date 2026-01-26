from flask import Flask, request, render_template_string
import requests
import time

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profit Oracle</title>
<style>
body { font-family: Arial; background:#0f172a; color:#e5e7eb; margin:0 }
.container { padding:16px; max-width:480px; margin:auto }
textarea { width:100%; height:120px; border-radius:8px; padding:10px }
button { width:100%; padding:12px; background:#22c55e; border:none; border-radius:8px; margin-top:10px }
.card { background:#1e293b; padding:12px; margin-top:12px; border-radius:10px }
.ca { font-size:12px; word-break:break-all; color:#94a3b8 }
.pred { color:#38bdf8; margin-top:6px }
</style>
</head>
<body>
<div class="container">
<h2>📊 Profit Oracle (Live Data)</h2>
<form method="post">
<textarea name="cas" placeholder="Enter Contract Addresses (one per line)"></textarea>
<button type="submit">Run Prediction</button>
</form>

{% for r in results %}
<div class="card">
<div>{{ r.conf }} {{ r.symbol }}</div>
<div class="ca">{{ r.ca }}</div>
<div>Price: ${{ r.price }}</div>
<div>FDV (MC): ${{ r.mc }}</div>
<div>Liquidity: ${{ r.liq }}</div>
<div class="pred">📈 Predicted MC: ${{ r.pred_mc }} ({{ r.pct }}%)</div>
</div>
{% endfor %}
</div>
</body>
</html>
"""

def fetch_dexscreener(ca):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    r = requests.get(url, timeout=10)
    data = r.json()
    if "pairs" not in data or len(data["pairs"]) == 0:
        return None
    pair = data["pairs"][0]
    return {
        "symbol": pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
        "price": float(pair.get("priceUsd", 0)),
        "liq": float(pair.get("liquidity", {}).get("usd", 0)),
        "mc": float(pair.get("fdv", 0)),
        "buy_sell": 1.2,   # placeholder until trade parsing added
        "chaos": 0.3       # placeholder until volatility calc added
    }

def score(up, chaos):
    return up * (1 - chaos)

def confidence(score):
    if score >= 0.45: return "🟢"
    if score >= 0.25: return "🟡"
    return "🔴"

@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    if request.method == "POST":
        cas = request.form.get("cas","").splitlines()

        for ca in cas:
            ca = ca.strip()
            if not ca: continue

            d = fetch_dexscreener(ca)
            if not d or d["price"] == 0 or d["mc"] == 0:
                continue

            upside = 0.35 if d["liq"] > 30000 else 0.2
            final = score(upside, d["chaos"])
            conf = confidence(final)
            if conf == "🔴": continue

            pred_mc = int(d["mc"] * (1 + final))

            results.append({
                "conf": conf,
                "symbol": d["symbol"],
                "ca": ca,
                "price": round(d["price"], 8),
                "mc": int(d["mc"]),
                "liq": int(d["liq"]),
                "pred_mc": pred_mc,
                "pct": round(final * 100, 2)
            })

    return render_template_string(HTML, results=results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
