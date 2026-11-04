#!/usr/bin/env bash
set -euo pipefail

# Initialize
private_key=""
rpc_url=""
chain_id=""
contract_name=""

# Default path if private_key not passed
DEFAULT_KEY_PATH="blockchain/quorum-test-network/config/nodes/validator1/accountPrivateKey"

usage() {
  echo "Usage: $0 [--private_key <hexkey>] --rpc_url <url> --chain_id <id> [--contract <name>]"
  echo "       If --private_key is not given, will try to read from:"
  echo "       $DEFAULT_KEY_PATH"
  echo "       If --contract is not given, deploy_contract.js default is used (AttestationManagerRR)."
  exit 1
}

# Parse args
while [[ $# -gt 0 ]]; do
  case "$1" in
    --private_key) private_key="${2:-}";    shift 2 ;;
    --rpc_url)     rpc_url="${2:-}";        shift 2 ;;
    --chain_id)    chain_id="${2:-}";       shift 2 ;;
    --contract)    contract_name="${2:-}";  shift 2 ;;
    -h|--help)     usage ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

# If private_key not provided, try default file
if [[ -z "$private_key" ]]; then
  if [[ -f "$DEFAULT_KEY_PATH" ]]; then
    private_key=$(<"$DEFAULT_KEY_PATH")
    echo "ℹ️  Loaded private key from $DEFAULT_KEY_PATH"
  else
    echo "❌ Error: no --private_key passed and default key file not found: $DEFAULT_KEY_PATH"
    exit 1
  fi
fi

# Validate required args
if [[ -z "$rpc_url" || -z "$chain_id" ]]; then
  echo "❌ Error: --rpc_url and --chain_id are required."
  usage
fi

echo "🚀 Deploying smart contract"
echo "Private Key: [HIDDEN]"
echo "RPC URL    : $rpc_url"
echo "Chain ID   : $chain_id"
[[ -n "$contract_name" ]] && echo "Contract   : $contract_name"

# Rewrite the RPC URL to talk directly to validator1 over the Besu docker
# network, so we don't need the host-side port to be reachable from the
# hardhat container. host port mappings can stay bound to 127.0.0.1 only.
# Maps:
#   http://localhost:21001  -> http://validator1:8545
#   http://localhost:21002  -> http://validator2:8545  ...
besu_net="quorum-dev-quickstart"
rpc_url_internal="$rpc_url"
for i in 1 2 3 4; do
  rpc_url_internal="${rpc_url_internal//localhost:2100$i/validator$i:8545}"
  rpc_url_internal="${rpc_url_internal//127.0.0.1:2100$i/validator$i:8545}"
done

# Build optional env flags
contract_env_flag=""
[[ -n "$contract_name" ]] && contract_env_flag="-e CONTRACT_NAME=$contract_name"

# Run in Docker — attached to the Besu network so we can resolve validatorN.
docker run --rm --name hardhat \
  --network "$besu_net" \
  -v "$(pwd)/smart-contracts/scripts":/smart-contracts/scripts \
  -v "$(pwd)/smart-contracts/deployments":/smart-contracts/deployments \
  -v "$(pwd)/smart-contracts/contracts":/smart-contracts/contracts \
  -v "$(pwd)/smart-contracts/test":/smart-contracts/test \
  -v "$(pwd)/smart-contracts/artifacts":/smart-contracts/artifacts \
  -v "$(pwd)/smart-contracts/hardhat.config.js":/smart-contracts/hardhat.config.js \
  -e PRIVATE_KEY="$private_key" \
  -e RPC_URL="$rpc_url_internal" \
  -e CHAIN_ID="$chain_id" \
  -e ADMINS -e ADMIN_THRESHOLD \
  ${contract_env_flag} \
  hardhat:latest \
  bash -lc "npx hardhat run scripts/deploy_contract.js --network besu"
