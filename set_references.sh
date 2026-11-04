#!/usr/bin/env bash
# Anchors the reference of each robot in AttestationManagerV2.
# Each admin key approves in turn; the contract applies a reference once its
# threshold is reached. Reads the references from ref-measurements/robotN.json.
set -euo pipefail

N_ROBOTS="${1:?usage: ./set_references.sh <n_robots> <admin_key_file>...}"
shift
[[ $# -ge 1 ]] || { echo "❌ at least one admin key file is required" >&2; exit 1; }

cd "$(dirname "${BASH_SOURCE[0]}")"

for key_file in "$@"; do
  docker run --rm \
    --network quorum-dev-quickstart \
    -v "$(pwd)/smart-contracts/scripts":/smart-contracts/scripts \
    -v "$(pwd)/smart-contracts/deployments":/smart-contracts/deployments \
    -v "$(pwd)/smart-contracts/contracts":/smart-contracts/contracts \
    -v "$(pwd)/smart-contracts/artifacts":/smart-contracts/artifacts \
    -v "$(pwd)/smart-contracts/hardhat.config.js":/smart-contracts/hardhat.config.js \
    -v "$(pwd)/ref-measurements":/ref-measurements:ro \
    -e PRIVATE_KEY="$(<"$key_file")" \
    -e RPC_URL="http://validator1:8545" \
    -e CHAIN_ID=1337 \
    -e N_ROBOTS="$N_ROBOTS" \
    -e REF_DIR=/ref-measurements \
    hardhat:latest \
    bash -lc "npx hardhat run scripts/set_references.js --network besu"
done
