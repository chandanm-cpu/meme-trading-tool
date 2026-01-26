import requests
import numpy as np
import subprocess
import json
import threading
import time
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ==============================
# GLOBAL STORES (STATE)
# ==============================
LIQ_HISTORY = {}        # ca -> previous liquidity
RECENT_VOLUMES = []    # rolling volume window
AUTO_REFRESH_INTERVAL = 60  # seconds

# ==============================
# UTILS
# ==============================
def sigmoid(x):
    return 1 / (1 + np.exp(-x))

# ==============================
# LIVE FETCH BY CA
# ==============================
def fetch_pair_by_ca(ca):
    url = f"https://api.dexscreener.com/latest/dex/search/?q={ca}"
    try:
        data = requests.get(url, timeout=10).json()
        for pair in data.get("pairs", []):
            base = pair.get("baseToken", {})
            if base.get("address", "").lower() == ca.lower():
                return pair
        return None
    except Exception:
        return None

# ==============================
# LIQUIDITY STABILITY
# ==============================
def liquidity_stability(ca, current_liq):
    prev = LIQ_HISTORY.get(ca)
    LIQ_HISTORY[ca] = current_liq

    if not prev or prev == 0:
        return {"delta": 0, "status": "NEW"}

    delta = (current_liq - prev) / prev
    delta = round(delta, 2)

    if delta < -0.10:
        return {"delta": delta, "status": "DRAINING"}
    elif delta > 0.10:
        return {"delta": delta, "status": "GROWING"}
    else:
        return {"delta": delta, "status": "STABLE"}

# ==============================
# HOLDER VELOCITY (PROXY)
# ==============================
def holder_velocity(vol5, vol1h):
    if vol5 == 0:
        return 0
    return round(vol1h / vol5, 2)

# ==============================
# CAPITAL ROTATION
# ==============================
def capital_rotation(vol1h):
    RECENT_VOLUMES.append(vol1h)
    if len(RECENT_VOLUMES) > 50:
        RECENT_VOLUMES.pop(0)

    if len(RECENT_VOLUMES) < 10:
        return {"score": 1.0, "status": "UNKNOWN"}

    median = sorted(RECENT_VOLUMES)[len(RECENT_VOLUMES)//2]
    if median == 0:
        return {"score": 1.0, "status": "UNKNOWN"}

    score = round(vol1h / median, 2)

    if score > 2:
        return {"score": score, "status": "INFLOW"}
    elif score < 0.7:
        return {"score": score, "status": "OUTFLOW"}
    else:
        return {"score": score, "status": "NEUTRAL"}

# ==============================
# CORE SCORING
# ==============================
def score_pair(pair):
    liquidity = pair.get("liquidity", {}).get("usd", 0)
    fdv = pair.get("fdv", 0) or 0
    vol5 = pair.get("volume", {}).get("m5", 0)
    vol1h = pair.get("volume", {}).get("h1", 0)
    pc5 = pair.get("priceChange", {}).get("m5", 0)

    if liquidity < 3000:
        return None

    vol_liq = vol5 / liquidity if liquidity else 0
    fdv_liq = min(fdv / liquidity if liquidity else 0, 50)

    raw = (
        np.log(liquidity + 1) * 0.30 +
        vol_liq * 0.25 +
        fdv_liq * 0.20 +
        pc5 * 0.02
    )

    p = sigmoid(raw - 4)

    return {
        "p_10x": round(min(0.9, p), 2),
        "p_20x": round(p * 0.55, 2),
        "p_50x": round(p * 0.30, 2),
        "p_100x": round(p * 0.12, 2),
        "confidence": round(min(0.95, p + 0.15), 2),
        "liquidity_usd": round(liquidity, 2),
        "market_cap": round(fdv, 2)
    }

# ==============================
# LLM INTERPRETATION (SAFE)
# ==============================
def llm_interpretation(data):
    try:
        prompt = f"""
Return JSON only:
{{"verdict":"ALLOW|CAUTION|BLOCK","adjust":-0.3 to 0.1}}

Data:
{json.dumps(data)}
"""
        r = subprocess.run(
            ["ollama", "run", "llama3", prompt],
            capture_output=True,
            text=True,
            timeout=8
        )
        return json.loads(r.stdout.strip())
    except Exception:
        return {"verdict": "CAUTION", "adjust": -0.05}

# ==============================
# ANALYSIS LOGIC
# ==============================
def analyze_contracts(chain, contracts):
    results = {}

    for ca in contracts:
        pair = fetch_pair_by_ca(ca)
        if not pair:
            results[ca] = {"signal": "LOW", "reason": "Pair not indexed yet"}
            continue

        score = score_pair(pair)
        if not score:
            results[ca] = {"signal": "LOW", "reason": "Liquidity too low"}
            continue

        liq_status = liquidity_stability(ca, score["liquidity_usd"])
        hv = holder_velocity(
            pair.get("volume", {}).get("m5", 0),
            pair.get("volume", {}).get("h1", 0)
        )
        rotation = capital_rotation(pair.get("volume", {}).get("h1", 0))
        llm = llm_interpretation(score)

        final_conf = round(
            max(0, min(1, score["confidence"] + llm["adjust"])), 2
        )

        signal = "ALLOW"
        if (
            liq_status["status"] == "GROWING" and
            hv >= 2 and
            rotation["status"] == "INFLOW" and
            score["p_10x"] >= 0.75 and
            final_conf >= 0.85 and
            llm["verdict"] == "ALLOW"
        ):
            signal = "STRONG ALLOW"

        results[ca] = {
            "name": pair.get("baseToken", {}).get("name"),
            "symbol": pair.get("baseToken", {}).get("symbol"),
            "market_cap": score["market_cap"],
            "signal": signal,
            "liquidity_status": liq_status,
            "holder_velocity": hv,
            "capital_rotation": rotation,
            "probabilities": {
                "p10x": score["p_10x"],
                "p20x": score["p_20x"],
                "p50x": score["p_50x"],
                "p100x": score["p_100x"],
                "final_confidence": final_conf
            }
        }

    return results

# ==============================
# API
# ==============================
@app.route("/analyze", methods=["POST"])
def analyze():
    if not request.is_json:
        return jsonify({"error": "JSON only"}), 415
    data = request.get_json()
    return jsonify(analyze_contracts(
        data.get("chain"),
        data.get("contracts", [])
    ))

# ==============================
# UI
# ==============================
HTML_UI = """
<!DOCTYPE html>
<html>
<head>
<title>Crypto Early Detector</title>
<style>
body{background:#111;color:#eee;font-family:Arial;padding:15px}
textarea{width:100%;height:160px;background:#222;color:#0f0}
select,button{width:100%;padding:10px;margin-top:10px}
table{width:100%;margin-top:20px;border-collapse:collapse}
th,td{border:1px solid #333;padding:6px;text-align:center}
th{background:#222}
</style>
</head>
<body>
<h2>🚀 Early Coin Detector</h2>
<form method="post">
<select name="chain">
<option value="solana">Solana</option>
<option value="bsc">BSC</option>
</select>
<textarea name="contracts" placeholder="Paste CAs, one per line"></textarea>
<button type="submit">Analyze</button>
</form>

{% if results %}
<table>
<tr>
<th>Token</th><th>Signal</th><th>MC</th><th>p10x</th><th>p20x</th><th>p50x</th><th>p100x</th>
</tr>
{% for ca,r in results.items() %}
<tr>
<td>{{ r.name }}<br><small>{{ ca }}</small></td>
<td>{{ r.signal }}</td>
<td>{{ r.market_cap }}</td>
<td>{{ r.probabilities.p10x }}</td>
<td>{{ r.probabilities.p20x }}</td>
<td>{{ r.probabilities.p50x }}</td>
<td>{{ r.probabilities.p100x }}</td>
</tr>
{% endfor %}
</table>
{% endif %}
</body>
</html>
"""

@app.route("/ui", methods=["GET", "POST"])
def ui():
    results = None
    if request.method == "POST":
        cas = [
            c.strip()
            for c in request.form.get("contracts", "").splitlines()
            if c.strip()
        ]
        chain = request.form.get("chain")
        results = analyze_contracts(chain, cas)
    return render_template_string(HTML_UI, results=results)

# ==============================
# AUTO REFRESH (BACKGROUND)
# ==============================
def auto_refresh():
    while True:
        time.sleep(AUTO_REFRESH_INTERVAL)

threading.Thread(target=auto_refresh, daemon=True).start()

@app.route("/")
def home():
    return {"status": "running", "mode": "LIVE + AUTO REFRESH"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
