import os, json, requests
import pandas as pd

TOKEN = os.environ["TG_TOKEN"]
CHAT = os.environ["TG_CHAT"]
OANDA_KEY = os.environ["OANDA_API_KEY"]
OANDA_URL = "https://api-fxpractice.oanda.com/v3/instruments"

MARKETS = {
    "NQ1!": "NAS100_USD",
    "ES1!": "SPX500_USD",
    "GC1!": "XAU_USD",
    "YM1!": "US30_USD",
    "CL1!": "WTICO_USD",
    "SILVER": "XAG_USD",
    "BTCUSD": "XBTUSD",
    "ETHUSD": "ETHUSD",
}
CRYPTO = {"BTCUSD", "ETHUSD"}   # trade 24/7, candles start at 00:00 UTC
TFS = ["1W", "1D", "6H", "4H", "3H", "2H", "1H"]
DOJI_MIN, REQ_OPP, FRESH_MIN = 0.20, False, 60

FREQ = {"1D": "1D", "6H": "6h", "4H": "4h", "3H": "3h", "2H": "2h", "1H": "1h"}
DUR = {
    "1W": pd.Timedelta(days=7), "1D": pd.Timedelta(days=1),
    "6H": pd.Timedelta(hours=6), "4H": pd.Timedelta(hours=4),
    "3H": pd.Timedelta(hours=3), "2H": pd.Timedelta(hours=2),
    "1H": pd.Timedelta(hours=1),
}
SHIFT = pd.Timedelta(hours=18)   # CME session starts 18:00 New York
SETTLE = pd.Timedelta(seconds=5) # Real-time feed needs only 5s buffer

def clock(name):
    return ("UTC", pd.Timedelta(0)) if name in CRYPTO else ("America/New_York", SHIFT)

def send(msg):
    r = requests.post(
        f"https://api.telegram.org/bot{TOKEN}/sendMessage",
        data={"chat_id": CHAT, "text": msg},
        timeout=15,
    )
    print("TG:", r.status_code, r.text)

def load(name, ticker):
    tz, shift = clock(name)

    if name in CRYPTO:
        # Keyless real-time 1H candles from Kraken (720 bars = 30 days)
        r = requests.get(
            "https://api.kraken.com/0/public/OHLC",
            params={"pair": ticker, "interval": 60},
            timeout=15,
        )
        r.raise_for_status()
        res = r.json()["result"]
        pair_key = [k for k in res.keys() if k != "last"][0]
        rows = [
            {
                "time": pd.to_datetime(int(c[0]), unit="s", utc=True),
                "Open": float(c[1]),
                "High": float(c[2]),
                "Low": float(c[3]),
                "Close": float(c[4]),
            }
            for c in res[pair_key]
        ]
    else:
        # Real-time 1H candles from OANDA v20 REST API (1500 bars = ~62 days)
        headers = {"Authorization": f"Bearer {OANDA_KEY}"}
        params = {"granularity": "H1", "count": 1500, "price": "M"}
        r = requests.get(
            f"{OANDA_URL}/{ticker}/candles",
            headers=headers,
            params=params,
            timeout=15,
        )
        r.raise_for_status()
        rows = [
            {
                "time": pd.to_datetime(c["time"], utc=True),
                "Open": float(c["mid"]["o"]),
                "High": float(c["mid"]["h"]),
                "Low": float(c["mid"]["l"]),
                "Close": float(c["mid"]["c"]),
            }
            for c in r.json().get("candles", [])
        ]

    if not rows:
        return pd.DataFrame()

    df = pd.DataFrame(rows).set_index("time")
    df.index = df.index.tz_convert(tz).tz_localize(None) - shift
    return df[["Open", "High", "Low", "Close"]]

def candles(df, tf, name):
    tz, shift = clock(name)
    agg = {"Open": "first", "High": "max", "Low": "min", "Close": "last"}
    if tf == "1W":
        rule = "W-MON" if name in CRYPTO else "W-SUN"
        r = df.resample(rule, closed="left", label="left").agg(agg).dropna()
    else:
        r = df.resample(FREQ[tf], origin="start_day").agg(agg).dropna()
    now = pd.Timestamp.now(tz=tz).tz_localize(None) - shift
    r = r[r.index + DUR[tf] + SETTLE <= now]
    return r, now

def ebp(r):
    if len(r) < 2:
        return 0
    c, p = r.iloc[-1], r.iloc[-2]
    rng = p.High - p.Low
    if rng <= 0 or abs(p.Close - p.Open) / rng < DOJI_MIN:
        return 0
    if c.Low < p.Low and c.Close > max(p.Open, p.Close) and c.Close > c.Open and (not REQ_OPP or p.Close < p.Open):
        return 1
    if c.High > p.High and c.Close < min(p.Open, p.Close) and c.Close < c.Open and (not REQ_OPP or p.Close > p.Open):
        return -1
    return 0

state = json.load(open("state.json")) if os.path.exists("state.json") else []

for name, tk in MARKETS.items():
    try:
        df = load(name, tk)
        if df.empty:
            continue
        for tf in TFS:
            r, now = candles(df, tf, name)
            d = ebp(r)
            print(name, tf, "last candle:", r.index[-1] if len(r) else None, "ebp:", d)
            if d == 0:
                continue
            start = r.index[-1]
            age = (now - (start + DUR[tf])).total_seconds() / 60
            key = f"{name}|{tf}|{start}"
            if age <= FRESH_MIN and key not in state:
                c, p = r.iloc[-1], r.iloc[-2]
                send(
                    f"{name} - {tf} {'BULL' if d == 1 else 'BEAR'} EBP closed {int(age)} min ago.\n"
                    #f"candle opened {start + clock(name)[1]} {'UTC' if name in CRYPTO else 'NY'}\n"
                    #f"prev O{p.Open:.2f} H{p.High:.2f} L{p.Low:.2f} C{p.Close:.2f}\n"
                    #f"last O{c.Open:.2f} H{c.High:.2f} L{c.Low:.2f} C{c.Close:.2f}"
                )
                state.append(key)
    except Exception as e:
        print(name, e)

json.dump(state[-500:], open("state.json", "w"))
