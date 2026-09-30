"""Fetch N years of 1H candles for BTC, ETH, SOL into data/candles/{inst}-1H.csv."""
import csv, sys, time
from pathlib import Path
import httpx

years = float(sys.argv[1]) if len(sys.argv) > 1 else 4
since = int((time.time() - years * 365 * 86400) * 1000)
with httpx.Client(base_url="https://www.okx.com", timeout=20) as c:
    for inst in ("BTC-USDT", "ETH-USDT", "SOL-USDT"):
        rows, after = {}, None
        while True:
            p = {"instId": inst, "bar": "1H", "limit": "100"}
            if after: p["after"] = str(after)
            for attempt in range(5):
                r = c.get("/api/v5/market/history-candles", params=p)
                if r.status_code == 200 and r.json().get("code") == "0": break
                time.sleep(1)
            data = r.json()["data"]
            if not data: break
            for x in data:
                if x[8] == "1": rows[int(x[0])] = x[:5]
            after = int(data[-1][0])
            if after <= since: break
            time.sleep(0.11)
        out = sorted(v for k, v in rows.items() if k >= since)
        with open(Path("data/candles") / f"{inst}-1H.csv", "w", newline="") as f:
            csv.writer(f).writerows(out)
        print(inst, len(out), "hours from", time.strftime("%Y-%m-%d", time.gmtime(int(out[0][0]) / 1000)), flush=True)
