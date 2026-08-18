# SOP — Faithful Decisive-Investor SOXL Control (Alpaca Paper)

**System:** `faithful_control.py` v3 · **Venue:** Alpaca **paper** only · **Status:** ARMED (LIVE-PAPER) 2026-08-13 — full review passed (D1–D4, R1–R3, N1–N2); deterministic **launchd** scheduler (no LLM in the execution path); agent-turn cron dead (single fire); read-only reporter wired.
**Purpose:** Forward-paper-trade the Decisive Investor patent (US 8,589,281 B1) **faithfully**, as the *control arm* of a research experiment, so we can later measure whether risk treatments improve it. This document is for an external reviewer (Claude) to verify the live system against.

> Not financial advice. Paper account only. Real-money execution is never in scope for this file.

---

## 1. Fingerprint (verify these first)
- **File:** `trading/strategy_lab/soxl-block/faithful_control.py` (workspace-relative)
- **sha256 — reviewed bytes (DISARMED):** `e588e61349ee9a296750e979d41fcdb30ae18b423d5b0fb1d9eb98d409b1dd9d`
- **sha256 — deployed (ARMED):** `2ab7fd5693643eaf444df0e50b04b3551799a6a30dabe1f0b3d0638c0e585569` (differs from the reviewed bytes **only** by the `ARMED = True` literal + its comment)
- **sha lineage:** `ac3a32d9` (v2 verified-logic) → `5bc04b40` (v2 armed) → `77eb02d4` (v3 disarmed) → `039f40b3` (v3 armed **unreviewed** ~07:10–08:5x PT 2026-08-13; one partial fill; flattened) → `32957e44` (v3+ lock + price guard) → `5051cc85` (D1–D4 review fixes) → `7e88f915` (R1–R3 residuals) → **`e588e613`** (N1: synthetic price can't suppress a real buy; N2: exposure+drawdown on the guard-trip row). DISARMED. Arming flips only the `ARMED` literal.
- **Verify deployed == published (do not trust a hash typed into a doc):**
  `shasum -a 256 <path>/faithful_control.py` **and** `git show <commit>:faithful_control.py | shasum -a 256` — both must equal the value above.
- **The actual bytes:** `faithful_control.py` is published in this repo for independent hashing/review (paper-only, stdlib, no credentials in the file).
- **Dependencies:** Python **stdlib only** (`json, os, sys, math, fcntl, urllib`). No `alpaca-py`, no pandas. Broker I/O hand-rolled over `urllib`.
- **Endpoint:** hard-coded `BASE = "https://paper-api.alpaca.markets"` (paper). Data from `data.alpaca.markets`; price fallback FMP.
- **Schedule:** hourly, `30 6-12 * * 1-5` America/Los_Angeles, command `FAITHFUL_EXECUTION_ENABLED=true python3 …/faithful_control.py`. (Execution-runtime note: see §10 — being moved to a deterministic OS cron.)

## 2. Safety architecture (dual gate)
Real orders are placed **only if BOTH** are true:
1. **File gate:** `ARMED = True` (single literal in the config block).
2. **Env gate:** `FAITHFUL_EXECUTION_ENABLED=true` in the process environment (set inside the cron command, not in `env.json`).

`LIVE = (env == "true") and ARMED`. Default (either gate off) = **DRY-RUN**: logs intended orders, places nothing, never writes state. Disarm = flip `ARMED=False` **or** remove the env var; resting orders can then be cancelled on request. Credentials (the standard Alpaca paper key-id + secret) are read at runtime from `~/.openclaw/config/env.json` and never appear in this file, the code output, or any published artifact.

## 3. Strategy rules (faithful to the patent + Aaron's statement)
| Parameter | Value | Source |
|---|---|---|
| Instrument | SOXL (3× semis ETF) | patent showcase = 3× fund |
| Rung spacing `s` | **0.024** (2.4% geometric) | patent; Aaron statement median ratio 1.02400 (4 dp) |
| Buy window | **5 rungs** below price | patent 3–5 (NOT Aaron's deep 8–10 — that's a tail amplifier we exclude) |
| Sizing | `shares = floor(BLOCK_PCT · basis / rung)`, `BLOCK_PCT=0.025` | ≈ patent growth-rate g=20%; matches Aaron's ~2.5%/block |
| Sizing **basis** | `cash_available` = `cash − committed_pending` | Aaron's block $ shrank ~60% while marked equity fell ~30% → sizes off settled cash |
| Sell target | rung × 1.024, **pegged to the rung** (not the fill) | statement: buy at $121.44 → sell $127.93 = 124.93×1.024 |
| Sell arming | two-step: sell placed **only after** the buy fills | patent Claim-1 mechanic; our reconcile step |
| Order type | GTC limit | statement: all GTC |
| Stop / regime / exposure cap | **No explicit cap** | but see note below — cash-available sizing makes exposure **self-limit** |

> **Exposure correction (reviewer catch, 2026-08-13).** Because each block is 2.5% of *remaining* cash, deployed cash decays as `0.975ⁿ`: ~35 rungs down (≈ a −58% SOXL move) leaves ~41% cash / ~59% deployed — landing on Aaron's observed ~61%. So this arm is **structurally self-limiting**, not the patent's uncapped ~100% tail. It is therefore the **Aaron-faithful replica**, not the pure uncapped patent. Consequence: a hard exposure-cap treatment (T4) tested against this baseline is measured against an already-throttled control. Resolution pending owner decision (§5): keep this as the live baseline and run the **uncapped pure-patent** (equity-basis sizing) as a *sim* reference for clean cap comparisons, **or** switch the live arm to equity-basis sizing and move cash-available to a treatment.

## 4. The engine (v3 — how it maintains the ladder)
1. **Fixed anchored lattice.** Rung prices are `anchor × (1+s)^n` for integer `n`. The `anchor` is set **once when flat** and persisted in `faithful_control_state.json`; it does **not** move with live price. (v2 bug: recomputed rungs off the moving price each cycle → prices drifted → dedup failed → order stack ran to 29. v3 fixes this at the root.)
2. **Window maintenance each cycle:** the buy window is the 5 lattice rungs immediately **below** live price. Cancel any resting buy **outside** that window; place any window rung **not** already open. → holds **at most 5** pending buys; the same rung price recurs, so it can never stack.
3. **Cash-available sizing / self-throttle:** blocks sized off `cash − committed_pending`, with guard `cost > (cash − committed)` so total pending can never exceed cash. As inventory deploys, cash falls, block size shrinks.
4. **Lifecycle:** pending_buy → (fill) → held → place rung-pegged sell → pending_sell → (fill) → realize P&L, free.
5. **Broker reconcile (LIVE):** adopts orphan buys found at the broker; warns on unknown sells; never re-places a price already resting.
6. **Crash-safe:** atomic state writes (tmp + `os.replace`), per-op saves, `try/finally`. `_req` treats 204/empty body as success (fixes the DELETE-cancel bug).
7. **Ledger (tail-first):** `faithful_control_ledger.jsonl`. Headline = **marked NAV** (`cash + market_value`, incl. unrealized). Tracks exposure %, current/max drawdown, exposure-at-drawdown (the kill metric), blocks held/pending, realized/unrealized. Win rate is deliberately **not** a headline (it's ~100% and misleading for this strategy).
8. **Window price source (reviewer Q):** the rung window is computed off `last_price()` → Alpaca `data.alpaca.markets/v2/stocks/SOXL/trades/latest` (IEX last trade on the free feed), with FMP `/stable/quote` as fallback. **Price guard:** a cycle is **skipped entirely** (no place/cancel) if the quote is `None`/`≤0` or jumped **>35%** (`PRICE_GUARD`) vs the last accepted cycle's price — bad-print protection. Note: the *broker* fills resting orders against the real consolidated tape regardless; the feed only decides *where the window sits*, and a slightly-off IEX print at worst shifts the window by pennies (bounded to 5 rungs).
9. **Single-instance lock:** an `fcntl.flock` on `.faithful_control.lock` acquired at entry; a second or overlapping invocation exits immediately. Prevents two schedulers / an overrun cycle from both deciding a rung is missing and double-placing it (the 29-order failure class).

## 5. Roles (corrected per reviewer, 2026-08-13)
The earlier draft called the live account "Arm 0, pure patent" **and** listed cash-available sizing as sim-only "treatment T6" — a contradiction, since the live account *runs* cash-available sizing. Corrected:
- **Live paper account = Aaron-replica (cash-available sizing).** It is **not** the uncapped pure patent; §3 shows cash-available sizing self-limits exposure to ~59%. So "T6" is **not a treatment — it is the live baseline**, and the claim "the paper account runs the pure patent and nothing else" is retracted.
- **The live account's job = fidelity validation, not statistical control.** It is n=1 on one price path, so it can't be a statistical control. Its role is to prove the sim's order mechanics match a real broker's fills. Ground truth = Aaron's statements; the live arm replicates his cash-available behavior so they compare directly. (Reviewer's framing, adopted.)
- **Sim control = uncapped pure patent (equity-basis sizing).** The theoretical ~100% tail belongs in sim — it costs nothing there and paper fills are simulated anyway, so running it live would add no information.
- **Treatments (sim only, never routed to the broker):** T1 drawdown breaker, T2 regime filter (200-DMA), T3 per-block stop, T4 exposure cap, T5 combined. Each judged tail-first against **both** the sim uncapped control and the cash-available baseline (so a hard cap is measured against uncapped *and* soft-throttled references).
- **Pre-committed:** a good paper stretch proves nothing; real money is never auto-armed.

## 6. Operating procedures
- **Arm:** flip `ARMED=False`→`True` (exact-string edit), ensure the cron command carries the env var, enable the cron, run one cycle, verify at the broker. Prereq: account **flat** + no stale orders (fresh anchor).
- **Disarm:** flip `ARMED=False`, remove env var; optionally cancel resting orders.
- **Monitor:** hourly cron maintains the ladder 6:30–12:30 PT and reports only on fills/sells/errors (silent on quiet cycles). Read-only dashboard (below) for state.
- **Flatten/reset:** close position at market, clear orders, delete state file → next armed cycle re-anchors clean.

## 7. Read-only dashboard (separate, cannot trade)
- `dashboard.py` → static HTML, **GET-only** (grep-verified: no POST/DELETE/PUT). Cannot place/cancel orders.
- Published keyless to GitHub Pages (mini generates HTML, verifies no secret is present with a hard-abort, pushes only the finished page). Refreshed hourly.
- Live SOXL price + projected-buy ladders (up/down) via a **keyless** public quote feed (CNBC, client-side, CORS); account tiles refresh hourly.
- URL: https://woodschase-dot.github.io/soxl-desk-611ea73d/

## 8. Live state snapshot (2026-08-13, ~09:5x PT — ARMED)
- Account `PA3···QR1N` (Alpaca **paper**): cash **$100,202.32**, **FLAT**, anchor **151.26**.
- 5 resting GTC buys, **16 sh each**: 147.71 · 144.25 · 140.87 · 137.57 · 134.35 (exact 2.4% lattice below price; no stacking). Flat share counts are correct here — sequential `committed` deduction cancels the rise cheaper rungs would otherwise produce (without it: 16/17/17/18/18).
- Scheduler: **launchd** `ai.soxl.faithfulcontrol` (`:30`, hours 6–12), wrapper `run_faithful.sh` → reporter drains `NEEDS_ATTENTION.log`/`EVENTS.log`. Agent-turn cron `2012b969` **disabled**.
- Verification still owed at live fills: D1 (no double-sell) and D2 (cancel-a-filled → held) fault-injected against a **single rung on the first natural fill**, broker-direct.

## 9. Verification checklist for the reviewer
1. sha256 of the deployed file == `039f40b3…d331`.
2. Endpoint is paper, hard-coded; single file; stdlib only.
3. Dual gate: both `ARMED` and env must be true; default places nothing.
4. Order path: resting **GTC limit** buys + rung-pegged conditional sell placed **only after** fill (two-step). No market-on-tick orders. No stop/regime/cap.
5. Anchored-lattice invariant: at most `WINDOW_RUNGS` (5) pending buys; out-of-window buys cancelled; rung prices stable off the persisted anchor (can't reproduce the 29-order stack).
6. Sizing: `floor(0.025 · (cash − committed) / rung)`, with the `cost ≤ cash − committed` guard (self-throttle).
7. Ledger headline is marked NAV; drawdown off marked-NAV peak.
8. Reconcile adopts orphan buys, never double-places a resting price; state writes atomic.
9. Live broker state matches §8 (independently queryable).

## 10. Execution-runtime migration (reviewer catch, 2026-08-13)
The v3 armed cycle ran via an OpenClaw isolated **agent-turn** (LLM runtime, codex/gpt-5.5) told to run the exact command — a nondeterministic layer between scheduler and an armed trading script. **That agent-turn cron is now DISABLED** (system disarmed). Re-arm sequence (reviewer's order, so two schedulers never run armed at once):
1. Install a deterministic **OS cron** (`crontab`, `30 6-12 * * 1-5`) running `FAITHFUL_EXECUTION_ENABLED=true python3 …/faithful_control.py` directly — plain shell, no LLM.
2. Dry-run it (ARMED=False) and confirm **exactly one** process fires per tick.
3. Flip `ARMED=True`; keep the agent-turn cron disabled (an OpenClaw agent stays only as a *read-only* reporter).
The `fcntl` single-instance lock (§4.9) is the backstop that makes even an accidental double-schedule safe.

## 11. Defects fixed after external code review (2026-08-13)
All four found by adversarial code review; fixed in sha `5051cc85`, dry-run + unit-tested.
- **D1 (high) — sell leg had no crash guard.** A crash after the broker accepted a sell but before `save_state` left the block persisted as `held` → next cycle placed a *second* sell (the v2 buy-bug, un-ported to sells). Fix: reconcile now tracks resting sells and **adopts** orphan sells; before placing, a `held` block whose target already rests as a sell adopts it instead of double-placing.
- **D2 (high) — cancel race orphaned a filled position.** `cancel()` swallowed errors and the block was deleted regardless; an order that filled between the status check and the cancel was dropped → shares held with no block, no sell, no ledger. Fix: `cancel()` returns success/failure; on failure the order is re-checked and **promoted to `held`** if filled (so its sell is placed next cycle), only deleted if truly canceled.
- **D3 (high) — price guard latched forever, silently.** A genuine >35% move tripped the guard, which then compared every later cycle against the stale pre-move price and tripped forever, while the cron stayed silent. Fix: **confirm-on-second** (a suspect move is accepted once the next quote confirms it), and a trip is now a **loud non-zero exit** the reporter surfaces.
- **D4 (low) — `broker_resting` mixed sides.** Resting *sells* (at exact `rung×1.024` lattice points) could suppress *buy* placement at the same price. Fix: buys and sells tracked separately; only buys suppress buy placement.

### Residuals fixed (second review pass)
- **R1 — no fabricated P&L.** A synthesized block for an orphan sell (unknown cost basis) is tagged `synthetic` and **excluded from `realized_pnl`** (logged separately). The ledger — the whole measurement instrument — can't invent profit.
- **R2 — the loud exit must be heard.** A guard trip writes a `price_guard:tripped` ledger row *and* exits non-zero; §12's OS-cron wrapper + read-only reporter surface both (a bare `sys.exit(3)` into a crontab with no MTA is silent otherwise).
- **R3 — record the tail.** The guard-trip path now writes a marked-NAV snapshot flagged `price_guard:tripped` — a >35% move is exactly the event to capture, not the one blank row.
- **Minor — partial sells.** Resting-sell adoption compares quantity and logs a WARNING on partial coverage (full partial-fill handling remains a documented limitation).

## 12. Re-arm sequence (gated on review + owner go)
1. Patched file → new sha; verify **deployed == published** via the two-command check (§1).
2. Install **OS `crontab`** (`30 6-12 * * 1-5`) running a wrapper that captures **exit code + stderr**; on non-zero exit OR a new `price_guard:tripped` ledger row, the read-only OpenClaw reporter posts an alert (R2). Confirm the agent-turn cron is dead and **exactly one** process fires per tick.
3. Dry-run incl. a **forced guard trip** and a **kill-mid-sell-place**, fault-injected against a **single rung** and **verified by querying the broker directly**, not the script's own state file (the script's self-account is the thing under test).
4. `ARMED=True`.

## 13. Reconcile test harness (deterministic)
`test_reconcile.py` runs the real `run()` logic against a fake broker stubbed **at the lowest layer** — `_req(method, url, body)` — so everything above it executes real code: `cancel()`'s try/except contract, `place_limit()`'s `o["id"]` extraction, `list_open_orders()`'s `or []`, `get_position()`'s 404→None, and the 204/empty-body handling. The only fake is the HTTP round-trip, which makes the v2 204/empty-body DELETE class **reachable inside the harness** rather than stubbed past.

Two fake layers, on purpose: **most cases stub `_req`** (the HTTP round-trip) so the reconcile logic above it runs real; the **REQ case stubs `urlopen` one layer lower** so `_req`'s *own* empty-body/204 parse runs — that is the v2 `json.loads('')` site, which an `_req`-level fake would skip.

**12 cases, each engineered so it cannot pass without exercising the branch it names:**
- **D1** — step-0 adopt: `held` block whose sell already rests (crash: placed, unsaved) → adopt, no double.
- **D1b** — step-1 guard: `held` block whose `sell_id` is already known → adopt via `tgt in broker_sells`, no 2nd sell.
- **R1N1** — resting sell, no block → synthetic `buy=None`, real buy ladder (5 rungs) unaffected.
- **R1F** — synthetic sell **FILLS** → `realized_pnl` stays `0.00` (the actual R1 fix: no fabricated P&L at fill time).
- **R1C** — control: a real block's sell fills → `realized_pnl` books the **true** amount (16×(133.12−130.00)=49.92).
- **N2** — guard trip **with** inventory → `exposure_pct`/`current_drawdown_pct` validated against independently-computed constants (2.3590 / −6.8945), not the code's own output.
- **D3** — **two cycles, no pre-seeded `guard_px`**: cycle 1 must *write* `guard_px` on trip; cycle 2 reads it to **RELEASE** (no latch), ladder resumes. A broken trip-write can't pass because nothing supplies the value.
- **PART** — resting sell covers fewer shares than the block → partial-coverage **WARNING** + adopt.
- **D2** — cancel fails because order **FILLED** mid-cycle → promote to `held` (branch log asserted), sell next cycle.
- **D2b** — cancel fails, order **STILL OPEN** → block untouched (`shares`/`buy_fill` unchanged), retried, nothing double-placed.
- **REQ** — `urlopen`-stubbed: `_req`'s own `204`/empty-body → `None` (not a `json.loads('')` crash), real body → parsed. The v2 DELETE bug's exact site.
- **INV** — **multi-cycle random walk (300 cycles, with fills)**: asserts pending buys ≤ `WINDOW_RUNGS` on **every** cycle. This is the invariant whose breach *was* the v2 29-order runaway; single-cycle branch tests can't see it.

**Anti-vacuity red/green (`redgreen.sh`):** each case is run against the builds predating its fix.

| case | pre-D1D4 `a7065c3` | pre-R1 `3b4f376` | pre-N1 `fefd212` | reviewed `a388aef` | armed `2ab7fd56` |
|---|---|---|---|---|---|
| D1 / D1b / D3 / D2 / D2b | FAIL | PASS | PASS | PASS | PASS |
| R1F / PART | FAIL | FAIL | PASS | PASS | PASS |
| R1N1 / N2 | FAIL | FAIL | FAIL | PASS | PASS |
| R1C · REQ · INV | PASS | PASS | PASS | PASS | PASS |

D1..D2b go **red on exactly the build before their own fix** → no case passes without its branch present. **R1C/REQ/INV are green on all captured builds by design, not vacuously:** R1C is a control (real-P&L booking was never broken; it isolates R1F's 0.00 as deliberate suppression); REQ/INV guard fixes that *predate* `a7065c3` (the 204-body parse and the anchored-lattice dedup), so no captured build is red on them. Their non-vacuity is proven by **sabotage** instead: strip `_req`'s 204 guard → REQ **FAILs** (`json.loads('')` crash); disable `open_prices` dedup → INV **FAILs at cycle 1 with 8 open buys** — the exact v2 stacking.

> **Scope (verbatim):** the mock proves the reconcile **logic** is correct; it does **not** prove real-Alpaca behavior on that path. The v2 204/empty-body DELETE bug is the standing proof that gap is real — invisible to everything except a live DELETE. Real-API confirmation of D1/D2 comes **organically on the first natural fill**, verified broker-direct.

---
*Reference implementation of the Decisive Investor block ladder; retained as the honest control arm. Backtest verdict: DISQUALIFIED-as-designed (volatility-harvesting martingale — many small wins hiding an open-ended tail). Aaron's July statement (−57% SOXL: +$23.5k realized / −$77.6k unrealized ≈ −$54k) is the real-money confirmation.*

Reviewed-baseline hash (deployed must differ by exactly the ARMED literal): `da497a2332de773c167adb80a43af4aa402f6261d4d43f9174ded41b8c01975b` (2026-08-18, suite 19/19).
