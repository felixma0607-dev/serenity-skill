#!/usr/bin/env python3
"""
Distribution day counter (IBD-style methodology).

Rule: a trading day counts as a "distribution day" when the index closes
down >= 0.2% AND volume is higher than the immediately preceding trading day's
volume. This matches the definition documented in references/minervini-technical.md.

Usage
-----
1. Fetch the index's history page (use Chrome, not raw web_fetch, since
   stockanalysis.com caches aggressively and serves stale snapshots via
   web_fetch — navigate + get_page_text bypasses the cache):
     https://stockanalysis.com/etf/spy/history/
     https://stockanalysis.com/etf/qqq/history/
2. Save the raw page text (or just the historical-data table portion) to a
   .txt file. The script's regex looks for lines shaped like:
     "Jul 8, 2026 743.16 746.15 739.51 745.40 745.40 -0.31% 43,767,393"
   i.e. Date  Open  High  Low  Close  Adj.Close  Change%  Volume
   Extra header/nav/footer text around it is ignored automatically.
3. Run:
     python3 distribution_days.py spy_history.txt
   or pipe text directly:
     cat spy_history.txt | python3 distribution_days.py -

Output: total distribution days in the most recent 20 trading days (~4 weeks)
and 26 trading days (~5.2 weeks), with the specific flagged dates, so you can
see at a glance whether the count is at/above the "5-6+ in 4-5 weeks" caution
threshold used in this project's methodology.
"""

import re
import sys
from datetime import datetime

LINE_RE = re.compile(
    r"([A-Z][a-z]{2} \d{1,2}, \d{4})\s+"      # Date: "Jul 8, 2026"
    r"[\d,]+\.\d+\s+"                          # Open
    r"[\d,]+\.\d+\s+"                          # High
    r"[\d,]+\.\d+\s+"                          # Low
    r"([\d,]+\.\d+)\s+"                        # Close  (captured)
    r"(?:[\d,]+\.\d+|-)\s+"                    # Adj. Close (or "-")
    r"(-?[\d.]+)%\s+"                          # Change % (captured)
    r"([\d,]+)"                                # Volume (captured)
)


def parse(text):
    rows = []
    for line in text.splitlines():
        m = LINE_RE.search(line)
        if not m:
            continue
        date_str, close_str, chg_str, vol_str = m.groups()
        date = datetime.strptime(date_str, "%b %d, %Y")
        close = float(close_str.replace(",", ""))
        chg = float(chg_str)
        vol = int(vol_str.replace(",", ""))
        rows.append({"date": date, "close": close, "chg": chg, "vol": vol})
    # stockanalysis.com history tables are newest-first; sort oldest -> newest
    rows.sort(key=lambda r: r["date"])
    return rows


def find_distribution_days(rows):
    flagged = []
    for i in range(1, len(rows)):
        today, prev = rows[i], rows[i - 1]
        if today["chg"] <= -0.2 and today["vol"] > prev["vol"]:
            flagged.append(today)
    return flagged


def summarize(rows, label):
    flagged = find_distribution_days(rows)
    flagged_dates = {f["date"] for f in flagged}

    def count_in_window(n):
        window = rows[-n:] if len(rows) >= n else rows
        window_dates = {r["date"] for r in window}
        return sorted(d for d in flagged_dates if d in window_dates)

    last20 = count_in_window(20)
    last26 = count_in_window(26)

    print(f"=== {label} ===")
    print(f"Rows parsed: {len(rows)} trading days "
          f"({rows[0]['date'].strftime('%Y-%m-%d')} to {rows[-1]['date'].strftime('%Y-%m-%d')})")
    print(f"Distribution days in last 20 sessions (~4 weeks): {len(last20)}")
    for d in last20:
        print(f"    - {d.strftime('%Y-%m-%d')}")
    print(f"Distribution days in last 26 sessions (~5.2 weeks): {len(last26)}")
    for d in last26:
        print(f"    - {d.strftime('%Y-%m-%d')}")
    threshold_20 = len(last20) >= 5
    threshold_26 = len(last26) >= 6
    if threshold_20 or threshold_26:
        print("=> ELEVATED: at/above the 5-6+ in 4-5 week caution threshold.")
    else:
        print("=> Normal range.")
    print()


def main():
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    src = sys.argv[1]
    text = sys.stdin.read() if src == "-" else open(src, encoding="utf-8").read()
    rows = parse(text)
    if not rows:
        print("No rows parsed — check the input format matches a "
              "stockanalysis.com /history/ page dump.")
        sys.exit(1)
    label = src if src != "-" else "stdin"
    summarize(rows, label)


if __name__ == "__main__":
    main()
