#!/usr/bin/env python3
"""
SIM CONTROL ARM — never touches the broker. SIM ONLY.

Purpose: the uncapped equity-basis control the live (Aaron-replica) arm can't be.
One parameter off the live arm: EQUITY_BASIS. The strategy itself — anchored 2.4%
lattice, sizing, rung-pegged two-step sells, window bound of 5 — is IMPORTED from
faithful_control, so the sim uses the *same code objects* the live arm does.

    live arm   : EQUITY_BASIS = "cash_available"  (throttles ~0.975^n)
    sim control: EQUITY_BASIS = "marked_nav"      (uncapped, approaches ~100%)

CADENCE (reviewer #3 — the one that makes this faithful, not a different strategy):
  The live arm maintains the ladder SEVEN times a day (launchd :30, 6:30-12:30 PT).
  So the sim runs window maintenance ONLY at those 7 mark times; 1-minute bars are
  used ONLY to decide what filled between marks. Re-evaluating the window every
  minute would be a 55x-faster strategy, not one parameter apart.

FILL MODEL (reviewer — the trap that flatters grid backtests):
  A rung and its take-profit can both sit inside one coarse bar; filling both books a
  round trip that never happened, most often in the volatile stretches where the tail
  lives. With minute-granularity fills + marks-cadence arming this can't happen: a
  buy's sell is armed only at the NEXT mark, so buy+sell never clear in one bar. The
  DAILY smoke run (fills off daily bars) DOES have the trap; we hard-forbid same-bar
  round trips there and report how often the constraint BINDS — if large, daily is
  unusable for anything quantitative (and so was any prior daily-bar grid backtest).

DATA CAVEATS (reviewer #1,#2):
  * adjustment=split (raw bars would turn a split into a fake -catastrophic- DD).
  * 1-min is IEX (free plan). IEX is a few % of consolidated volume, so its lows are
    thinner than the tape -> fewer fills -> UNDERSTATED deployment/drawdown. The bias
    makes the strategy look SAFER than reality. Numbers below are a floor on the tail.

JUDGE METRIC: exposure-at-max-drawdown, not return.
"""
from __future__ import annotations
import json, os, csv, urllib.request, urllib.parse

from faithful_control import lattice_price, window_rungs, block_shares, SPACING, WINDOW_RUNGS, BLOCK_PCT

SYMBOL = "SOXL"
CACHE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sim_data")
os.makedirs(CACHE, exist_ok=True)
# live cron marks in UTC (6:30..12:30 America/Los_Angeles, PDT = UTC-7 for the summer window)
MARKS = {"13:30", "14:30", "15:30", "16:30", "17:30", "18:30", "19:30"}


def _env():
    return json.load(open(os.path.expanduser("~/.openclaw/config/env.json")))

def load_bars(timeframe, start, end):
    fn = os.path.join(CACHE, f"SOXL_{timeframe}_{start}_{end}.csv")
    if os.path.exists(fn):
        with open(fn) as f:
            return [(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4])) for r in csv.reader(f)]
    e = _env()
    hdr = {"APCA-API-KEY-ID": e["APCA_API_KEY_ID"], "APCA-API-SECRET-KEY": e["APCA_API_SECRET_KEY"]}
    bars, token = [], None
    while True:
        feed = "iex" if "Min" in timeframe else "sip"      # intraday: IEX (free). daily: SIP has fuller history.
        q = {"timeframe": timeframe, "start": start, "end": end, "limit": 10000,
             "adjustment": "split", "feed": feed}
        if token: q["page_token"] = token
        url = f"https://data.alpaca.markets/v2/stocks/{SYMBOL}/bars?{urllib.parse.urlencode(q)}"
        with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=60) as x:
            d = json.loads(x.read())
        for b in d.get("bars", []):
            bars.append((b["t"], b["o"], b["h"], b["l"], b["c"]))
        token = d.get("next_page_token")
        if not token: break
    with open(fn, "w", newline="") as f:
        csv.writer(f).writerows(bars)
    return bars


def split_sanity(bars):
    """Reviewer #1: any single-bar move > +-40% is either a split artifact or a real event."""
    out = []
    for i in range(1, len(bars)):
        pc = bars[i - 1][4]; c = bars[i][4]
        if pc and abs(c / pc - 1) > 0.40:
            out.append((bars[i][0][:10], round(pc, 2), round(c, 2), round((c / pc - 1) * 100, 1)))
    return out


def simulate(bars, basis, cadence):
    """cadence 'marks': maintain at the 7 daily marks, fills at minute granularity (FAITHFUL).
       cadence 'bar':   maintain every bar, fills off that bar (DAILY SMOKE — has the fill trap)."""
    START = 100_000.0
    cash = START; pos = 0; realized = 0.0; anchor = None
    resting_buys = {}                 # rung -> {"shares","target"}
    held = []                         # {"shares","cost","target","armed","peeked"}
    peak = START; max_dd = 0.0; exp_at_dd = 0.0; peak_exp = 0.0
    rts = 0; buy_fills = 0; binds = 0; latency_missed = 0; maints = 0

    for (t, o, h, l, c) in bars:
        # ---- fills since the last bar (minute granularity in 'marks' mode) ----
        for rung in sorted(list(resting_buys), reverse=True):
            if l <= rung:
                b = resting_buys.pop(rung)
                cash -= b["shares"] * rung; pos += b["shares"]; buy_fills += 1
                tgt = round(rung * (1 + SPACING), 2)
                held.append({"shares": b["shares"], "cost": rung, "target": tgt, "armed": False, "peeked": False})
                if cadence == "bar" and h >= tgt:
                    binds += 1                          # same-bar round trip forbidden & counted (daily trap)
        for hb in list(held):
            if hb["armed"] and h >= hb["target"]:       # sell fills only if it was armed at a prior mark
                realized += hb["shares"] * (hb["target"] - hb["cost"])
                cash += hb["shares"] * hb["target"]; pos -= hb["shares"]; rts += 1
                held.remove(hb)
            elif (not hb["armed"]) and h >= hb["target"] and not hb["peeked"]:
                hb["peeked"] = True                     # target reached before the cron armed the sell (missed)

        # ---- maintenance: only at marks (faithful) or every bar (smoke) ----
        if cadence == "bar" or t[11:16] in MARKS:
            maints += 1
            for hb in held:
                if not hb["armed"]:
                    hb["armed"] = True                  # arm the conditional sell (the cron cycle after the fill)
                    if hb["peeked"]:
                        latency_missed += 1
            if anchor is None:
                anchor = c
            if pos == 0 and not held and not resting_buys:
                anchor = c
            tgts = window_rungs(anchor, c); keep = {pr for _, pr in tgts}
            for rung in list(resting_buys):
                if rung not in keep:
                    del resting_buys[rung]
            mnav = cash + pos * c
            committed = sum(b["shares"] * r for r, b in resting_buys.items())
            for _, rung in tgts:
                if rung in resting_buys:
                    continue
                bbasis = (cash - committed) if basis == "cash_available" else mnav
                sh = block_shares(bbasis, rung); cost = sh * rung
                if sh <= 0 or cost > (cash - committed):
                    continue
                resting_buys[rung] = {"shares": sh, "target": round(rung * (1 + SPACING), 2)}
                committed += cost
            mnav = cash + pos * c
            peak = max(peak, mnav); dd = mnav / peak - 1.0
            exp = (pos * c) / mnav if mnav else 0.0
            peak_exp = max(peak_exp, exp)
            if dd < max_dd:
                max_dd = dd; exp_at_dd = exp

    last_c = bars[-1][4]
    trapped = pos
    trapped_cost = (sum(x["cost"] * x["shares"] for x in held) / pos) if pos else 0.0
    mnav = cash + pos * last_c
    days = len({b[0][:10] for b in bars})
    return {
        "basis": basis, "cadence": cadence, "bars": len(bars), "days": days,
        "maintenances": maints, "maint_per_day": round(maints / days, 1) if days else 0,
        "total_return_pct": round((mnav / START - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd * 100, 2),
        "exposure_at_max_dd_pct": round(exp_at_dd * 100, 2),
        "peak_exposure_pct": round(peak_exp * 100, 2),
        "realized_pnl": round(realized, 2),
        "trapped_shares": trapped, "trapped_avg_cost": round(trapped_cost, 2), "last_price": round(last_c, 2),
        "round_trips": rts, "buy_fills": buy_fills,
        "same_bar_binds": binds, "bind_pct": round(100 * binds / buy_fills, 1) if buy_fills else 0.0,
        "cron_latency_missed_rt": latency_missed,
    }


def _fmt(m):
    return (f"  basis={m['basis']:<14} ret={m['total_return_pct']:>8.2f}%  maxDD={m['max_drawdown_pct']:>7.2f}%  "
            f"EXPOSURE@maxDD={m['exposure_at_max_dd_pct']:>6.2f}%  peakExp={m['peak_exposure_pct']:>6.2f}%  "
            f"trapped={m['trapped_shares']:>5}sh@{m['trapped_avg_cost']:.2f}  rt={m['round_trips']:>4}  "
            f"maint={m['maint_per_day']}/day")


if __name__ == "__main__":
    # ---- data sanity (reviewer #1) ----
    daily = load_bars("1Day", "2016-01-01", "2026-08-11")
    disc = split_sanity(daily)
    print("=== split/adjustment sanity (single-day |move|>40% on split-adjusted daily) ===")
    print(f"  {len(disc)} flagged: {disc if disc else 'none'}")

    # ---- IEX vs SIP spot check (reviewer #2): compare intraday-derived daily low/high to SIP daily ----
    minute = load_bars("1Min", "2026-07-01", "2026-08-11")
    iex_day = {}
    for t, o, h, l, c in minute:
        d = t[:10]; lo, hi = iex_day.get(d, (l, h)); iex_day[d] = (min(lo, l), max(hi, h))
    sip_day = {b[0][:10]: (b[3], b[2]) for b in daily}
    print("\n=== IEX 1-min vs SIP daily low/high (first 5 overlapping days) ===")
    n = 0
    for d in sorted(iex_day):
        if d in sip_day and n < 5:
            il, ih = iex_day[d]; sl, sh = sip_day[d]
            print(f"  {d}  IEX[{il:.2f},{ih:.2f}]  SIP[{sl:.2f},{sh:.2f}]  low gap {100*(il-sl)/sl:+.2f}%")
            n += 1

    # ---- faithful run: 1-min fills, marks-cadence maintenance ----
    print(f"\n### 1-min fills / MARKS cadence (7/day) — SOXL selloff Jul->Aug 2026  "
          f"[px {minute[0][4]:.2f}->{minute[-1][4]:.2f}]  (IEX -> tail is a FLOOR)")
    for basis in ("cash_available", "marked_nav"):
        m = simulate(minute, basis, "marks")
        print(_fmt(m) + f"  cron-missed-rt={m['cron_latency_missed_rt']}")

    # ---- daily smoke (fill trap present; bind count is the headline) ----
    print(f"\n### daily fills / every-bar — 2016->2026 SMOKE TEST  [px {daily[0][4]:.2f}->{daily[-1][4]:.2f}]")
    for basis in ("cash_available", "marked_nav"):
        m = simulate(daily, basis, "bar")
        print(_fmt(m) + f"  SAME-BAR-BINDS={m['same_bar_binds']} ({m['bind_pct']}% of buy-fills) <- fill trap")
