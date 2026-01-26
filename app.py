import requests
import numpy as np
import json
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# =========================
# GLOBAL STATE
# =========================
LIQ_HISTORY = {}       # CA -> last liquidity
RECENT_VOLUMES = []   # rolling market volumes

# =========================
# UTILS
# =========================
def sigmoid(x):
    return 1 / (1 + np.exp(-x))

# =========================
# LIVE FETCH FROM DEXSCREENER
# =========================
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

# =========================
# DUMP / DISTRIBUTION GUARD
# =========================
def dump_guard(pair):
    pc5 = pair.get("priceChange", {}).get("m5", 0)
    pc1h = pair.get("priceChange", {}).get("h1", 0)
    vol5 = pair.get("volume", {}).get("m5", 0)
    vol1h = pair.get("volume", {}).get("h1", 0)

    # Price exhaustion
    if pc1h > 20 and pc5 < -3:
        return True
    if pc5 < -8:
        return True

    # Volume decay
    if vol1h > 0 and (vol5 / vol1h) < 0.08:
        return True

    return False

# =========================
# LIQUIDITY STABILITY
# =========================
def liquidity_stability(ca, current_liq):
    prev = LIQ_HISTORY.get(ca)
    LIQ_HISTORY[ca] = current_liq

    if not prev or prev == 0:
        return {"status": "NEW", "delta": 0}

    delta = round((current_liq - prev) / prev, 2)

    if delta < -0.10:
        return {"status": "DRAINING", "delta": delta}
    elif delta > 0.10:
        return {"status": "GROWING", "delta": delta}
    else:
        return {"status": "STABLE", "delta": delta}

# =========================
# HOLDER VELOCITY (PROXY)
# =========================
def holder_velocity(vol5, vol1h):
    if vol5 == 0:
        return 0
    return round(vol1h / vol5, 2)

# =========================
# CAPITAL ROTATION
# =========================
def capital_rotation(vol1h):
    RECENT_VOLUMES.append(vol1h)
    if len(RECENT_VOLUMES) > 50:
        RECENT_VOLUMES.pop(0)

    if len(RECENT_VOLUMES) < 10:
        return {"status": "UNKNOWN", "score": 1.0}

    median = sorted(RECENT_VOLUMES)[len(RECENT_VOLUMES)//2]
    if median == 0:
        return {"status": "UNKNOWN", "score": 1.0}

    score = round(vol1h / median, 2)

    if score > 2:
        return {"status": "INFLOW", "score": score}
    elif score < 0.7:
        return {"status": "OUTFLOW", "score": score}
    else:
        return {"status": "NEUTRAL", "score": score}

# =========================
# CORE SCORING
# =========================
def score_pair(pair):
    liquidity = pair.get("liquidity", {}).get("usd", 0)
    fdv = pair.get("fdv", 0) or 0
    vol5 = pair.get("volume", {}).get("m5", 0)
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
        "p10x": round(min(0.9, p), 2),
        "p20x": round(p * 0.55, 2),
        "p50x": round(p * 0.30, 2),
        "p100x": round(p * 0.12, 2),
        "confidence": round(min(0.95, p + 0.15), 2),
        "liquidity": round(liquidity, 2),
        "market_cap": round(fdv, 2)
    }

# =========================
# ANALYSIS LOGIC
# =========================
def analyze_contracts(chain, contracts):
    results = {}

    for ca in contracts:
        pair = fetch_pair_by_ca(ca)
        if not pair:
            results[ca] = {"signal": "LOW", "reason": "Pair not indexed yet"}
            continue

        if dump_guard(pair):
            results[ca] = {"signal": "BLOCK", "reason": "Dump / distribution detected"}
            continue

        score = score_pair(pair)
        if not score:
            results[ca] = {"signal": "LOW", "reason": "Liquidity too low"}
            continue

        liq = liquidity_stability(ca, score["liquidity"])
        hv = holder_velocity(
            pair.get("volume", {}).get("m5", 0),
            pair.get("volume", {}).get("h1", 0)
        )
        rotation = capital_rotation(pair.get("volume", {}).get("h1", 0))

        signal = "ALLOW"
        if (
            liq["status"] == "GROWING" and
            hv >= 2 and
            rotation["status"] == "INFLOW" and
            score["p10x"] >= 0.75 and
            score["confidence"] >= 0.85
        ):
            signal = "STRONG ALLOW"

        token = pair.get("baseToken", {})

        results[ca] = {
            "name": token.get("name"),
            "symbol": token.get("symbol"),
            "market_cap": score["market_cap"],
            "signal": signal,
            "liquidity_status": liq,
            "holder_velocity": hv,
            "capital_rotation": rotation,
            "probabilities": score
        }

    return results

# =========================
# API ENDPOINT
# =========================
@app.route("/analyze", methods=["POST"])
def analyze():
    if not request.is_json:
        return jsonify({"error": "Content-Type must be application/json"}), 415

    data = request.get_json()
    return jsonify(analyze_contracts(
        data.get("chain"),
        data.get("contracts", [])
    ))

# =========================
# UI (SAFE HTML STRING)
# =========================
HTML_UI = """
<!DOCTYPE html>
<html>
<head>
<title>Dump-Safe Coin Detector</title>
<style>
body { background:#111; color:#eee; font-family:Arial; padding:15px; }
textarea { width:100%; height:160px; background:#222; color:#0f0; }
select, button { width:100%; padding:10px; margin-top:10px; }
table { width:100%; margin-top:20px; border-collapse:collapse; }
th, td { border:1px solid #333; padding:6px; text-align:center; }
th { background:#222; }
.ALLOW { color:#00ff99; }
.STRONG { color:#00ffaa; font-weight:bold; }
.BLOCK { color:#ff4444; font-weight:bold; }
</style>
</head>
<body>

<h2>🛡️ Dump-Safe Early Coin Detector</h2>

<form method="post">
<select name="chain">
<option value="solana">Solana</option>
<option value="bsc">BSC</option>
</select>

<textarea name="contracts" placeholder="Paste contract addresses, one per line"></textarea>
<button type="submit">Analyze</button>
</form>

{% if results %}
<table>
<tr>
<th>Token</th>
<th>Signal</th>
<th>Market Cap</th>
<th>p10x</th>
<th>p20x</th>
<th>p50x</th>
<th>p100x</th>
</tr>
{% for ca, r in results.items() %}
<tr>
<td>{{ r.get("name","-") }}<br><small>{{ ca }}</small></td>
<td class="{{ r.get("signal","") }}">{{ r.get("signal","-") }}</td>
<td>{{ r.get("market_cap","-") }}</td>
<td>{{ r.get("probabilities",{}).get("p10x","-") }}</td>
<td>{{ r.get("probabilities",{}).get("p20x","-") }}</td>
<td>{{ r.get("probabilities",{}).get("p50x","-") }}</td>
<td>{{ r.get("probabilities",{}).get("p100x","-") }}</td>
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

@app.route("/")
def home():
    return {"status": "running", "mode": "DUMP-SAFE"}

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
