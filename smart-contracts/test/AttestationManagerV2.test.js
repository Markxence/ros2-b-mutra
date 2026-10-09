const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("AttestationManagerV2: admin keys", function () {
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
});
