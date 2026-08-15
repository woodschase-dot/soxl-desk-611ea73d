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

## Predictions worth asserting
- Under a grid in a **downtrend**, `illusion_delta` should **widen monotonically** (specific-lot
  keeps booking +2.4% while the running average sits ever further above the harvest price). A
  non-monotonic delta in a pure downtrend is a bug or a regime change — flag it.
- `realized_speclot + unrealized_speclot ≡ realized_avgcost + unrealized_avgcost ≡ total NAV −
  start`. The two conventions must reconcile to the **same total**; only the split differs.
  Assert this identity every cycle — it's the cheap check that neither ledger is leaking.

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
