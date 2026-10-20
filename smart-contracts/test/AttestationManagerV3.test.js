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

describe("AttestationManagerV3: jury draw", function () {
  const REF = ethers.encodeBytes32String("ref");
  const seed = (s) => ethers.id(s);
  let secaas, a1, robots, am, n;

  async function cycle(prover, verified) {
    const id = ethers.encodeBytes32String(`c${n++}`);
    const receipt = await (await am.connect(prover).SendEvidence(id, REF, 1)).wait();
    if (await am.IsVerifier(id, secaas.address)) {
      await am.connect(secaas).ResolveAttestationSECaaS(id, verified);
    } else {
      for (const r of robots) {
        if (await am.IsVerifier(id, r.address)) {
          await am.connect(r).CloseAttestationProcess(id, verified);
          break;
        }
      }
    }
    return { id, block: receipt.blockNumber };
  }

  // Deploys with a jury of 3, threshold 2, and makes the first k robots eligible.
  async function setup(k) {
    const AM = await ethers.getContractFactory("AttestationManagerV3", secaas);
    am = await AM.deploy([a1.address], 1, 3, 2, 5);
    for (const r of robots) {
      await am.connect(r).RegisterAgent("r");
      await am.connect(a1).SetReference(r.address, REF);
    }
    n = 0;
    for (const r of robots.slice(0, k)) await cycle(r, true);
  }

  beforeEach(async () => {
    const signers = await ethers.getSigners();
    [secaas, a1] = signers;
    robots = signers.slice(2, 10);
  });

  it("draws distinct jurors and never the prover", async () => {
    await setup(8);
    const prover = robots[0].address;
    for (let i = 0; i < 20; i++) {
      const jury = await am.DrawJury(seed(`s${i}`), prover);
      expect(jury.length).to.equal(3);
      expect(new Set(jury).size).to.equal(3);
      expect(jury).to.not.include(prover);
    }
  });

  it("gives the same jury for the same seed, and another one for other seeds", async () => {
    await setup(8);
    const prover = robots[0].address;
    expect(await am.DrawJury(seed("a"), prover)).to.deep.equal(await am.DrawJury(seed("a"), prover));
    const juries = new Set();
    for (let i = 0; i < 10; i++) juries.add([...(await am.DrawJury(seed(`s${i}`), prover))].sort().join());
    expect(juries.size).to.be.greaterThan(1);
  });

  it("draws a smaller jury down to the threshold, and none below it", async () => {
    await setup(3);
    expect((await am.DrawJury(seed("a"), robots[0].address)).length).to.equal(2);
    await am.connect(robots[2]).RemoveAgent();
    expect((await am.DrawJury(seed("a"), robots[0].address)).length).to.equal(0);
  });

  it("previews the jury from the hash of the evidence block and the cycle id", async () => {
    await setup(8);
    const { id, block } = await cycle(robots[0], true);
    await ethers.provider.send("evm_mine", []);
    const hash = (await ethers.provider.getBlock(block)).hash;
    const expected = await am.DrawJury(ethers.keccak256(ethers.AbiCoder.defaultAbiCoder().encode(["bytes32", "bytes32"], [hash, id])), robots[0].address);
    expect(await am.PreviewJury(id)).to.deep.equal(expected);
    await expect(am.PreviewJury(ethers.encodeBytes32String("none"))).to.be.revertedWith("PreviewJury : unknown cycle");
  });
});
