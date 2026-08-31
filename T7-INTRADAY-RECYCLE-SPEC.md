# T7 — Intraday Capital Recycling (SIM ONLY, spec for review — NOT implemented)

**Status:** proposed treatment. Never touches the broker or the live arm. Sim-only, judged on
**exposure-at-maxDD**, not turnover.
**Author:** Sam · **Requested by:** Chase (#5237) · **Date:** 2026-08-30

## The claim under test
A friend proposes: when a block's take-profit clears intraday, **recycle the freed capital
immediately** (same session) instead of waiting for the next maintenance mark. Claimed: **+25–50%
turnover with "essentially identical" exposure.**

## Prior evidence says the claim is probably false — and why
Our existing **cadence** sweep already tested this mechanism from the other side. Recycling freed
capital intraday = **re-evaluating/re-deploying the window more often** = moving toward the
every-bar cadence:

| cadence | maxDD | exposure@maxDD |
|---|---|---|
| every-bar (≈ recycle every fill) | **−51%** | **92%** |
| 7-marks/day (faithful, current live) | **−19.7%** | **49.3%** |

Turnover and exposure are **the same knob**: faster redeployment fills more rungs before the
cash-available throttle re-sizes, so cash decays faster, exposure climbs, and the tail deepens.
"Same exposure, more turnover" is not a free lunch the throttle allows — more turnover *is* more
exposure. T7's job is to **measure the size of that coupling**, not to assume it.

## The treatment
Clone the faithful sim (`sim_control.py`), one parameter apart:
- **Control (T0):** current — sells arm/clear only at the 7 daily marks; freed capital redeploys
  at the next mark.
- **T7:** when a held block's target is hit intraday, **immediately** (same bar) free its capital
  and re-run window maintenance, re-placing/re-sizing rungs off the new cash-available basis before
  the next mark.
- Everything else identical: anchored 2.4% lattice, WINDOW_RUNGS=5, cash-available sizing,
  rung-pegged sells. `EQUITY_BASIS` held fixed at `cash_available` (and a second run at `marked_nav`).

## Fill-model guardrail (do not let it self-inflate)
Same trap as GAPFILL §2: a recycle that both frees a block and refills a rung **in the same bar**
can book a round trip that never rested. Forbid same-bar buy+sell on the recycled capital; count
how often the constraint binds. Use 1-min bars for fill detection, real SIP-clipped SOXL, split-adjusted.

## Acceptance / what to report (judge on the tail, not the churn)
Run both arms on the same paths and report, side by side:
1. **exposure@maxDD** — the headline. Prediction: T7 > T0 (worse), monotonic with recycle frequency.
2. maxDD %, peak exposure, trapped-inventory avg cost.
3. turnover (round-trip count) — report it, but it is **not** the pass criterion.
4. `illusion_delta` (speclot − avgcost) — recycling books more small specific-lot "wins" while the
   avg-cost hole deepens; expect the illusion to **widen** under T7. That widening is the tell that
   the extra turnover is manufactured, not earned.
- **Verdict rule:** T7 is only worth considering if exposure@maxDD is within a small band of T0
  (≈ ±2pp) while turnover rises materially. If exposure climbs with turnover (expected), the claim
  is refuted and T7 stays a documented negative — the throttle is doing its job and should not be
  bypassed.

## Runs
- `T7 × {cash_available, marked_nav}` vs `T0 × {same}`, on: the −47% July path, the −26% Aug 19–28
  path (the live tail we just recorded), and a chop/rebound month.
- The Aug 19–28 path matters most: it lets us check T7's sim prediction against what the **live**
  arm actually did (17.9% exposure@maxDD on −26%), once the live orphan-share divergence is resolved.

## Not in scope
- No broker, no live arm, no arming. Sim files only. T-series stays gated behind a realistic
  control fill model (GAPFILL still owed).
