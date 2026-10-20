const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("AttestationManagerV3: jury parameters", function () {
  let secaas, a1, AM;

  beforeEach(async () => {
    [secaas, a1] = await ethers.getSigners();
    AM = await ethers.getContractFactory("AttestationManagerV3", secaas);
  });

  it("stores the jury size, threshold and timeout", async () => {
    const am = await AM.deploy([a1.address], 1, 3, 2, 5);
    expect(await am.jurySize()).to.equal(3);
    expect(await am.juryThreshold()).to.equal(2);
    expect(await am.juryTimeoutBlocks()).to.equal(5);
  });

  it("rejects a threshold of 0 or above the jury size, and a timeout of 0", async () => {
    await expect(AM.deploy([a1.address], 1, 3, 0, 5)).to.be.revertedWith("Invalid jury threshold");
    await expect(AM.deploy([a1.address], 1, 3, 4, 5)).to.be.revertedWith("Invalid jury threshold");
    await expect(AM.deploy([a1.address], 1, 3, 2, 0)).to.be.revertedWith("Invalid jury timeout");
  });
});

describe("AttestationManagerV3: eligible agents", function () {
  const REF = ethers.encodeBytes32String("ref");
  let secaas, a1, robotA, robotB, am, n;

  // One cycle of prover, closed by whoever the contract elected.
  async function cycle(prover, verified) {
    const id = ethers.encodeBytes32String(`c${n++}`);
    await am.connect(prover).SendEvidence(id, REF, 1);
    if (await am.IsVerifier(id, secaas.address)) {
      await am.connect(secaas).ResolveAttestationSECaaS(id, verified);
    } else {
      const verifier = [robotA, robotB].find((r) => r.address !== prover.address);
      await am.connect(verifier).CloseAttestationProcess(id, verified);
    }
  }
  const eligible = async () => [...(await am.GetEligibleAgents())].sort();

  beforeEach(async () => {
    [secaas, a1, robotA, robotB] = await ethers.getSigners();
    const AM = await ethers.getContractFactory("AttestationManagerV3", secaas);
    am = await AM.deploy([a1.address], 1, 3, 2, 5);
    for (const r of [robotA, robotB]) {
      await am.connect(r).RegisterAgent("r");
      await am.connect(a1).SetReference(r.address, REF);
    }
    n = 0;
  });

  it("adds an agent on success, once only", async () => {
    expect(await eligible()).to.deep.equal([]);
    await cycle(robotA, true);
    await cycle(robotA, true);
    expect(await eligible()).to.deep.equal([robotA.address]);
    await cycle(robotB, true);
    expect(await eligible()).to.deep.equal([robotA.address, robotB.address].sort());
  });

  it("removes an agent on failure", async () => {
    await cycle(robotA, true);
    await cycle(robotB, true);
    await cycle(robotA, false);
    expect(await eligible()).to.deep.equal([robotB.address]);
  });

  it("removes an agent that leaves, and empties the list on ResetChain", async () => {
    await cycle(robotA, true);
    await cycle(robotB, true);
    await am.connect(robotA).RemoveAgent();
    expect(await eligible()).to.deep.equal([robotB.address]);
    await am.connect(secaas).ResetChain();
    expect(await eligible()).to.deep.equal([]);
  });
});
