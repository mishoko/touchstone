#!/usr/bin/env bash
# DemoFlow (spec §5.3) on Unichain Sepolia, after DeployDemo: drives the mock reference pool and the hook pool along a
# real price path, then runs three curator actions and records each transaction:
#   A1  a re-placement while the reference lags spot          -> lands, charged (Action event)
#   A2  a re-placement with spot pushed 90 ticks off the ref  -> reverts Fence (sent with a fixed gas limit, so the
#                                                                failed transaction is on chain)
#   A3  a small rebalanceSwap                                  -> lands, charged
# Each action is followed by `settle` once its markout window (H_MARKOUT = 20 s) has closed.
#
# Price path: the tick moves of the mainnet USDC/WETH 0.05% pool (0x88e6...5640) at nine hourly samples, blocks
# 26,387,320 to 26,389,720 (slot0 ticks 200031, 200071, 200070, 200074, 200030, 200032, 200010, 199992, 199974),
# applied as offsets to the demo pool's current tick and compressed to STEP_SEC seconds per hour.
# Real time is needed between steps (COOLDOWN 30 blocks, the markout window, the reference TWAP catching up), which is
# why this is a shell driver of single transactions and not a forge script (a script simulates every call in one block).
#
# Usage: RPC=<unichain sepolia> OUT=output/demo/demoflow.jsonl script/demo/demoflow.sh
# Needs DEPLOYER_PK (the bundler's owner, never printed; visible to local process listings while cast runs: run on a
# single-user machine, testnet keys only). Addresses come from script/demo/deployments-1301.json.
set -euo pipefail
: "${RPC:?}" "${DEPLOYER_PK:?}"
OUT=${OUT:-output/demo/demoflow.jsonl}
STEP_SEC=${STEP_SEC:-30}
WAIT_SEC=${WAIT_SEC:-40}
DEPLOY=script/demo/deployments-1301.json
[ "$(cast chain-id --rpc-url "$RPC")" = "1301" ] || { echo "not Unichain Sepolia"; exit 1; }
mkdir -p "$(dirname "$OUT")"

get() { python3 -c "import json,sys;d=json.load(open('$DEPLOY'));print(d$1)"; }
POOL=$(get "['step0b']['v3_reference_pool_500']")
DRIVER=$(get "['step0b']['ref_driver']")
USDC=$(get "['step0b']['mUSDC_token0']")
WETH=$(get "['step0b']['mWETH_token1']")
BUNDLER=$(get "['deploy']['curator_bundler']")
HOOK=$(get "['deploy']['hook']")
PATH_OFFSETS=(0 40 39 43 -1 1 -21 -39 -57)
W=2000
SPACING=10
FENCE_SEL=$(cast sig 'Fence()')

sqrt_at() { python3 -c "from decimal import Decimal as D,getcontext;getcontext().prec=80;print(int((D('1.0001')**int('$1')).sqrt()*(D(2)**96)))"; }
ref_spot() { cast call "$POOL" 'slot0()(uint160,int24,uint16,uint16,uint16,uint8,bool)' --rpc-url "$RPC" | sed -n 2p | awk '{print $1}'; }
live_ref() { cast call "$HOOK" 'liveRef()(int24)' --rpc-url "$RPC" | awk '{print $1}'; }
send() { cast send --json --rpc-url "$RPC" --private-key "$DEPLOYER_PK" "$@"; }
log() { # label, tx json
  python3 - "$1" "$2" "$OUT" <<'EOF'
import json, sys, time
label, raw, out = sys.argv[1], sys.argv[2], sys.argv[3]
r = json.loads(raw) if raw.strip().startswith("{") else {}
row = {"step": label, "tx": r.get("transactionHash"), "block": int(r.get("blockNumber", "0x0"), 16),
       "status": int(r.get("status", "0x0"), 16), "gasUsed": int(r.get("gasUsed", "0x0"), 16), "time": int(time.time())}
open(out, "a").write(json.dumps(row) + "\n")
print(json.dumps(row))
EOF
}
align() { python3 -c "t=int('$1');print(t//$SPACING*$SPACING)"; }
# drive the reference pool to `tick` (an exact-input swap stopped at the target price) and move the hook pool's spot
drive_ref() {
  local target=$1 cur sq dir amt
  cur=$(ref_spot); sq=$(sqrt_at "$target")
  [ "$cur" = "$target" ] && return 0
  if [ "$target" -lt "$cur" ]; then dir=true; amt=200000000000; else dir=false; amt=50000000000000000000; fi
  log "ref->$target" "$(send "$DRIVER" 'swap(bool,uint256,uint160)' "$dir" "$amt" "$sq")" >/dev/null
}
move_spot() { log "${2:-spot}->$1" "$(send "$BUNDLER" 'moveSpot(uint160)' "$(sqrt_at "$1")")"; }

echo "# DemoFlow $(date -u +%FT%TZ)" >&2
T0=$(ref_spot)
echo "reference tick at start: $T0" >&2

# 0. fund the bundler for moveSpot (its own balance pays the hook-pool swaps)
log fund0 "$(send "$USDC" 'transfer(address,uint256)' "$BUNDLER" 200000000000)"
log fund1 "$(send "$WETH" 'transfer(address,uint256)' "$BUNDLER" 50000000000000000000)"

# 1. the price path through both pools
for o in "${PATH_OFFSETS[@]}"; do
  t=$((T0 + o))
  drive_ref "$t"
  move_spot "$t" "path"
  sleep "$STEP_SEC"
done
END=$((T0 + ${PATH_OFFSETS[${#PATH_OFFSETS[@]}-1]}))

# 2. wait until the cached reference is within 30 ticks of spot (the TWAP catches up), at most 8 minutes
for _ in $(seq 1 48); do
  lr=$(live_ref); d=$((lr - END)); d=${d#-}
  echo "spot $END liveRef $lr" >&2
  [ "$d" -le 30 ] && break
  sleep 10
done
log refresh "$(send "$BUNDLER" 'refreshRef()')"

# 3. A1: re-place a W-wide range around spot (contains spot and the reference): lands, charged
c=$(align "$END"); lo=$((c - W / 2)); hi=$((c + W / 2))
log A1_setPositions "$(send "$BUNDLER" 'setPositions(int24,int24,(bool,bool,uint128),uint160,uint256)' "$lo" "$hi" '(false,false,0)' 0 "$(cast max-uint)")"
sleep "$WAIT_SEC"
log A1_settle "$(send "$BUNDLER" 'settle()')"

# 4. A2: push spot 90 ticks above the reference, then try to re-place: the fence (60 ticks) refuses it
lr=$(live_ref)
move_spot $((lr + 90)) "push" >/dev/null
c2=$(align $((lr + 90))); lo2=$((c2 - W / 2)); hi2=$((c2 + W / 2))
reason=$(cast call "$BUNDLER" 'setPositions(int24,int24,(bool,bool,uint128),uint160,uint256)' "$lo2" "$hi2" '(false,false,0)' 0 "$(cast max-uint)" --from "$(cast wallet address --private-key "$DEPLOYER_PK")" --rpc-url "$RPC" 2>&1 || true)
echo "A2 eth_call: $reason" >&2
case "$reason" in *"$FENCE_SEL"*|*Fence*) echo "A2 eth_call reverts Fence ($FENCE_SEL)" >&2 ;; *) echo "A2: expected Fence, got: $reason" >&2; exit 1 ;; esac
a2=$(cast send --json --gas-limit 3000000 --rpc-url "$RPC" --private-key "$DEPLOYER_PK" "$BUNDLER" 'setPositions(int24,int24,(bool,bool,uint128),uint160,uint256)' "$lo2" "$hi2" '(false,false,0)' 0 "$(cast max-uint)" 2>/dev/null) || true
[ -n "$a2" ] || a2='{}'
log A2_setPositions_Fence "$a2"
move_spot "$lr" "back" >/dev/null
sleep "$WAIT_SEC"

# 5. A3: a small rebalanceSwap (100 mUSDC for mWETH): lands, charged
log A3_rebalanceSwap "$(send "$BUNDLER" 'rebalanceSwap(bool,uint128,uint256)' true 100000000 "$(cast max-uint)")"
sleep "$WAIT_SEC"
log A3_settle "$(send "$BUNDLER" 'settle()')"
echo "DemoFlow done; rows in $OUT" >&2
