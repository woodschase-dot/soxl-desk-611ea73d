#!/bin/bash
# Red/green anti-vacuity driver (reviewer gap 3).
# Runs test_reconcile.py against each build that PREDATES a fix, plus the reviewed
# and armed builds, and prints a per-case PASS/FAIL matrix. A case that PASSES on
# a build lacking its fix is not exercising its branch -> the test is vacuous.
#
# Expected: each case FAILS on the build predating ITS fix, PASSES on a388aef/CURRENT.
set -u
DIR="/Users/claw/.openclaw/workspace/trading/strategy_lab/soxl-block"
PUB="$DIR/publish"
IDS="D1 D1b R1N1 R1F R1C N2 D3 PART D2 D2b REQ INV"
REFS="a7065c3 3b4f376 fefd212 a388aef CURRENT"
label() { case "$1" in
  a7065c3) echo "pre-D1D4";; 3b4f376) echo "pre-R1";; fefd212) echo "pre-N1";;
  a388aef) echo "reviewed";; CURRENT) echo "armed";; esac; }

WORK="$(mktemp -d)"
run_one() {
  local ref="$1" tmp; tmp="$(mktemp -d)"
  if [ "$ref" = "CURRENT" ]; then cp "$DIR/faithful_control.py" "$tmp/faithful_control.py"
  else git -C "$PUB" show "${ref}:faithful_control.py" > "$tmp/faithful_control.py" 2>/dev/null; fi
  cp "$DIR/test_reconcile.py" "$tmp/test_reconcile.py"
  ( cd "$tmp" && python3 test_reconcile.py 2>/dev/null | grep '^RESULT ' ) > "$WORK/$ref.out"
  rm -rf "$tmp"
}
for ref in $REFS; do run_one "$ref"; done

printf '\n%-8s' "case"
for ref in $REFS; do printf '%-10s' "$(label "$ref")"; done
printf '\n'; printf -- '-%.0s' $(seq 1 62); printf '\n'
for id in $IDS; do
  printf '%-8s' "$id"
  for ref in $REFS; do
    v="$(awk -v i="$id" '$2==i{print $3}' "$WORK/$ref.out")"
    printf '%-10s' "${v:-ERR}"
  done
  printf '\n'
done
rm -rf "$WORK"
