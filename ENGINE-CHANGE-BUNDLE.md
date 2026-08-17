# Engine Change Bundle — reviewed disarm→fix→suite→re-arm (NOT YET APPLIED)

**Status:** proposed. Armed engine `faithful_control.py` untouched until the ceremony.
**Author:** Sam · **Scoped by:** Chase (#5016, #5024, #5027) · **Date:** 2026-08-17
**Ceremony:** disarm → apply → `test_reconcile.py` green (+ new cases below) → both-hash check
→ re-arm. Deployed only on Chase's go.

---

## D5 — staleness guard (feed-freeze), HIGH
**Defect:** the price guard catches *jumps*, not *staleness*. A frozen feed produces zero jump
and sails through (the weekend ran 10 cycles on a stuck $144.80). Mid-session, a frozen feed
leaves the ladder positioned against a price that no longer exists, then un-sticks as one large
apparent jump that trips the guard for two cycles at exactly the wrong moment.
**Fix (Chase's, from the payload, not repetition-counting):** `trades/latest` returns the trade
timestamp `t`. Reject any quote whose `t` is older than a threshold (**5–10 min**, config), and
skip the cycle (place nothing) with a `NEEDS_ATTENTION` alert. Timestamp beats repetition-count:
repetition has a false-positive mode on genuinely quiet ticks; `t` does not.
**New test:** `STALE` — feed a quote with `t` older than threshold → cycle places nothing, alerts,
does not advance the ladder. And a fresh-but-identical price → NOT rejected (no false positive).

## D6 — save_state failure root-cause, HIGH
**Defect:** the 08-15 13:30Z cycle placed the 128.12 buy and did not persist it → orphan → adopted
14:30Z. The adoption is D2/reconcile working live (good), but the *drop* is a real persistence bug.
**Root-cause targets:** an exception between `place_limit` and `save_state`, or a code path that
places after the save point. Fix = single atomic save at end-of-cycle covering all placements, or
save-after-each-placement; and assert post-cycle that every broker order maps to a state block.
**New test:** `PERSIST` — inject a placement that raises before the legacy save point → next cycle
must adopt exactly once and not double-place (already partly covered by D1; make the *drop* explicit).

## D7 — order-age contamination on late-armed sells, MEDIUM (new, from #5027)
**Defect:** a buy that fills *after* the last mark has its sell armed "next cycle." Across a
non-trading gap (weekend/overnight), the sell isn't resting when the gap happens — the engine
places it marketable at the open and books improvement the resting strategy never earned. Live
proof: the 144.25 block's sell, submitted 13:30:08Z into a $150.81 market, filled $151.79 and
booked **+$120.96 vs the ~$55.68** a resting fill would have (~$65 of late-fill inflation).
**This contaminates realized as a measurement** — exactly the §2 order-age failure, now on the
live arm's sell side.
**Options (Chase's call, do not silently pick):**
- (a) **Tag, don't change** *(min, recommended first):* flag any trip whose sell was
  *marketable-on-submission* (limit ≤ market at submit time). Decompose realized into
  `realized_resting` vs `realized_latefill` so the measurement can separate strategy harvest from
  placement luck. Zero behavior change; pure instrumentation.
- (b) **Place GTC sell immediately on buy fill** (Aaron-faithful: his GTC sells rest from the fill,
  which is *why* his gaps are real resting-order captures). Trades away the two-step arm's crash
  safety (D1's whole point) — so only with a crash-safe immediate-place path.
**New test:** `LATEARM` — buy fills post-mark; next cycle the market has gapped above target; assert
the trip is tagged `latefill` and its improvement lands in `realized_latefill`, not `realized_resting`.

---

## Also fold in (already spec'd, land in the same reviewed pass)
- **Ledger accounting** (`LEDGER-ACCOUNTING-SPEC.md`): dual-convention realized + `illusion_delta`
  + **per-trip** `harvest_phase` (log all trips individually — one session can carry both signs from
  lot dispersion, #5027). Drop Alpaca's `unrealized_pl`/`avg_entry` (audited stale); mark at
  `qty × current_price`; compute cost/realized/unrealized from our own lots.

## Not in this bundle (unchanged, deliberately)
- GAPFILL sim model is **sim-only** and gated on validation against **Aaron's real Fidelity fills**,
  not live paper improvement (paper fills are Alpaca's engine, not the market). Bundle does not touch it.
- `WINDOW_RUNGS` stays 5. Deployed engine must still differ from reviewed `e588e613` by only the
  `ARMED` line + these reviewed diffs — any extra line means unreviewed code armed.
