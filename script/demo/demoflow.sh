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
M_DEPLOYED=$(get ".get('deploy', {}).get('m_perBlock')")
[ "$M_DEPLOYED" = "697615423" ] || { echo "deployments file is not the 2.2%/yr DEMO deploy (m_perBlock=$M_DEPLOYED)"; exit 1; }
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
# the last logged row must have landed (status 1, a transaction hash), else stop
expect_ok() { python3 -c "import json,sys;r=json.loads(open('$OUT').read().splitlines()[-1]);sys.exit(0 if r['status']==1 and r['tx'] else 1)" || { echo "step $1 did not land; see $OUT"; exit 1; }; }
align() { python3 -c "t=int('$1');print(t//$SPACING*$SPACING)"; }
# drive the reference pool to `tick` (an exact-input swap stopped at the target price) and move the hook pool's spot
drive_ref() {
  local target=$1 cur sq dir amt
  cur=$(ref_spot); sq=$(sqrt_at "$target")
  [ "$cur" = "$target" ] && return 0
  if [ "$target" -lt "$cur" ]; then dir=true; amt=200000000000; else dir=false; amt=50000000000000000000; fi
  log "ref->$target" "$(send "$DRIVER" 'swap(bool,uint256,uint160)' "$dir" "$amt" "$sq")" >/dev/null
  expect_ok "ref->$target"
}
move_spot() { log "${2:-spot}->$1" "$(send "$BUNDLER" 'moveSpot(uint160)' "$(sqrt_at "$1")")"; expect_ok "${2:-spot}->$1"; }

echo "# DemoFlow $(date -u +%FT%TZ)" >&2
T0=$(ref_spot)
echo "reference tick at start: $T0" >&2

# 0. fund the bundler for moveSpot (its own balance pays the hook-pool swaps)
log fund0 "$(send "$USDC" 'transfer(address,uint256)' "$BUNDLER" 200000000000)"; expect_ok fund0
log fund1 "$(send "$WETH" 'transfer(address,uint256)' "$BUNDLER" 50000000000000000000)"; expect_ok fund1

# 1. the price path through both pools
for o in "${PATH_OFFSETS[@]}"; do
  t=$((T0 + o))
  drive_ref "$t"
  move_spot "$t" "path"
  sleep "$STEP_SEC"
done
END=$((T0 + ${PATH_OFFSETS[${#PATH_OFFSETS[@]}-1]}))

# 2. wait until the reference (the live TWAP) is within 30 ticks of spot, at most 8 minutes
caught=0
for _ in $(seq 1 48); do
  lr=$(live_ref); d=$((lr - END)); d=${d#-}
  echo "spot $END liveRef $lr" >&2
  if [ "$d" -le 30 ]; then caught=1; break; fi
  sleep 10
done
[ "$caught" = 1 ] || { echo "the reference did not catch up with spot in 8 minutes"; exit 1; }
log refresh "$(send "$BUNDLER" 'refreshRef()')"; expect_ok refresh

# 3. A1: re-place a W-wide range around spot (contains spot and the reference): lands, charged
c=$(align "$END"); lo=$((c - W / 2)); hi=$((c + W / 2))
log A1_setPositions "$(send "$BUNDLER" 'setPositions(int24,int24,(bool,bool,uint128),uint160,uint256)' "$lo" "$hi" '(false,false,0)' 0 "$(cast max-uint)")"; expect_ok A1
sleep "$WAIT_SEC"
log A1_settle "$(send "$BUNDLER" 'settle()')"; expect_ok A1_settle

# 4. A2: push spot 90 ticks above the reference, then try to re-place: the fence (60 ticks) refuses it
lr=$(live_ref)
trap 'move_spot "$lr" "back-on-exit" >/dev/null || true' EXIT
move_spot $((lr + 90)) "push" >/dev/null
c2=$(align $((lr + 90))); lo2=$((c2 - W / 2)); hi2=$((c2 + W / 2))
reason=$(cast call "$BUNDLER" 'setPositions(int24,int24,(bool,bool,uint128),uint160,uint256)' "$lo2" "$hi2" '(false,false,0)' 0 "$(cast max-uint)" --from "$(cast wallet address --private-key "$DEPLOYER_PK")" --rpc-url "$RPC" 2>&1 || true)
echo "A2 eth_call: $reason" >&2
case "$reason" in *"$FENCE_SEL"*|*Fence*) echo "A2 eth_call reverts Fence ($FENCE_SEL)" >&2 ;; *) echo "A2: expected Fence, got: $reason" >&2; exit 1 ;; esac
a2=$(cast send --json --gas-limit 3000000 --rpc-url "$RPC" --private-key "$DEPLOYER_PK" "$BUNDLER" 'setPositions(int24,int24,(bool,bool,uint128),uint160,uint256)' "$lo2" "$hi2" '(false,false,0)' 0 "$(cast max-uint)" 2>/dev/null) || true
[ -n "$a2" ] || a2='{}'
log A2_setPositions_Fence "$a2"
python3 -c "import json,sys;r=json.loads(open('$OUT').read().splitlines()[-1]);sys.exit(0 if r['status']==0 and r['tx'] else 1)" || { echo "A2: no failed transaction on chain"; exit 1; }
a2tx=$(python3 -c "import json;print(json.loads(open('$OUT').read().splitlines()[-1])['tx'])")
a2run=$(cast run "$a2tx" --rpc-url "$RPC" 2>&1 | tail -20 || true)
case "$a2run" in *Fence*|*"$FENCE_SEL"*) echo "A2 on chain reverted Fence: $a2tx" >&2 ;; *) echo "A2 on chain: Fence not confirmed by cast run (eth_call pre-check said Fence); tx $a2tx" >&2 ;; esac
move_spot "$lr" "back" >/dev/null
trap - EXIT
sleep "$WAIT_SEC"

# 5. A3: a small rebalanceSwap (100 mUSDC for mWETH, or 0.03 mWETH the other way if the vault holds too little
#    idle token0; eth_call first): lands, charged
ME=$(cast wallet address --private-key "$DEPLOYER_PK")
A3_DIR=true
cast call "$BUNDLER" 'rebalanceSwap(bool,uint128,uint256)' true 100000000 "$(cast max-uint)" --from "$ME" --rpc-url "$RPC" >/dev/null 2>&1 || {
  A3_DIR=false; A3_AMT=30000000000000000
  cast call "$BUNDLER" 'rebalanceSwap(bool,uint128,uint256)' false "$A3_AMT" "$(cast max-uint)" --from "$ME" --rpc-url "$RPC" >/dev/null || { echo "A3: rebalanceSwap reverts in both directions"; exit 1; }; }
if [ "$A3_DIR" = true ]; then A3_AMT=100000000; fi
log A3_rebalanceSwap "$(send "$BUNDLER" 'rebalanceSwap(bool,uint128,uint256)' "$A3_DIR" "$A3_AMT" "$(cast max-uint)")"; expect_ok A3
sleep "$WAIT_SEC"
log A3_settle "$(send "$BUNDLER" 'settle()')"; expect_ok A3_settle
echo "DemoFlow done; rows in $OUT" >&2
