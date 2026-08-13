# Incident Report — v2 SOXL grid order runaway (2026-08-12)

**Severity:** low (paper account, no capital at risk) · **Status:** resolved (v3 anchored lattice)
**System:** `faithful_control.py` v2 (armed 2026-08-11) · **Account:** Alpaca **paper** only

## Summary
Over 2026-08-11→08-12 the armed v2 control accumulated **~29 resting buy orders** and a **51-share position** instead of the intended rolling **5-rung** ladder. Paper only; net P&L ≈ flat (equity ~$100,001 vs ~$99,939 start; +$67 unrealized on the 51 shares at the time). No real money involved.

## Timeline (PT)
- 08-11 13:25 — v2 armed; 5 GTC buys placed correctly below $133.04.
- 08-12 06:30 — cycle reported **6 pending** (not 5) and **4 cancels FAILED** with `Expecting value: line 1 column 1` (JSON parse error on the DELETE responses).
- Through 08-12 — order count grew toward ~29; a dip filled blocks, leaving a 51-share position.
- 08-12 evening — disarmed (`ARMED=False`), cron disabled, all orders cancelled (finalized at next open), `_req` cancel bug fixed.
- 08-13 07:10 — v3 (anchored lattice + cash-available sizing) armed from a flat slate after review.

## Root cause (two compounding bugs)
1. **Moving-anchor ladder (primary).** v2 recomputed every rung off the *live price each cycle* (`rung = round(px·(1−s)^j, 2)`). As price drifted between cycles, the computed rung prices shifted, so the "is this rung already resting?" dedup (keyed on exact price) never matched the prior cycle's orders. Result: each cycle believed the window was empty and placed a fresh set — orders accumulated instead of being reused.
2. **Cancel path broken (amplifier).** `_req` did `json.load(resp)` on every response; a successful Alpaca DELETE returns **204 / empty body**, which threw `Expecting value: line 1 column 1`. So the cleanup cancels that *should* have trimmed the ladder were reported as failures and the stale orders survived — letting the count climb.

## Impact
- Paper account only; no capital risk. Behaviour was wrong (order count, exposure path) but net ≈ flat.
- No other broker route touched; safety gates and paper-hardcoding held throughout.

## Fixes (all in v3)
- **Anchored fixed lattice** — rung prices are `anchor·(1+s)^n` off a **persisted anchor set once when flat**, not recomputed off live price. The same rung recurs every cycle, dedup works, and maintenance cancels anything outside the 5-rung window → **can hold at most 5 pending buys**. (This is the root-cause fix.)
- **`_req` 204/empty-body fix** — empty/204 responses return `None` (success), so cancels no longer false-fail.
- **Crash-safe state** — atomic tmp+`os.replace`, per-op saves, `try/finally`; broker reconcile adopts orphan buys and never re-places a resting price.

## Follow-ups added after external review (2026-08-13)
- **Single-instance `fcntl` lock** — prevents two schedulers / an overrun cycle from both maintaining the ladder (the same duplicate-order failure class, different trigger).
- **Price sanity guard** — a cycle is skipped if the quote is invalid or jumped >35% vs the last accepted cycle.
- **Deterministic OS-cron migration** — armed execution moves off the LLM (agent-turn) runtime to a plain shell cron; an OpenClaw agent stays only as a read-only reporter.

## Lesson
A grid's identity is its **price lattice**, and that lattice must be **state**, not recomputed from a moving input each tick. Dedup/idempotency keyed on a value that drifts is not idempotency. Pair it with a single-instance guard so concurrency can't reintroduce the same class of bug.
