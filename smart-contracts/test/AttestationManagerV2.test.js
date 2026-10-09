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

describe("AttestationManagerV2: the verifier gets the anchored reference", function () {
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

  it("ignores the reference sent by the SECaaS", async () => {
    // First round: no peer is eligible yet, the SECaaS verifies robot A.
    await am.connect(robotA).SendEvidence(b32("boot"), REF, 1);
    await am.connect(secaas).ResolveAttestationSECaaS(b32("boot"), true);

    await am.connect(robotB).SendEvidence(b32("id"), REF, 1);
    await am.connect(secaas).SendRefSignature(b32("id"), b32("forged"));
    const [, ref] = await am.GetAttestationSignatures(b32("id"), robotA.address);
    expect(ref).to.equal(REF);
  });
});
