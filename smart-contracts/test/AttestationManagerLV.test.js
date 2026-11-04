const { expect } = require("chai");
const { ethers } = require("hardhat");

// Solidity / ethers helpers
const ZERO_BYTES32 = "0x" + "00".repeat(32);

function bytes32FromString(s) {
  // Pads short ASCII to 32 bytes (matches the Go TextToBytes32 helper).
  const buf = Buffer.alloc(32);
  Buffer.from(s, "utf8").copy(buf);
  return "0x" + buf.toString("hex");
}

// Reference rolling-hash construction. Must mirror the prover/verifier:
//   H_1 = m_1
//   H_i = SHA256(H_{i-1} || m_i)   for i >= 2
function rollingHashConst(m, k) {
  let h = m; // hex with 0x prefix, 32 bytes
  for (let i = 2; i <= k; i++) {
    h = ethers.sha256(ethers.concat([h, m]));
  }
  return h;
}

describe("AttestationManagerLV — IterQ (per-attestation K)", function () {
  let secaas, prover, AM, am;

  beforeEach(async () => {
    [secaas, prover] = await ethers.getSigners();
    AM = await ethers.getContractFactory("AttestationManagerLV", secaas);
    am = await AM.deploy();
    await am.waitForDeployment();
    await am.connect(prover).RegisterAgent("p1");
  });

  it("exposes MAX_ITER_COUNT = 10000", async () => {
    expect(await am.MAX_ITER_COUNT()).to.equal(10000);
  });

  it("rejects K = 0", async () => {
    const id = bytes32FromString("att-k0");
    const sig = bytes32FromString("sig");
    await expect(
      am.connect(prover).SendEvidence(id, sig, 0)
    ).to.be.revertedWith("SendEvidence : iterCount out of range");
  });

  it("rejects K > MAX_ITER_COUNT", async () => {
    const id = bytes32FromString("att-kmax");
    const sig = bytes32FromString("sig");
    await expect(
      am.connect(prover).SendEvidence(id, sig, 10001)
    ).to.be.revertedWith("SendEvidence : iterCount out of range");
  });

  it("emits AttestationStarted with the supplied K", async () => {
    const id = bytes32FromString("att-emit");
    const sig = bytes32FromString("sig-emit");
    await expect(am.connect(prover).SendEvidence(id, sig, 7))
      .to.emit(am, "AttestationStarted")
      .withArgs(id, 7);
  });

  it("GetAttestationSignatures returns (fresh, ref, K)", async () => {
    // SECaaS is the elected verifier on the very first attestation
    // (currentVerifier = secaas before any rotation), so the secaas-deployer
    // signer can read GetAttestationSignatures even while state is still Open
    // (verifier == secaas branch of the require).
    const id = bytes32FromString("att-get");
    const m = bytes32FromString("measurement");
    const K = 5;

    await am.connect(prover).SendEvidence(id, m, K);

    const [fresh, ref, k] = await am
      .connect(secaas)
      .GetAttestationSignatures(id, secaas.address);

    expect(fresh).to.equal(m);
    expect(ref).to.equal(ZERO_BYTES32); // SECaaS hasn't sent the ref yet
    expect(k).to.equal(K);
  });

  it("end-to-end success: prover sends rolling hash for K=5, secaas resolves with the chained ref", async () => {
    const id = bytes32FromString("att-e2e");
    const baseRef = bytes32FromString("ref-bin"); // single-shot reference
    const K = 5;

    // In a no-tamper world all m_i == baseRef, so the prover's rolling hash
    // equals the verifier-derived expected value.
    const fresh = rollingHashConst(baseRef, K);

    await am.connect(prover).SendEvidence(id, fresh, K);

    // SECaaS oracle posts the *single-shot* reference; the contract just
    // stores it. The verifier (here, also secaas because of currentVerifier)
    // is what would re-derive the chained value off-chain. We mimic that
    // here by computing it locally and asserting the values that
    // GetAttestationSignatures returns are sufficient to reproduce it.
    await am.connect(secaas).SendRefSignature(id, baseRef);

    const [returnedFresh, returnedRef, returnedK] = await am
      .connect(secaas)
      .GetAttestationSignatures(id, secaas.address);

    expect(returnedFresh).to.equal(fresh);
    expect(returnedRef).to.equal(baseRef);
    expect(returnedK).to.equal(K);

    // Off-chain re-derivation a verifier would do:
    const derivedRef = rollingHashConst(returnedRef, returnedK);
    expect(derivedRef).to.equal(returnedFresh);

    // Close the round positively. With VRP=1 this stores Closed state.
    await expect(am.connect(secaas).CloseAttestationProcess(id, true))
      .to.emit(am, "AttestationCompleted")
      .withArgs(id);
  });
});
