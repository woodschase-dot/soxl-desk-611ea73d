#!/usr/bin/env python3
"""
Deterministic reconcile tests for faithful_control.py, run against a fake in-memory
broker + temp state so the REAL run() logic executes without a live account.

SCOPE (recorded verbatim, per reviewer): this harness proves the reconcile LOGIC is
correct. It does NOT prove real-Alpaca behavior on that path. The v2 204/empty-body
DELETE bug is the standing proof that gap is real — it was invisible to everything
except a live DELETE. Real-API confirmation of D1/D2 comes organically on the first
natural fill, verified broker-direct.

Cases (each engineered so it CANNOT pass without exercising the branch it names):
  D1   held block whose sell already rests (crash: placed, unsaved) -> adopt, 1 sell
  R1/N1 resting sell, no block -> synthetic buy=None, buy ladder unaffected
  N2   guard trip WITH inventory -> exposure/drawdown computed non-zero (arithmetic)
  D2   cancel fails because order FILLED mid-cycle -> promote to held, sell next cycle
  D2b  cancel fails, order STILL OPEN -> block untouched, retried, nothing double-placed
"""
import json, os, tempfile
import faithful_control as fc

class FakeBroker:
    def __init__(self):
        self.orders={}; self.pos=0; self.n=0
        self.fill_on_cancel=set()      # oid "fills" the instant cancel is attempted (the D2 race)
        self.cancel_fails_open=set()   # cancel fails transiently; order stays open (D2b)
    def account(self): return {"equity":"100000","cash":"100000"}
    def price(self): return 151.0
    def position(self):
        return ({"symbol":"SOXL","qty":str(self.pos),"market_value":str(self.pos*151.0),
                 "unrealized_pl":"0","avg_entry_price":"151","current_price":"151"} if self.pos else None)
    def open_orders(self): return [dict(o) for o in self.orders.values() if o["status"] in ("new","accepted")]
    def get(self,oid): return self.orders.get(oid)
    def place(self,side,qty,limit):
        self.n+=1; oid=f"o{self.n}"
        self.orders[oid]={"id":oid,"symbol":"SOXL","side":side,"qty":str(qty),
                          "limit_price":f"{limit:.2f}","type":"limit","time_in_force":"gtc","status":"new"}
        return oid
    def cancel(self,oid):
        o=self.orders.get(oid)
        if not o: return False
        if oid in self.cancel_fails_open: return False                 # transient fail, order still 'new'
        if oid in self.fill_on_cancel:                                 # filled just before the DELETE
            o["status"]="filled"; o["filled_avg_price"]=o["limit_price"]; self.pos+=int(float(o["qty"]))
            return False
        if o["status"] in ("new","accepted"): o["status"]="canceled"; return True
        return False

B=FakeBroker()
fc.LIVE=True
fc.get_account=lambda:B.account(); fc.last_price=lambda:B.price(); fc.get_position=lambda:B.position()
fc.list_open_orders=lambda:B.open_orders(); fc.get_order=lambda oid:B.get(oid)
fc.place_limit=lambda side,qty,limit:B.place(side,qty,limit); fc.cancel=lambda oid:B.cancel(oid)
fc.STATE=tempfile.mktemp(suffix=".json"); fc.LEDGER=tempfile.mktemp(suffix=".jsonl")
def sells(): return [o for o in B.open_orders() if o["side"]=="sell"]
def reset(): B.orders={}; B.pos=0; B.fill_on_cancel=set(); B.cancel_fails_open=set()
def put(st): json.dump(st, open(fc.STATE,"w"))

print("=== D1: held block, its sell already resting (crash: placed, unsaved) -> ADOPT, no double ===")
reset(); rest=B.place("sell",16,154.66); B.pos=16
put({"anchor":151.0,"last_px":151.0,"realized_pnl":0.0,"equity_peak":None,
     "blocks":{"151.00":{"level":0,"buy":151.0,"target":154.66,"shares":16,"status":"held",
                         "buy_id":None,"sell_id":None,"buy_fill":151.0}}})
fc.run()
b=[x for x in json.load(open(fc.STATE))["blocks"].values() if x.get("target")==154.66][0]
print(f"  sells={len(sells())} (1)  status={b['status']}  sell_id_adopted={b['sell_id']==rest}")
print("  D1", "PASS" if len(sells())==1 and b["status"]=="pending_sell" and b["sell_id"]==rest else "FAIL")

print("\n=== R1/N1: resting sell, NO matching block -> synthetic buy=None, buys unaffected ===")
reset(); os.path.exists(fc.STATE) and os.remove(fc.STATE); B.pos=16; B.place("sell",16,154.66)
fc.run()
s=json.load(open(fc.STATE)); syn=[x for x in s["blocks"].values() if x.get("synthetic")]
print(f"  sells={len(sells())} (1)  synthetic={len(syn)}  buy_None={syn and syn[0]['buy'] is None}  buys_placed={len([o for o in B.open_orders() if o['side']=='buy'])}")
print("  R1/N1", "PASS" if len(sells())==1 and len(syn)==1 and syn[0]["buy"] is None else "FAIL")

print("\n=== N2: guard trip WITH inventory -> exposure/drawdown NON-zero (validate arithmetic) ===")
reset(); B.pos=16
put({"anchor":151.0,"last_px":250.0,"realized_pnl":0.0,"equity_peak":110000.0,"blocks":{}})
open(fc.LEDGER,"w").close(); fc.run()
row=json.loads(open(fc.LEDGER).read().strip().splitlines()[-1])
exp=2416/102416*100; dd=(102416/110000-1)*100
print(f"  exposure_pct={row['exposure_pct']:.4f} (={exp:.4f})  drawdown={row['current_drawdown_pct']:.4f} (={dd:.4f})")
print("  N2", "PASS" if row.get("price_guard")=="tripped" and abs(row['exposure_pct']-exp)<0.01 and abs(row['current_drawdown_pct']-dd)<0.01 else "FAIL")

print("\n=== D2: out-of-window buy, cancel FAILS because it filled mid-cycle -> promote to held (not deleted), sell next cycle ===")
reset()
oid=B.place("buy",16,130.00); B.fill_on_cancel.add(oid)     # step-1 get_order sees 'new'; step-3 cancel discovers the fill
put({"anchor":151.0,"last_px":151.0,"realized_pnl":0.0,"equity_peak":None,
     "blocks":{"130.00":{"level":-9,"buy":130.0,"target":133.12,"shares":16,"status":"pending_buy",
                         "buy_id":oid,"sell_id":None,"buy_fill":None}}})
fc.run()                                                    # cycle 1: cancel fails->filled->promote
b=json.load(open(fc.STATE))["blocks"].get("130.00")
promoted = b is not None and b["status"]=="held" and b["buy_fill"]==130.0
fc.run()                                                    # cycle 2: held -> place its sell
sell_at_target = any(round(float(o["limit_price"]),2)==133.12 for o in sells())
print(f"  after c1: block_kept={b is not None} status={b and b['status']} buy_fill={b and b['buy_fill']}  |  after c2: sell@133.12={sell_at_target}")
print("  D2", "PASS" if promoted and sell_at_target else "FAIL")

print("\n=== D2b: out-of-window buy, cancel FAILS but order STILL OPEN -> block untouched, retried, no double ===")
reset()
oid=B.place("buy",16,130.00); B.cancel_fails_open.add(oid)
put({"anchor":151.0,"last_px":151.0,"realized_pnl":0.0,"equity_peak":None,
     "blocks":{"130.00":{"level":-9,"buy":130.0,"target":133.12,"shares":16,"status":"pending_buy",
                         "buy_id":oid,"sell_id":None,"buy_fill":None}}})
fc.run()
b=json.load(open(fc.STATE))["blocks"].get("130.00")
buys_at_130=[o for o in B.open_orders() if o["side"]=="buy" and round(float(o["limit_price"]),2)==130.00]
print(f"  block_kept={b is not None} status={b and b['status']}  sells={len(sells())} (0)  orders@130={len(buys_at_130)} (1, no double)")
print("  D2b", "PASS" if b is not None and b["status"]=="pending_buy" and len(sells())==0 and len(buys_at_130)==1 else "FAIL")
