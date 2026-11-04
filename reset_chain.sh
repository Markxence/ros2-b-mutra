#!/usr/bin/env bash
# Resets the attestation chain of AttestationManagerV2 or V3. Each admin key
# approves in turn; the contract applies the reset once its threshold is
# reached. The other contracts are reset by the SECaaS (POST /reset).
set -euo pipefail

USAGE="usage: ./reset_chain.sh <contract> <admin_key_file>..."
CONTRACT_NAME="${1:?$USAGE}"
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
    -e PRIVATE_KEY="$(<"$key_file")" \
    -e RPC_URL="http://validator1:8545" \
    -e CHAIN_ID=1337 \
    -e CONTRACT_NAME="$CONTRACT_NAME" \
    hardhat:latest \
    bash -lc "npx hardhat run scripts/reset_chain.js --network besu"
done
