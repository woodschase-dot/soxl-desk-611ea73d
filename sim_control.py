#!/usr/bin/env python3
"""
SIM CONTROL ARM — never touches the broker. SIM ONLY.

Purpose: the uncapped equity-basis control the live (Aaron-replica) arm can't be.
It is one parameter off the live arm: EQUITY_BASIS. Everything that defines the
strategy — the anchored 2.4% lattice, cash-available/marked-NAV sizing, rung-pegged
two-step sells, window bound of 5 — is IMPORTED from faithful_control, so the sim
uses the *same code objects* the live arm does. The only sim-specific code is the
fill model and the P&L/exposure accounting.

    live arm  : EQUITY_BASIS = "cash_available"  (throttles ~0.975^n -> ~59% cap)
    sim control: EQUITY_BASIS = "marked_nav"     (uncapped, approaches ~100%)

FILL MODEL (the trap that ruins grid backtests — reviewer):
  A rung and its take-profit target can BOTH sit inside one bar's [low, high]. Filling
  both books a round trip that never happened, and it happens most in the volatile
  stretches where the tail lives — inflating the many-small-wins side while drawdown
  stays honest. So: a buy and its own sell can NEVER fill in the same bar. Sells armed
  on a bar are ineligible until the next bar; we COUNT how often the constraint binds
  (target within the arming bar's range). If that count is large on daily bars, the
  daily results are a smoke test only — use 1-minute.

JUDGE METRIC: exposure-at-max-drawdown, not return. A rally makes both arms look the
same; the tail is where they differ.
"""
from __future__ import annotations
import json, os, csv, math, urllib.request, urllib.parse, datetime as dt

# --- the strategy IS the live arm's code (one parameter apart) --------------------
from faithful_control import lattice_price, window_rungs, block_shares, SPACING, WINDOW_RUNGS, BLOCK_PCT

SYMBOL = "SOXL"
CACHE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_data")
os.makedirs(CACHE, exist_ok=True)


# ----------------------------- data (real SOXL bars) ------------------------------ #
def _env():
    return json.load(open(os.path.expanduser("~/.openclaw/config/env.json")))

def load_bars(timeframe, start, end):
    """Real split-adjusted SOXL OHLC from Alpaca, cached to CSV. Returns [(t,o,h,l,c), ...]."""
    fn = os.path.join(CACHE, f"SOXL_{timeframe}_{start}_{end}.csv")
    if os.path.exists(fn):
        with open(fn) as f:
            return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in csv.reader(f)]
    e = _env()
    hdr = {"APCA-API-KEY-ID": e["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": e["APCA_API_SECRET_KEY"]}
    bars, token = [], None
    while True:
        # intraday: free plan requires IEX. daily: SIP has the fuller pre-2020 history.
        feed = "iex" if "Min" in timeframe else "sip"
        q = {"timeframe": timeframe, "start": start, "end": end, "limit": 10000,
             "adjustment": "split", "feed": feed}
        if token: q["page_token"] = token
        url = f"https://data.alpaca.markets/v2/stocks/{SYMBOL}/bars?{urllib.parse.urlencode(q)}"
        r = urllib.request.Request(url, headers=hdr)
        with urllib.request.urlopen(r, timeout=60) as x:
            d = json.loads(x.read())
        for b in d.get("bars", []):
            bars.append((b["t"], b["o"], b["h"], b["l"], b["c"]))
        token = d.get("next_page_token")
        if not token: break
    with open(fn, "w", newline="") as f:
        csv.writer(f).writerows(bars)
    return bars


# ----------------------------- the sim -------------------------------------------- #
def simulate(bars, basis, record=False):
    """Run the faithful ladder over a real price path with an honest fill model.
    `basis` in {"cash_available","marked_nav"}. Returns a metrics dict."""
    START = 100_000.0
    cash = START; pos = 0; realized = 0.0
    anchor = None
    resting_buys = {}     # rung(2dp) -> {"shares":int, "target":float}
    resting_sells = {}    # target(2dp) -> {"shares":int, "cost":float, "armed":int}   (held inventory, sell resting)
    peak_nav = START; max_dd = 0.0; exp_at_max_dd = 0.0; peak_exp = 0.0
    round_trips = 0; buy_fills = 0; binds = 0
    curve = []

    for i, (t, o, h, l, c) in enumerate(bars):
        # 1) SELLS placed on a PRIOR bar fill if the bar trades up to the target
        for tgt in sorted(list(resting_sells)):
            s = resting_sells[tgt]
            if s["armed"] < i and h >= tgt:
                realized += s["shares"] * (tgt - s["cost"])
                cash += s["shares"] * tgt; pos -= s["shares"]; round_trips += 1
                del resting_sells[tgt]

        # 2) BUYS fill if the bar trades down to the rung; each then ARMS a sell (ineligible this bar)
        for rung in sorted(list(resting_buys), reverse=True):
            if l <= rung:
                b = resting_buys.pop(rung)
                cash -= b["shares"] * rung; pos += b["shares"]; buy_fills += 1
                tgt = round(rung * (1 + SPACING), 2)
                if h >= tgt:                      # same-bar round trip WOULD have happened -> forbidden, counted
                    binds += 1
                # merge if a sell already rests at tgt (shouldn't at 2dp, but be safe)
                if tgt in resting_sells:
                    ex = resting_sells[tgt]
                    tot = ex["shares"] + b["shares"]
                    ex["cost"] = (ex["cost"] * ex["shares"] + rung * b["shares"]) / tot
                    ex["shares"] = tot
                else:
                    resting_sells[tgt] = {"shares": b["shares"], "cost": rung, "armed": i}

        # 3) WINDOW MAINTENANCE — identical lattice/sizing to the live arm, off the bar close
        if anchor is None:
            anchor = c
        flat = (pos == 0 and not resting_sells and not resting_buys)
        if flat:
            anchor = c
        target = window_rungs(anchor, c)          # [(level, price), ...] — same function the live arm calls
        keep = {pr for _, pr in target}
        for rung in list(resting_buys):           # cancel out-of-window buys
            if rung not in keep:
                del resting_buys[rung]
        mnav = cash + pos * c
        committed = sum(b["shares"] * rung for rung, b in resting_buys.items())
        for _, rung in target:
            if rung in resting_buys:
                continue
            b = (cash - committed) if basis == "cash_available" else mnav
            shares = block_shares(b, rung)        # floor(BLOCK_PCT*basis/rung) — the live arm's sizing
            cost = shares * rung
            if shares <= 0 or cost > (cash - committed):
                continue
            resting_buys[rung] = {"shares": shares, "target": round(rung * (1 + SPACING), 2)}
            committed += cost

        # 4) MARKED-NAV LEDGER (tail-first): drawdown + exposure-at-drawdown are the deliverable
        mnav = cash + pos * c
        peak_nav = max(peak_nav, mnav)
        dd = mnav / peak_nav - 1.0
        exp = (pos * c) / mnav if mnav else 0.0
        peak_exp = max(peak_exp, exp)
        if dd < max_dd:
            max_dd = dd; exp_at_max_dd = exp
        if record:
            curve.append((t, round(mnav, 2), round(dd * 100, 3), round(exp * 100, 3), pos))

    last_c = bars[-1][4]
    trapped = pos
    trapped_cost = (sum(s["cost"] * s["shares"] for s in resting_sells.values()) / pos) if pos else 0.0
    mnav = cash + pos * last_c
    return {
        "basis": basis, "bars": len(bars),
        "final_marked_nav": round(mnav, 2),
        "total_return_pct": round((mnav / START - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "exposure_at_max_dd_pct": round(exp_at_max_dd * 100, 2),   # <-- the judge metric
        "peak_exposure_pct": round(peak_exp * 100, 2),
        "realized_pnl": round(realized, 2),
        "unrealized_pnl": round(pos * last_c - trapped_cost * pos, 2),
        "trapped_shares": trapped,
        "trapped_avg_cost": round(trapped_cost, 2), "last_price": round(last_c, 2),
        "round_trips": round_trips, "buy_fills": buy_fills,
        "same_bar_binds": binds,
        "bind_pct_of_buyfills": round(100 * binds / buy_fills, 1) if buy_fills else 0.0,
        "curve": curve if record else None,
    }


def _fmt(m):
    return (f"  basis={m['basis']:<14} ret={m['total_return_pct']:>7.2f}%  "
            f"maxDD={m['max_drawdown_pct']:>7.2f}%  EXPOSURE@maxDD={m['exposure_at_max_dd_pct']:>6.2f}%  "
            f"peakExp={m['peak_exposure_pct']:>6.2f}%  trapped={m['trapped_shares']:>5}sh@{m['trapped_avg_cost']:.2f}  "
            f"roundtrips={m['round_trips']:>4}  same-bar-binds={m['same_bar_binds']}({m['bind_pct_of_buyfills']}%)")


if __name__ == "__main__":
    import sys
    runs = [
        ("1-min · SOXL selloff Jul->Aug 2026 (Aaron's regime)", "1Min", "2026-07-01", "2026-08-11"),
        ("daily · 2016->2026 long horizon (smoke test)",        "1Day", "2016-01-01", "2026-08-11"),
    ]
    for title, tf, start, end in runs:
        bars = load_bars(tf, start, end)
        if not bars:
            print(f"\n### {title}\n  (no bars)"); continue
        print(f"\n### {title}")
        print(f"  {len(bars)} bars  {bars[0][0][:10]}..{bars[-1][0][:10]}  "
              f"px {bars[0][4]:.2f} -> {bars[-1][4]:.2f}")
        for basis in ("cash_available", "marked_nav"):
            print(_fmt(simulate(bars, basis)))
