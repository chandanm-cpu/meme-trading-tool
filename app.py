from flask import Flask, request, render_template_string
import random
import time

app = Flask(__name__)

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Profit Oracle</title>
<style>
body {
  font-family: Arial, sans-serif;
  background: #0f172a;
  color: #e5e7eb;
  margin: 0;
}
.container {
  padding: 16px;
  max-width: 480px;
  margin: auto;
}
h1 {
  text-align: center;
}
textarea {
  width: 100%;
  height: 120px;
  border-radius: 8px;
  padding: 10px;
  margin-bottom: 12px;
  border: none;
}
button {
  width: 100%;
  padding: 12px;
  background: #22c55e;
  border: none;
  border-radius: 8px;
  font-size: 16px;
}
.card {
  background: #1e293b;
  padding: 12px;
  margin-top: 12px;
  border-radius: 10px;
}
.conf {
  font-size: 22px;
}
.ca {
  font-size: 12px;
  word-break: break-all;
  color: #94a3b8;
}
.pred {
  margin-top: 8px;
  color: #38bdf8;
}
</style>
</head>
<body>
<div class="container">
<h1>📊 Profit Oracle</h1>
<form method="post">
<textarea name="cas" placeholder="Enter Contract Addresses (one per line)"></textarea>
<button type="submit">Run Prediction</button>
</form>

{% for r in results %}
<div class="card">
<div class="conf">{{ r.conf }}</div>
<div class="ca">{{ r.ca }}</div>
<div>Price: {{ r.price }}</div>
<div>MC: ${{ r.mc }}</div>
<div>Liq: ${{ r.liq }}</div>
<div class="pred">📈 Pred MC: ${{ r.pred_mc }} ({{ r.pct }}%)</div>
</div>
{% endfor %}
</div>
</body>
</html>
"""

def snapshot():
    return {
        "price": round(random.uniform(0.00001, 0.0003), 8),
        "mc": random.randint(40000, 250000),
        "liq": random.randint(15000, 120000),
        "buy_sell": round(random.uniform(0.6, 1.7), 2),
        "chaos": round(random.uniform(0.1, 0.8), 2),
        "age": random.randint(1, 20),
        "time": int(time.time())
    }

def hard_safety(meta):
    if meta["dev_sell"] >= 8:
        return False
    if meta["lp_unlock"] < 30:
        return False
    return True

def regime(s):
    if s["chaos"] > 0.65:
        return "CHAOS"
    if s["buy_sell"] > 1.2 and s["chaos"] < 0.45:
        return "EXPANSION"
    return "NEUTRAL"

def dump_risk(s):
    risk = 0
    if s["buy_sell"] < 0.9:
        risk += 0.3
    if s["chaos"] > 0.5:
        risk += 0.4
    return min(risk, 1)

def upside(s):
    up = 0
    if s["buy_sell"] > 1.2:
        up += 0.4
    if s["liq"] > 30000:
        up += 0.3
    return min(up, 1)

def score(up, dump, chaos):
    return up * (1 - dump) * (1 - chaos)

def confidence(final, dump, chaos):
    if final >= 0.45 and dump <= 0.35 and chaos <= 0.4:
        return "🟢"
    if final >= 0.25:
        return "🟡"
    return "🔴"

@app.route("/", methods=["GET", "POST"])
def index():
    results = []

    if request.method == "POST":
        cas = request.form.get("cas", "").splitlines()

        for ca in cas:
            ca = ca.strip()
            if not ca:
                continue

            s = snapshot()

            meta = {
                "dev_sell": 0,
                "lp_unlock": 120
            }

            if not hard_safety(meta):
                continue

            if regime(s) != "EXPANSION":
                continue

            dump = dump_risk(s)
            up = upside(s)
            final = score(up, dump, s["chaos"])
            conf = confidence(final, dump, s["chaos"])

            if conf == "🔴":
                continue

            pred_mc = int(s["mc"] * (1 + final))

            results.append({
                "conf": conf,
                "ca": ca,
                "price": s["price"],
                "mc": s["mc"],
                "liq": s["liq"],
                "pred_mc": pred_mc,
                "pct": round(final * 100, 2)
            })

    return render_template_string(HTML, results=results)

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
