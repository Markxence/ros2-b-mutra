// SPDX-License-Identifier: MIT
pragma solidity ^0.8.0;

contract AttestationManagerV3 {

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
        AttestationState state;
        AttestationResult result;
        uint256 timestamp;
        uint256 seedBlock; // block holding the evidence: its hash seeds the jury draw
        bool juryDrawn;    // the jury is drawn and frozen at the first vote
        address[] jury;
        mapping(address => uint8) vote; // per juror: 0 no vote, 1 against, 2 for
        uint8 votesFor;
        uint8 votesAgainst;
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

    mapping(address => Agent) private agent;
    mapping(bytes32 => MutualAttestation) private attestation;
    mapping(address => uint256) private indexOf;

    // Reference anchoring. referenceOf[agent] is written by the admins only,
    // never by the SECaaS. It holds SHA256(h_ref || salt_agent), so reading
    // it does not reveal which binary the agent runs.
    mapping(address => bool) public isAdmin;
    uint256 public adminThreshold;
    mapping(address => bytes32) public referenceOf;
    mapping(address => uint256) public referenceVersion;
    mapping(bytes32 => mapping(address => bool)) private approvedBy;
    mapping(bytes32 => uint256) private approvalCount;

    // Verifier jury, fixed at deployment: jurySize jurors are drawn per cycle,
    // juryThreshold matching votes close it, and after juryTimeoutBlocks blocks
    // without a close the cycle goes to the SECaaS.
    uint256 public jurySize;
    uint256 public juryThreshold;
    uint256 public juryTimeoutBlocks;

    // Agents whose last attestation succeeded: the pool the jury is drawn
    // from. eligibleSlot is the index in eligible plus one, 0 when absent, so
    // adding and removing cost the same whatever the number of agents.
    address[] private eligible;
    mapping(address => uint256) private eligibleSlot;

    // Last round of each prover. A prover may open a new round only once
    // this one is closed or with the SECaaS, so it cannot draw several juries
    // for the same state and keep the last verdict.
    mapping(address => bytes32) private lastRound;

    event AgentRegistered(address agent);
    event AgentRemoved(address agent);
    event AttestationStarted(bytes32 id, uint32 iterCount);
    event ReadyForEvaluation(bytes32 id);
    event AttestationCompleted(bytes32 id);
    event VoteCast(bytes32 indexed id, address indexed juror, bool verdict);
    event JurorDissent(bytes32 indexed id, address indexed juror);
    event JurorAbsent(bytes32 indexed id, address indexed juror);
    event ChainReset();
    event ReferenceApproved(address indexed agent, bytes32 newReference, address indexed admin);
    event ReferenceSet(address indexed agent, bytes32 oldReference, bytes32 newReference, uint256 version);

    constructor(address[] memory admins, uint256 threshold, uint256 size, uint256 quorum, uint256 timeoutBlocks) {
        secaas = msg.sender;
        participants.push(secaas);
        indexOf[secaas] = 0;

        require(threshold >= 1 && threshold <= admins.length, "Invalid admin threshold");
        for (uint i = 0; i < admins.length; i++) {
            require(admins[i] != address(0) && admins[i] != secaas, "Admin must differ from the SECaaS");
            require(!isAdmin[admins[i]], "Duplicate admin");
            isAdmin[admins[i]] = true;
        }
        adminThreshold = threshold;

        require(quorum >= 1 && quorum <= size, "Invalid jury threshold");
        require(timeoutBlocks >= 1, "Invalid jury timeout");
        jurySize = size;
        juryThreshold = quorum;
        juryTimeoutBlocks = timeoutBlocks;
    }

    // An admin approves newReference for an agent. The reference changes once
    // adminThreshold distinct admins have approved the same value. Approvals
    // are bound to the current version, so they cannot be replayed later.
    function SetReference(address agentAddress, bytes32 newReference) public {
        require(isAdmin[msg.sender], "SetReference : caller is not an admin");
        require(newReference != bytes32(0), "SetReference : empty reference");

        bytes32 proposal = keccak256(abi.encode(agentAddress, newReference, referenceVersion[agentAddress]));
        require(!approvedBy[proposal][msg.sender], "SetReference : already approved by this admin");
        approvedBy[proposal][msg.sender] = true;
        approvalCount[proposal]++;
        emit ReferenceApproved(agentAddress, newReference, msg.sender);

        if (approvalCount[proposal] < adminThreshold) return;

        bytes32 oldReference = referenceOf[agentAddress];
        referenceOf[agentAddress] = newReference;
        referenceVersion[agentAddress]++;
        emit ReferenceSet(agentAddress, oldReference, newReference, referenceVersion[agentAddress]);
    }

    function ResetChain() public {
        require(msg.sender == secaas, "Only the SECaaS can reset the chain");
        while (eligible.length > 0) {
            delete eligibleSlot[eligible[eligible.length - 1]];
            eligible.pop();
        }
        emit ChainReset();
    }

    function _setEligible(address a, bool on) internal {
        uint256 slot = eligibleSlot[a];
        if (on == (slot != 0)) return;
        if (on) {
            eligible.push(a);
            eligibleSlot[a] = eligible.length;
            return;
        }
        address last = eligible[eligible.length - 1];
        eligible[slot - 1] = last;
        eligibleSlot[last] = slot;
        eligible.pop();
        delete eligibleSlot[a];
    }

    function GetEligibleAgents() public view returns (address[] memory) {
        return eligible;
    }

    // Draws min(jurySize, eligible agents other than the prover) distinct
    // jurors from seed. Returns an empty jury when fewer than juryThreshold
    // can be drawn: the cycle then goes to the SECaaS. Indexes are drawn with
    // rejection, without copying the list.
    // ponytail: rejection loop, fine for a jury of a few agents; switch to a
    // partial Fisher-Yates if jurySize gets close to the number of agents.
    function DrawJury(bytes32 seed, address prover) public view returns (address[] memory jury) {
        uint256 size = _juryCount(prover);
        jury = new address[](size);
        uint256 found = 0;
        for (uint256 k = 0; found < size; k++) {
            address candidate = eligible[uint256(keccak256(abi.encode(seed, k))) % eligible.length];
            if (candidate == prover) continue;
            bool taken = false;
            for (uint256 j = 0; j < found; j++) {
                if (jury[j] == candidate) { taken = true; break; }
            }
            if (!taken) jury[found++] = candidate;
        }
    }

    // Number of jurors a cycle of prover gets, 0 when the SECaaS must decide.
    function _juryCount(address prover) internal view returns (uint256) {
        uint256 pool = eligible.length - (eligibleSlot[prover] != 0 ? 1 : 0);
        uint256 size = jurySize < pool ? jurySize : pool;
        return size < juryThreshold ? 0 : size;
    }

    // The jury of a cycle, seeded by the hash of the block holding its
    // evidence and by its id. Readable from the next block on, so that the
    // prover commits to its evidence before the draw is known.
    function PreviewJury(bytes32 id) public view returns (address[] memory) {
        MutualAttestation storage a = attestation[id];
        require(a.prover != address(0), "PreviewJury : unknown cycle");
        require(block.number > a.seedBlock, "PreviewJury : evidence block not sealed yet");
        bytes32 blockHash = blockhash(a.seedBlock);
        require(blockHash != bytes32(0), "PreviewJury : evidence block too old");
        return DrawJury(keccak256(abi.encode(blockHash, id)), a.prover);
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
        _setEligible(msg.sender, false);

        uint idx = indexOf[msg.sender];
        address last = participants[participants.length - 1];
        participants[idx] = last;
        indexOf[last] = idx;
        participants.pop();

        delete indexOf[msg.sender];
        delete agent[msg.sender];
        emit AgentRemoved(msg.sender);
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
        require(referenceOf[msg.sender] != bytes32(0), "SendEvidence : no reference anchored for this agent");
        MutualAttestation storage last = attestation[lastRound[msg.sender]];
        require(last.prover == address(0) || last.state == AttestationState.Closed || last.verifier == secaas,
                "SendEvidence : prover has an open round");
        // A reused id would overwrite the record of its cycle, votes included.
        require(attestation[id].prover == address(0), "SendEvidence : id already used");
        lastRound[msg.sender] = id;

        MutualAttestation storage currentAttestation = attestation[id];
        currentAttestation.id = id;
        // A jury cycle has no single verifier: its jurors are drawn at the
        // first vote. The SECaaS verifies when no jury can be drawn.
        currentAttestation.verifier = _juryCount(msg.sender) > 0 ? address(0) : secaas;
        currentAttestation.prover = msg.sender;
        currentAttestation.fresh_signature = freshMeasurement;
        currentAttestation.result = AttestationResult.None;
        currentAttestation.timestamp = 0;
        currentAttestation.iterCount = iterCount;
        currentAttestation.seedBlock = block.number;

        // The reference is already on chain, so the jurors can evaluate at
        // once. The SECaaS is only notified when it is the elected verifier.
        if (currentAttestation.verifier == secaas) {
            currentAttestation.state = AttestationState.Open;
            emit AttestationStarted(id, iterCount);
        } else {
            currentAttestation.state = AttestationState.ReadyForEvaluation;
            emit ReadyForEvaluation(id);
        }
    }

    // A juror gives its verdict. The first vote draws the jury and freezes it,
    // so that later changes to the eligible list do not change who may vote.
    // juryThreshold matching votes close the cycle; a jury that has all voted
    // without reaching it hands the cycle to the SECaaS.
    function Vote(bytes32 id, bool verdict) public {
        MutualAttestation storage a = attestation[id];
        require(a.prover != address(0), "Vote : unknown cycle");
        require(a.state != AttestationState.Closed, "Vote : cycle closed");
        require(a.verifier != secaas, "Vote : cycle is with the SECaaS");
        require(block.number > a.seedBlock, "Vote : evidence block not sealed yet");
        if (!a.juryDrawn) {
            a.jury = PreviewJury(id);
            a.juryDrawn = true;
        }
        require(_contains(a.jury, msg.sender), "Vote : caller is not in the jury");
        require(a.vote[msg.sender] == 0, "Vote : already voted");

        a.vote[msg.sender] = verdict ? 2 : 1;
        if (verdict) a.votesFor++; else a.votesAgainst++;
        emit VoteCast(id, msg.sender, verdict);

        if (a.votesFor >= juryThreshold || a.votesAgainst >= juryThreshold) {
            a.verifier = msg.sender; // the juror whose vote closed the cycle, for GetAttestationInfo
            _close(id, a.votesFor >= juryThreshold);
        } else if (a.votesFor + a.votesAgainst == a.jury.length) {
            _toSecaas(id);
        }
    }

    // Anyone may hand a jury cycle to the SECaaS once juryTimeoutBlocks
    // blocks have passed since its evidence without a close.
    function Expire(bytes32 id) public {
        MutualAttestation storage a = attestation[id];
        require(a.prover != address(0), "Expire : unknown cycle");
        require(a.state != AttestationState.Closed, "Expire : cycle closed");
        require(a.verifier != secaas, "Expire : cycle is with the SECaaS");
        require(block.number >= a.seedBlock + juryTimeoutBlocks, "Expire : timeout not reached");
        // The jurors that did not vote lose their eligibility until their next
        // success. Without a vote the jury was never frozen: draw it now, from
        // the eligible list of this block. Past 256 blocks the evidence block
        // hash is gone and no one can be named absent.
        if (!a.juryDrawn) {
            bytes32 blockHash = blockhash(a.seedBlock);
            if (blockHash != bytes32(0)) {
                a.jury = DrawJury(keccak256(abi.encode(blockHash, id)), a.prover);
                a.juryDrawn = true;
            }
        }
        for (uint256 i = 0; i < a.jury.length; i++) {
            if (a.vote[a.jury[i]] == 0) {
                _setEligible(a.jury[i], false);
                emit JurorAbsent(id, a.jury[i]);
            }
        }
        _toSecaas(id);
    }

    function _toSecaas(bytes32 id) internal {
        MutualAttestation storage a = attestation[id];
        a.verifier = secaas;
        a.state = AttestationState.Open;
        emit AttestationStarted(id, a.iterCount);
    }

    function _contains(address[] memory list, address who) internal pure returns (bool) {
        for (uint256 i = 0; i < list.length; i++) {
            if (list[i] == who) return true;
        }
        return false;
    }

    function _close(bytes32 id, bool verified) internal {
        MutualAttestation storage a = attestation[id];
        a.state = AttestationState.Closed;
        a.timestamp = block.timestamp;
        a.result = verified ? AttestationResult.Success : AttestationResult.Failure;
        _setEligible(a.prover, verified);
        // A juror that voted against the verdict, of the jury or of the SECaaS,
        // loses its eligibility until its next success.
        uint8 against = verified ? 1 : 2;
        for (uint256 i = 0; i < a.jury.length; i++) {
            if (a.vote[a.jury[i]] == against) {
                _setEligible(a.jury[i], false);
                emit JurorDissent(id, a.jury[i]);
            }
        }
        emit AttestationCompleted(id);
    }

    // The frozen jury of a cycle and each juror's vote (0 no vote, 1 against,
    // 2 for), both empty before the first vote.
    function GetJury(bytes32 id) public view returns (address[] memory jurors, uint8[] memory votes) {
        MutualAttestation storage a = attestation[id];
        jurors = a.jury;
        votes = new uint8[](jurors.length);
        for (uint256 i = 0; i < jurors.length; i++) votes[i] = a.vote[jurors[i]];
    }

    function GetProverAddress(bytes32 id, address callAddress) public view returns (address) {
        require(callAddress == secaas, "You are not the SECaaS.");
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state == AttestationState.Open, "Attestation is closed or not exists");
        return currentAttestation.prover;
    }

    // The evidence and the anchored reference are public on chain anyway, so
    // anyone may read them. The caller argument stays for ABI compatibility.
    function GetAttestationSignatures(bytes32 id, address) public view returns (bytes32, bytes32, uint32) {
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.prover != address(0), "Unknown attestation");
        require(
            currentAttestation.state == AttestationState.ReadyForEvaluation ||
            currentAttestation.verifier == secaas,
            "Attestation not ready for evaluation"
        );
        return (currentAttestation.fresh_signature, referenceOf[currentAttestation.prover], currentAttestation.iterCount);
    }

    function GetAttestationInfo(bytes32 id) public view returns (address, address, AttestationResult, uint256) {
        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state == AttestationState.Closed, "Attestation process is not closed");
        return (currentAttestation.prover, currentAttestation.verifier, currentAttestation.result, currentAttestation.timestamp);
    }

    function GetAttestationState(bytes32 id) public view returns (AttestationState) {
        return attestation[id].state;
    }

    function ResolveAttestationSECaaS(bytes32 id, bool verified) public {
        require(msg.sender == secaas, "Only the SECaaS can resolve attestations");

        MutualAttestation storage currentAttestation = attestation[id];
        require(currentAttestation.state == AttestationState.Open, "Not in Open state");
        require(currentAttestation.verifier == secaas, "SECaaS not elected as verifier");
        _close(id, verified);
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
}
