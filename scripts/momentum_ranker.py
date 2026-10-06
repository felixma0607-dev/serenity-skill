#!/usr/bin/env python3
"""
Daily momentum leaderboard (排行榜) — "睇排行榜簡簡單單選股，再深入研究".

For every stock in config/ranking-universe.json it computes, as of each of the
last N trading days:

  SPOT   close price
  D20    % distance from the 20-day SMA   (short-term extension)
  D50    % distance from the 50-day SMA   (medium-term trend)
  D200   % distance from the 200-day SMA
  R1M / R3M  21 / 63 trading-day returns
  RS     IBD-style relative strength percentile against all S&P 500 members (1-99)
         weighted return = 0.4*R3M + 0.2*R6M + 0.2*R9M + 0.2*R12M
         (rs_pool = the same percentile inside this universe only; used for SCORE)
  TT     Minervini trend-template checks passed (0-8); check 8 = RS >= 70 AND the
         RS line (stock / SPY) higher than one month ago
  SCORE  0.5*RS_POOL + 0.3*(TT/8*100) + 0.2*R1M percentile  -> rank
  ZONE   過熱 / 偏強 / 整理 / 偏弱 / 弱勢  (see zone())

plus a daily market "temperature" SCORE (0-100) built from universe breadth
and SPY/QQQ trend, and persistence stats (streak / days in Top N).

History is backfilled from price data, so the "N 天都在 Top 10" view works
from the first run. Output: data/ranking-data.js (window.RANKING_DATA), read by
ranking-dashboard.html. Stdlib only — no pip install.

Usage:
  python3 scripts/momentum_ranker.py              # backfill last 30 trading days
  python3 scripts/momentum_ranker.py --days 60
"""
import argparse
import json
import os
import sys
import time
import urllib.request

import earnings
import signal_log
import market_state
from bisect import bisect_left, bisect_right
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config", "ranking-universe.json")
OUT_JS = os.path.join(ROOT, "data", "ranking-data.js")
SIGNAL_LOG = os.path.join(ROOT, "data", "signal-log.json")
PORTFOLIO = os.path.join(ROOT, "data", "portfolio.json")  # holdings are always ranked too
EARNINGS = os.path.join(ROOT, "data", "earnings.json")
RISK_PER_TRADE = 1.0  # % of the account lost if a full-size signal is stopped out (🟡 = half)
EARNINGS_WARN_DAYS = 10  # calendar days: a signal this close to earnings gets a warning
MAX_RISK = 8.0  # % — widest stop allowed; beyond this the setup is "等收窄"
MIN_RISK = 1.5  # % — stops tighter than this sit inside normal daily noise
RULE_VERSION = 4  # 4 = v3 + 🟡 shallow 10MA pullbacks at half size (2026-09-27)
UD_MIN = 1.2  # 20-day up-volume / down-volume needed to count as accumulation
SIGNAL_RS = "pool"  # entry signals keep the in-universe RS/TT (rule v4): a 1y backtest (2026-10-03) found the S&P 500 RS gave more signals but lower R (FTD days: avg 2.75R vs 4.17R, median 1.64R vs 2.13R)
DISCOVER_RS = 80  # 新強勢股發現: S&P 500 members outside the universe with TT 8/8 and RS >= this
CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval=1d"


def fetch(sym, rng="2y"):
    req = urllib.request.Request(CHART_URL.format(sym=sym, rng=rng), headers={"User-Agent": "Mozilla/5.0"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                res = json.load(r)["chart"]["result"][0]
            meta = res["meta"]
            q = res["indicators"]["quote"][0]
            tz = meta.get("gmtoffset", 0)
            bars = {}  # date -> (close, high, low, volume, open)
            for ts, c, h, l, v, o in zip(res["timestamp"], q["close"], q["high"], q["low"],
                                         q["volume"], q["open"]):
                if c is not None:
                    d = datetime.fromtimestamp(ts + tz, tz=timezone.utc).strftime("%Y-%m-%d")
                    bars[d] = (c, h if h is not None else c, l if l is not None else c, v or 0,
                               o if o is not None else c)
            # during the US session Yahoo includes today's unfinished bar; ranking it as a full
            # day would distort volume/distribution/FTD reads, so drop it until after the close
            live = None
            reg = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
            if reg.get("start") and time.time() < reg.get("end", 0) + 600:
                today = datetime.fromtimestamp(reg["start"] + tz, tz=timezone.utc).strftime("%Y-%m-%d")
                if today in bars:
                    live = {"date": today, "price": bars[today][0], "high": bars[today][1],
                            "low": bars[today][2], "volume": bars[today][3]}
                    del bars[today]
            return sym, {"name": meta.get("shortName") or meta.get("longName") or sym, "bars": bars, "live": live}
        except Exception as e:  # network hiccup / rate limit
            err = e
            time.sleep(1.5 * (attempt + 1))
    print(f"  ! {sym}: {err}", file=sys.stderr)
    return sym, None


def sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def ema(xs, n):
    if len(xs) < n:
        return None
    k, e = 2 / (n + 1), sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def ret(xs, n):
    return xs[-1] / xs[-1 - n] - 1 if len(xs) > n else None


def pct(a, b):
    return (a / b - 1) * 100 if a is not None and b else None


def percentiles(values):
    """Map {key: value} -> {key: 1..99 percentile rank}; None stays None."""
    ks = sorted((k for k, v in values.items() if v is not None), key=lambda k: values[k])
    n = len(ks)
    out = {k: None for k in values}
    for i, k in enumerate(ks):
        out[k] = round(1 + 98 * i / (n - 1)) if n > 1 else 50
    return out


def zone(d20, d50, d200):
    if d20 is None or d50 is None:
        return "—"
    if d20 >= 10 or d50 >= 25:
        return "過熱"
    if d20 >= 0:
        return "偏強"
    if d50 >= 0:
        return "整理"
    if d200 is None or d200 >= 0:
        return "偏弱"
    return "弱勢"


def stock_metrics(xs):
    c = xs[-1]
    s20, s50, s150, s200 = sma(xs, 20), sma(xs, 50), sma(xs, 150), sma(xs, 200)
    s200_1m = sma(xs[:-21], 200) if len(xs) > 221 else None
    win = xs[-252:]
    hi, lo = max(win), min(win)
    r3 = ret(xs, 63)
    tt = [  # Minervini trend template (RS>=70 check added after percentiles)
        s150 is not None and c > s150 and s200 is not None and c > s200,
        s150 is not None and s200 is not None and s150 > s200,
        s200 is not None and s200_1m is not None and s200 > s200_1m,
        s50 is not None and s150 is not None and s200 is not None and s50 > s150 and s50 > s200,
        s50 is not None and c > s50,
        c >= lo * 1.30,
        c >= hi * 0.75,
    ]
    return {
        "spot": round(c, 2),
        "d20": pct(c, s20), "d50": pct(c, s50), "d200": pct(c, s200),
        "r1m": (ret(xs, 21) or 0) * 100 if ret(xs, 21) is not None else None,
        "r3m": r3 * 100 if r3 is not None else None,
        "off_high": pct(c, hi),
        "_wret": weighted_return(xs), "_tt": sum(tt),
    }


def weighted_return(xs):
    """IBD-style RS input: 0.4*R3M + 0.2*R6M + 0.2*R9M + 0.2*R12M (missing legs re-weighted)."""
    parts = [(ret(xs, 63), 0.4), (ret(xs, 126), 0.2), (ret(xs, 189), 0.2), (ret(xs, 252), 0.2)]
    have = [(r, w) for r, w in parts if r is not None]
    return sum(r * w for r, w in have) / sum(w for _, w in have) if have else None


def market_rank(val, ref_sorted):
    """Percentile (1-99) of val against the S&P 500 weighted-return distribution."""
    if val is None or len(ref_sorted) < 2:
        return None
    return min(99, round(1 + 98 * bisect_left(ref_sorted, val) / (len(ref_sorted) - 1)))


def rs_line_up(closes_by_date, bench_by_date, date, prev_date):
    """Minervini's 'RS line rising': stock/SPY ratio higher than one month ago."""
    try:
        return closes_by_date[date] / bench_by_date[date] > closes_by_date[prev_date] / bench_by_date[prev_date]
    except (KeyError, ZeroDivisionError, TypeError):
        return False


def entry_signal(bars, m, tt, rs, lev=1, max_risk=None):
    """Classify today's entry setup from daily OHLCV (list of (c,h,l,v,o), oldest first).

    Pullback buying is the only order-generating setup (user preference, 2026-09-27:
    breakouts kept reversing right after entry). A one-year backtest on this universe
    (scratch pullback_bt.py) ranked the stop placement below as the best variant.

    pullback  回調買點: Stage 2 (TT>=7, RS>=70), rising 20MA, close within -2%..+3% of the
              20MA, above the 50MA, and BOTH volume tests pass:
                dry-up        5-day avg volume < 50-day avg volume, and
                accumulation  over 20 days, volume on up days >= UD_MIN x volume on down days.
              A one-year backtest (scratch volume_bt.py) found the pair beat either test alone
              and every other definition tried (median volume, down-day volume, climax-low day).
              Order: buy-stop above today's high (bounce confirmation).
              Stop: under the pullback's own low (5-day low - 1%), MIN_RISK..MAX_RISK.
    shallow   淺回調（半注）: same as pullback but the touch is on a rising 10MA while the stock
              is still 3-12% above its 20MA — strong trends often never reach the 20MA. One-year
              backtest: catches about twice as many big runners, but win rate ~35% vs 57%, so the
              order is flagged size="half".
    vol_wait  回調中，等量能: price is in the pullback zone but a volume test fails — no order.
    watch     突破中，等回踩: a Stage 2 stock pushing into its 20-day high on a tight base —
              no order; wait for it to come back to the 10/20MA on light volume.
    extended  過熱等回調: zone 過熱 — wait for price back near the 20MA.
    stretched 延伸中: strong but D20 4-10% — wait for D20 < 4%.
    tighten   等收窄: pullback setup whose pullback low is more than MAX_RISK under the
              entry (or so close it sits inside daily noise) — no order.
    Leveraged ETFs (lev > 1): the D20 bands are scaled by the leverage factor and the
    stop cap comes from max_risk_override, since a 3x fund moves ~3x a normal stock.
    """
    max_risk = max_risk or MAX_RISK
    closes = [b[0] for b in bars]
    c, h = bars[-1][0], bars[-1][1]
    s10, s20, s50 = sma(closes, 10), sma(closes, 20), sma(closes, 50)
    s20_prev = sma(closes[:-5], 20)
    vols = [b[3] for b in bars]
    v50 = sma(vols, 50) or 0
    vol_ratio = vols[-1] / v50 if v50 else None
    dry = bool(v50 and sma(vols, 5) < v50)
    up_v = sum(bars[k][3] for k in range(-20, 0) if bars[k][0] > bars[k - 1][0])
    dn_v = sum(bars[k][3] for k in range(-20, 0) if bars[k][0] < bars[k - 1][0])
    ud = up_v / dn_v if dn_v else None
    accum = ud is not None and ud >= UD_MIN
    pivot = max(b[1] for b in bars[-20:])
    base = bars[-10:]
    tight = (max(b[1] for b in base) - min(b[2] for b in base)) / c * 100
    low5 = min(b[2] for b in bars[-5:])
    d20 = (m["d20"] if m["d20"] is not None else 0) / lev  # leverage-adjusted
    stage2 = tt >= 7 and (rs or 0) >= 70
    base_info = {"entry": None, "stop": None, "risk": None, "gap": None, "stop_basis": None,
                 "vol_ratio": r1(vol_ratio), "tight": r1(tight), "rule": RULE_VERSION,
                 "v5_50": round(sma(vols, 5) / v50, 2) if v50 else None,
                 "ud20": round(ud, 2) if ud is not None else None}

    s10_prev = sma(closes[:-3], 10)
    d10 = (c / s10 - 1) * 100 / lev
    zone20 = stage2 and s20_prev and s20 > s20_prev and -2 <= d20 <= 3 and c > s50
    zone10 = (not zone20 and stage2 and s10_prev and s10 > s10_prev
              and -2 <= d10 <= 2 and 3 < d20 <= 12 and c > s50)
    if zone20 or zone10:
        where = "20MA" if zone20 else "10MA"
        if not (dry and accum):
            missing = []
            if not dry:
                missing.append(f"近 5 日量是 50 日均量的 {sma(vols, 5) / v50:.1f} 倍，未縮")
            if not accum:
                missing.append(f"20 日上漲量/下跌量 {ud:.2f}，未到 {UD_MIN}" if ud else "20 日沒有下跌日可比較")
            return {**base_info, "sig": "vol_wait", "note": f"價格已回到 {where} 買點區，但" + "；".join(missing)}
        entry, stop = h * 1.002, low5 * 0.99
        risk = (1 - stop / entry) * 100
        if not MIN_RISK <= risk <= max_risk:
            why = "太遠" if risk > max_risk else "太近（容易被正常波動掃出）"
            return {**base_info, "sig": "tighten",
                    "note": f"{where} 回調 setup，但回調低點 {low5:,.2f} 距進場 {risk:.1f}% {why}，等下一個較乾淨的回踩"}
        order = {**base_info, "entry": round(entry, 2), "stop": round(stop, 2), "stop_basis": "回調低點",
                 "risk": round(risk, 1), "gap": round((entry / c - 1) * 100, 1)}
        if zone20:
            return {**order, "sig": "pullback", "size": "full",
                    "note": f"縮量回踩 20MA（{s20:,.2f}），突破今日高點確認反彈；停損在 5 日低點 {low5:,.2f} 下"}
        return {**order, "sig": "shallow", "size": "half",
                "note": f"強勢股縮量回踩 10MA（{s10:,.2f}），只下半注；突破今日高點確認反彈，停損在 5 日低點 {low5:,.2f} 下"}
    if stage2 and d20 < 10 and c >= pivot * 0.95 and tight < 12:
        return {**base_info, "sig": "watch",
                "note": f"接近 20 日高點 {pivot:,.2f}，不追突破；等縮量回到 10MA（{s10:,.2f}）或 20MA（{s20:,.2f}）"}
    if stage2 and d20 >= 10:
        return {**base_info, "sig": "extended", "note": f"等回測 20MA 附近（{s20:,.2f}–{s20 * (1 + 0.03 * lev):,.2f}）"}
    if stage2 and 4 <= d20 < 10:
        return {**base_info, "sig": "stretched", "note": f"等 D20 < {4 * lev:g}%（約 {s20 * (1 + 0.04 * lev):,.2f} 以下）"}
    return {**base_info, "sig": None, "note": ""}


def market_score(date, series, benchmarks):
    above20 = above50 = n = 0
    for xs in series.values():
        if len(xs) >= 50:
            n += 1
            above20 += xs[-1] > sma(xs, 20)
            above50 += xs[-1] > sma(xs, 50)
    b20 = above20 / n if n else 0
    b50 = above50 / n if n else 0
    checks = []
    for xs in benchmarks.values():
        if len(xs) >= 200:
            checks += [xs[-1] > ema(xs, 21), xs[-1] > sma(xs, 50), sma(xs, 50) > sma(xs, 200)]
    trend = sum(checks) / len(checks) if checks else 0
    return {
        "date": date,
        "score": round(100 * (0.3 * b20 + 0.3 * b50 + 0.4 * trend), 1),
        "breadth20": round(b20 * 100, 1),
        "breadth50": round(b50 * 100, 1),
        "index_trend": round(trend * 100, 1),
    }


def r1(x):
    return round(x, 1) if x is not None else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=60, help="trading days of history to (re)build")
    ap.add_argument("--refresh-sp500", action="store_true", help="re-download the S&P 500 member list")
    args = ap.parse_args()

    cfg = json.load(open(CONFIG))
    try:
        held = [h["ticker"] for h in json.load(open(PORTFOLIO))["holdings"]]
    except (FileNotFoundError, KeyError, json.JSONDecodeError):
        held = []
    tickers = list(dict.fromkeys(cfg["tickers"] + held))
    benches = cfg.get("benchmarks", ["SPY", "QQQ"])
    top_n, lookback = cfg.get("top_n", 10), cfg.get("lookback_days", 7)
    lev_map = cfg.get("leveraged", {})
    risk_map = cfg.get("max_risk_override", {})

    print(f"Fetching {len(tickers)} tickers + {len(benches)} benchmarks…")
    with ThreadPoolExecutor(max_workers=8) as pool:
        data = {s: d for s, d in pool.map(fetch, tickers + benches) if d}
    sp500 = market_state.load_sp500(args.refresh_sp500)
    print(f"Fetching {len(sp500)} S&P 500 members for market breadth…")
    with ThreadPoolExecutor(max_workers=16) as pool:
        sp_data = {s: d for s, d in pool.map(lambda t: fetch(t, "2y"), sp500) if d}
    sp_series = {s: ([k for k, _ in sorted(d["bars"].items())], [b[0] for _, b in sorted(d["bars"].items())])
                 for s, d in sp_data.items()}
    idx_states = {b: market_state.index_states(sorted(data[b]["bars"].items())) for b in ("SPY", "QQQ") if b in data}
    if benches[0] not in data:
        sys.exit(f"benchmark {benches[0]} unavailable — aborting")

    calendar = sorted(data[benches[0]]["bars"])
    dates = calendar[-args.days:]
    sorted_bars = {s: sorted(d["bars"].items()) for s, d in data.items()}

    def bars_asof(sym, date):
        return [b for d, b in sorted_bars[sym] if d <= date]

    def closes_asof(sym, date):
        return [b[0] for b in bars_asof(sym, date)]

    bench_close = {d: b[0] for d, b in data[benches[0]]["bars"].items()}
    month_ago = {d: calendar[max(0, i - 21)] for i, d in enumerate(calendar)}

    snapshots, market = {}, []
    for date in dates:
        series = {s: closes_asof(s, date) for s in tickers if s in data}
        series = {s: xs for s, xs in series.items() if len(xs) >= 60}
        m = {s: stock_metrics(xs) for s, xs in series.items()}
        # leveraged ETFs are ranked against the stocks but never shift the stocks' own ranks
        plain = {s: v for s, v in m.items() if s not in lev_map}
        rs = percentiles({s: v["_wret"] for s, v in plain.items()})
        mom = percentiles({s: v["r1m"] for s, v in plain.items()})
        for s in m.keys() - plain.keys():
            for out, key in ((rs, "_wret"), (mom, "r1m")):
                others = [v[key] for v in plain.values() if v[key] is not None]
                val = m[s][key]
                out[s] = (round(1 + 98 * sum(o < val for o in others) / max(1, len(others) - 1))
                          if val is not None and others else None)
                out[s] = min(99, out[s]) if out[s] is not None else None
        sp_closes = {s: cl[:bisect_right(ds, date)] for s, (ds, cl) in sp_series.items()}
        ref = sorted(w for w in (weighted_return(cl) for cl in sp_closes.values() if len(cl) >= 64) if w is not None)
        rows = []
        for s, v in m.items():
            rs_m = market_rank(v["_wret"], ref)
            line_up = rs_line_up({d: b[0] for d, b in data[s]["bars"].items()}, bench_close, date, month_ago[date])
            tt = v["_tt"] + ((rs_m or 0) >= 70 and line_up)
            tt_pool = v["_tt"] + ((rs[s] or 0) >= 70)
            score = 0.5 * (rs[s] or 0) + 0.3 * (tt / 8 * 100) + 0.2 * (mom[s] or 0)
            lev = lev_map.get(s, 1)
            sig = entry_signal(bars_asof(s, date), v, *((tt, rs_m) if SIGNAL_RS == "market" else (tt_pool, rs[s])),
                               lev, risk_map.get(s))
            rows.append({**sig,
                "ticker": s, "name": data[s]["name"], "spot": v["spot"],
                "zone": zone(*(x / lev if x is not None else None for x in (v["d20"], v["d50"], v["d200"]))),
                "lev": lev if lev > 1 else None,
                "d20": r1(v["d20"]), "d50": r1(v["d50"]), "d200": r1(v["d200"]),
                "r1m": r1(v["r1m"]), "r3m": r1(v["r3m"]), "off_high": r1(v["off_high"]),
                "rs": rs_m, "rs_pool": rs[s], "rs_line_up": line_up, "tt": tt, "score": round(score, 1),
            })
        rows.sort(key=lambda r: -r["score"])
        for i, r in enumerate(rows, 1):
            r["rank"] = i
        snapshots[date] = rows
        mk = market_state.weather(date, sp_closes, {b: closes_asof(b, date) for b in benches if b in data},
                                  {b: st[date] for b, st in idx_states.items() if date in st})
        pool = market_score(date, {s: xs for s, xs in series.items() if s not in lev_map}, {})
        mk["pool_breadth20"], mk["pool_breadth50"] = pool["breadth20"], pool["breadth50"]
        market.append(mk)
        # market gate: new buy-stops only when the weather is >= 50 AND SPY or QQQ has a live FTD
        for r in rows:
            r["armed"] = bool(r["sig"] in ("pullback", "shallow") and mk["score"] >= 50 and mk["ftd_ok"])

    # 新強勢股發現: S&P 500 members outside the universe that pass all 8 trend-template checks
    last = dates[-1]
    sp_closes = {s: cl[:bisect_right(ds, last)] for s, (ds, cl) in sp_series.items()}
    ref = sorted(w for w in (weighted_return(cl) for cl in sp_closes.values() if len(cl) >= 64) if w is not None)
    discover = []
    for s, cl in sp_closes.items():
        if s in data or len(cl) < 221:
            continue
        v = stock_metrics(cl)
        rs_m = market_rank(v["_wret"], ref)
        if v["_tt"] < 7 or (rs_m or 0) < DISCOVER_RS:
            continue
        ds = sp_series[s][0]
        if not rs_line_up(dict(zip(ds, sp_series[s][1])), bench_close, last, month_ago[last]):
            continue
        discover.append({"ticker": s, "name": sp_data[s]["name"], "spot": v["spot"], "rs": rs_m,
                         "d20": r1(v["d20"]), "d50": r1(v["d50"]), "r3m": r1(v["r3m"]),
                         "off_high": r1(v["off_high"]),
                         "zone": zone(v["d20"], v["d50"], v["d200"])})
    discover.sort(key=lambda r: -r["rs"])

    # earnings dates + position size for each signal (1% account risk, 🟡 half)
    earn = earnings.get(tickers, EARNINGS)
    try:
        port = json.load(open(PORTFOLIO))
        acct_total = port["cash"] + sum(h["shares"] * (closes_asof(h["ticker"], last)[-1] if h["ticker"] in data else h["price"])
                                        for h in port["holdings"])
        account = {"total": round(acct_total, 2), "cash": port["cash"], "risk_pct": RISK_PER_TRADE}
    except (FileNotFoundError, KeyError, json.JSONDecodeError, IndexError):
        account = None
    for date in dates:
        for r in snapshots[date]:
            e = earn.get(r["ticker"]) or {}
            r["earn"], r["earn_kind"] = e.get("date"), e.get("kind")
    for r in snapshots[last]:
        if r["earn"]:
            r["earn_days"] = (datetime.strptime(r["earn"], "%Y-%m-%d") - datetime.strptime(last, "%Y-%m-%d")).days
        if account and r.get("entry") and r.get("stop") and r["entry"] > r["stop"]:
            budget = account["total"] * RISK_PER_TRADE / 100 * (0.5 if r["sig"] == "shallow" else 1)
            r["size_shares"] = int(budget // (r["entry"] - r["stop"]))
            r["size_amount"] = round(r["size_shares"] * r["entry"], 2)

    # persistence: rank change, Top-N streak, Top-N hits in last `lookback` days
    for i, date in enumerate(dates):
        prev = {r["ticker"]: r["rank"] for r in snapshots[dates[i - 1]]} if i else {}
        window = dates[max(0, i - lookback + 1): i + 1]
        tops = [{r["ticker"] for r in snapshots[d] if r["rank"] <= top_n} for d in window]
        for r in snapshots[date]:
            t = r["ticker"]
            r["chg"] = prev[t] - r["rank"] if t in prev else None
            r["hits"] = sum(t in s for s in tops)
            streak = 0
            for d in reversed(dates[: i + 1]):
                if any(x["ticker"] == t and x["rank"] <= top_n for x in snapshots[d]):
                    streak += 1
                else:
                    break
            r["streak"] = streak

    for j, mk in enumerate(market):
        mk["delta"] = round(mk["score"] - market[j - 1]["score"], 1) if j else None

    os.makedirs(os.path.dirname(OUT_JS), exist_ok=True)
    log = signal_log.update(SIGNAL_LOG, dates, snapshots, sorted_bars)

    out = {
        "signals": log,
        "signal_rules": {"order_days": signal_log.ORDER_DAYS, "horizons": signal_log.HORIZONS},
        "groups": cfg.get("groups", {}),
        "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "top_n": top_n, "lookback": lookback,
        "dates": dates, "snapshots": snapshots, "market": market,
        "account": account,
        "discover": {"date": last, "min_rs": DISCOVER_RS, "rows": discover[:25], "total": len(discover)},
    }
    with open(OUT_JS, "w") as f:
        f.write("window.RANKING_DATA = ")
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write(";\n")

    last = dates[-1]
    mk = market[-1]
    print(f"\n{last}  市場天氣 {mk['score']}  (Δ {mk['delta']})  " + " | ".join(mk["reasons"]))
    for r in snapshots[last][:top_n]:
        print(f"{r['rank']:>3} {r['ticker']:<6} {r['spot']:>10,.2f}  {r['zone']}  "
              f"D20 {r['d20']:+6.1f}%  D50 {r['d50']:+6.1f}%  RS {r['rs']:>2}  TT {r['tt']}/8  "
              f"Top{top_n} {r['hits']}/{lookback}d  score {r['score']}")
    print(f"\n新強勢股發現（S&P 500 成分股、不在股票池、TT 8/8、RS ≥ {DISCOVER_RS}）：共 {len(discover)} 隻")
    for r in discover[:10]:
        print(f"  {r['ticker']:<6} RS {r['rs']:>2}  {r['zone']}  D20 {r['d20']:+6.1f}%  距高點 {r['off_high']:+6.1f}%  {r['name']}")
    labels = {"pullback": "🟢 回調買點", "shallow": "🟡 淺回調(半注)"}
    setups = [r for r in snapshots[last] if r["sig"] in labels and r["rank"] <= 20]
    gate_ok = mk["score"] >= 50 and mk["ftd_ok"]
    print(f"\n進場訊號（Top 20）{'' if gate_ok else '— 市場天氣 < 50 或未有 FTD，先不掛新單'}")
    for r in setups or []:
        size = f"  {r['size_shares']} 股 ≈ ${r['size_amount']:,.0f}" if r.get("size_shares") is not None else ""
        soon = (f"  ⚠️ {r['earn_days']} 日後公布業績" if r.get("earn_days") is not None
                and 0 <= r["earn_days"] <= EARNINGS_WARN_DAYS else "")
        print(f"  {labels[r['sig']]}  {r['ticker']:<6} buy-stop {r['entry']:,.2f} ({r['gap']:+.1f}%)  "
              f"停損 {r['stop']:,.2f}（{r['stop_basis']}下）  風險 {r['risk']}%{size}{soon}  | {r['note']}")
    if not setups:
        print("  今天沒有符合條件的 setup")
    st = signal_log.summary(log)
    fmt = lambda v, suf="%": "—" if v is None else f"{v:+.2f}{suf}" if suf == "%" else f"{v}{suf}"
    print(f"\n訊號紀錄（市場天氣 ≥ 50 且有 FTD 時發出的訊號）：共 {st['signals']} 筆，成交 {st['filled']}"
          f"（成交率 {st['fill_rate'] if st['fill_rate'] is not None else '—'}%），"
          f"停損 {st['stopped']}，持有中 {st['open']}，平均 R {st['avg_r'] if st['avg_r'] is not None else '—'}")
    for h in signal_log.HORIZONS:
        print(f"  {h:>2} 日：平均 {fmt(st[f'avg{h}'])}  勝率 {st[f'win{h}'] if st[f'win{h}'] is not None else '—'}%  (n={st[f'n{h}']})")
    for sig_, lab in (("pullback", "🟢 回調"), ("shallow", "🟡 淺回調")):
        t = signal_log.summary(log, sig=sig_)
        print(f"  {lab}：{t['signals']} 筆訊號，成交 {t['filled']}，停損 {t['stopped']}，"
              f"平均 R {t['avg_r'] if t['avg_r'] is not None else '—'}")
    print(f"\nwrote {os.path.relpath(OUT_JS, ROOT)}, {os.path.relpath(SIGNAL_LOG, ROOT)}")


if __name__ == "__main__":
    main()
