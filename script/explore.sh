#!/usr/bin/env bash
# Fuzz exploration: 20,000 runs per fuzz test under a fresh random seed (printed, so a failure can be replayed with
# `forge test --fuzz-seed <seed> --mt <test>`). `forge test` itself runs with the fixed seed in foundry.toml.
set -euo pipefail
cd "$(dirname "$0")/.."
seed="0x$(od -An -N8 -tx1 /dev/urandom | tr -d ' \n')"
echo "explore: fuzz seed ${seed}"
FOUNDRY_PROFILE=explore forge test --fuzz-seed "${seed}" --mt "${1:-testFuzz}"
