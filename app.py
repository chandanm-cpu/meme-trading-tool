import threading, time, requests, numpy as np, subprocess, json
from flask import Flask, request, jsonify

app = Flask(__name__)
CACHE = {}
SCAN_INTERVAL = 60

# -----------------------------
# Utils
# -----------------------------
def sigmoid(x):
    return 1 / (1 + np.exp(-x))

# -----------------------------
# LLM via Ollama (FREE)
# -----------------------------
def ollama_llm(data):
    prompt = f"""
You are a crypto market risk analyst.
Do NOT predict prices.

Analyze this token data and respond ONLY in JSON:
{{"verdict":"ALLOW|CAUTION|BLOCK","confidence_adjustment":-0.3 to 0.1,"reason":"text"}}

Data:
{json.dumps(data)}
"""
    try:
        result = subprocess.run(
            ["ollama", "run", "llama3", prompt],
            capture_output=True, text=True, timeout=10
        )
        return json.loads(result.stdout.strip())
    except Exception:
        return {
            "verdict": "CAUTION",
            "confidence_adjustment": -0.05,
            "reason": "LLM unavailable"
        }

# -----------------------------
# Core Scoring (Research-Based)
# -----------------------------
def score_token(pair):
    liq = pair.get("liquidity", {}).get("usd", 0)
    fdv = pair.get("fdv", 0) or 0
    vol5 = pair.get("volume", {}).get("m5", 0)
    vol1h = pair.get("volume", {}).get("h1", 0)
    pc5 = pair.get("priceChange", {}).get("m5", 0)

    if liq < 5000:
        return None

    vol_liq = vol5 / liq if liq else 0
    fdv_liq = fdv / liq if liq else 0

    raw = (
        np.log(liq + 1) * 0.30 +
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
        "liquidity_usd": liq,
        "fdv": fdv,
        "volume_5m": vol5,
        "volume_1h": vol1h
    }

# -----------------------------
# DexScreener Scanner
# -----------------------------
def scan():
    global CACHE
    while True:
        tmp = {"solana": {}, "bsc": {}}
        for chain in ["solana", "bsc"]:
            url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}"
            pairs = requests.get(url, timeout=10).json().get("pairs", [])

            for p in pairs:
                token = p.get("baseToken", {})
                ca = token.get("address")
                if not ca:
                    continue

                score = score_token(p)
                if not score:
                    continue

                llm = ollama_llm(score)
                score["final_confidence"] = round(
                    max(0, min(1, score["confidence"] + llm["confidence_adjustment"])), 2
                )

                tmp[chain][ca] = {
                    "name": token.get("name"),
                    "symbol": token.get("symbol"),
                    "chain": chain,
                    "score": score,
                    "llm_verdict": llm["verdict"],
                    "llm_reason": llm["reason"]
                }

        CACHE = tmp
        time.sleep(SCAN_INTERVAL)

threading.Thread(target=scan, daemon=True).start()

# -----------------------------
# Monthly Backtest (Proxy)
# -----------------------------
def monthly_backtest(pair_address, chain):
    url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pair_address}"
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

# -----------------------------
# API
# -----------------------------
@app.route("/analyze", methods=["POST"])
def analyze():
    data = request.json
    chain = data["chain"]
    out = {}

    for ca in data["contracts"]:
        token = CACHE.get(chain, {}).get(ca)
        if not token:
            out[ca] = {"signal": "LOW", "reason": "Not detected"}
            continue

        backtest = monthly_backtest(ca, chain)
        out[ca] = {**token, "monthly_backtest": backtest}

    return jsonify(out)

@app.route("/")
def status():
    return {
        "status": "running",
        "tracked": {
            "solana": len(CACHE.get("solana", {})),
            "bsc": len(CACHE.get("bsc", {}))
        }
    }

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
