# app/internal/blockchain/client.py

import json
import time
import threading
import warnings

from web3 import Web3, __version__ as web3_lib_version
from web3.providers.rpc import HTTPProvider
from web3.middleware import ExtraDataToPOAMiddleware
from web3._utils.events import event_abi_to_log_topic
from web3.exceptions import ContractLogicError
from app.internal.blockchain.types import (
    AttestationState,
    AttestationManagerContractEvents,
    as_bytes32,
    text_to_bytes32
)
from app.internal.blockchain.formatters import build_attestation_chain_table
from app.internal.logger import info, warn, error, debug

warnings.filterwarnings("ignore", message="There was an issue with the method eth_maxPriorityFeePerGas")


class BlockchainClient:
    def __init__(self, eth_address, private_key, eth_node_url, contract_address, contract_abi=None, abi_path=None):
        # --- Web3.py v7 Connection Setup ---
        # Note: Sync WebSockets were removed in v7. We must use HTTP.
        if eth_node_url.startswith("ws://") or eth_node_url.startswith("wss://"):
            raise ValueError(
                "Web3.py v7 does not support synchronous WebSockets. "
                "Please use an HTTP/HTTPS endpoint (e.g., http://...)"
            )
        
        if eth_node_url.startswith("http://") or eth_node_url.startswith("https://"):
            self.web3 = Web3(HTTPProvider(eth_node_url))
        else:
            raise ValueError("eth_node_url must start with http:// or https://")

        self.web3.middleware_onion.inject(ExtraDataToPOAMiddleware, layer=0)
        
        # Simple connection check
        if not self.web3.is_connected():
            raise ConnectionError(f"Cannot connect to Ethereum node at {eth_node_url}")

        # --- Keys & Address ---
        self.private_key = private_key
        acct = self.web3.eth.account.from_key(self.private_key)
        derived_addr = acct.address  # checksum

        if eth_address:
            provided = Web3.to_checksum_address(eth_address)
            if provided != derived_addr:
                warn(f"Provided ETH_ADDRESS ({provided}) != address from private key ({derived_addr}). Using derived.")

            self.eth_address = derived_addr
        else:
            self.eth_address = derived_addr

        abi = contract_abi

        if not abi:
            if not abi_path:
                raise ValueError("No ABI provided. Set 'contract_abi' in config or provide abi_path.")
            with open(abi_path, "r") as f:
                abi = json.load(f).get("abi")

        if not abi:
            raise ValueError("ABI not found (neither contract_abi nor abi_path produced an ABI).")

        self.contract_address = Web3.to_checksum_address(contract_address)

        self.contract = self.web3.eth.contract(
            address=Web3.to_checksum_address(contract_address), 
            abi=abi
        )

        info("Web3 Client Initialized:")
        info(f"Web3 Lib Version : {web3_lib_version}")
        info(f"Account Address  : {self.eth_address}")
        info(f"Contract Address : {self.contract.address}")
        info(f"Node URL         : {eth_node_url}")
        try:
            info(f"Node Client      : {self.web3.client_version}")
        except Exception:
            info("Node Client      : (Unknown)")

        # Initialize local nonce and lock
        self._nonce_lock = threading.Lock()
        self._local_nonce = self.web3.eth.get_transaction_count(self.eth_address, 'pending')


    def send_signed_transaction(self, build_transaction):
        # Apply gas-price bump once before any retry loop.
        if 'maxFeePerGas' not in build_transaction and 'maxPriorityFeePerGas' not in build_transaction:
            build_transaction['gasPrice'] = int(self.web3.eth.gas_price * 1.25)
        elif 'maxFeePerGas' in build_transaction:
            build_transaction['maxFeePerGas'] = int(build_transaction['maxFeePerGas'] * 1.25)

        for attempt in range(2):
            with self._nonce_lock:
                build_transaction['nonce'] = self._local_nonce
                self._local_nonce += 1

            try:
                signed_txn = self.web3.eth.account.sign_transaction(build_transaction, self.private_key)
                tx_hash = self.web3.eth.send_raw_transaction(signed_txn.raw_transaction)
                return tx_hash.hex()
            except Exception as e:
                err_str = str(e).lower()
                nonce_error = any(k in err_str for k in ("nonce", "replacement transaction", "already known"))
                if attempt == 0 and nonce_error:
                    # Nonce is stale — refresh from the network and retry once.
                    with self._nonce_lock:
                        self._local_nonce = self.web3.eth.get_transaction_count(self.eth_address, 'pending')
                    warn(f"Nonce error on attempt 1, refreshed nonce to {self._local_nonce}, retrying: {e}")
                    continue
                raise


    def _send_tx(self, tx_data: dict, wait: bool = False, timeout: int = 60) -> str:
        """
        Sends a built transaction dict and optionally waits for mining.
        Returns tx_hash (hex str). If wait=True and tx fails, raises.
        """
        tx_hash = self.send_signed_transaction(tx_data)

        if wait:
            debug(f"Waiting for transaction {tx_hash} to be mined...")
            receipt = self.web3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
            if receipt.status != 1:
                raise Exception(
                    f"Transaction {tx_hash} was mined in block {receipt.blockNumber} but failed (status=0)."
                )
            debug(f"Transaction {tx_hash} successfully included in block {receipt.blockNumber}")

        return tx_hash

    def get_event_class(self, event_enum: AttestationManagerContractEvents):
        """
        Returns the web3 ContractEvent class for the given enum (e.g., contract.events.AttestationStarted)
        """
        return getattr(self.contract.events, event_enum.value)

    def get_event_abi(self, event_enum: AttestationManagerContractEvents) -> dict:
        """
        Returns the ABI dict for the event (used by the watcher to decode logs).
        """
        return self.get_event_class(event_enum)._get_event_abi()

    def get_event_topic(self, event_enum: AttestationManagerContractEvents) -> str:
        """
        Returns keccak(signature) topic for the event (topic0) from its ABI.
        """
        abi = self.get_event_abi(event_enum)
        return event_abi_to_log_topic(abi).hex()
    
    def get_transaction_receipt(self, tx_hash: str) -> dict:
        """
        Retrieves details of the transaction receipt for the specified hash, including:
        
        - Block info: Block hash, block number, and timestamp.
        - Gas usage: Gas used and cumulative gas.
        - Status: Transaction success (1) or failure (0).
        - Sender/Receiver: from_address and to_address.
        - Logs: Event logs generated during the transaction.
        - Gas price: Actual gas price paid.
        - Timestamp: The timestamp of the block in which the transaction was included.
        
        Args:
            tx_hash (str): The transaction hash to retrieve the receipt.

        Returns:
            dict: A dictionary containing transaction receipt details and block timestamp, or an error message.
        """
        try:
            # Get the transaction receipt
            receipt = self.web3.eth.get_transaction_receipt(tx_hash)

            if receipt:
                # Convert HexBytes to strings for JSON serialization
                receipt_dict = dict(receipt)
                receipt_dict['blockHash'] = receipt_dict['blockHash'].hex()
                receipt_dict['transactionHash'] = receipt_dict['transactionHash'].hex()
                receipt_dict['logsBloom'] = receipt_dict['logsBloom'].hex()
                receipt_dict['logs'] = [dict(log) for log in receipt_dict['logs']]

                # Rename fields to be more descriptive
                receipt_dict['from_address'] = receipt_dict.pop('from')
                receipt_dict['to_address'] = receipt_dict.pop('to')

                # Convert nested hex values in logs
                for log in receipt_dict['logs']:
                    log['blockHash'] = log['blockHash'].hex()
                    log['transactionHash'] = log['transactionHash'].hex()
                    log['topics'] = [topic.hex() for topic in log['topics']]

                # Retrieve the block number from the receipt
                block_number = receipt['blockNumber']

                # Fetch the block details using the block number
                block = self.web3.eth.get_block(block_number)

                # Add the block timestamp to the receipt dictionary
                receipt_dict['timestamp'] = block['timestamp']

                return receipt_dict

            else:
                raise Exception("Error: Transaction receipt not found")

        except Exception as e:
            raise Exception(f"An exception occurred: {str(e)}")


    def create_event_filter(self, event_name: AttestationManagerContractEvents, last_n_blocks: int = None):
        """
        Creates a filter for the given contract event. Robust against empty chains
        and node warm-up by avoiding getBlock('latest').
        """
        try:
            # Get current chain height safely
            current_height = self.web3.eth.block_number  # returns int, never raises BlockNotFound
            # If node is brand new, current_height can be 0 (genesis)
            if current_height is None or current_height < 0:
                current_height = 0

            if last_n_blocks is not None and last_n_blocks > 0:
                from_block = max(0, current_height - last_n_blocks)
            else:
                # Start at 0 so we won't miss very early events if the app races the node
                from_block = 0

            # Build the event class
            event_cls = getattr(self.contract.events, event_name.value, None)
            if event_cls is None:
                raise ValueError(f"Event '{event_name}' does not exist in the contract ABI.")

            # IMPORTANT: pass integers, not hex strings, for web3.py v5
            # Also add toBlock='latest' for clarity
            event_filter = event_cls.create_filter(fromBlock=from_block, toBlock='latest')
            return event_filter

        except ValueError as ve:
            # wrong event name / ABI mismatch
            raise ve
        except Exception as e:
            # As a last resort, start from 'earliest'
            try:
                event_cls = getattr(self.contract.events, event_name.value)
                return event_cls.create_filter(fromBlock=0, toBlock='latest')
            except Exception as inner:
                raise Exception(f"Failed to create filter for event '{event_name}': {inner}") from e
    
    def reset_attestation_chain(self, wait: bool = False, timeout: int = 60):
        try:
            tx_data = self.contract.functions.ResetChain().build_transaction({'from': self.eth_address})
            return self._send_tx(tx_data, wait=wait, timeout=timeout)
        except Exception as e:
            raise Exception(f"An error occurred while calling the function: {str(e)}")

    def has_jury(self) -> bool:
        """True for a jury contract (V3), which has Expire."""
        return any(e.get("type") == "function" and e.get("name") == "Expire" for e in self.contract.abi)

    def has_anchored_reference(self) -> bool:
        """True for V2 and V3, where the admins anchor each agent's reference on chain."""
        return any(e.get("type") == "function" and e.get("name") == "referenceOf" for e in self.contract.abi)

    def jury_timeout_blocks(self) -> int:
        return self.contract.functions.juryTimeoutBlocks().call()

    def expire(self, attestation_id: str, wait: bool = True, timeout: int = 60) -> str:
        """Hands a V3 jury cycle to the SECaaS once its timeout has passed."""
        tx_data = self.contract.functions.Expire(text_to_bytes32(attestation_id)).build_transaction({'from': self.eth_address})
        return self._send_tx(tx_data, wait=wait, timeout=timeout)

    def set_secaas_only(self, enabled: bool, wait: bool = True, timeout: int = 60):
        try:
            tx_data = self.contract.functions.SetSecaasOnly(enabled).build_transaction({'from': self.eth_address})
            return self._send_tx(tx_data, wait=wait, timeout=timeout)
        except Exception as e:
            raise Exception(f"An error occurred while calling SetSecaasOnly: {str(e)}")

    def send_evidence(self, attestation_id, fresh_signature, iter_count: int = 1, wait: bool = False, timeout: int = 60):
        """Submit an attestation request with IterQ depth K (iter_count).
        Contract enforces 1 <= K <= MAX_ITER_COUNT (10000).
        K=1 means a single measurement (no rolling-hash chaining).
        SECaaS does not call this in production; kept aligned with the Solidity
        ABI for benchmarking and tooling consistency.
        """
        try:
            att_id_bytes = text_to_bytes32(attestation_id)
            fresh_signature_bytes = as_bytes32(fresh_signature)

            tx_data = self.contract.functions.SendEvidence(
                att_id_bytes,
                fresh_signature_bytes,
                int(iter_count),
            ).build_transaction({'from': self.eth_address})

            return self._send_tx(tx_data, wait=wait, timeout=timeout)

        except Exception as e:
            raise Exception(f"An error occurred while requesting attestation: {str(e)}")

    def register_agent(self, name, wait: bool = False, timeout: int = 60):
        try:
            tx_data = self.contract.functions.RegisterAgent(name).build_transaction({'from': self.eth_address})
            return self._send_tx(tx_data, wait=wait, timeout=timeout)
        except Exception as e:
            error(f"Failed to register domain: {str(e)}")
            raise Exception(f"Failed to register domain: {str(e)}")
        

    def remove_agent(self, wait: bool = False, timeout: int = 60):
        try:
            tx_data = self.contract.functions.RemoveAgent().build_transaction({'from': self.eth_address})
            return self._send_tx(tx_data, wait=wait, timeout=timeout)
        except Exception as e:
            error(f"Failed to unregister domain: {str(e)}")
            raise Exception(f"Failed to unregister domain: {str(e)}")

    def is_registered(self):
        return self.contract.functions.IsRegistered(self.eth_address).call()
        
    def is_prover_agent(self, attestation_id):
        att_id = text_to_bytes32(attestation_id)

        # If closed, contract would revert in IsProver => treat as "not prover" (or "not allowed")
        state = self.contract.functions.GetAttestationState(att_id).call()
        if state == AttestationState.Closed:
            return False
        try:
            return self.contract.functions.IsProver(att_id, self.eth_address).call()
        except ContractLogicError as e:
            # covers revert reasons like "closed or not exists"
            info(f"IsProver reverted for {attestation_id}: {e}")
            return False

    def is_verifier_agent(self, attestation_id):
        att_id = text_to_bytes32(attestation_id)
        # IsVerifier reverts with ContractLogicError when state==Closed;
        # the except below handles that identically to a pre-check, without
        # the extra GetAttestationState round-trip.
        try:
            return self.contract.functions.IsVerifier(att_id, self.eth_address).call()
        except ContractLogicError as e:
            info(f"IsVerifier reverted for {attestation_id}: {e}")
            return False
    

    def send_reference_signature(self, attestation_id, ref_signature, wait: bool = False, timeout: int = 60):
        try:
            ref_signature_bytes = as_bytes32(ref_signature)

            tx_data = self.contract.functions.SendRefSignature(
                text_to_bytes32(attestation_id),
                ref_signature_bytes
            ).build_transaction({'from': self.eth_address})

            return self._send_tx(tx_data, wait=wait, timeout=timeout)

        except Exception as e:
            raise Exception(f"An error occurred while sending the reference signature: {str(e)}")

    def get_attestation_signatures(self, attestation_id):
        """Returns (fresh_hex, ref_hex, iter_count) where iter_count (K) is the
        rolling-hash depth the prover used. The verifier must apply the same
        K-fold to ref before comparing to fresh.
        """
        try:
            att_id = text_to_bytes32(attestation_id)
            fresh, ref, iter_count = self.contract.functions.GetAttestationSignatures(att_id, self.eth_address).call()
            return Web3.to_hex(fresh), Web3.to_hex(ref), int(iter_count)
        except Exception as e:
            raise Exception(f"An error occurred while getting the attestation signatures: {str(e)}")
        
    def send_attestation_result(self, attestation_id, verified, wait: bool = False, timeout: int = 60):
        try:
            tx_data = self.contract.functions.CloseAttestationProcess(
                text_to_bytes32(attestation_id),
                verified
            ).build_transaction({'from': self.eth_address})
            
            return self._send_tx(tx_data, wait=wait, timeout=timeout)

        except Exception as e:
            raise Exception(f"An error occurred while closing the attestation process: {str(e)}")

    def resolve_attestation(self, attestation_id: str, verified: bool, wait: bool = False, timeout: int = 60) -> str:
        try:
            tx_data = self.contract.functions.ResolveAttestationSECaaS(
                text_to_bytes32(attestation_id),
                verified
            ).build_transaction({'from': self.eth_address})
            
            return self._send_tx(tx_data, wait=wait, timeout=timeout)
        except Exception as e:
            raise Exception(f"An error occurred while resolving attestation: {str(e)}")

    def get_agent_info(self, agent_address):
        try:
            name, is_registered, completed_attestations  = self.contract.functions.GetAgentInfo(agent_address, self.eth_address).call()
            return name, is_registered, completed_attestations
        except Exception as e:
            raise Exception(f"An error occurred while getting agent info: {str(e)}")

    def get_prover_address(self, attestation_id):
        try:
            return self.contract.functions.GetProverAddress(text_to_bytes32(attestation_id), self.eth_address).call()
        except Exception as e:
            raise Exception(f"An error occurred while getting agent info: {str(e)}")

    def get_attestation_chain(self):
        try:
            attestation_chain = self.contract.functions.GetAttestationChain().call()
            return attestation_chain
        
        except Exception as e:
            raise Exception(f"An error occurred while getting the attestation chain: {str(e)}")
      
    def get_attestation_info(self, attestation_id):
        try:
            prover_address, verifier_address, attestation_result, timestamp  = self.contract.functions.GetAttestationInfo(text_to_bytes32(attestation_id)).call()
            return prover_address, verifier_address, attestation_result, timestamp
        
        except Exception as e:
            raise Exception(f"An error occurred while getting attestation info: {str(e)}")

    def get_attestation_state(self, attestation_id):
        try:
            return self.contract.functions.GetAttestationState(text_to_bytes32(attestation_id)).call()
        except Exception as e:
            raise Exception(f"An error occurred while getting attestation info: {str(e)}")

    def display_attestation_state(self, attestation_id):  
        current_attestation_state = self.get_attestation_state(attestation_id)
        try:
            state_name = AttestationState(current_attestation_state).name
            info(f"Attestation state: {state_name}")
        except ValueError:
            error(f"Unknown attestation state: {current_attestation_state}")

    def _safe_display(self, chain_display_n: int):
        try:
            attestation_chain = self.get_attestation_chain()
            if not attestation_chain:
                info("=== Attestation chain is empty ===")
                return

            table = build_attestation_chain_table(
                attestation_chain=attestation_chain,
                get_attestation_info=self.get_attestation_info,
                get_agent_info=self.get_agent_info,
                last_n=chain_display_n,
            )
            info("\n" + table.get_string())

        except Exception as e:
            error(f"Failed to display attestation chain: {e}")

    def periodic_attestation_chain_display(self, chain_display_n: int, interval: int, run_immediately: bool = True):
        if run_immediately:
            self._safe_display(chain_display_n)
        while True:
            time.sleep(interval)
            self._safe_display(chain_display_n)

