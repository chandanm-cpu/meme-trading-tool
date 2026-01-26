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
<meta http-equiv="refresh" content="10">
<style>
body { font-family: Arial; background:#0f172a; color:#e5e7eb; margin:0 }
.container { padding:16px; max-width:520px; margin:auto }
textarea { width:100%; height:120px; border-radius:8px; padding:10px }
button { width:100%; padding:12px; background:#22c55e; border:none; border-radius:8px; margin-top:10px }
.card { background:#1e293b; padding:12px; margin-top:12px; border-radius:10px }
.ca { font-size:12px; word-break:break-all; color:#94a3b8 }
.pred { color:#38bdf8; margin-top:6px }
.bad { color:#f87171 }
.warn { color:#facc15 }
</style>
</head>
<body>
<div class="container">
<h2>📊 Profit Oracle (Live)</h2>
<p style="font-size:12px;color:#94a3b8">Auto-refresh every 10 seconds</p>
<form method="post">
<textarea name="cas" placeholder="Enter Contract Addresses (one per line)">{{cas}}</textarea>
<button type="submit">Run Prediction</button>
</form>

{% for r in results %}
<div class="card">
<div>{{ r.conf }} {{ r.symbol }}</div>
<div class="ca">{{ r.ca }}</div>
<div>Price: ${{ r.price }}</div>
<div>FDV (MC): ${{ r.mc }}</div>
<div>Liquidity: ${{ r.liq }}</div>
<div>Buys/Sells (24h): {{ r.buys }} / {{ r.sells }}</div>
<div class="pred">📈 Predicted MC: ${{ r.pred_mc }} ({{ r.pct }}%)</div>
<div class="{{ r.risk_class }}">Risk Note: {{ r.risk_note }}</div>
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
    if "pairs" not in data or not data["pairs"]:
        return None

    pair = data["pairs"][0]

    txns = pair.get("txns", {}).get("h24", {})
    buys = txns.get("buys", 0)
    sells = txns.get("sells", 0)

    return {
        "symbol": pair.get("baseToken", {}).get("symbol", "UNKNOWN"),
        "price": float(pair.get("priceUsd") or 0),
        "liq": float(pair.get("liquidity", {}).get("usd") or 0),
        "mc": float(pair.get("fdv") or 0),
        "buys": buys,
        "sells": sells
    }

def confidence(score):
    if score >= 0.45: return "🟢"
    if score >= 0.25: return "🟡"
    return "🔴"

@app.route("/", methods=["GET", "POST"])
def index():
    results = []
    cas_text = ""

    if request.method == "POST":
        cas_text = request.form.get("cas","")
        cas = cas_text.splitlines()

        for ca in cas:
            ca = ca.strip()
            if not ca:
                continue

            d = fetch_dexscreener(ca)
            if not d or d["price"] == 0 or d["mc"] == 0:
                continue

            # ---------- BUY / SELL LOGIC ----------
            total_tx = d["buys"] + d["sells"]
            buy_sell_ratio = d["buys"] / max(d["sells"], 1)

            # ---------- DEV RISK HEURISTIC (FREE) ----------
            if total_tx < 20:
                risk_note = "Very early / low activity"
                risk_class = "warn"
            elif d["sells"] > d["buys"] * 1.5:
                risk_note = "Heavy sell pressure (possible dev unload)"
                risk_class = "bad"
            else:
                risk_note = "No obvious dev selling"
                risk_class = ""

            # ---------- SCORING ----------
            upside = 0.25
            if d["liq"] > 30000:
                upside += 0.15
            if buy_sell_ratio > 1.2:
                upside += 0.2

            chaos_penalty = min(d["sells"] / max(total_tx,1), 0.5)
            final_score = upside * (1 - chaos_penalty)

            conf = confidence(final_score)

            pred_mc = int(d["mc"] * (1 + final_score))

            results.append({
                "conf": conf,
                "symbol": d["symbol"],
                "ca": ca,
                "price": round(d["price"], 8),
                "mc": int(d["mc"]),
                "liq": int(d["liq"]),
                "buys": d["buys"],
                "sells": d["sells"],
                "pred_mc": pred_mc,
                "pct": round(final_score * 100, 2),
                "risk_note": risk_note,
                "risk_class": risk_class
            })

    return render_template_string(HTML, results=results, cas=cas_text)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
