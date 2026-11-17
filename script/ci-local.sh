#!/usr/bin/env bash
# Runs the CI steps locally, in the same order as .github/workflows/ci.yml. Any failing step fails the run.
#   script/ci-local.sh            (CLAIMS_PHASE defaults to P0)
set -euo pipefail
cd "$(dirname "$0")/.."
export CLAIMS_PHASE="${CLAIMS_PHASE:-P0}"
FOUNDRY_VERSION="1.5.0"
ANVIL_PORT="${ANVIL_PORT:-8545}"
ANVIL_PK="0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80" # anvil's default account 0
T0=$SECONDS; TS=$SECONDS
# each step prints the time the previous one took and the running total (to attribute the run's wall time)
step() { printf '\n==> %s   [prev %ds, total %ds]\n' "$*" $((SECONDS - TS)) $((SECONDS - T0)); TS=$SECONDS; }

step "toolchain: forge ${FOUNDRY_VERSION}"
forge --version | head -1 | grep -q "forge Version: ${FOUNDRY_VERSION}" \
  || { echo "expected forge ${FOUNDRY_VERSION}, got: $(forge --version | head -1)"; exit 1; }

step "size gate: every src/ runtime <= 22,000 B"
python3 script/size_gate.py

step "size budget record (growth > 256 B over the accepted size needs a note)"
python3 script/size_budget.py --selftest
python3 script/size_budget.py

step "size gate rejects the oversized fixture"
set +e
out=$(FOUNDRY_PROFILE=oversized python3 script/size_gate.py --scope test/fixtures 2>&1); rc=$?
set -e
echo "$out"
[ "$rc" -ne 0 ] && echo "$out" | grep -q "Oversized: runtime .* > 22000 B" \
  || { echo "the size gate did not reject the oversized fixture (exit $rc)"; exit 1; }
echo "oversized fixture rejected (exit $rc)"

step "anvil deploy of every src/ contract"
anvil --port "$ANVIL_PORT" --silent &
ANVIL_PID=$!
trap 'kill $ANVIL_PID 2>/dev/null || true' EXIT
for _ in $(seq 1 50); do cast chain-id --rpc-url "http://127.0.0.1:$ANVIL_PORT" >/dev/null 2>&1 && break; sleep 0.2; done
forge script script/AnvilDeploy.s.sol --rpc-url "http://127.0.0.1:$ANVIL_PORT" --broadcast --private-key "$ANVIL_PK" >/dev/null
python3 script/anvil_check.py --rpc-url "http://127.0.0.1:$ANVIL_PORT"
kill $ANVIL_PID 2>/dev/null || true
trap - EXIT

step "forge test"
forge test

step "invariant campaign 256 x 128 and its non-vacuity (the fixed seed; forge test runs 64 x 64)"
mkdir -p output/nonvacuity
rm -f output/nonvacuity/inv.csv
TS_NV_LOG=output/nonvacuity/inv.csv FOUNDRY_INVARIANT_RUNS=256 FOUNDRY_INVARIANT_DEPTH=128 \
  forge test --mp 'test/invariant/*'
python3 script/nonvacuity.py --selftest
python3 script/nonvacuity.py output/nonvacuity/inv.csv

step "golden vectors are reproducible"
python3 model/reference.py --vectors
git diff --exit-code -- test/vectors
[ -z "$(git status --porcelain -- test/vectors)" ] || { git status --porcelain -- test/vectors; exit 1; }

step "claims coverage (CLAIMS_PHASE=${CLAIMS_PHASE})"
python3 script/claims.py --selftest
python3 script/claims.py

step "static tests"
python3 test/static/test_static.py

step "storage-layout snapshots"
python3 script/storage_layout.py

step "mutation tags"
python3 script/mutate.py --audit

step "no mutation markers"
python3 script/markers.py

printf '\nci-local: all steps passed in %ds\n' $((SECONDS - T0))
