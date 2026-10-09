const { expect } = require("chai");
const { ethers } = require("hardhat");

const ZERO_BYTES32 = "0x" + "00".repeat(32);
const b32 = (s) => ethers.encodeBytes32String(s);

describe("AttestationManagerV2: anchored reference", function () {
  let secaas, a1, a2, a3, robot, outsider, AM, am;

  beforeEach(async () => {
    [secaas, a1, a2, a3, robot, outsider] = await ethers.getSigners();
    AM = await ethers.getContractFactory("AttestationManagerV2", secaas);
    am = await AM.deploy([a1.address, a2.address, a3.address], 2);
    await am.waitForDeployment();
  });

  it("rejects the SECaaS as admin, a duplicate admin and a bad threshold", async () => {
    await expect(AM.deploy([secaas.address], 1)).to.be.revertedWith("Admin must differ from the SECaaS");
    await expect(AM.deploy([a1.address, a1.address], 1)).to.be.revertedWith("Duplicate admin");
    await expect(AM.deploy([a1.address], 2)).to.be.revertedWith("Invalid admin threshold");
    await expect(AM.deploy([a1.address], 0)).to.be.revertedWith("Invalid admin threshold");
  });

  it("rejects SetReference from the SECaaS, an agent or an outsider", async () => {
    for (const who of [secaas, robot, outsider]) {
      await expect(am.connect(who).SetReference(robot.address, b32("ref")))
        .to.be.revertedWith("SetReference : caller is not an admin");
    }
  });

  it("changes the reference only after 2 of 3 admins approve the same value", async () => {
    await expect(am.connect(a1).SetReference(robot.address, b32("ref")))
      .to.emit(am, "ReferenceApproved").withArgs(robot.address, b32("ref"), a1.address)
      .and.not.to.emit(am, "ReferenceSet");
    expect(await am.referenceOf(robot.address)).to.equal(ZERO_BYTES32);

    // A different value from another admin does not complete the first proposal.
    await am.connect(a2).SetReference(robot.address, b32("other"));
    expect(await am.referenceOf(robot.address)).to.equal(ZERO_BYTES32);

    await expect(am.connect(a3).SetReference(robot.address, b32("ref")))
      .to.emit(am, "ReferenceSet").withArgs(robot.address, ZERO_BYTES32, b32("ref"), 1);
    expect(await am.referenceOf(robot.address)).to.equal(b32("ref"));
  });

  it("does not count the same admin twice", async () => {
    await am.connect(a1).SetReference(robot.address, b32("ref"));
    await expect(am.connect(a1).SetReference(robot.address, b32("ref")))
      .to.be.revertedWith("SetReference : already approved by this admin");
  });

  it("does not let a stale approval count after the reference changed", async () => {
    await am.connect(a1).SetReference(robot.address, b32("evil")); // 1 of 2, left pending
    await am.connect(a1).SetReference(robot.address, b32("v1"));
    await am.connect(a2).SetReference(robot.address, b32("v1"));  // version 1
    // One more approval of "evil" must not be enough: the pending one is void.
    await am.connect(a2).SetReference(robot.address, b32("evil"));
    expect(await am.referenceOf(robot.address)).to.equal(b32("v1"));
  });
});

// Brings a contract to the point where robot A is an eligible peer verifier:
// A's first attestation has no peer available, so the SECaaS resolves it.
async function makePeerVerifier(am, secaas, robotA, ref) {
  await am.connect(robotA).SendEvidence(b32("boot"), ref, 1);
  await am.connect(secaas).ResolveAttestationSECaaS(b32("boot"), true);
}

const gasOf = async (txPromise) => (await (await txPromise).wait()).gasUsed;

describe("AttestationManagerV2: two-transaction cycle", function () {
  let secaas, a1, robotA, robotB, am;
  const REF = b32("ref");

  beforeEach(async () => {
    [secaas, a1, robotA, robotB] = await ethers.getSigners();
    const AM = await ethers.getContractFactory("AttestationManagerV2", secaas);
    am = await AM.deploy([a1.address], 1);
    await am.waitForDeployment();
    for (const r of [robotA, robotB]) {
      await am.connect(r).RegisterAgent("r");
      await am.connect(a1).SetReference(r.address, REF);
    }
  });

  it("rejects evidence from an agent without an anchored reference", async () => {
    const [, , , , robotC] = await ethers.getSigners();
    await am.connect(robotC).RegisterAgent("c");
    await expect(am.connect(robotC).SendEvidence(b32("x"), REF, 1))
      .to.be.revertedWith("SendEvidence : no reference anchored for this agent");
  });

  it("notifies the SECaaS only when it is the elected verifier", async () => {
    await expect(am.connect(robotA).SendEvidence(b32("boot"), REF, 1))
      .to.emit(am, "AttestationStarted").withArgs(b32("boot"), 1)
      .and.not.to.emit(am, "ReadyForEvaluation");
    await am.connect(secaas).ResolveAttestationSECaaS(b32("boot"), true);

    await expect(am.connect(robotB).SendEvidence(b32("id"), REF, 1))
      .to.emit(am, "ReadyForEvaluation").withArgs(b32("id"))
      .and.not.to.emit(am, "AttestationStarted");
  });

  it("closes a peer-verified cycle in two transactions, with the anchored reference", async () => {
    await makePeerVerifier(am, secaas, robotA, REF);
    await am.connect(robotB).SendEvidence(b32("id"), REF, 3);

    const [fresh, ref, k] = await am.GetAttestationSignatures(b32("id"), robotA.address);
    expect([fresh, ref, k]).to.deep.equal([REF, REF, 3n]);

    await expect(am.connect(robotA).CloseAttestationProcess(b32("id"), true))
      .to.emit(am, "AttestationCompleted").withArgs(b32("id"));
    const [prover, verifier, result] = await am.GetAttestationInfo(b32("id"));
    expect([prover, verifier, result]).to.deep.equal([robotB.address, robotA.address, 2n]);
  });

  it("uses a reference changed by the admin from the next cycle on", async () => {
    await makePeerVerifier(am, secaas, robotA, REF);
    await am.connect(a1).SetReference(robotB.address, b32("new"));
    await am.connect(robotB).SendEvidence(b32("id"), REF, 1);
    const [, ref] = await am.GetAttestationSignatures(b32("id"), robotA.address);
    expect(ref).to.equal(b32("new"));
  });

  it("costs less gas per peer-verified cycle than LV", async () => {
    const LV = await ethers.getContractFactory("AttestationManagerLV", secaas);
    const lv = await LV.deploy();
    await lv.waitForDeployment();
    for (const r of [robotA, robotB]) await lv.connect(r).RegisterAgent("r");
    await makePeerVerifier(lv, secaas, robotA, REF);
    await makePeerVerifier(am, secaas, robotA, REF);

    const lvGas =
      (await gasOf(lv.connect(robotB).SendEvidence(b32("id"), REF, 1))) +
      (await gasOf(lv.connect(secaas).SendRefSignature(b32("id"), REF))) +
      (await gasOf(lv.connect(robotA).CloseAttestationProcess(b32("id"), true)));
    const v2Gas =
      (await gasOf(am.connect(robotB).SendEvidence(b32("id"), REF, 1))) +
      (await gasOf(am.connect(robotA).CloseAttestationProcess(b32("id"), true)));

    console.log(`      gas per cycle: LV ${lvGas} (3 tx), V2 ${v2Gas} (2 tx), ${(100n * (lvGas - v2Gas)) / lvGas}% less`);
    expect(v2Gas).to.be.lessThan(lvGas);
  });
});
