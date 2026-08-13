#!/usr/bin/env python3
"""
Deterministic reconcile tests for faithful_control.py.

HOW IT'S WIRED (reviewer gap 2): the fake broker is stubbed at the LOWEST layer —
fc._req(method, url, body). Everything above it runs the REAL code: cancel()'s
try/except contract, place_limit()'s o["id"] extraction, list_open_orders()'s
`or []`, get_position()'s 404->None, and the 204/empty-body handling in _req's
callers. The only thing faked is the HTTP round-trip itself. This makes the v2
204/empty-body DELETE class reachable inside the harness instead of stubbed past.

SCOPE (recorded verbatim, per reviewer): this harness proves the reconcile LOGIC
is correct against a broker that behaves as documented. It does NOT prove real-
Alpaca behavior on that path. The v2 204/empty-body DELETE bug is the standing
proof that gap is real — it was invisible to everything except a live DELETE.
Real-API confirmation of D1/D2 comes organically on the first natural fill,
verified broker-direct.

ANTI-VACUITY (reviewer gap 3): every case is also run against the builds that
PREDATE its fix (see redgreen.sh). A case that passes on a build without its fix
is not testing its branch. Regenerate with ./redgreen.sh. Result (2026-08-13):

  case    pre-D1D4  pre-R1   pre-N1   reviewed  armed
  -----   --------  -------  -------  --------  -----
  D1      FAIL      PASS     PASS     PASS      PASS
  D1b     FAIL      PASS     PASS     PASS      PASS
  R1N1    FAIL      FAIL     FAIL     PASS      PASS
  R1F     FAIL      FAIL     PASS     PASS      PASS
  R1C     PASS      PASS     PASS     PASS      PASS   <- CONTROL: books real P&L on
  N2      FAIL      FAIL     FAIL     PASS      PASS      every build (never broken);
  D3      FAIL      PASS     PASS     PASS      PASS      isolates R1F's 0.00 as a
  PART    FAIL      FAIL     PASS     PASS      PASS      deliberate suppression, not
  D2      FAIL      PASS     PASS     PASS      PASS      a dead path.
  D2b     FAIL      PASS     PASS     PASS      PASS
  builds: a7065c3(32957e44) 3b4f376(5051cc85) fefd212(7e88f915) a388aef(e588e613) armed(2ab7fd56)

  Every case except the R1C control goes red on exactly the build predating its
  own fix and green once the fix lands. No case passes without its branch present.

Cases (id -> branch it must exercise):
  D1    step-0 adopt: held block whose sell already rests (crash: placed, unsaved)
  D1b   step-1 guard: held block whose sell_id is already known -> adopt, no 2nd sell
  R1N1  resting sell, no block -> synthetic buy=None, real buy ladder untouched
  R1F   synthetic sell FILLS -> realized_pnl stays 0.0, block removed (the actual R1 fix)
  R1C   real block's sell FILLS -> realized_pnl books the TRUE amount (control for R1F)
  N2    guard trip WITH inventory -> exposure/drawdown computed non-zero (arithmetic)
  D3    guard trip then confirming quote -> RELEASE, no latch, ladder resumes
  PART  resting sell covers fewer shares than the block -> partial-coverage WARNING
  D2    cancel fails because order FILLED mid-cycle -> promote to held, sell next cycle
  D2b   cancel fails, order STILL OPEN -> block untouched, retried, nothing double-placed
"""
import io, json, os, sys, contextlib, tempfile
import faithful_control as fc


class FakeBroker:
    """Behaves like the Alpaca REST endpoints faithful_control actually calls,
    at the _req layer. Order state lives here; the engine drives it."""
    def __init__(self):
        self.orders = {}
        self.pos = 0
        self.n = 0
        self.px = 151.0
        self.cash = 100000.0
        self.equity = 100000.0
        self.fill_on_cancel = set()     # oid fills the instant a DELETE is attempted (the D2 race)
        self.cancel_fails_open = set()   # DELETE fails transiently; order stays open (D2b)

    def seed(self, side, qty, limit, status="new", filled_avg_price=None):
        self.n += 1
        oid = f"o{self.n}"
        o = {"id": oid, "symbol": "SOXL", "side": side, "qty": str(int(qty)),
             "limit_price": f"{float(limit):.2f}", "type": "limit",
             "time_in_force": "gtc", "status": status}
        if filled_avg_price is not None:
            o["filled_avg_price"] = f"{float(filled_avg_price):.2f}"
        self.orders[oid] = o
        return oid

    def _open(self):
        return [dict(o) for o in self.orders.values() if o["status"] in ("new", "accepted")]

    def req(self, method, url, body=None):
        S = fc.SYMBOL
        if method == "GET" and url.endswith(f"/v2/stocks/{S}/trades/latest"):
            return {"trade": {"p": self.px}}
        if method == "GET" and url.endswith("/v2/account"):
            return {"equity": f"{self.equity:.2f}", "cash": f"{self.cash:.2f}"}
        if method == "GET" and url.endswith(f"/v2/positions/{S}"):
            if self.pos == 0:
                raise RuntimeError("404 position does not exist")   # get_position() catches -> None
            return {"symbol": S, "qty": str(self.pos), "market_value": f"{self.pos*self.px:.2f}",
                    "unrealized_pl": "0", "avg_entry_price": f"{self.px:.2f}",
                    "current_price": f"{self.px:.2f}"}
        if method == "GET" and "/v2/orders?" in url:     # list_open_orders (query string)
            return self._open()
        if method == "GET" and "/v2/orders/" in url:     # get_order(oid)
            return self.orders.get(url.rsplit("/", 1)[1])
        if method == "POST" and url.endswith("/v2/orders"):
            self.n += 1
            oid = f"o{self.n}"
            o = {"id": oid, "symbol": body["symbol"], "side": body["side"], "qty": body["qty"],
                 "limit_price": body["limit_price"], "type": "limit",
                 "time_in_force": "gtc", "status": "new"}
            self.orders[oid] = o
            return o                                      # place_limit reads o["id"]
        if method == "DELETE" and "/v2/orders/" in url:
            oid = url.rsplit("/", 1)[1]
            o = self.orders.get(oid)
            if not o:
                raise RuntimeError("404 not found")
            if oid in self.cancel_fails_open:
                raise RuntimeError("503 transient — order still open")     # cancel() -> False, order 'new'
            if oid in self.fill_on_cancel:
                o["status"] = "filled"; o["filled_avg_price"] = o["limit_price"]
                self.pos += int(float(o["qty"]))
                raise RuntimeError("422 order already filled")             # cancel() -> False, then get_order sees filled
            if o["status"] in ("new", "accepted"):
                o["status"] = "canceled"
                return None                                # 204/empty -> success -> cancel() True
            raise RuntimeError("422 cannot cancel")
        raise AssertionError(f"unhandled {method} {url}")


B = None
def fresh():
    """New broker + temp state/ledger, _req pointed at it, LIVE on."""
    global B
    B = FakeBroker()
    fc._req = lambda m, u, b=None: B.req(m, u, b)
    fc.LIVE = True
    fc.STATE = tempfile.mktemp(suffix=".json")
    fc.LEDGER = tempfile.mktemp(suffix=".jsonl")
    return B

def put(st):
    json.dump(st, open(fc.STATE, "w"))

def sells():
    return [o for o in B._open() if o["side"] == "sell"]

def buys():
    return [o for o in B._open() if o["side"] == "buy"]

def run_capture():
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        res = fc.run()
    return res, buf.getvalue()

def blocks_now():
    return json.load(open(fc.STATE))["blocks"]


# ------------------------------- cases ------------------------------------- #
def d1():
    fresh(); B.pos = 16
    rest = B.seed("sell", 16, 154.66)          # sell resting; block never saved its id (crash)
    put({"anchor": 151.0, "last_px": 151.0, "realized_pnl": 0.0, "equity_peak": None,
         "blocks": {"151.00": {"level": 0, "buy": 151.0, "target": 154.66, "shares": 16,
                               "status": "held", "buy_id": None, "sell_id": None, "buy_fill": 151.0}}})
    run_capture()
    b = [x for x in blocks_now().values() if round(x.get("target", 0), 2) == 154.66][0]
    ok = len(sells()) == 1 and b["status"] == "pending_sell" and b["sell_id"] == rest
    return ok, f"sells={len(sells())} status={b['status']} adopted_id={b['sell_id']==rest}"

def d1b():
    fresh(); B.pos = 16
    rest = B.seed("sell", 16, 154.66)          # block already KNOWS this id -> step 0 skips, step-1 guard must catch it
    put({"anchor": 151.0, "last_px": 151.0, "realized_pnl": 0.0, "equity_peak": None,
         "blocks": {"151.00": {"level": 0, "buy": 151.0, "target": 154.66, "shares": 16,
                               "status": "held", "buy_id": None, "sell_id": rest, "buy_fill": 151.0}}})
    run_capture()
    b = [x for x in blocks_now().values() if round(x.get("target", 0), 2) == 154.66][0]
    # without the step-1 `tgt in broker_sells` guard, run() would place a SECOND sell -> 2 sells
    ok = len(sells()) == 1 and b["status"] == "pending_sell" and b["sell_id"] == rest
    return ok, f"sells={len(sells())} (no 2nd) status={b['status']} sell_id_kept={b['sell_id']==rest}"

def r1n1():
    fresh(); B.pos = 16
    B.seed("sell", 16, 154.66)                 # resting sell, NO block (state lost)
    run_capture()                              # fresh temp STATE path doesn't exist -> default state
    syn = [x for x in blocks_now().values() if x.get("synthetic")]
    nb = len(buys())
    ok = len(sells()) == 1 and len(syn) == 1 and syn[0]["buy"] is None and nb == 5
    return ok, f"sells={len(sells())} synthetic={len(syn)} buy_None={bool(syn) and syn[0]['buy'] is None} buys_placed={nb} (5)"

def r1_fill():
    """The ACTUAL R1 fix: a synthetic sell that FILLS must not book fabricated P&L."""
    fresh(); B.pos = 0
    sid = B.seed("sell", 16, 133.12, status="filled", filled_avg_price=133.12)
    # block as a pre-N1 build would have created it: a back-computed buy=130.0 (=133.12/1.024), synthetic
    put({"anchor": 151.0, "last_px": 151.0, "realized_pnl": 0.0, "equity_peak": None,
         "blocks": {"osell-x": {"level": None, "buy": 130.0, "target": 133.12, "shares": 16,
                                "status": "pending_sell", "buy_id": None, "sell_id": sid,
                                "buy_fill": None, "synthetic": True}}})
    run_capture()
    s = json.load(open(fc.STATE))
    syn_left = [x for x in s["blocks"].values() if x.get("synthetic")]
    ok = abs(s["realized_pnl"] - 0.0) < 1e-9 and len(syn_left) == 0
    return ok, f"realized={s['realized_pnl']:.2f} (must be 0.00; pre-R1 books +49.92) synthetic_left={len(syn_left)}"

def r1_control():
    """Control for R1F: a REAL block's sell fill must book the true amount, not zero."""
    fresh(); B.pos = 0
    sid = B.seed("sell", 16, 133.12, status="filled", filled_avg_price=133.12)
    put({"anchor": 151.0, "last_px": 151.0, "realized_pnl": 0.0, "equity_peak": None,
         "blocks": {"130.00": {"level": -9, "buy": 130.0, "target": 133.12, "shares": 16,
                               "status": "pending_sell", "buy_id": None, "sell_id": sid,
                               "buy_fill": 130.0}}})    # NOT synthetic
    run_capture()
    realized = json.load(open(fc.STATE))["realized_pnl"]
    expected = 16 * (133.12 - 130.0)           # 49.92
    ok = abs(realized - expected) < 1e-6
    return ok, f"realized={realized:.2f} (expected {expected:.2f})"

def n2():
    fresh(); B.pos = 16; B.px = 151.0
    put({"anchor": 151.0, "last_px": 250.0, "realized_pnl": 0.0, "equity_peak": 110000.0, "blocks": {}})
    open(fc.LEDGER, "w").close()
    run_capture()
    row = json.loads(open(fc.LEDGER).read().strip().splitlines()[-1])
    exp = 2416 / 102416 * 100                   # mv/mnav
    dd = (102416 / 110000 - 1) * 100            # mnav/peak-1  (peak, NOT mnav)
    ok = (row.get("price_guard") == "tripped"
          and abs(row["exposure_pct"] - exp) < 0.01
          and abs(row["current_drawdown_pct"] - dd) < 0.01)
    return ok, f"exposure={row['exposure_pct']:.4f}(={exp:.4f}) dd={row['current_drawdown_pct']:.4f}(={dd:.4f})"

def d3():
    """Guard trips on a suspect quote, then RELEASES on the confirming second quote (no latch)."""
    fresh(); B.pos = 0; B.px = 91.0
    put({"anchor": 150.0, "last_px": 150.0, "guard_px": 90.0, "realized_pnl": 0.0,
         "equity_peak": None, "blocks": {}})   # 91 vs 150 = 39% jump (trips), but 91 vs guard 90 = 1% (confirms)
    _, text = run_capture()
    s = json.load(open(fc.STATE))
    released = "PRICE GUARD released" in text
    ok = released and s.get("guard_px") is None and abs((s.get("last_px") or 0) - 91.0) < 1e-9 and len(buys()) == 5
    return ok, f"released={released} guard_px_cleared={s.get('guard_px') is None} last_px={s.get('last_px')} buys={len(buys())} (5)"

def partial_qty():
    fresh(); B.pos = 16
    B.seed("sell", 10, 154.66)                  # only 10 sh rest; block wants 16
    put({"anchor": 151.0, "last_px": 151.0, "realized_pnl": 0.0, "equity_peak": None,
         "blocks": {"151.00": {"level": 0, "buy": 151.0, "target": 154.66, "shares": 16,
                               "status": "held", "buy_id": None, "sell_id": None, "buy_fill": 151.0}}})
    _, text = run_capture()
    b = [x for x in blocks_now().values() if round(x.get("target", 0), 2) == 154.66][0]
    warned = "partial" in text.lower()
    ok = warned and len(sells()) == 1 and b["status"] == "pending_sell"
    return ok, f"warned={warned} sells={len(sells())} status={b['status']}"

def d2():
    fresh()
    oid = B.seed("buy", 16, 130.00); B.fill_on_cancel.add(oid)   # 'new' at step 1; fills inside step-3 DELETE
    put({"anchor": 151.0, "last_px": 151.0, "realized_pnl": 0.0, "equity_peak": None,
         "blocks": {"130.00": {"level": -9, "buy": 130.0, "target": 133.12, "shares": 16,
                               "status": "pending_buy", "buy_id": oid, "sell_id": None, "buy_fill": None}}})
    _, t1 = run_capture()                        # cycle 1: out-of-window cancel fails -> filled -> promote
    b = blocks_now().get("130.00")
    promoted = b is not None and b["status"] == "held" and b["buy_fill"] == 130.0
    branchlog = "cancel FAILED but order FILLED" in t1
    run_capture()                                # cycle 2: held -> place its sell
    sell_ok = any(round(float(o["limit_price"]), 2) == 133.12 for o in sells())
    ok = promoted and branchlog and sell_ok
    return ok, f"promoted={promoted} branch_fired={branchlog} sell@133.12={sell_ok}"

def d2b():
    fresh()
    oid = B.seed("buy", 16, 130.00); B.cancel_fails_open.add(oid)
    put({"anchor": 151.0, "last_px": 151.0, "realized_pnl": 0.0, "equity_peak": None,
         "blocks": {"130.00": {"level": -9, "buy": 130.0, "target": 133.12, "shares": 16,
                               "status": "pending_buy", "buy_id": oid, "sell_id": None, "buy_fill": None}}})
    run_capture()
    b = blocks_now().get("130.00")
    at130 = [o for o in buys() if round(float(o["limit_price"]), 2) == 130.00]
    # tightened (gap 4): block must be OTHERWISE unchanged, not merely present
    ok = (b is not None and b["status"] == "pending_buy" and b["shares"] == 16
          and b["buy_fill"] is None and len(sells()) == 0 and len(at130) == 1)
    return ok, (f"status={b and b['status']} shares={b and b['shares']} buy_fill={b and b['buy_fill']} "
                f"orders@130={len(at130)} (1) sells={len(sells())} (0)")


CASES = [
    ("D1",   "step-0 adopt resting sell (crash, unsaved)",          d1),
    ("D1b",  "step-1 guard: sell_id known -> no 2nd sell",          d1b),
    ("R1N1", "synthetic buy=None; real buy ladder intact",          r1n1),
    ("R1F",  "synthetic sell FILLS -> realized stays 0.00",         r1_fill),
    ("R1C",  "real sell FILLS -> books true P&L (control)",         r1_control),
    ("N2",   "guard trip arithmetic WITH inventory",                n2),
    ("D3",   "guard trip -> confirm-on-second RELEASE, no latch",   d3),
    ("PART", "partial-qty sell coverage warns + adopts",            partial_qty),
    ("D2",   "cancel-fails-FILLED -> held, sell next cycle",        d2),
    ("D2b",  "cancel-fails-OPEN -> block untouched, no double",     d2b),
]

if __name__ == "__main__":
    results = []
    for cid, name, fn in CASES:
        try:
            ok, detail = fn()
        except Exception as e:
            ok, detail = False, f"error: {type(e).__name__}: {e}"
        # grep-stable tag for redgreen.sh:  RESULT <id> <PASS|FAIL>
        print(f"RESULT {cid} {'PASS' if ok else 'FAIL'}")
        print(f"  {'PASS ✅' if ok else 'FAIL ❌'}  [{cid}] {name}")
        print(f"         {detail}")
        results.append(ok)
    allok = all(results)
    print("\n" + ("ALL PASS ✅" if allok else "SOME FAILED ❌"))
    sys.exit(0 if allok else 1)
