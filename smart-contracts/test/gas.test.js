const { expect } = require("chai");
const { ethers } = require("hardhat");

// Gas per function and per cycle, V2 against V3 with a jury of 1, 2 and 3,
// for a swarm of 4 and of 16 agents. A V2 cycle is SendEvidence plus the
// peer verifier's CloseAttestationProcess. A V3 cycle is SendEvidence plus
// one Vote per juror; the votes after the close revert, but on a real chain
// they are mined and paid for, so they are counted too.
const b32 = (s) => ethers.encodeBytes32String(s);
const REF = b32("ref");
const CYCLES = 8; // averaged: the cost of the draw depends on the hash

const used = async (tx) => (await (await tx).wait()).gasUsed;

// A vote sent after the close: mined with status 0 like on Besu.
async function usedReverted(contract, signer, id) {
  const before = await ethers.provider.getBlockNumber();
  const data = contract.interface.encodeFunctionData("Vote", [id, true]);
  await expect(signer.sendTransaction({ to: await contract.getAddress(), data, gasLimit: 500000 })).to.be.reverted;
  const block = await ethers.provider.getBlock(before + 1);
  expect(block.transactions.length).to.equal(1);
  const receipt = await ethers.provider.getTransactionReceipt(block.transactions[0]);
  expect(receipt.status).to.equal(0);
  return receipt.gasUsed;
}

async function deploy(name, args, agents, admin) {
  const am = await (await ethers.getContractFactory(name)).deploy(...args);
  for (const a of agents) {
    await am.connect(a).RegisterAgent("r");
    await am.connect(admin).SetReference(a.address, REF);
  }
  return am;
}

async function measureV2(signers, m) {
  const [secaas, admin, ...rest] = signers;
  const agents = rest.slice(0, m);
  const am = await deploy("AttestationManagerV2", [[admin.address], 1], agents, admin);
  await am.connect(agents[0]).SendEvidence(b32("boot"), REF, 1);
  await am.connect(secaas).ResolveAttestationSECaaS(b32("boot"), true);
  let send = 0n, close = 0n;
  for (let n = 0; n < CYCLES; n++) {
    const prover = agents[1 + (n % (m - 1))];
    const id = b32(`c${n}`);
    send += await used(am.connect(prover).SendEvidence(id, REF, 1));
    const verifier = await (async () => {
      for (const a of agents) if (await am.IsVerifier(id, a.address)) return a;
      throw new Error(`no peer verifier for ${id}`);
    })();
    close += await used(am.connect(verifier).CloseAttestationProcess(id, true));
  }
  return { send: send / BigInt(CYCLES), votes: [close / BigInt(CYCLES)] };
}

async function measureV3(signers, m, size, quorum) {
  const [secaas, admin, ...rest] = signers;
  const agents = rest.slice(0, m);
  const am = await deploy("AttestationManagerV3", [[admin.address], 1, size, quorum, 5], agents, admin);
  // Make every agent eligible with one successful cycle each: the SECaaS
  // closes it while too few agents are eligible for a jury.
  for (const [i, a] of agents.entries()) {
    const id = b32(`boot${i}`);
    await am.connect(a).SendEvidence(id, REF, 1);
    if (await am.IsVerifier(id, secaas.address)) {
      await am.connect(secaas).ResolveAttestationSECaaS(id, true);
      continue;
    }
    await ethers.provider.send("evm_mine", []);
    const jury = await am.PreviewJury(id);
    for (const j of jury.slice(0, quorum)) await am.connect(agents.find((s) => s.address === j)).Vote(id, true);
  }
  expect((await am.GetEligibleAgents()).length).to.equal(m);

  let send = 0n;
  const votes = Array(size).fill(0n);
  for (let n = 0; n < CYCLES; n++) {
    const prover = agents[n % m];
    const id = b32(`c${n}`);
    send += await used(am.connect(prover).SendEvidence(id, REF, 1));
    await ethers.provider.send("evm_mine", []);
    const jury = await am.PreviewJury(id);
    expect(jury.length).to.equal(size);
    for (const [k, j] of jury.entries()) {
      const juror = agents.find((s) => s.address === j);
      votes[k] += k < quorum ? await used(am.connect(juror).Vote(id, true)) : await usedReverted(am, juror, id);
    }
  }
  return { send: send / BigInt(CYCLES), votes: votes.map((v) => v / BigInt(CYCLES)) };
}

describe("Gas per function and per cycle", function () {
  this.timeout(600000);

  it("prints the table, and the gas per cycle does not depend on the number of agents", async () => {
    const signers = await ethers.getSigners();
    const configs = [
      ["V2", (m) => measureV2(signers, m)],
      ["V3 jury 1, threshold 1", (m) => measureV3(signers, m, 1, 1)],
      ["V3 jury 2, threshold 2", (m) => measureV3(signers, m, 2, 2)],
      ["V3 jury 3, threshold 2", (m) => measureV3(signers, m, 3, 2)],
    ];
    const rows = [];
    for (const [name, measure] of configs) {
      const perM = {};
      for (const m of [4, 16]) {
        const r = await measure(m);
        const total = r.send + r.votes.reduce((a, b) => a + b, 0n);
        perM[m] = total;
        rows.push({ contract: name, agents: m, SendEvidence: Number(r.send),
          "verdicts (Close or Vote 1..k)": r.votes.map(Number).join(" / "), "per cycle": Number(total) });
      }
      // Within 5 %: the draw loops a few more times when the pool is small.
      const ratio = Number(perM[16]) / Number(perM[4]);
      expect(ratio, `${name}: ${perM[4]} gas with 4 agents, ${perM[16]} with 16`).to.be.within(0.95, 1.05);
    }
    console.table(rows);
  });
});
