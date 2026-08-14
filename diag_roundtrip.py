#!/usr/bin/env python3
"""Diagnostic for Chase's round-trip gap question (#4982).
Replays Aaron's actual July path (faithful cadence, cash basis, depth 5 = the live arm)
and logs EVERY round trip's realized % = (target-cost)/cost, so we can settle candidate
(b): is the sim booking the structural 2.4% floor, or Aaron's ~11% median?
Uses the exact cached, SIP-clipped 1-min series the published sim uses."""
import statistics as st
import sim_control as S
from faithful_control import SPACING, window_rungs, block_shares

daily  = S.load_bars("1Day", "2016-01-01", "2026-08-11")
minute = S.load_bars("1Min", "2026-07-01", "2026-08-11")
minute, _ = S.clean_minute(minute, daily)

import faithful_control as fc
fc.WINDOW_RUNGS = 5
START = 100_000.0
cash = START; pos = 0; realized = 0.0; anchor = None
resting = {}; held = []
trips = []          # (cost, target, shares, pct, pnl)
MARKS = S.MARKS

for (t, o, h, l, c) in minute:
    for rung in sorted(list(resting), reverse=True):
        if l <= rung:
            b = resting.pop(rung)
            cash -= b["shares"] * rung; pos += b["shares"]
            tgt = round(rung * (1 + SPACING), 2)
            held.append({"shares": b["shares"], "cost": rung, "target": tgt, "armed": False, "peeked": False})
    for hb in list(held):
        if hb["armed"] and h >= hb["target"]:
            pnl = hb["shares"] * (hb["target"] - hb["cost"])
            realized += pnl; cash += hb["shares"] * hb["target"]; pos -= hb["shares"]
            pct = (hb["target"] - hb["cost"]) / hb["cost"] * 100
            trips.append((hb["cost"], hb["target"], hb["shares"], pct, pnl))
            held.remove(hb)
        elif (not hb["armed"]) and h >= hb["target"] and not hb["peeked"]:
            hb["peeked"] = True
    if t[11:16] in MARKS:
        for hb in held:
            hb["armed"] = True
        if anchor is None: anchor = c
        if pos == 0 and not held and not resting: anchor = c
        tgts = window_rungs(anchor, c); keep = {pr for _, pr in tgts}
        for rung in list(resting):
            if rung not in keep: del resting[rung]
        committed = sum(b["shares"] * r for r, b in resting.items())
        for _, rung in tgts:
            if rung in resting: continue
            sh = block_shares(cash - committed, rung); cost = sh * rung
            if sh <= 0 or cost > (cash - committed): continue
            resting[rung] = {"shares": sh, "target": round(rung * (1 + SPACING), 2)}
            committed += cost

pcts = [x[3] for x in trips]
pnls = [x[4] for x in trips]
blocks = [x[0]*x[2] for x in trips]
print(f"round trips           : {len(trips)}")
print(f"realized total        : ${sum(pnls):,.2f}")
print(f"realized $/trip        : mean ${st.mean(pnls):.2f}  median ${st.median(pnls):.2f}")
print(f"realized %/trip        : mean {st.mean(pcts):.4f}%  median {st.median(pcts):.4f}%  "
      f"min {min(pcts):.4f}%  max {max(pcts):.4f}%")
print(f"block $ per trip       : mean ${st.mean(blocks):,.2f}  median ${st.median(blocks):,.2f}")
print(f"distinct %/trip values : {sorted(set(round(p,3) for p in pcts))[:12]}")
