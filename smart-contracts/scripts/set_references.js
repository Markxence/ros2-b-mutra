// Anchors the reference of each robot in AttestationManagerV2 or V3 (CONTRACT_NAME).
// The signer (PRIVATE_KEY) must be an admin of the contract. Run once per
// admin key: the contract applies a reference once enough admins approved it.
const { ethers } = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
  const { CONTRACT_NAME, N_ROBOTS, REF_DIR } = process.env;
  if (!CONTRACT_NAME || !N_ROBOTS || !REF_DIR) throw new Error("CONTRACT_NAME, N_ROBOTS and REF_DIR are required");

  const deployment = path.join(__dirname, "..", "deployments", `${hre.network.name}-${CONTRACT_NAME}.json`);
  const { address } = JSON.parse(fs.readFileSync(deployment));
  const [admin] = await ethers.getSigners();
  const am = await ethers.getContractAt(CONTRACT_NAME, address, admin);

  const txs = [];
  for (let i = 1; i <= parseInt(N_ROBOTS); i++) {
    const ref = JSON.parse(fs.readFileSync(path.join(REF_DIR, `robot${i}.json`)));
    txs.push(await am.SetReference(ref.eth_address, "0x" + ref.combined_hash, { gasPrice: 0 }));
  }
  await Promise.all(txs.map((tx) => tx.wait()));
  console.log(`Admin ${admin.address} approved ${txs.length} references`);
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
