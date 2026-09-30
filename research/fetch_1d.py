"""Fetch all available daily (UTC) candles for BTC, ETH, SOL into data/candles/{inst}-1D.csv."""
import csv
import time
from pathlib import Path

import httpx

with httpx.Client(base_url="https://www.okx.com", timeout=20) as c:
    for inst in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        rows, after = {}, None
        while True:
            p = {"instId": inst, "bar": "1Dutc", "limit": "100"}
            if after:
                p["after"] = str(after)
            for _ in range(5):
                r = c.get("/api/v5/market/history-candles", params=p)
                if r.status_code == 200 and r.json().get("code") == "0":
                    break
                time.sleep(1)
            data = r.json()["data"]
            if not data:
                break
            for x in data:
                if x[8] == "1":
                    rows[int(x[0])] = x[:5]
            after = int(data[-1][0])
            time.sleep(0.11)
        out = [rows[k] for k in sorted(rows)]
        with open(Path("data/candles") / f"{inst}-1D.csv", "w", newline="") as f:
            csv.writer(f).writerows(out)
        print(inst, len(out), "days from", time.strftime("%Y-%m-%d", time.gmtime(int(out[0][0]) / 1000)), "first close", out[0][4], flush=True)
