const { expect } = require("chai");
const { ethers } = require("hardhat");

// Closes a cycle with verdict: by the SECaaS when it is the verifier,
// otherwise by juryThreshold jurors. A call sees the latest block as the
// current one, so a block is mined before the jury can be read.
async function settle(am, secaas, signers, id, verdict) {
  if (await am.IsVerifier(id, secaas.address)) return am.connect(secaas).ResolveAttestationSECaaS(id, verdict);
  await ethers.provider.send("evm_mine", []);
  const jury = await am.PreviewJury(id);
  for (const j of jury.slice(0, Number(await am.juryThreshold()))) {
    await am.connect(signers.find((s) => s.address === j)).Vote(id, verdict);
  }
}

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

  // Two agents only: no jury can be drawn, the SECaaS closes every cycle.
  async function cycle(prover, verified) {
    const id = ethers.encodeBytes32String(`c${n++}`);
    await am.connect(prover).SendEvidence(id, REF, 1);
    await settle(am, secaas, [robotA, robotB], id, verified);
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
    await settle(am, secaas, robots, id, verified);
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

  const close = (id, verified) => settle(am, secaas, robots, id, verified);
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
    await expect(am.connect(signer(jury[2])).Vote(id, true)).to.be.revertedWith("Vote : cycle closed");
  });

  it("freezes the jury at the first vote", async () => {
    await am.connect(signer(jury[0])).Vote(id, true);
    // Two non-jurors fail an attestation and leave the eligible list.
    for (const r of robots.filter((r) => r !== robots[0] && !jury.includes(r.address)).slice(0, 2)) {
      await close(await evidence(r), false);
    }
    expect([...(await am.GetJury(id))[0]]).to.deep.equal(jury);
    await expect(am.connect(signer(jury[1])).Vote(id, true)).to.emit(am, "VoteCast");
  });
});

describe("AttestationManagerV3: closure and expiry", function () {
  const REF = ethers.encodeBytes32String("ref");
  let secaas, a1, robots, am, n;

  const signer = (address) => robots.find((r) => r.address === address);
  async function evidence(prover) {
    const id = ethers.encodeBytes32String(`c${n++}`);
    await am.connect(prover).SendEvidence(id, REF, 1);
    return id;
  }

  // Deploys with a jury of size and threshold quorum, makes the 8 robots
  // eligible, then robot 0 sends evidence. Returns the cycle and its jury.
  async function setup(size, quorum) {
    const AM = await ethers.getContractFactory("AttestationManagerV3", secaas);
    am = await AM.deploy([a1.address], 1, size, quorum, 5);
    for (const r of robots) {
      await am.connect(r).RegisterAgent("r");
      await am.connect(a1).SetReference(r.address, REF);
    }
    n = 0;
    for (const r of robots) await settle(am, secaas, robots, await evidence(r), true);
    const id = await evidence(robots[0]);
    await ethers.provider.send("evm_mine", []);
    return { id, jury: [...(await am.PreviewJury(id))].map(signer) };
  }
  const eligible = async (r) => (await am.GetEligibleAgents()).includes(r.address);
  const result = async (id) => (await am.GetAttestationInfo(id))[2];
  const SUCCESS = 2n, FAILURE = 1n;

  beforeEach(async () => {
    const signers = await ethers.getSigners();
    [secaas, a1] = signers;
    robots = signers.slice(2, 10);
  });

  it("goes to the SECaaS until a jury can be drawn, then to the jury", async () => {
    const AM = await ethers.getContractFactory("AttestationManagerV3", secaas);
    am = await AM.deploy([a1.address], 1, 3, 2, 5);
    for (const r of robots.slice(0, 3)) {
      await am.connect(r).RegisterAgent("r");
      await am.connect(a1).SetReference(r.address, REF);
    }
    n = 0;
    // Robots 0 and 1 have fewer than 2 eligible peers when they send evidence.
    for (const r of robots.slice(0, 2)) {
      const id = await evidence(r);
      expect(await am.IsVerifier(id, secaas.address)).to.equal(true);
      await am.connect(secaas).ResolveAttestationSECaaS(id, true);
    }
    await expect(am.connect(robots[2]).SendEvidence(ethers.encodeBytes32String("j"), REF, 1))
      .to.emit(am, "ReadyForEvaluation");
    expect(await am.IsVerifier(ethers.encodeBytes32String("j"), secaas.address)).to.equal(false);
  });

  it("closes on 2 votes for", async () => {
    const { id, jury } = await setup(3, 2);
    await am.connect(jury[0]).Vote(id, true);
    await expect(am.connect(jury[1]).Vote(id, true)).to.emit(am, "AttestationCompleted").withArgs(id);
    expect(await result(id)).to.equal(SUCCESS);
    expect(await eligible(robots[0])).to.equal(true);
  });

  it("closes on 2 votes against and removes the prover from the eligible list", async () => {
    const { id, jury } = await setup(3, 2);
    await am.connect(jury[0]).Vote(id, false);
    await am.connect(jury[1]).Vote(id, false);
    expect(await result(id)).to.equal(FAILURE);
    expect(await eligible(robots[0])).to.equal(false);
  });

  it("closes on 2 against 1, in both directions", async () => {
    for (const [verdicts, expected] of [[[true, false, true], SUCCESS], [[false, true, false], FAILURE]]) {
      const { id, jury } = await setup(3, 2);
      await am.connect(jury[0]).Vote(id, verdicts[0]);
      await am.connect(jury[1]).Vote(id, verdicts[1]);
      expect(await am.GetAttestationState(id)).to.equal(1n); // ReadyForEvaluation
      await am.connect(jury[2]).Vote(id, verdicts[2]);
      expect(await result(id)).to.equal(expected);
    }
  });

  it("hands a tie in a jury of 2 to the SECaaS", async () => {
    const { id, jury } = await setup(2, 2);
    expect(jury.length).to.equal(2);
    await am.connect(jury[0]).Vote(id, true);
    await expect(am.connect(jury[1]).Vote(id, false)).to.emit(am, "AttestationStarted").withArgs(id, 1);
    expect(await am.IsVerifier(id, secaas.address)).to.equal(true);
    await am.connect(secaas).ResolveAttestationSECaaS(id, false);
    expect(await result(id)).to.equal(FAILURE);
  });

  it("lets a juror read the evidence before voting", async () => {
    const { id, jury } = await setup(3, 2);
    const [fresh, ref, iter] = await am.GetAttestationSignatures(id, jury[0].address);
    expect([fresh, ref, iter]).to.deep.equal([REF, REF, 1n]);
    await expect(am.GetAttestationSignatures(ethers.encodeBytes32String("none"), jury[0].address))
      .to.be.revertedWith("Unknown attestation");
  });

  it("gives the jurors and their votes, and the closing juror as verifier", async () => {
    const { id, jury } = await setup(3, 2);
    const votes = async () => [...(await am.GetJury(id))[1]].map(Number);
    expect([...(await am.GetJury(id))[0]]).to.deep.equal([]);
    await am.connect(jury[0]).Vote(id, false);
    expect([...(await am.GetJury(id))[0]]).to.deep.equal(jury.map((j) => j.address));
    expect(await votes()).to.deep.equal([1, 0, 0]);
    await am.connect(jury[1]).Vote(id, true);
    await am.connect(jury[2]).Vote(id, true);
    expect(await votes()).to.deep.equal([1, 2, 2]);
    expect((await am.GetAttestationInfo(id))[1]).to.equal(jury[2].address);
  });

  // Mines until the latest block is n, so that the next transaction goes in n + 1.
  async function mineTo(n) {
    const latest = await ethers.provider.getBlockNumber();
    if (n > latest) await ethers.provider.send("hardhat_mine", ["0x" + (n - latest).toString(16)]);
  }

  it("expires a jury cycle 5 blocks after its evidence, not before", async () => {
    const { id, jury } = await setup(3, 2);
    const seed = (await am.queryFilter(am.filters.ReadyForEvaluation(), 0)).find((e) => e.args[0] === id).blockNumber;
    await am.connect(jury[0]).Vote(id, true);
    await mineTo(seed + 3);
    await expect(am.connect(robots[0]).Expire(id)).to.be.revertedWith("Expire : timeout not reached");
    await mineTo(seed + 4);
    await expect(am.connect(robots[0]).Expire(id)).to.emit(am, "AttestationStarted").withArgs(id, 1);
    expect(await am.IsVerifier(id, secaas.address)).to.equal(true);
    await expect(am.connect(jury[1]).Vote(id, true)).to.be.revertedWith("Vote : cycle is with the SECaaS");
    await expect(am.connect(robots[0]).Expire(id)).to.be.revertedWith("Expire : cycle is with the SECaaS");
    await am.connect(secaas).ResolveAttestationSECaaS(id, true);
    expect(await result(id)).to.equal(SUCCESS);
  });

  it("refuses a second round while the prover's round is open", async () => {
    const { id, jury } = await setup(3, 2);
    const again = (k) => am.connect(robots[0]).SendEvidence(ethers.encodeBytes32String(k), REF, 1);
    await expect(again("again")).to.be.revertedWith("SendEvidence : prover has an open round");
    await expect(am.connect(robots[0]).SendEvidence(id, REF, 1)).to.be.revertedWith("SendEvidence : prover has an open round");
    await expect(am.connect(robots[1]).SendEvidence(ethers.encodeBytes32String("other"), REF, 1)).to.emit(am, "ReadyForEvaluation");
    await am.connect(jury[0]).Vote(id, true);
    await am.connect(jury[1]).Vote(id, true);
    await expect(again("after close")).to.emit(am, "ReadyForEvaluation");
  });

  it("rejects an id already used, by the same prover or another", async () => {
    const { id, jury } = await setup(3, 2);
    await am.connect(jury[0]).Vote(id, true);
    await am.connect(jury[1]).Vote(id, true);
    await expect(am.connect(robots[0]).SendEvidence(id, REF, 1)).to.be.revertedWith("SendEvidence : id already used");
    await expect(am.connect(robots[1]).SendEvidence(id, REF, 1)).to.be.revertedWith("SendEvidence : id already used");
  });

  it("lets the prover open a new round once its round is with the SECaaS", async () => {
    const { id } = await setup(3, 2);
    await ethers.provider.send("hardhat_mine", ["0x10"]);
    await am.connect(robots[0]).Expire(id);
    await expect(am.connect(robots[0]).SendEvidence(ethers.encodeBytes32String("next"), REF, 1)).to.emit(am, "ReadyForEvaluation");
  });

  it("does not expire a closed or unknown cycle", async () => {
    const { id, jury } = await setup(3, 2);
    await am.connect(jury[0]).Vote(id, true);
    await am.connect(jury[1]).Vote(id, true);
    await ethers.provider.send("hardhat_mine", ["0x10"]);
    await expect(am.Expire(id)).to.be.revertedWith("Expire : cycle closed");
    await expect(am.Expire(ethers.encodeBytes32String("none"))).to.be.revertedWith("Expire : unknown cycle");
  });

  // True when robot r is drawn in none of 50 juries seeded at random.
  async function neverDrawn(r) {
    for (let k = 0; k < 50; k++) {
      if ((await am.DrawJury(ethers.id(`seed${k}`), robots[0].address)).includes(r.address)) return false;
    }
    return true;
  }

  it("takes the eligibility of a juror that voted against the verdict, until its next success", async () => {
    const { id, jury } = await setup(3, 2);
    await am.connect(jury[0]).Vote(id, false);
    await am.connect(jury[1]).Vote(id, true);
    await expect(am.connect(jury[2]).Vote(id, true)).to.emit(am, "JurorDissent").withArgs(id, jury[0].address);
    expect(await eligible(jury[0])).to.equal(false);
    expect(await eligible(jury[1])).to.equal(true);
    expect(await neverDrawn(jury[0])).to.equal(true);
    await settle(am, secaas, robots, await evidence(jury[0]), true);
    expect(await eligible(jury[0])).to.equal(true);
  });

  it("takes the eligibility of a juror that voted against the SECaaS verdict", async () => {
    const { id, jury } = await setup(2, 2);
    await am.connect(jury[0]).Vote(id, true);
    await am.connect(jury[1]).Vote(id, false); // a tie goes to the SECaaS
    await expect(am.connect(secaas).ResolveAttestationSECaaS(id, false)).to.emit(am, "JurorDissent").withArgs(id, jury[0].address);
    expect(await eligible(jury[0])).to.equal(false);
    expect(await eligible(jury[1])).to.equal(true);
  });

  it("takes the eligibility of the jurors that did not vote before the expiry, until their next success", async () => {
    const { id, jury } = await setup(3, 2);
    await am.connect(jury[0]).Vote(id, true);
    await ethers.provider.send("hardhat_mine", ["0x10"]);
    const tx = am.connect(robots[0]).Expire(id);
    await expect(tx).to.emit(am, "JurorAbsent").withArgs(id, jury[1].address);
    await expect(tx).to.emit(am, "JurorAbsent").withArgs(id, jury[2].address);
    expect(await eligible(jury[0])).to.equal(true);
    for (const j of jury.slice(1)) {
      expect(await eligible(j)).to.equal(false);
      expect(await neverDrawn(j)).to.equal(true);
    }
    await settle(am, secaas, robots, await evidence(jury[1]), true);
    expect(await eligible(jury[1])).to.equal(true);
  });

  it("names the whole jury absent when no one voted", async () => {
    const { id, jury } = await setup(3, 2);
    await ethers.provider.send("hardhat_mine", ["0x10"]);
    await am.connect(robots[0]).Expire(id);
    expect([...(await am.GetJury(id))[0]]).to.deep.equal(jury.map((j) => j.address));
    for (const j of jury) expect(await eligible(j)).to.equal(false);
  });
});
