#!/usr/bin/env bash
# DEMO step 0b, part 2 (spec §4.8): reference swaps through the RefDriver for at least 1.5 x (GRID_SEC + REF_WINDOW)
# = 900 s before the vault is deployed, so the reference pool holds a TWAP history. A bounded random walk around the
# start tick; one swap per SPACING seconds (>= (GRID_SEC + REF_WINDOW) / MIN_CARD = 0.6 s).
# Usage: RPC=<unichain sepolia> DRIVER=<RefDriver> POOL=<v3 pool> DURATION=960 script/demo/warmup.sh
# Needs DEPLOYER_PK in the environment (never printed). cast takes it as --private-key, so it is visible to local
# process listings while a swap is sent: run on a single-user machine, testnet keys only.
set -euo pipefail
: "${RPC:?}" "${DRIVER:?}" "${POOL:?}" "${DEPLOYER_PK:?}"
DURATION=${DURATION:-960}
SPACING=${SPACING:-15}
CENTER=${CENTER:-196250}
[ "$(cast chain-id --rpc-url "$RPC")" = "1301" ] || { echo "not Unichain Sepolia"; exit 1; }
end=$(( $(date +%s) + DURATION ))
i=0
while [ "$(date +%s)" -lt "$end" ]; do
  tick=$(cast call "$POOL" 'slot0()(uint160,int24,uint16,uint16,uint16,uint8,bool)' --rpc-url "$RPC" | sed -n 2p | awk '{print $1}')
  r=$(( RANDOM % 100 ))
  if [ "$tick" -gt $((CENTER + 250)) ]; then dir=true; elif [ "$tick" -lt $((CENTER - 250)) ]; then dir=false;
  elif [ $((RANDOM % 2)) -eq 0 ]; then dir=true; else dir=false; fi
  if [ "$dir" = true ]; then amt=$(( (300 + r * 40) * 1000000 )); else amt="$(( 100 + r * 13 ))000000000000000"; fi
  cast send "$DRIVER" 'swap(bool,uint256,uint160)' "$dir" "$amt" 0 --rpc-url "$RPC" --private-key "$DEPLOYER_PK" >/dev/null
  i=$((i + 1))
  echo "$(date +%T) swap $i zeroForOne=$dir tick_before=$tick"
  sleep "$SPACING"
done
echo "warm-up done: $i swaps"
