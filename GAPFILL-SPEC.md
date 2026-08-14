# Gap-Fill Model — Spec for Review (NOT YET IMPLEMENTED)

**Status:** proposed. `sim_control.py` untouched; live arm untouched.
**Author:** Sam · **Requested/scoped by:** Chase (#4985) · **Date:** 2026-08-13
**Why:** the current sim fills every resting order at its exact rung price, booking the
structural 2.4% floor on every round trip (diagnostic `diag_roundtrip.py`: mean/median
**2.3998%**). Aaron's real July booked median **+11.1%**, mean **+14.0%** per trip because
GTC limit orders fill **at limit or better** — on a gapped-down open, many rungs fill at
one low price. This is the source of BOTH the per-trip gap (2.4% vs 11%) and the trip-count
gap (sim ~180 vs Aaron ~29–35): the sim decomposes one fat multi-rung gap-capture into many
thin rung-by-rung crawls. Fixing the fill model closes both at once, or it doesn't — and the
acceptance gate below makes that a pass/fail test rather than a knob to turn.

---

## 1. The rule — three parts, not one

`min(rung, gap_open)` is only the price. The complete rule has a gate, a multi-rung
requirement, and a symmetric sell side.

### 1a. Buy gate + price
A resting buy at rung `R` fills on a bar iff **`low ≤ R`**. Fill price = **`min(R, open)`**.
- Normal case (`open` above `R`, price dips to touch `R`): fills at `R` — unchanged from today.
- Gap case (`open` below `R`): fills at `open` — the improvement.

### 1b. Multi-rung — all resting rungs evaluated on the same bar
If a bar opens below several resting rungs, **every** such rung fills, all at that same
`open`. This is the "8 lots at $165.40 on 07/08" mechanism.
- **Already satisfied by the current structure**: the fill loop (`sim_control.py:123`) iterates
  *all* resting rungs each bar, not one per cycle. So `min(R, open)` gives multi-rung capture
  for free. The spec's job here is to *assert* it stays that way, not to add it.

### 1c. Symmetry — sells get the same treatment
A resting sell at target `T` fills iff **`high ≥ T`**, at price **`max(T, open)`**.
Modeling improvement on buys only is an asymmetry that biases *against* the strategy — no
more legitimate than biasing for it. On a gap-**up** open through a target, the sell captures
the better price, exactly as Aaron's "5 lots sold at $198.92 on 07/10" did.

---

## 2. The part that silently inflates everything if wrong — order age / sequencing

Gap improvement is real **only if the order was already resting when the gap happened.** A
rung (re)placed by this cycle's maintenance must NOT be allowed to capture this cycle's open.

**Required per-bar sequence (fills BEFORE maintenance):**
1. Apply fills against the ladder **as it stood at the end of the previous cycle** (buys, then
   arm-eligible sells), using this bar's `open`.
2. *Then* run window maintenance (cancel/re-place rungs, arm sells for the next cycle).

- **Already correct today**: `sim_control.py` runs the fill block (lines 123–138) *before* the
  maintenance block (lines 140–165). A rung placed at maintenance in bar *N* is first eligible
  in bar *N+1*'s fill pass — i.e. it rested through bar *N+1*'s open. Preserve this ordering.
- **Add an explicit assertion**, not just careful code: at fill time, assert every order being
  filled has `placed_cycle < current_cycle` (tag each resting order with the maintenance-cycle
  index that created it; refuse to fill any order whose tag equals the current cycle). Maintenance-
  first fabricates fills at prices the order never rested through, and it does so in the
  flattering direction — this assertion is the tripwire.
- Same rule for sells: a sell may fill only from the cycle **after** it was armed (already the
  `hb["armed"]` gate; keep it, and the arm sets its own cycle tag).

---

## 3. Build-time sanity check

With 1-min bars, genuine gaps concentrate at the **session open** (the first minute bar of the
RTH session; `open` ≈ prior close on mid-session bars, so `min/max` reduces to the rung).
- Instrument fill improvement in basis points, bucketed by "first bar of session" vs "mid-session."
- **Expectation:** material improvement lands almost entirely on session-open bars. If meaningful
  improvement shows up mid-session, the model is filling on intrabar noise — stop and fix before
  trusting any number.

---

## 4. Acceptance gate — a test, because the rule has zero free parameters

`min`/`max` introduce **no tunable coefficient**. So reproducing Aaron is a *prediction*, not a
fit. Re-run his actual July path at his scale (~3.36× the $100k sleeve, or compare count/percent
which are scale-invariant) and require **all three**:

| metric              | current (rung-exact) | required after gap-fill |
|---------------------|----------------------|-------------------------|
| trip count          | ~180                 | **~29–35**              |
| mean per-trip gain  | 2.3998%              | **~11–14%**             |
| aggregate realized  | ~$6–7k ($20k scaled) | **≈ $23.5k**            |

> **Curve-fit tripwire (state verbatim in `sim_control.py` header):** the gap-fill rule must
> stay parameter-free. If a tunable slippage/participation coefficient is *ever* added, this
> acceptance gate stops being an out-of-sample test and becomes curve-fitting to a single month.
> Any such change must be flagged and the gate re-labeled, not quietly crossed.

---

## 5. Re-open the window question — mandatory re-run, not optional

The earlier sweep found window depth **throttle-neutral under cash sizing** — but that was
measured with **rung-exact fills**, where price crawls through rungs one at a time and the
`0.975ⁿ` throttle keeps pace. Gap fills are precisely the case where depth *should* bite: a
9-rung ladder catches more rungs in a **single gapped-down open** than a 5-rung ladder, and it
catches them **before maintenance can re-size anything**.

- **Required:** after the change, re-run the depth sweep (win 5/8/9/10, cash basis, faithful
  cadence, gap fills on).
- **If depth becomes a lever under cash sizing**, the original window objection was right for a
  reason neither of us had modeled, and the "keep 5, it's throttle-neutral" conclusion is
  reopened — with live-arm implications. Report this explicitly either way.

---

## 6. Confounds to hold fixed (do not attribute prematurely)

- **Trapped-average sign.** Today Aaron's trapped avg ($184.50) is *higher* than the sim's (~$170),
  the opposite of the "cheaper real fills → lower real avg cost" caveat. That comparison is
  confounded by his depth-8–10 window and a more monotonic path. Do **not** attribute the sign to
  gap fills until the model exists **and** depth is held fixed (win 5 both sides).

---

## 7. Change surface (for the reviewer)

Localized to `sim_control.py:simulate()`:
- **Buy fill (line ~126):** `cash -= shares*rung` → `fp = min(rung, o); cash -= shares*fp`; store
  `cost = fp` (not `rung`). Sell target still pegs to the **rung**: `round(rung*1.024, 2)` — that's
  Aaron's behavior (sell = buy-*rung* × 1.024, not fill × 1.024) and it's what makes the gap show
  up as realized gain rather than being spacing-neutralized.
- **Sell fill (line ~133):** `target` → `max(target, o)` as the realized sell price.
- **Cycle tags:** add `placed_cycle` to each resting buy and `armed_cycle` to each held block;
  fill-time assertions per §2.
- **Instrumentation:** per §3 (session-open vs mid-session improvement bps) and the §4 table.
- **No change** to `faithful_control.py`. The live arm fills against a real broker; this is
  sim-only. Note in the header that the sim now models gap fills the live paper account gets from
  Alpaca automatically.

**Gate on the T-series:** no T1–T6 treatment comparison runs until this lands and §4 passes. An
unrealistic control fill model would propagate a bias into every treatment delta — the exact
failure this framework exists to prevent.
