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
