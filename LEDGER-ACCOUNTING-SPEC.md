# Ledger Accounting Spec — report BOTH conventions + the illusion delta (review-only)

**Status:** proposed. Armed engine `faithful_control.py` **NOT** to be touched by this — it
already books specific-lot, which is correct and Aaron-faithful. This is a **reporter-layer**
add (reads broker FILL activities + engine state; writes an accounting ledger). Engine stays
byte-identical except by the normal disarm→review→re-arm path.
**Author:** Sam · **Scoped by:** Chase (#5004) · **Date:** 2026-08-15

## Why
The same round trip books **+$54.08 / −$26.84 / −$108.19** under specific-lot / average-cost /
FIFO — identical fills, three signs. Picking one convention *hides* the artifact; the point of
this instrument is to *measure* it. So the ledger reports both and tracks their difference.

## Fields (per cycle, scoped to the v3 arm — fills after the last flatten-to-zero)
- `realized_speclot` — the engine's own figure (block `buy_fill` basis). Aaron-comparable,
  matches Fidelity. **Source of truth = engine `state.realized_pnl`.**
- `realized_avgcost` — running average-cost ledger recomputed from broker FILL activities:
  `realized += qty × (sell_px − running_avg)` on each sell; `running_avg` unchanged by sells.
- `illusion_delta = realized_speclot − realized_avgcost` — **the size of the accounting
  illusion, as a tracked quantity.** First trip: **+$54.08 − (−$26.84) = +$80.92.**

## Fields, identities, predictions

### `illusion_delta` and its per-trip SIGN (the useful part — reviewer #5007)
- `illusion_delta = realized_speclot − realized_avgcost` (cumulative). First trip: **+$80.92**.
- **Per-trip** `delta_i = speclot_i − avgcost_i` is a live phase read, and its SIGN is the signal:
  - `delta_i > 0` (running avg **above** sale price) → **harvesting into a hole** — the
    accumulating, benign-*looking* but dangerous phase.
  - `delta_i < 0` (running avg **below** sale price) → **harvesting out of one** — recovery.
  Emit `harvest_phase = sign(delta_i)` as its own field: it reads the regime from *accounting*,
  not price.
- **Correction to the earlier "widens monotonically" claim (it was overstated):** the cumulative
  delta widens **only while `avg_cost > sale_price`**. In a recovery, avgcost booking exceeds
  speclot and each trip contributes a *negative* delta. Any monotonicity assertion must be
  **conditional on `avg_cost > sale_price`**, never unconditional.

### Reconcile identities — get this exactly right or it fails on the next held cycle
- **Per convention, EVERY cycle (holds with inventory):**
  `realized_X + unrealized_X ≡ NAV − start`, for each `X ∈ {speclot, avgcost}`, where
  `unrealized_X` is computed in that convention's basis (per-block cost for speclot, running
  average for avgcost). Assert this for each X *separately* — it's the cheap leak-check.
- **Cross-convention realized equality holds ONLY when flat:** `realized_speclot ≡
  realized_avgcost` **iff `position == 0`.** While inventory is held the two realized figures
  differ by exactly the unrealized portion each allocates differently. Asserting their equality
  mid-hold fails on the very next 47-share cycle — do NOT assert it unconditionally.

## Do NOT
- Do **not** read Alpaca's `/positions` `unrealized_pl` / `avg_entry_price` into any ledger
  field. Audited 2026-08-15: its `avg_entry` = **$146.7133 → $146.7134** across the 16-share
  sale (**did not move**) and matches **no** convention (avg $145.93 / LIFO $147.65 / FIFO
  $144.20) — it carries history from the arm/disarm churn + the v2 51-share flatten. Mark the
  position at broker `qty × current_price`; compute cost/realized/unrealized from our own lots.
- Do **not** collapse to specific-lot alone "for simplicity" — that reinstates the exact
  artifact this instrument exists to expose.

## Placement
`accounting_reconcile.py` (new, read-only): pulls `/v2/account/activities?activity_types=FILL`,
rebuilds the average-cost ledger over v3 fills, reads `state.realized_pnl` for speclot, writes
`accounting_ledger.jsonl` + a one-line reporter summary. No import-time side effects on the
engine. Fold the three fields into the main ledger row only at the next reviewed engine change.
