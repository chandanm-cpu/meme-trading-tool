import requests
import numpy as np
import subprocess
import json
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# =====================================================
# Utils
# =====================================================
def sigmoid(x):
    return 1 / (1 + np.exp(-x))


# =====================================================
# LIVE FETCH BY CA (CRITICAL FIX)
# =====================================================
def fetch_pair_by_ca(ca):
    """
    Fetch token pair LIVE using CA.
    Works for very new coins once a pair exists.
    """
    url = f"https://api.dexscreener.com/latest/dex/search/?q={ca}"
    try:
        r = requests.get(url, timeout=10).json()
        pairs = r.get("pairs", [])
        for p in pairs:
            base = p.get("baseToken", {})
            if base.get("address", "").lower() == ca.lower():
                return p
        return None
    except Exception:
        return None


# =====================================================
# Core Scoring Engine (Research-Aligned)
# =====================================================
def score_pair(pair):
    liquidity = pair.get("liquidity", {}).get("usd", 0)
    fdv = pair.get("fdv", 0) or 0
    vol5 = pair.get("volume", {}).get("m5", 0)
    vol1h = pair.get("volume", {}).get("h1", 0)
    pc5 = pair.get("priceChange", {}).get("m5", 0)

    if liquidity < 3000:
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
# LLM INTERPRETATION (OPTIONAL, SAFE FALLBACK)
# =====================================================
def llm_interpretation(data):
    """
    If Ollama is not installed, this safely falls back.
    """
    prompt = f"""
You are a crypto risk analyst.
Do NOT predict prices.

Return JSON only:
{{"verdict":"ALLOW|CAUTION|BLOCK","confidence_adjustment":-0.3 to 0.1,"reason":"text"}}

Data:
{json.dumps(data)}
"""
    try:
        result = subprocess.run(
            ["ollama", "run", "llama3", prompt],
            capture_output=True,
            text=True,
            timeout=10
        )
        return json.loads(result.stdout.strip())
    except Exception:
        return {
            "verdict": "CAUTION",
            "confidence_adjustment": -0.05,
            "reason": "LLM not available"
        }


# =====================================================
# API: ANALYZE (LIVE, NO CACHE)
# =====================================================
@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json
    chain = data.get("chain")
    contracts = data.get("contracts", [])

    results = {}

    for ca in contracts:
        pair = fetch_pair_by_ca(ca)

        if not pair:
            results[ca] = {
                "signal": "LOW",
                "reason": "Pair not indexed yet (VERY EARLY or no LP)"
            }
            continue

        score = score_pair(pair)
        if not score:
            results[ca] = {
                "signal": "LOW",
                "reason": "Liquidity too low"
            }
            continue

        llm = llm_interpretation(score)
        final_conf = max(
            0,
            min(1, score["confidence"] + llm["confidence_adjustment"])
        )

        token = pair.get("baseToken", {})

        results[ca] = {
            "name": token.get("name"),
            "symbol": token.get("symbol"),
            "chain": chain,
            "score": {**score, "final_confidence": round(final_conf, 2)},
            "llm_verdict": llm["verdict"],
            "llm_reason": llm["reason"],
            "note": "Live fetched"
        }

    return jsonify(results)


# =====================================================
# SIMPLE UI (MOBILE FRIENDLY)
# =====================================================
HTML_UI = """
<!DOCTYPE html>
<html>
<head>
<title>Live CA Analyzer</title>
<style>
body { background:#111; color:#eee; font-family:Arial; padding:15px; }
textarea { width:100%; height:160px; background:#222; color:#0f0; font-size:14px; }
select,button { padding:10px; margin-top:10px; width:100%; }
table { width:100%; margin-top:20px; border-collapse:collapse; }
th,td { border:1px solid #333; padding:6px; text-align:center; }
th { background:#222; }
</style>
</head>
<body>

<h2>🔴 LIVE Coin Analyzer</h2>

<form method="post">
<select name="chain">
<option value="solana">Solana</option>
<option value="bsc">BSC</option>
</select>

<textarea name="contracts" placeholder="Paste contract addresses, one per line"></textarea>
<button type="submit">Analyze (Live)</button>
</form>

{% if results %}
<table>
<tr>
<th>Token</th>
<th>p10x</th>
<th>p20x</th>
<th>p50x</th>
<th>p100x</th>
<th>Conf</th>
<th>LLM</th>
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
        payload = {"chain": chain, "contracts": cas}
        with app.test_request_context():
            results = analyze().json
    return render_template_string(HTML_UI, results=results)


# =====================================================
# HEALTH CHECK
# =====================================================
@app.route("/")
def home():
    return {"status": "running", "mode": "LIVE FETCH ONLY"}


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
