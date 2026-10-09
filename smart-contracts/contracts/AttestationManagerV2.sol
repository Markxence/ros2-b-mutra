// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract AttestationManagerV2 {

    enum AttestationResult {None, Failure, Success}

    enum AttestationState {Open, ReadyForEvaluation, Closed}

    struct Agent {
        string name;
        bool registered;
    }

    struct MutualAttestation {
        bytes32 id;
        address verifier;
        address prover;
        bytes32 fresh_signature;
        bytes32 ref_signature;
        AttestationState state;
        AttestationResult result;
        uint256 timestamp;
        uint32 iterCount; // IterQ depth: number of nested SHA-256 folds
                         // composing fresh_signature. Verifier must apply
                         // the same fold count to ref_signature before
                         // comparing. K=1 = single measurement (legacy).
    }

    // IterQ bound: rejects accidental/malicious huge K that would burn
    // verifier CPU. 10000 covers 10000 * SSP ~= 55h at SSP=20s.
    uint32 public constant MAX_ITER_COUNT = 10000;

    address private secaas;
    address[] private participants;

    // Root-of-trust verifier: always the last successfully verified agent.
    // Initialised to secaas (default fallback when no agent has been verified yet).
    address private currentVerifier;

    mapping(address => Agent) private agent;
    mapping(bytes32 => MutualAttestation) private attestation;
    mapping(address => uint256) private indexOf;

    // Admin keys, distinct from the SECaaS key. They will be the only ones
    // allowed to write the on-chain references.
    mapping(address => bool) public isAdmin;
    uint256 public adminThreshold;

    event AgentRegistered(address agent);
    event AgentRemoved(address agent);
    event AttestationStarted(bytes32 id, uint32 iterCount);
    event ReadyForEvaluation(bytes32 id);
    event AttestationCompleted(bytes32 id);
    event ChainReset();

    constructor(address[] memory admins, uint256 threshold) {
        secaas = msg.sender;
        participants.push(secaas);
        indexOf[secaas] = 0;
        currentVerifier = secaas;

        require(threshold >= 1 && threshold <= admins.length, "Invalid admin threshold");
        for (uint i = 0; i < admins.length; i++) {
            require(admins[i] != address(0) && admins[i] != secaas, "Admin must differ from the SECaaS");
            require(!isAdmin[admins[i]], "Duplicate admin");
            isAdmin[admins[i]] = true;
        }
        adminThreshold = threshold;
    }

    function ResetChain() public {
        require(msg.sender == secaas, "Only the SECaaS can reset the chain");
        currentVerifier = secaas;
        emit ChainReset();
    }

    function RegisterAgent(string memory name) public {
        require(bytes(name).length > 0, "Name is not valid");
        Agent storage currentAgent = agent[msg.sender];
        currentAgent.name = name;
        currentAgent.registered = true;
        indexOf[msg.sender] = participants.length;
        participants.push(msg.sender);
        emit AgentRegistered(msg.sender);
    }

    function RemoveAgent() public {
        Agent storage currentAgent = agent[msg.sender];
        require(currentAgent.registered, "RemoveAgent : Agent is not registered");

        // If the departing agent is the current root-of-trust, fall back to secaas.
        if (msg.sender == currentVerifier) {
            currentVerifier = secaas;
        }

        uint idx = indexOf[msg.sender];
        address last = participants[participants.length - 1];
        participants[idx] = last;
        indexOf[last] = idx;
        participants.pop();

        delete indexOf[msg.sender];
        delete agent[msg.sender];
        emit AgentRemoved(msg.sender);
    }

    // Returns currentVerifier unless prover == currentVerifier, in which case falls back to secaas.
    function _assignVerifier(address prover) internal view returns (address) {
        if (currentVerifier != prover) {
            return currentVerifier;
        }
        return secaas;
    }

    function GetAgentInfo(address agentAddress, address callAddress) public view returns (string memory, bool, bytes32[] memory) {
        Agent storage currentAgent = agent[callAddress];
        Agent storage agentToFind = agent[agentAddress];
        require(currentAgent.registered || callAddress == secaas, "GetAgentInfo : Agent is not registered");
        return (agentToFind.name, agentToFind.registered, new bytes32[](0));
    }

    function SendEvidence(bytes32 id, bytes32 freshMeasurement, uint32 iterCount) public {
        Agent storage currentAgent = agent[msg.sender];
        require(currentAgent.registered, "SendEvidence : Agent is not registered");
        require(iterCount >= 1 && iterCount <= MAX_ITER_COUNT, "SendEvidence : iterCount out of range");

        MutualAttestation storage currentAttestation = attestation[id];
        currentAttestation.id = id;
        currentAttestation.verifier = _assignVerifier(msg.sender);
        currentAttestation.prover = msg.sender;
        currentAttestation.fresh_signature = freshMeasurement;
        currentAttestation.ref_signature = bytes32(0);
        currentAttestation.state = AttestationState.Open;
        currentAttestation.result = AttestationResult.None;
        currentAttestation.timestamp = 0;
        currentAttestation.iterCount = iterCount;

        emit AttestationStarted(id, iterCount);
    }

    function GetProverAddress(bytes32 id, address callAddress) public view returns (address) {
        require(callAddress == secaas, "You are not the SECaaS.");
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state == AttestationState.Open, "Attestation is closed or not exists");
        return currentAttestation.prover;
    }

    function SendRefSignature(bytes32 id, bytes32 refMeasurement) public returns (bool) {
        require(msg.sender == secaas, "Only the SECaaS can send the reference signature");

        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state == AttestationState.Open, "Invalid state for SECaaS response");

        currentAttestation.ref_signature = refMeasurement;
        currentAttestation.state = AttestationState.ReadyForEvaluation;

        emit ReadyForEvaluation(id);
        return true;
    }

    function GetAttestationSignatures(bytes32 id, address callAddress) public view returns (bytes32, bytes32, uint32) {
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.verifier == callAddress, "Only the verifier can retrieve the signatures");
        require(
            currentAttestation.state == AttestationState.ReadyForEvaluation ||
            currentAttestation.verifier == secaas,
            "Attestation not ready for evaluation"
        );
        return (currentAttestation.fresh_signature, currentAttestation.ref_signature, currentAttestation.iterCount);
    }

    function GetAttestationInfo(bytes32 id) public view returns (address, address, AttestationResult, uint256) {
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state == AttestationState.Closed, "Attestation process is not closed");
        return (currentAttestation.prover, currentAttestation.verifier, currentAttestation.result, currentAttestation.timestamp);
    }

    function GetAttestationState(bytes32 id) public view returns (AttestationState) {
        return attestation[id].state;
    }

    function CloseAttestationProcess(bytes32 id, bool verified) public returns (bool) {
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.verifier == msg.sender, "Only the verifier can close the attestation process");
        require(currentAttestation.state == AttestationState.ReadyForEvaluation, "Attestation is not ready for closure");

        currentAttestation.state = AttestationState.Closed;
        currentAttestation.timestamp = block.timestamp;

        if (verified) {
            currentAttestation.result = AttestationResult.Success;
            // The successfully verified agent becomes the new root-of-trust verifier.
            currentVerifier = currentAttestation.prover;
        } else {
            currentAttestation.result = AttestationResult.Failure;
        }

        emit AttestationCompleted(id);
        return true;
    }

    function ResolveAttestationSECaaS(bytes32 id, bool verified) public {
        require(msg.sender == secaas, "Only the SECaaS can resolve attestations");

        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state == AttestationState.Open, "Not in Open state");
        require(currentAttestation.verifier == secaas, "SECaaS not elected as verifier");

        currentAttestation.state = AttestationState.Closed;
        currentAttestation.timestamp = block.timestamp;

        if (verified) {
            currentAttestation.result = AttestationResult.Success;
            currentVerifier = currentAttestation.prover;
        } else {
            currentAttestation.result = AttestationResult.Failure;
        }

        emit AttestationCompleted(id);
    }

    function IsProver(bytes32 id, address callAddress) public view returns (bool) {
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state != AttestationState.Closed, "Attestation process is closed or not exists");
        return currentAttestation.prover == callAddress;
    }

    function IsVerifier(bytes32 id, address callAddress) public view returns (bool) {
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state != AttestationState.Closed, "Attestation process is closed or not exists");
        return currentAttestation.verifier == callAddress;
    }

    function IsRegistered(address callAddress) public view returns (bool) {
        return agent[callAddress].registered;
    }

    // Always returns an empty array — LV does not maintain a global chain log.
    // Present to keep the ABI compatible with AttestationManagerRR.
    function GetAttestationChain() public pure returns (bytes32[] memory) {
        return new bytes32[](0);
    }

    // Current root-of-trust verifier — restricted to SECaaS to avoid leaking verifier identity.
    function GetCurrentVerifier() public view returns (address) {
        require(msg.sender == secaas, "Only the SECaaS can query the current verifier");
        return currentVerifier;
    }
}
