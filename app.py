import threading
import time
import requests
import numpy as np
import subprocess
import json
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

SCAN_INTERVAL = 60  # seconds
CACHE = {"solana": {}, "bsc": {}}

# =====================================================
# Utility
# =====================================================
def sigmoid(x):
    return 1 / (1 + np.exp(-x))


# =====================================================
# LLM Interpretation Layer (FREE via Ollama)
# Requires: ollama run llama3
# =====================================================
def llm_interpretation(data):
    """
    LLM is used ONLY for interpretation & risk gating,
    NOT for price prediction.
    """
    prompt = f"""
You are a crypto market risk analyst.
Do NOT predict prices.

Return ONLY valid JSON in this format:
{{"verdict":"ALLOW|CAUTION|BLOCK","confidence_adjustment":-0.3 to 0.1,"reason":"short text"}}

Data:
{json.dumps(data)}
"""
    try:
        result = subprocess.run(
            ["ollama", "run", "llama3", prompt],
            capture_output=True,
            text=True,
            timeout=12
        )
        return json.loads(result.stdout.strip())
    except Exception:
        return {
            "verdict": "CAUTION",
            "confidence_adjustment": -0.05,
            "reason": "LLM unavailable or timeout"
        }


# =====================================================
# Core Scoring Engine (Research-Aligned)
# =====================================================
def score_pair(pair):
    liquidity = pair.get("liquidity", {}).get("usd", 0)
    fdv = pair.get("fdv", 0) or 0
    vol5 = pair.get("volume", {}).get("m5", 0)
    vol1h = pair.get("volume", {}).get("h1", 0)
    pc5 = pair.get("priceChange", {}).get("m5", 0)

    if liquidity < 5000:
        return None

    vol_liq = vol5 / liquidity if liquidity else 0
    fdv_liq = fdv / liquidity if liquidity else 0

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
        "rug_risk": round(1 - p, 2),
        "confidence": round(min(0.95, p + 0.15), 2),
        "liquidity_usd": round(liquidity, 2),
        "fdv": round(fdv, 2),
        "volume_5m": round(vol5, 2),
        "volume_1h": round(vol1h, 2),
        "price_change_5m": round(pc5, 2)
    }


# =====================================================
# Monthly Historical Proxy Backtest (Free)
# =====================================================
def monthly_backtest(pair_address, chain):
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
    try:
        r = requests.get(url, timeout=10).json().get("pair")
        if not r:
            return None

        pc24 = r.get("priceChange", {}).get("h24", 0)

        return {
            "24h_return_x": round(1 + pc24 / 100, 2),
            "hit_2x": pc24 >= 100,
            "hit_5x": pc24 >= 400,
            "hit_10x": pc24 >= 900
        }
    except Exception:
        return None


# =====================================================
# Background Scanner (Auto Refresh)
# =====================================================
def scanner():
    global CACHE
    while True:
        new_cache = {"solana": {}, "bsc": {}}

        for chain in ["solana", "bsc"]:
            url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}"
            try:
                pairs = requests.get(url, timeout=10).json().get("pairs", [])
            except Exception:
                continue

            for p in pairs:
                token = p.get("baseToken", {})
                ca = token.get("address")
                if not ca:
                    continue

                score = score_pair(p)
                if not score:
                    continue

                llm = llm_interpretation(score)
                final_conf = max(
                    0,
                    min(1, score["confidence"] + llm["confidence_adjustment"])
                )

                new_cache[chain][ca] = {
                    "name": token.get("name"),
                    "symbol": token.get("symbol"),
                    "chain": chain,
                    "score": {**score, "final_confidence": round(final_conf, 2)},
                    "llm_verdict": llm["verdict"],
                    "llm_reason": llm["reason"]
                }

        CACHE = new_cache
        time.sleep(SCAN_INTERVAL)


threading.Thread(target=scanner, daemon=True).start()

# =====================================================
# API: Analyze pasted CAs
# =====================================================
@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json
    chain = data["chain"]
    contracts = data["contracts"]

    out = {}
    for ca in contracts:
        token = CACHE.get(chain, {}).get(ca)
        if not token:
            out[ca] = {"signal": "LOW", "reason": "Not detected or insufficient data"}
        else:
            token["monthly_backtest"] = monthly_backtest(ca, chain)
            out[ca] = token

    return jsonify(out)


# =====================================================
# Simple Browser UI (Boxes to Paste CAs)
# =====================================================
HTML_UI = """
<!DOCTYPE html>
<html>
<head>
<title>CA Analyzer</title>
<style>
body { background:#111; color:#eee; font-family:Arial; padding:20px; }
textarea { width:100%; height:140px; background:#222; color:#0f0; }
select,button { padding:8px; margin-top:10px; }
table { width:100%; margin-top:20px; border-collapse:collapse; }
th,td { border:1px solid #333; padding:6px; text-align:center; }
th { background:#222; }
</style>
</head>
<body>

<h2>🔍 Paste Contract Addresses</h2>

<form method="post">
<select name="chain">
<option value="solana">Solana</option>
<option value="bsc">BSC</option>
</select><br><br>

<textarea name="contracts" placeholder="One CA per line"></textarea><br>
<button type="submit">Analyze</button>
</form>

{% if results %}
<h2>📊 Results</h2>
<table>
<tr>
<th>Token</th><th>p10x</th><th>p20x</th><th>p50x</th><th>p100x</th>
<th>Final Conf</th><th>LLM</th><th>Reason</th>
</tr>
{% for ca,r in results.items() %}
<tr>
<td>{{ r.get("name","-") }}<br><small>{{ ca }}</small></td>
<td>{{ r.get("score",{}).get("p_10x","-") }}</td>
<td>{{ r.get("score",{}).get("p_20x","-") }}</td>
<td>{{ r.get("score",{}).get("p_50x","-") }}</td>
<td>{{ r.get("score",{}).get("p_100x","-") }}</td>
<td>{{ r.get("score",{}).get("final_confidence","-") }}</td>
<td>{{ r.get("llm_verdict","-") }}</td>
<td>{{ r.get("llm_reason","-") }}</td>
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
        chain = request.form["chain"]
        cas = [c.strip() for c in request.form["contracts"].splitlines() if c.strip()]
        results = {}
        for ca in cas:
            results[ca] = CACHE.get(chain, {}).get(ca, {
                "llm_verdict": "LOW",
                "llm_reason": "Not detected yet"
            })
    return render_template_string(HTML_UI, results=results)


# =====================================================
# Health Check
# =====================================================
@app.route("/")
def home():
    return {
        "status": "running",
        "tracked": {
            "solana": len(CACHE.get("solana", {})),
            "bsc": len(CACHE.get("bsc", {}))
        }
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
