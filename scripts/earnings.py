"""
Next earnings dates (業績日期) for the leaderboard and holdings.

Source: Nasdaq's public API (vendor: Zacks).
  1. /api/analyst/{sym}/earnings-date  -> the upcoming date when Zacks has one
     ("estimated" when it is derived from the company's history, not announced).
  2. otherwise /api/company/{sym}/earnings-surprise -> the last reported date,
     and the next one is estimated as last + 91 days (one quarter).

Results are cached in data/earnings.json and refreshed after CACHE_DAYS, or as
soon as a cached date has passed. Stdlib only.
"""
import json
import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

CACHE_DAYS = 3
QUARTER_DAYS = 91
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Origin": "https://www.nasdaq.com",
    "Referer": "https://www.nasdaq.com/",
}


def _get(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get("data") or {}


def _mdy(s):
    return datetime.strptime(s, "%m/%d/%Y").date()


def lookup(sym):
    """-> {"date": ISO or None, "kind": "announced" | "estimated" | "projected" | None, "last": ISO or None}"""
    sym_q = sym.replace("-", ".")
    out = {"date": None, "kind": None, "last": None}
    try:
        text = _get(f"https://api.nasdaq.com/api/analyst/{sym_q}/earnings-date").get("reportText") or ""
        m = re.search(r"(\d{2}/\d{2}/\d{4})", text)
        if m:
            out["date"] = _mdy(m.group(1)).isoformat()
            out["kind"] = "estimated" if "estimated" in text or "algorithm" in text else "announced"
    except Exception:
        pass
    try:
        rows = (_get(f"https://api.nasdaq.com/api/company/{sym_q}/earnings-surprise")
                .get("earningsSurpriseTable") or {}).get("rows") or []
        reported = sorted(_mdy(r["dateReported"]) for r in rows if r.get("dateReported"))
        if reported:
            out["last"] = reported[-1].isoformat()
            if not out["date"]:
                nxt = reported[-1] + timedelta(days=QUARTER_DAYS)
                while nxt < date.today():
                    nxt += timedelta(days=QUARTER_DAYS)
                out["date"], out["kind"] = nxt.isoformat(), "projected"
    except Exception:
        pass
    return out


def get(tickers, path):
    """Return {ticker: lookup()} using the cache at `path`; refresh stale or passed entries."""
    try:
        with open(path) as f:
            cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        cache = {}
    today = date.today()

    def stale(e):
        if not e or not e.get("fetched"):
            return True
        if (today - date.fromisoformat(e["fetched"])).days >= CACHE_DAYS:
            return True
        return bool(e.get("date")) and e["date"] < today.isoformat()

    todo = [t for t in tickers if stale(cache.get(t))]
    if todo:
        with ThreadPoolExecutor(max_workers=4) as pool:
            for t, res in zip(todo, pool.map(lookup, todo)):
                if res["date"] or res["last"] or t not in cache:  # keep the old entry on a failed lookup
                    cache[t] = {**res, "fetched": today.isoformat()}
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump(cache, f, ensure_ascii=False, indent=1, sort_keys=True)
    return {t: cache.get(t) for t in tickers}
