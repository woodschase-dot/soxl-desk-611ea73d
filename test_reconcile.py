#!/usr/bin/env python3
"""
Deterministic reconcile tests — D1 (held block + resting sell -> adopt, no double-place)
and R1/N1 (resting sell, no block -> synthetic buy=None, buys unaffected).
Runs the REAL run() logic against a fake in-memory broker + temp state — no live account,
repeatable. (Per reviewer: don't surgery the armed system; isolate the test.)
"""
import json, os, tempfile
import faithful_control as fc

class FakeBroker:
    def __init__(self): self.orders={}; self.pos=0; self.n=0
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
        if o and o["status"] in ("new","accepted"): o["status"]="canceled"; return True
        return False

B=FakeBroker()
fc.LIVE=True
fc.get_account=lambda:B.account(); fc.last_price=lambda:B.price(); fc.get_position=lambda:B.position()
fc.list_open_orders=lambda:B.open_orders(); fc.get_order=lambda oid:B.get(oid)
fc.place_limit=lambda side,qty,limit:B.place(side,qty,limit); fc.cancel=lambda oid:B.cancel(oid)
fc.STATE=tempfile.mktemp(suffix=".json"); fc.LEDGER=tempfile.mktemp(suffix=".jsonl")
sells=lambda:[o for o in B.open_orders() if o["side"]=="sell"]

print("=== D1: held block whose sell already rests at broker (crash: placed, not saved) ===")
rest=B.place("sell",16,154.66); B.pos=16               # a real resting sell + we hold the shares
json.dump({"anchor":151.0,"last_px":151.0,"realized_pnl":0.0,"equity_peak":None,
  "blocks":{"151.00":{"level":0,"buy":151.0,"target":154.66,"shares":16,"status":"held",
    "buy_id":None,"sell_id":None,"buy_fill":151.0}}}, open(fc.STATE,"w"))
fc.run()
blk=[b for b in json.load(open(fc.STATE))["blocks"].values() if b.get("target")==154.66][0]
print(f"  sells at broker: {len(sells())} (expect 1)  |  block status: {blk['status']}  sell_id adopted: {blk['sell_id']==rest}")
print("  D1", "PASS" if len(sells())==1 and blk["status"]=="pending_sell" and blk["sell_id"]==rest else "FAIL")

print("\n=== R1/N1: resting sell, NO matching block (state lost) -> synthetic buy=None ===")
os.remove(fc.STATE); B.orders={}; B.pos=16; rest2=B.place("sell",16,154.66)
fc.run()
s=json.load(open(fc.STATE)); syn=[b for b in s["blocks"].values() if b.get("synthetic")]
buys_placed=[o for o in B.open_orders() if o["side"]=="buy"]
print(f"  sells: {len(sells())} (expect 1)  |  synthetic blocks: {len(syn)}  buy=None: {syn and syn[0]['buy'] is None}  |  buy ladder placed: {len(buys_placed)} (unaffected)")
print("  R1/N1", "PASS" if len(sells())==1 and len(syn)==1 and syn[0]["buy"] is None else "FAIL")

print("\n=== N2: guard trip WITH inventory -> exposure/drawdown computed NON-zero (validates the arithmetic) ===")
B.orders={}; B.pos=16                                    # 16 sh @ 151 = $2,416 mkt value; cash 100,000
json.dump({"anchor":151.0,"last_px":250.0,"realized_pnl":0.0,"equity_peak":110000.0,"blocks":{}}, open(fc.STATE,"w"))
open(fc.LEDGER,"w").close()
fc.run()                                                 # px 151 vs last_px 250 -> >35% -> trip
row=json.loads(open(fc.LEDGER).read().strip().splitlines()[-1])
exp=2416/102416*100; dd=(102416/110000-1)*100           # hand-computed expected values
ok=(row.get("price_guard")=="tripped" and abs(row["exposure_pct"]-exp)<0.01 and abs(row["current_drawdown_pct"]-dd)<0.01)
print(f"  guard row: exposure_pct={row['exposure_pct']:.4f} (expect {exp:.4f})  current_drawdown_pct={row['current_drawdown_pct']:.4f} (expect {dd:.4f})")
print("  N2", "PASS" if ok else "FAIL")
