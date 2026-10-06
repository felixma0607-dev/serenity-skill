"""
Market weather (市場天氣) — the broad-market read used by the leaderboard and the home page.

Replaces the old "market temperature", which measured breadth on the leaderboard's own
tech-heavy universe and so read too optimistic whenever a handful of AI names were strong.
Now built from four parts (0-100):

  breadth   50 pts  % of S&P 500 members above their 20-day (25) and 50-day (25) SMA
  trend     20 pts  SPY/QQQ: close > 21EMA, close > 50SMA, 50SMA > 200SMA
  distrib.  15 pts  IBD distribution days in the last 20 sessions, per index:
                    0-2 -> full, 3-4 -> half, 5+ -> none   (averaged over SPY/QQQ)
  state     15 pts  IBD market state per index: confirmed uptrend 1, uptrend under
                    pressure 0.5, rally attempt 1/3, correction 0   (averaged)

Rules follow references/minervini-technical.md:
  - distribution day: close down >= 0.2% on volume above the prior session's
  - rally attempt: day 1 is the first up close at/after the correction low; a new low
    below the correction low resets the count
  - follow-through day (FTD): rally day >= 4, close up >= 1.7% on higher volume than the
    prior session -> uptrend; the FTD day's low is the failure line
  - an uptrend turns into a correction on either
      * a close below the FTD day's low (the FTD failed), or
      * a sell signal: a close below the 20-day SMA within 3 sessions of a distribution day
  - only a new FTD ends a correction (reclaiming the trigger day's high is not used here,
    to stay with Felix's "new positions wait for an FTD" rule)
"""
import json
import os
import re
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SP500 = os.path.join(ROOT, "config", "sp500.json")
WIKI = "https://en.wikipedia.org/w/index.php?title=List_of_S%26P_500_companies&action=raw"

FTD_GAIN = 1.7
DIST_DROP = -0.2
STATE_LABEL = {"confirmed": "確認上升", "pressure": "上升受壓", "rally": "反彈嘗試", "correction": "調整中"}


def load_sp500(refresh=False):
    if refresh or not os.path.exists(SP500):
        req = urllib.request.Request(WIKI, headers={"User-Agent": "Mozilla/5.0"})
        text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
        i = text.find('id="constituents"')
        table = text[i:text.find("|}", i)]
        syms = []
        for row in table.split("\n|-")[1:]:
            m = re.search(r"\{\{(?:NyseSymbol|NasdaqSymbol|Symbol)\|([A-Z.]+)", row)
            if m:
                syms.append(m.group(1).replace(".", "-"))  # Yahoo uses BRK-B
        if len(syms) < 450:
            raise RuntimeError(f"S&P 500 parse looks wrong ({len(syms)} symbols)")
        from datetime import date
        json.dump({"_comment": "S&P 500 成分股，用於市場天氣的寬度計算。來源：Wikipedia。用 --refresh-sp500 更新。",
                   "updated": date.today().isoformat(), "tickers": syms}, open(SP500, "w"), indent=1)
    return json.load(open(SP500))["tickers"]


def index_states(bars):
    """bars: [(date, (c,h,l,v,o)), ...] oldest first -> {date: state dict}."""
    out = {}
    state, low, rally_day, ftd_low, ftd_date = "correction", None, 0, None, None
    dist = []  # booleans per session
    closes = [b[1][0] for b in bars]
    for i in range(1, len(bars)):
        d, (c, h, l, v, o) = bars[i]
        pc, pv = bars[i - 1][1][0], bars[i - 1][1][3]
        chg = (c / pc - 1) * 100
        dist.append(chg <= DIST_DROP and v > pv)

        sma20 = _sma(closes[:i + 1], 20)
        if state == "uptrend" and (c < ftd_low or (sma20 and c < sma20 and any(dist[-3:]))):
            state, low, rally_day = "correction", l, 0
        elif state == "correction":
            if low is None or l < low:          # new low: the rally count restarts
                low = l
                rally_day = 1 if c > pc else 0
            elif rally_day == 0:
                rally_day = 1 if c > pc else 0
            else:
                rally_day += 1
                if rally_day >= 4 and chg >= FTD_GAIN and v > pv:
                    state, ftd_low, ftd_date = "uptrend", l, d

        window = dist[-20:]
        n = sum(window)
        dates = [bars[i - k][0] for k, f in enumerate(reversed(window)) if f]
        if state == "uptrend":
            key = "pressure" if n >= 5 else "confirmed"
        else:
            key = "rally" if rally_day else "correction"
        out[d] = {"state": key, "label": STATE_LABEL[key] + (f"第{rally_day}天" if key == "rally" else ""),
                  "dist20": n, "dist_dates": sorted(dates), "rally_day": rally_day if key == "rally" else None,
                  "ftd_date": ftd_date if state == "uptrend" else None,
                  "ftd_low": round(ftd_low, 2) if state == "uptrend" else None}
    return out


def _sma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _ema(xs, n):
    if len(xs) < n:
        return None
    k, e = 2 / (n + 1), sum(xs[:n]) / n
    for x in xs[n:]:
        e = x * k + e * (1 - k)
    return e


def breadth(closes_by_ticker):
    a20 = a50 = n = 0
    for xs in closes_by_ticker.values():
        if len(xs) >= 50:
            n += 1
            a20 += xs[-1] > _sma(xs, 20)
            a50 += xs[-1] > _sma(xs, 50)
    return (a20 / n * 100 if n else None, a50 / n * 100 if n else None, n)


def weather(date, sp_closes, bench_closes, states):
    """sp_closes / bench_closes: {ticker: closes up to date}; states: {'SPY': {...}, 'QQQ': {...}}"""
    b20, b50, n = breadth(sp_closes)
    checks = []
    for xs in bench_closes.values():
        if len(xs) >= 200:
            checks += [xs[-1] > _ema(xs, 21), xs[-1] > _sma(xs, 50), _sma(xs, 50) > _sma(xs, 200)]
    trend = sum(checks) / len(checks) if checks else 0
    dist_pts = [1 if s["dist20"] <= 2 else 0.5 if s["dist20"] <= 4 else 0 for s in states.values()]
    state_pts = [{"confirmed": 1, "pressure": .5, "rally": 1 / 3, "correction": 0}[s["state"]] for s in states.values()]
    dist_part = sum(dist_pts) / len(dist_pts)
    state_part = sum(state_pts) / len(state_pts)
    score = 25 * (b20 or 0) / 100 + 25 * (b50 or 0) / 100 + 20 * trend + 15 * dist_part + 15 * state_part
    ftd_ok = any(s["state"] in ("confirmed", "pressure") for s in states.values())

    reasons = [f"S&P 500 有 {b20:.0f}% 股票站上 20 日線、{b50:.0f}% 站上 50 日線"]
    for t, s in states.items():
        reasons.append(f"{t}：{s['label']}，近 20 日出貨日 {s['dist20']} 次")
    if not ftd_ok:
        reasons.append("SPY、QQQ 都未有 FTD，新倉先等確認")
    return {
        "date": date, "score": round(score, 1),
        "breadth20": round(b20, 1), "breadth50": round(b50, 1), "breadth_n": n,
        "index_trend": round(trend * 100, 1),
        "parts": {"breadth": round(25 * (b20 + b50) / 100, 1), "trend": round(20 * trend, 1),
                  "distribution": round(15 * dist_part, 1), "state": round(15 * state_part, 1)},
        "indexes": states, "ftd_ok": ftd_ok, "reasons": reasons,
    }
