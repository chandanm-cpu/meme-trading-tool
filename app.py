from flask import Flask, request, render_template_string
import requests

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>100× Filter Dashboard</title>
<style>
body { font-family: Arial; background:#0f172a; color:#e5e7eb; margin:0 }
.container { padding:16px; max-width:560px; margin:auto }
textarea { width:100%; height:120px; border-radius:8px; padding:10px }
button { width:100%; padding:12px; background:#22c55e; border:none; border-radius:8px; margin-top:10px }
.card { background:#1e293b; padding:12px; margin-top:12px; border-radius:10px }
.ca { font-size:12px; word-break:break-all; color:#94a3b8 }
.good { color:#4ade80 }
.warn { color:#facc15 }
.bad { color:#f87171 }
.score { font-size:13px; margin-top:4px }
</style>
</head>
<body>
<div class="container">
<h2>🔍 100× Potential Filter</h2>
<p style="font-size:12px;color:#94a3b8">
Shows ALL coins. No hiding. Uses free live data.
</p>

<form method="post">
<textarea name="cas" placeholder="Paste Contract Addresses (one per line)">{{cas}}</textarea>
<button type="submit">Analyze Coins</button>
</form>

{% for r in results %}
<div class="card">
<div class="{{r.color}}">
<b>{{ r.status }}</b>
</div>

<div><b>{{ r.name }}</b> ({{ r.symbol }})</div>
<div class="ca">{{ r.ca }}</div>

<div class="score">💰 Market Cap (FDV): ${{ r.mc }}</div>
<div class="score">🔒 Scarcity Score: {{ r.scarcity }}%</div>
<div class="score">⚡ Demand Score: {{ r.demand }}%</div>
<div class="score">💧 Liquidity / MC: {{ r.liq_ratio }}%</div>
<div class="score">📊 Buys / Sells (24h): {{ r.buys }} / {{ r.sells }}</div>
</div>
{% endfor %}
</div>
</body>
</html>
"""

def fetch(ca):
    url = f"https://api.dexscreener.com/latest/dex/tokens/{ca}"
    r = requests.get(url, timeout=10)
    j = r.json()
    if "pairs" not in j or not j["pairs"]:
        return None
    p = j["pairs"][0]
    tx = p.get("txns", {}).get("h24", {})
    return {
        "name": p.get("baseToken", {}).get("name", "Unknown"),
        "symbol": p.get("baseToken", {}).get("symbol", "UNKNOWN"),
        "fdv": float(p.get("fdv") or 0),
        "liq": float(p.get("liquidity", {}).get("usd") or 0),
        "buys": tx.get("buys", 0),
        "sells": tx.get("sells", 0)
    }

def scarcity_score(fdv, liq, buys, sells):
    if fdv == 0: return 0
    score = 0
    ratio = liq / fdv
    if ratio < 0.10: score += 40
    if ratio < 0.05: score += 30
    if sells < buys * 0.8: score += 20
    if sells < 20: score += 10
    return min(score, 100)

def demand_score(buys, sells):
    score = 0
    total = buys + sells
    ratio = buys / max(sells, 1)
    if ratio > 1.3: score += 30
    if ratio > 1.6: score += 30
    if buys > 100: score += 20
    if total > 200: score += 20
    return min(score, 100)

@app.route("/", methods=["GET","POST"])
def index():
    results = []
    cas_text = ""

    if request.method == "POST":
        cas_text = request.form.get("cas","")
        for ca in cas_text.splitlines():
            ca = ca.strip()
            if not ca:
                continue

            d = fetch(ca)
            if not d or d["fdv"] == 0:
                continue

            scarcity = scarcity_score(d["fdv"], d["liq"], d["buys"], d["sells"])
            demand = demand_score(d["buys"], d["sells"])
            liq_ratio = round((d["liq"] / d["fdv"]) * 100, 2)
            mc = int(d["fdv"])

            if scarcity >= 70 and demand >= 70:
                status, color = "🚀 POTENTIAL 100×", "good"
            elif scarcity >= 70:
                status, color = "👀 WATCH (Scarcity)", "warn"
            elif demand >= 70:
                status, color = "👀 WATCH (Demand)", "warn"
            else:
                status, color = "❌ AVOID", "bad"

            results.append({
                "ca": ca,
                "name": d["name"],
                "symbol": d["symbol"],
                "mc": mc,
                "scarcity": scarcity,
                "demand": demand,
                "liq_ratio": liq_ratio,
                "buys": d["buys"],
                "sells": d["sells"],
                "status": status,
                "color": color
            })

    return render_template_string(HTML, results=results, cas=cas_text)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
