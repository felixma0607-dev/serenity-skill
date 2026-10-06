"""
Signal log (訊號紀錄) — paper-tracks every 🟢 pullback signal the leaderboard issues
(breakout signals were logged too until rule v2 on 2026-09-27; those records are kept), so after a few weeks you can judge whether the rules work.

Simulation rules (deliberately simple and stated in the dashboard):
  - The buy-stop order is valid for ORDER_DAYS trading days after the signal day.
    It fills on the first day whose high >= entry, at max(entry, that day's open)
    (a gap up fills above the planned price).
  - After the fill, the stop is hit on the first day whose low <= stop (the fill
    day included — conservative), exiting at min(stop, that day's open).
  - Returns are measured 5 / 10 / 20 trading days after the fill, "with stop":
    once stopped out, the result stays at the stop exit. R = return / planned risk.
  - While a ticker has an open record (order pending or position live), repeat
    signals on the same ticker are not logged again.

The log is persisted in data/signal-log.json: records are never deleted, and a
rule change later does not rewrite signals already logged. Outcomes are
recomputed from price bars on every run.
"""
import json
import os

ORDER_DAYS = 5
HORIZONS = (5, 10, 20)


def _simulate(rec, bars_after):
    """bars_after: [(date, (c, h, l, v, o)), ...] strictly after the signal date."""
    out = {"status": "pending", "fill_date": None, "fill": None, "exit_date": None,
           "exit": None, "last": None, "ret": None, "r": None, "closed": None,
           **{f"ret{h}": None for h in HORIZONS}}
    entry, stop = rec["entry"], rec["stop"]
    fill_i = None
    for i, (d, b) in enumerate(bars_after[:ORDER_DAYS]):
        if b[1] >= entry:
            fill_i = i
            out["fill_date"], out["fill"] = d, round(max(entry, b[4] or entry), 2)
            break
    if fill_i is None:
        if len(bars_after) >= ORDER_DAYS:
            out["status"] = "expired"
            out["closed"] = bars_after[ORDER_DAYS - 1][0]
        return out

    fill = out["fill"]
    held = bars_after[fill_i:]
    stopped_at = None
    for k, (d, b) in enumerate(held):
        if b[2] <= stop:
            stopped_at = k
            out["exit_date"], out["exit"] = d, round(min(stop, b[4] or stop), 2)
            break

    def ret_at(k):
        if stopped_at is not None and stopped_at <= k:
            return (out["exit"] / fill - 1) * 100
        return (held[k][1][0] / fill - 1) * 100

    for h in HORIZONS:
        if len(held) > h:
            out[f"ret{h}"] = round(ret_at(h), 2)

    horizon = HORIZONS[-1]
    if stopped_at is not None and stopped_at <= horizon:
        out["status"] = "stopped"
        out["closed"] = out["exit_date"]
        out["ret"] = round((out["exit"] / fill - 1) * 100, 2)
    elif len(held) > horizon:
        out["status"] = "done"
        out["closed"] = held[horizon][0]
        out["ret"] = out[f"ret{horizon}"]
    else:
        out["status"] = "open"
        out["ret"] = round((held[-1][1][0] / fill - 1) * 100, 2)
    out["last"] = round(held[-1][1][0], 2)
    risk = (fill - stop) / fill * 100
    out["r"] = round(out["ret"] / risk, 2) if risk > 0 else None
    return out


def update(path, dates, snapshots, sorted_bars):
    """Add new signals from snapshots, recompute outcomes, save, return the log."""
    try:
        with open(path) as f:
            log = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        log = []
    known = {(r["date"], r["ticker"]) for r in log}
    latest = dates[-1]

    def recompute(rec):
        bars = sorted_bars.get(rec["ticker"])
        if bars:  # ticker dropped from the universe -> keep last known outcome
            rec.update(_simulate(rec, [(d, b) for d, b in bars if d > rec["date"]]))

    for rec in log:
        recompute(rec)

    for date in dates:
        for row in snapshots[date]:
            if row.get("sig") not in ("pullback", "shallow") or (date, row["ticker"]) in known:
                continue
            active = any(r["ticker"] == row["ticker"] and r["date"] < date
                         and (r["closed"] is None or r["closed"] >= date) for r in log)
            if active:
                continue
            rec = {k: row.get(k) for k in ("ticker", "sig", "rank", "entry", "stop",
                                           "stop_basis", "risk", "rs", "tt", "armed", "rule", "size")}
            rec.update(date=date, backfill=date < latest)
            recompute(rec)
            log.append(rec)
            known.add((date, row["ticker"]))

    log.sort(key=lambda r: (r["date"], r["rank"] or 999))
    with open(path, "w") as f:
        json.dump(log, f, ensure_ascii=False, indent=1)
    return log


def summary(log, armed_only=True, sig=None):
    rows = [r for r in log if (r.get("armed") or not armed_only) and (sig is None or r["sig"] == sig)]
    filled = [r for r in rows if r["fill"] is not None]
    resolved = [r for r in rows if r["status"] != "pending"]

    def avg(xs):
        xs = [x for x in xs if x is not None]
        return round(sum(xs) / len(xs), 2) if xs else None

    out = {"signals": len(rows), "filled": len(filled),
           "fill_rate": round(len(filled) / len(resolved) * 100) if resolved else None,
           "stopped": sum(r["status"] == "stopped" for r in rows),
           "open": sum(r["status"] == "open" for r in rows),
           "avg_r": avg(r["r"] for r in filled if r["status"] in ("done", "stopped"))}
    for h in HORIZONS:
        vals = [r[f"ret{h}"] for r in filled if r[f"ret{h}"] is not None]
        out[f"avg{h}"] = avg(vals)
        out[f"win{h}"] = round(sum(v > 0 for v in vals) / len(vals) * 100) if vals else None
        out[f"n{h}"] = len(vals)
    return out
