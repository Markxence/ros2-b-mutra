// Anchors the reference of each robot in AttestationManagerV2.
// The signer (PRIVATE_KEY) must be an admin of the contract. Run once per
// admin key: the contract applies a reference once enough admins approved it.
const { ethers } = require("hardhat");
const fs = require("fs");
const path = require("path");

async function main() {
  const { N_ROBOTS, REF_DIR } = process.env;
  if (!N_ROBOTS || !REF_DIR) throw new Error("N_ROBOTS and REF_DIR are required");

  const deployment = path.join(__dirname, "..", "deployments", `${hre.network.name}-AttestationManagerV2.json`);
  const { address } = JSON.parse(fs.readFileSync(deployment));
  const [admin] = await ethers.getSigners();
  const am = await ethers.getContractAt("AttestationManagerV2", address, admin);

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
