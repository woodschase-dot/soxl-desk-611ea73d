# SOP — Faithful Decisive-Investor SOXL Control (Alpaca Paper)

**System:** `faithful_control.py` v3 · **Venue:** Alpaca **paper** only · **Status:** ARMED (live paper) 2026-08-13
**Purpose:** Forward-paper-trade the Decisive Investor patent (US 8,589,281 B1) **faithfully**, as the *control arm* of a research experiment, so we can later measure whether risk treatments improve it. This document is for an external reviewer (Claude) to verify the live system against.

> Not financial advice. Paper account only. Real-money execution is never in scope for this file.

---

## 1. Fingerprint (verify these first)
- **File:** `/Users/claw/.openclaw/workspace/trading/strategy_lab/soxl-block/faithful_control.py`
- **sha256:** `039f40b3d2d81fe339048d71a64c0b6c1d6ced4ef85d42012328e5614d36d331`
- **Dependencies:** Python **stdlib only** (`json, os, sys, re, math, urllib`). No `alpaca-py`, no pandas. Broker I/O hand-rolled over `urllib`.
- **Endpoint:** hard-coded `BASE = "https://paper-api.alpaca.markets"` (paper). Data from `data.alpaca.markets`; price fallback FMP.
- **Cron:** `2012b969-2e9a-48ee-a0e2-7049f1e27a07` — "DI faithful SOXL control — hourly (paper)", schedule `30 6-12 * * 1-5` America/Los_Angeles, runtime `codex/gpt-5.5`, command:
  `FAITHFUL_EXECUTION_ENABLED=true python3 .../faithful_control.py`

## 2. Safety architecture (dual gate)
Real orders are placed **only if BOTH** are true:
1. **File gate:** `ARMED = True` (single literal in the config block).
2. **Env gate:** `FAITHFUL_EXECUTION_ENABLED=true` in the process environment (set inside the cron command, not in `env.json`).

`LIVE = (env == "true") and ARMED`. Default (either gate off) = **DRY-RUN**: logs intended orders, places nothing, never writes state. Disarm = flip `ARMED=False` **or** remove the env var; resting orders can then be cancelled on request. Credentials read from `~/.openclaw/config/env.json` (`APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`).

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
| Stop / regime / exposure cap | **NONE** | faithful — exposure can reach 100% as price falls (documented failure mode, kept on purpose to measure) |

## 4. The engine (v3 — how it maintains the ladder)
1. **Fixed anchored lattice.** Rung prices are `anchor × (1+s)^n` for integer `n`. The `anchor` is set **once when flat** and persisted in `faithful_control_state.json`; it does **not** move with live price. (v2 bug: recomputed rungs off the moving price each cycle → prices drifted → dedup failed → order stack ran to 29. v3 fixes this at the root.)
2. **Window maintenance each cycle:** the buy window is the 5 lattice rungs immediately **below** live price. Cancel any resting buy **outside** that window; place any window rung **not** already open. → holds **at most 5** pending buys; the same rung price recurs, so it can never stack.
3. **Cash-available sizing / self-throttle:** blocks sized off `cash − committed_pending`, with guard `cost > (cash − committed)` so total pending can never exceed cash. As inventory deploys, cash falls, block size shrinks.
4. **Lifecycle:** pending_buy → (fill) → held → place rung-pegged sell → pending_sell → (fill) → realize P&L, free.
5. **Broker reconcile (LIVE):** adopts orphan buys found at the broker; warns on unknown sells; never re-places a price already resting.
6. **Crash-safe:** atomic state writes (tmp + `os.replace`), per-op saves, `try/finally`. `_req` treats 204/empty body as success (fixes the DELETE-cancel bug).
7. **Ledger (tail-first):** `faithful_control_ledger.jsonl`. Headline = **marked NAV** (`cash + market_value`, incl. unrealized). Tracks exposure %, current/max drawdown, exposure-at-drawdown (the kill metric), blocks held/pending, realized/unrealized. Win rate is deliberately **not** a headline (it's ~100% and misleading for this strategy).

## 5. Experiment design (control vs treatments)
- **This file = Arm 0 (faithful control).** The paper account runs the pure patent and **nothing else** — no stop, no indicator, no cap ever routes to the account.
- **Treatments T1–T6 run only in sim** (deterministic forward paper-sim, never touches the broker): T1 drawdown breaker, T2 regime filter (200-DMA), T3 per-block stop, T4 exposure cap, T5 combined, T6 cash-available sizing basis. Judged tail-first vs the control.
- **Pre-committed:** a good paper stretch proves nothing; success = a treatment reduces the tail without killing return, vs this control. Real money never auto-armed.

## 6. Operating procedures
- **Arm:** flip `ARMED=False`→`True` (exact-string edit), ensure the cron command carries the env var, enable the cron, run one cycle, verify at the broker. Prereq: account **flat** + no stale orders (fresh anchor).
- **Disarm:** flip `ARMED=False`, remove env var; optionally cancel resting orders.
- **Monitor:** hourly cron maintains the ladder 6:30–12:30 PT and reports only on fills/sells/errors (silent on quiet cycles). Read-only dashboard (below) for state.
- **Flatten/reset:** close position at market, clear orders, delete state file → next armed cycle re-anchors clean.

## 7. Read-only dashboard (separate, cannot trade)
- `dashboard.py` → static HTML, **GET-only** (grep-verified: no POST/DELETE/PUT). Cannot place/cancel orders.
- Published keyless to GitHub Pages (mini generates HTML, verifies no secret is present with a hard-abort, pushes only the finished page). Hourly refresh cron `72d6f310`.
- Live SOXL price + projected-buy ladders (up/down) via a **keyless** public quote feed (CNBC, client-side, CORS); account tiles refresh hourly.
- URL: https://woodschase-dot.github.io/soxl-desk-611ea73d/

## 8. Live state snapshot (2026-08-13, ~08:15 PT)
- Account `PA3OU2TOQR1N`, **paper**: equity **$100,233.02**, cash **$100,233.02**, positions **FLAT**.
- Anchor **$148.68**. Resting GTC buys (re-centered as SOXL rose past the anchor rung):
  `148.68 ×15 · 145.20 ×17 · 141.79 ×17 · 138.47 ×17 · 135.22 ×17` — clean 2.4% lattice, exactly 5, no stacking.
- Realized $0, unrealized $0, no fills yet.

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

---
*Reference implementation of the Decisive Investor block ladder; retained as the honest control arm. Backtest verdict: DISQUALIFIED-as-designed (volatility-harvesting martingale — many small wins hiding an open-ended tail). Aaron's July statement (−57% SOXL: +$23.5k realized / −$77.6k unrealized ≈ −$54k) is the real-money confirmation.*
