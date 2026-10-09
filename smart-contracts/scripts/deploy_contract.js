const { ethers } = require("hardhat");
const fs = require("fs");
const path = require("path");

const CONTRACT_NAME = process.env.CONTRACT_NAME || "AttestationManagerRR";

async function main() {
  const [deployer] = await ethers.getSigners();
  console.log("Deployer:", deployer.address);

  const factory = await ethers.getContractFactory(CONTRACT_NAME);

  // AttestationManagerLV_StructDelete requires a VRP constructor argument; all others take none
  let contract;
  if (CONTRACT_NAME === "AttestationManagerLV_StructDelete") {
    const vrp = parseInt(process.env.VRP || "1");
    console.log(`VRP        : ${vrp}`);
    contract = await factory.deploy(vrp, { gasPrice: 0 });
  } else if (CONTRACT_NAME === "AttestationManagerV2") {
    // Admin keys that may write the on-chain references. Never the deployer (SECaaS).
    const { ADMINS, ADMIN_THRESHOLD } = process.env;
    if (!ADMINS || !ADMIN_THRESHOLD) {
      throw new Error("AttestationManagerV2 requires ADMINS (comma-separated addresses) and ADMIN_THRESHOLD");
    }
    console.log(`Admins     : ${ADMINS} (threshold ${ADMIN_THRESHOLD})`);
    contract = await factory.deploy(ADMINS.split(","), parseInt(ADMIN_THRESHOLD), { gasPrice: 0 });
  } else {
    contract = await factory.deploy({ gasPrice: 0 });
  }
  await contract.waitForDeployment();

  const addr = await contract.getAddress();
  console.log(`${CONTRACT_NAME} deployed at:`, addr);

  // Save address with <networkName>-<contractName>.json
  const networkName = hre.network.name;
  const outDir = path.join(__dirname, "..", "deployments");
  fs.mkdirSync(outDir, { recursive: true });
  const outfile = path.join(outDir, `${networkName}-${CONTRACT_NAME}.json`);
  fs.writeFileSync(
    outfile,
    JSON.stringify(
      {
        contract: CONTRACT_NAME,
        address: addr,
        network: networkName,
      },
      null,
      2
    )
  );
  console.log(`Saved to ${outfile}`);
}

main().catch((e) => {
  console.error(e);
  process.exitCode = 1;
});
