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

describe("AttestationManagerV3: vote", function () {
  const REF = ethers.encodeBytes32String("ref");
  let secaas, a1, robots, am, n;

  async function close(id, verified) {
    if (await am.IsVerifier(id, secaas.address)) return am.connect(secaas).ResolveAttestationSECaaS(id, verified);
    for (const r of robots) if (await am.IsVerifier(id, r.address)) return am.connect(r).CloseAttestationProcess(id, verified);
  }
  async function evidence(prover) {
    const id = ethers.encodeBytes32String(`c${n++}`);
    await am.connect(prover).SendEvidence(id, REF, 1);
    return id;
  }
  const signer = (address) => robots.find((r) => r.address === address);

  // Jury of 3, threshold 2; the 8 robots are eligible, then robot 0 sends evidence.
  let id, jury;
  beforeEach(async () => {
    const signers = await ethers.getSigners();
    [secaas, a1] = signers;
    robots = signers.slice(2, 10);
    const AM = await ethers.getContractFactory("AttestationManagerV3", secaas);
    am = await AM.deploy([a1.address], 1, 3, 2, 5);
    for (const r of robots) {
      await am.connect(r).RegisterAgent("r");
      await am.connect(a1).SetReference(r.address, REF);
    }
    n = 0;
    for (const r of robots) await close(await evidence(r), true);
    id = await evidence(robots[0]);
    // A call sees the latest block as the current one: the jury is readable
    // once a block has been sealed after the evidence block.
    await ethers.provider.send("evm_mine", []);
    jury = [...(await am.PreviewJury(id))];
  });

  it("records a juror's vote", async () => {
    await expect(am.connect(signer(jury[0])).Vote(id, true)).to.emit(am, "VoteCast").withArgs(id, jury[0], true);
  });

  it("rejects a vote in the evidence block", async () => {
    // Both transactions go into the same block; gas is fixed so that nothing
    // is estimated against a state that does not hold the evidence yet.
    await ethers.provider.send("evm_setAutomine", [false]);
    const fresh = ethers.encodeBytes32String("same-block");
    let sent, vote;
    try {
      sent = await am.connect(robots[1]).SendEvidence(fresh, REF, 1, { gasLimit: 1_000_000 });
      vote = await am.connect(robots[2]).Vote(fresh, true, { gasLimit: 1_000_000 });
      await ethers.provider.send("evm_mine", []);
    } finally {
      await ethers.provider.send("evm_setAutomine", [true]);
    }
    const [r1, r2] = await Promise.all([sent.hash, vote.hash].map((h) => ethers.provider.getTransactionReceipt(h)));
    expect(r1.blockNumber).to.equal(r2.blockNumber);
    expect(r1.status).to.equal(1);
    expect(r2.status).to.equal(0);
  });

  it("rejects a caller outside the jury, the prover included", async () => {
    const outsider = robots.find((r) => r !== robots[0] && !jury.includes(r.address));
    await expect(am.connect(outsider).Vote(id, true)).to.be.revertedWith("Vote : caller is not in the jury");
    await expect(am.connect(robots[0]).Vote(id, true)).to.be.revertedWith("Vote : caller is not in the jury");
  });

  it("rejects a second vote from the same juror", async () => {
    await am.connect(signer(jury[0])).Vote(id, true);
    await expect(am.connect(signer(jury[0])).Vote(id, false)).to.be.revertedWith("Vote : already voted");
  });

  it("rejects a vote on a closed cycle", async () => {
    await close(id, true);
    await expect(am.connect(signer(jury[0])).Vote(id, true)).to.be.revertedWith("Vote : cycle closed");
  });

  it("freezes the jury at the first vote", async () => {
    await am.connect(signer(jury[0])).Vote(id, true);
    // Two non-jurors fail an attestation and leave the eligible list.
    for (const r of robots.filter((r) => r !== robots[0] && !jury.includes(r.address)).slice(0, 2)) {
      await close(await evidence(r), false);
    }
    expect([...(await am.GetJury(id))]).to.deep.equal(jury);
    await expect(am.connect(signer(jury[1])).Vote(id, true)).to.emit(am, "VoteCast");
  });
});
