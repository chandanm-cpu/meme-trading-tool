from flask import Flask, request, render_template_string
import requests
import time
import math

app = Flask(__name__)

PREV = {}
SEEN = {}

TIER_A_TTL = 20 * 60
TIER_B_TTL = 40 * 60
TIER_C_TTL = 10 * 60

HTML = """
<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="10">
<title>Low MC 100× Filter</title>
<style>
body { font-family: Arial; background:#0f172a; color:#e5e7eb; margin:0 }
.container { padding:16px; max-width:620px; margin:auto }
textarea { width:100%; height:120px; border-radius:8px; padding:10px }
button { width:100%; padding:12px; background:#22c55e; border:none; border-radius:8px; margin-top:10px }
.card { background:#1e293b; padding:12px; margin-top:12px; border-radius:10px }
.ca { font-size:12px; word-break:break-all; color:#94a3b8 }
.a { color:#4ade80 }
.b { color:#facc15 }
.c { color:#60a5fa }
.d { color:#f87171 }
.small { font-size:12px; color:#94a3b8 }
</style>
</head>
<body>
<div class="container">
<h2>🔍 Low MC 100× Structural Filter</h2>
<p class="small">Auto refresh: 10s • Focus: 20k–100k MC • No hype</p>

<form method="post">
<textarea name="cas">{{cas}}</textarea>
<button type="submit">Analyze</button>
</form>

{% for r in results %}
<div class="card">
<div class="{{r.cls}}"><b>{{ r.tier }}</b></div>
<div><b>{{ r.name }}</b> ({{ r.symbol }})</div>
<div class="ca">{{ r.ca }}</div>

<div class="small">MC: ${{ r.mc }} | Liquidity: ${{ r.liq }}</div>
<div class="small">Scarcity: {{ r.scarcity }}% | Demand: {{ r.demand }}%</div>
<div class="small">Demand Accel: {{ r.accel }} | Liquidity Δ: {{ r.liq_delta }}</div>

{% if r.ml_prob is not none %}
<div class="small">🧠 ML Extreme Prob: {{ r.ml_prob }}%</div>
{% endif %}

<div class="small">⏳ Time Remaining: {{ r.time_left }} min</div>
<div class="small">Buys / Sells (24h): {{ r.buys }} / {{ r.sells }}</div>
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
        "symbol": p.get("baseToken", {}).get("symbol", "UNK"),
        "fdv": float(p.get("fdv") or 0),
        "liq": float(p.get("liquidity
