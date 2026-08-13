#!/usr/bin/env python3
"""
FAITHFUL DECISIVE-INVESTOR CONTROL ARM (v3)  —  SOXL, Alpaca PAPER only.

WHAT THIS IS
------------
A faithful implementation of US Patent 8,589,281 B1 + the real-statement
observations in aaron-sop-evaluation-2026-08-12.md, run as the *control* arm of
a paper experiment. Still: no stop, no regime filter — those are treatment
variants tested SEPARATELY in sim. This arm records what the pure strategy does
with real fills.

v3 CHANGES (2026-08-12, after the v2 runaway to 29 orders)
----------------------------------------------------------
1. FIXED ANCHORED LATTICE (the real fix for the runaway). Rungs live on a fixed
   geometric lattice  price_n = ANCHOR * (1+s)^n  for integer n. The lattice is
   set once (when flat) and does NOT move with live price. Each cycle the buy
   window is the WINDOW_RUNGS lattice points immediately BELOW current price;
   any open buy outside that window is cancelled, any window rung not yet open
   is placed. Because lattice prices are STABLE, the same rung recurs cycle to
   cycle, dedup-by-price works, and the ladder can never stack past the window.
   (v2 recomputed rungs off the moving price every cycle -> prices drifted ->
   dedup never matched -> 5 new orders every cycle -> 29-order runaway.)
2. CASH-AVAILABLE SIZING (matches Aaron's statement; self-throttling exposure).
   shares = floor(BLOCK_PCT * basis / rung), basis = (cash - committed_pending)
   when EQUITY_BASIS='cash_available'. As inventory deploys, cash falls, blocks
   shrink, and the grid brakes itself. BLOCK_PCT=0.025 ~= the patent's g=20%
   growth-rate sizing (0.20/(365/1.1)/0.024 = 2.51%/block), so this is the
   patent's own sizing lever, just based on deployable capital.
3. Sells are RUNG-PEGGED: target = rung*(1+s), to the penny (statement-confirmed).
4. Kept from v2: crash-safe try/finally + atomic state writes + per-op saves,
   broker reconcile (adopt orphan buys), and the _req empty/204 fix.

SAFETY (dual gate)
------------------
  * PAPER ONLY. Hard-coded to https://paper-api.alpaca.markets.
  * Places NOTHING unless BOTH  env FAITHFUL_EXECUTION_ENABLED='true'  AND
    ARMED=True (below). Default = DRY-RUN: logs intended orders, sends nothing.

Run:  python3 faithful_control.py                                   # DRY-RUN
      FAITHFUL_EXECUTION_ENABLED=true python3 faithful_control.py   # + ARMED below
"""
from __future__ import annotations
import json, os, sys, math, fcntl, urllib.request, urllib.parse, datetime as dt

# ----------------------------- CONFIG (faithful) ----------------------------- #
SYMBOL         = "SOXL"
SPACING        = 0.024     # 2.4% geometric rungs (statement: 1.02400 to 4 dp)
WINDOW_RUNGS   = 5         # pending buy rungs below price (patent 3-5; NOT Aaron's deep 8-10)
BLOCK_PCT      = 0.025     # per-block size = 2.5% of the sizing basis (~= patent g=20%)
EQUITY_BASIS   = "cash_available"   # cash_available | marked_nav
PRICE_GUARD    = 0.35      # reject a quote that jumped >35% vs the last accepted cycle (bad-print guard)
ARMED          = False     # <-- FILE GATE. DISARMED 2026-08-13 to harden (single-instance lock, price guard, OS-cron migration) per reviewer.
                           #     Second gate: env FAITHFUL_EXECUTION_ENABLED=true must also be set.

BASE   = "https://paper-api.alpaca.markets"
DATA   = "https://data.alpaca.markets"
STATE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faithful_control_state.json")
LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "faithful_control_ledger.jsonl")
LOCKFILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".faithful_control.lock")

LIVE = (os.environ.get("FAITHFUL_EXECUTION_ENABLED", "false").lower() == "true") and ARMED

def acquire_single_instance_lock():
    """Refuse to run if another instance already holds the lock (prevents two
    schedulers/overlapping cycles from both maintaining the ladder = duplicate orders).
    The returned handle must stay open for the process lifetime to hold the lock."""
    fh = open(LOCKFILE, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print("another faithful_control instance is running; exiting (single-instance lock)")
        sys.exit(0)
    return fh

# ----------------------------- Alpaca REST (stdlib) -------------------------- #
def _env():
    e = json.load(open(os.path.expanduser("~/.openclaw/config/env.json")))
    return e["APCA_API_KEY_ID"], e["APCA_API_SECRET_KEY"]
_KEY, _SEC = _env()
_HDR = {"APCA-API-KEY-ID": _KEY, "APCA-API-SECRET-KEY": _SEC, "Content-Type": "application/json"}

def _req(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(url, data=data, headers=_HDR, method=method)
    with urllib.request.urlopen(r, timeout=30) as resp:
        raw = resp.read().decode().strip()
        # 204 / empty body (e.g. a successful DELETE cancel) is success, not an error
        if getattr(resp, "status", 200) == 204 or not raw:
            return None
        return json.loads(raw)

def get_account():   return _req("GET", f"{BASE}/v2/account")
def get_position():
    try: return _req("GET", f"{BASE}/v2/positions/{SYMBOL}")
    except Exception: return None
def last_price():
    try: return float(_req("GET", f"{DATA}/v2/stocks/{SYMBOL}/trades/latest")["trade"]["p"])
    except Exception:
        e = json.load(open(os.path.expanduser("~/.openclaw/config/env.json")))
        q = _req("GET", f"https://financialmodelingprep.com/stable/quote?symbol={SYMBOL}&apikey={e['FMP_API_KEY']}")
        return float(q[0]["price"])
def get_order(oid):  return _req("GET", f"{BASE}/v2/orders/{oid}")

def list_open_orders():
    """OUR open orders for SYMBOL at the broker (LIVE reconcile)."""
    q = urllib.parse.urlencode({"status": "open", "symbols": SYMBOL, "limit": 500})
    return _req("GET", f"{BASE}/v2/orders?{q}") or []

def place_limit(side, qty, limit):
    body = {"symbol": SYMBOL, "qty": str(qty), "side": side, "type": "limit",
            "limit_price": f"{limit:.2f}", "time_in_force": "gtc"}
    if LIVE:
        o = _req("POST", f"{BASE}/v2/orders", body)
        return o["id"]
    log(f"  [DRY] would place {side.upper()} {qty} @ {limit:.2f} GTC")
    return f"DRYRUN-{side}-{limit:.2f}"

def cancel(oid):
    if LIVE:
        try: _req("DELETE", f"{BASE}/v2/orders/{oid}")   # 204/empty -> None -> success
        except Exception as ex: log(f"  cancel FAILED {oid}: {ex}")
    else:
        log(f"  [DRY] would cancel order {oid}")

# ----------------------------- state / logging ------------------------------ #
def now(): return dt.datetime.now(dt.timezone.utc).isoformat()
def log(m): print(m)
def load_state():
    if os.path.exists(STATE): return json.load(open(STATE))
    return {"anchor": None, "blocks": {}, "realized_pnl": 0.0, "equity_peak": None}
def save_state(s):
    if LIVE:                                             # atomic write; DRY never persists
        tmp = STATE + ".tmp"
        json.dump(s, open(tmp, "w"), indent=2)
        os.replace(tmp, STATE)

# ----------------------------- lattice helpers ------------------------------ #
def lattice_price(anchor, n):
    return round(anchor * (1 + SPACING) ** n, 2)

def window_rungs(anchor, px):
    """The WINDOW_RUNGS fixed-lattice buy rungs immediately BELOW px (level, price)."""
    n_at = math.floor(math.log(px / anchor) / math.log(1 + SPACING))
    # ensure strictly below px
    while lattice_price(anchor, n_at) >= px:
        n_at -= 1
    return [(n_at - j, lattice_price(anchor, n_at - j)) for j in range(WINDOW_RUNGS)]

def block_shares(basis, rung):
    return max(int((BLOCK_PCT * basis) / rung), 0)

# ----------------------------- main cycle ----------------------------------- #
def run():
    mode = "LIVE-PAPER" if LIVE else "DRY-RUN"
    acct = get_account(); equity = float(acct["equity"]); cash = float(acct["cash"])
    px = last_price()
    st = load_state()
    blocks = st["blocks"]        # key -> {level, buy, target, shares, status, buy_id, sell_id, buy_fill}

    # ---- PRICE SANITY GUARD (reviewer): never place/cancel off a bad or implausible quote ----
    last_px = st.get("last_px")
    if px is None or px <= 0 or (last_px and abs(px / last_px - 1) > PRICE_GUARD):
        log(f"=== FAITHFUL SOXL CONTROL v3 [{mode}] {now()} === ⚠️ PRICE GUARD: "
            f"px={px} vs last_accepted={last_px} (>{PRICE_GUARD:.0%} jump or invalid) — "
            f"NO orders placed/cancelled this cycle.")
        return {"ts": now(), "mode": mode, "price": px, "price_guard": "tripped", "last_px": last_px}
    st["last_px"] = px
    log(f"=== FAITHFUL SOXL CONTROL v3 [{mode}] {now()} === price={px:.2f} equity={equity:,.2f} cash={cash:,.2f}")

    try:
        # 0) BROKER RECONCILE (LIVE): adopt orphan buys, exclude any resting price ----
        broker_resting = set()
        if LIVE:
            known = ({b.get("buy_id") for b in blocks.values()} |
                     {b.get("sell_id") for b in blocks.values()})
            for o in list_open_orders():
                if o.get("symbol") != SYMBOL or not o.get("limit_price"): continue
                lp = round(float(o["limit_price"]), 2); broker_resting.add(lp)
                if o["id"] in known: continue
                if o["side"] == "buy":
                    blocks[f"orphan-{o['id'][:8]}"] = {"level": None, "buy": lp,
                        "target": round(lp * (1 + SPACING), 2), "shares": int(float(o["qty"])),
                        "status": "pending_buy", "buy_id": o["id"], "sell_id": None, "buy_fill": None}
                    log(f"  [RECONCILE] adopted orphan BUY {o['qty']} @ {lp:.2f}")
                else:
                    log(f"  [RECONCILE] WARNING unknown open SELL {o['qty']} @ {lp:.2f} (id {o['id']})")

        # 1) reconcile block lifecycle (fills -> sells -> realized) --------------------
        for key, b in list(blocks.items()):
            if b["status"] == "pending_buy" and LIVE:
                o = get_order(b["buy_id"])
                if o and o.get("status") == "filled":
                    b["buy_fill"] = float(o["filled_avg_price"]); b["status"] = "held"
                elif o and o.get("status") in ("canceled", "expired", "rejected"):
                    del blocks[key]; continue
            if b["status"] == "held":                    # arm the rung-pegged conditional sell
                b["sell_id"] = place_limit("sell", b["shares"], b["target"]); b["status"] = "pending_sell"
                save_state(st)
            if b["status"] == "pending_sell" and LIVE:
                o = get_order(b["sell_id"])
                if o and o.get("status") == "filled":
                    st["realized_pnl"] += b["shares"] * (float(o["filled_avg_price"]) - (b["buy_fill"] or b["buy"]))
                    del blocks[key]

        # 2) anchor the fixed lattice (set once; re-anchor only when fully flat) -------
        pos_qty = 0
        if LIVE:
            p = get_position(); pos_qty = abs(int(float(p["qty"]))) if p else 0
        flat = (pos_qty == 0 and not any(b["status"] in ("held", "pending_sell") for b in blocks.values()))
        if st["anchor"] is None or (flat and not blocks):
            st["anchor"] = px
        anchor = st["anchor"]

        # 3) maintain exactly the window: cancel out-of-window buys, place missing -----
        target_rungs = window_rungs(anchor, px)          # [(level, price), ...]
        target_prices = {pr for _, pr in target_rungs}
        for key, b in list(blocks.items()):              # cancel pending buys outside the window
            if b["status"] == "pending_buy" and round(b["buy"], 2) not in target_prices:
                cancel(b["buy_id"]); del blocks[key]; save_state(st)

        open_prices = {round(b["buy"], 2) for b in blocks.values()} | broker_resting
        committed = sum(b["shares"] * b["buy"] for b in blocks.values() if b["status"] == "pending_buy")
        for level, rung in target_rungs:
            if rung in open_prices: continue
            basis = (cash - committed) if EQUITY_BASIS == "cash_available" else equity
            shares = block_shares(basis, rung)
            cost = shares * rung
            if shares <= 0 or cost > (cash - committed): continue
            bid = place_limit("buy", shares, rung)
            blocks[f"{rung:.2f}"] = {"level": level, "buy": rung, "target": round(rung * (1 + SPACING), 2),
                                     "shares": shares, "status": "pending_buy", "buy_id": bid,
                                     "sell_id": None, "buy_fill": None}
            committed += cost; open_prices.add(rung); save_state(st)
    finally:
        save_state(st)

    # 4) LEDGER SNAPSHOT (tail-first; marked NAV is the headline number) --------------
    pos = get_position()
    upnl = float(pos["unrealized_pl"]) if pos else 0.0
    mv   = float(pos["market_value"]) if pos else 0.0
    marked_nav = cash + mv
    st["equity_peak"] = max(st.get("equity_peak") or marked_nav, marked_nav)
    ddown = (marked_nav / st["equity_peak"] - 1.0) if st["equity_peak"] else 0.0
    n_held = sum(1 for b in blocks.values() if b["status"] in ("held", "pending_sell"))
    n_pend = sum(1 for b in blocks.values() if b["status"] == "pending_buy")
    snap = {"ts": now(), "mode": mode, "price": px, "marked_nav": marked_nav, "cash": cash,
            "position_mv": mv, "exposure_pct": (mv / marked_nav * 100 if marked_nav else 0),
            "unrealized_pnl": upnl, "realized_pnl_cum": st["realized_pnl"],
            "blocks_held": n_held, "blocks_pending": n_pend, "current_drawdown_pct": ddown * 100,
            "anchor": st["anchor"]}
    log("  ledger: " + json.dumps({k: (round(v, 2) if isinstance(v, float) else v)
                                   for k, v in snap.items() if k != "ts"}))
    if LIVE:
        with open(LEDGER, "a") as f: f.write(json.dumps(snap) + "\n")
        save_state(st)
    log(f"  blocks: {n_held} held/selling, {n_pend} pending buys | exposure {snap['exposure_pct']:.1f}% "
        f"| marked NAV ${marked_nav:,.2f} | realized ${st['realized_pnl']:.2f} | uPnL ${upnl:.2f}")
    return snap

if __name__ == "__main__":
    _lock = acquire_single_instance_lock()   # exits if another instance holds it
    run()
