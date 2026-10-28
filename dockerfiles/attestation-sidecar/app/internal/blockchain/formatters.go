package blockchain

import (
	"bytes"
	"fmt"
	"math/big"
	"strings"

	"github.com/ethereum/go-ethereum/common"
	"github.com/olekukonko/tablewriter"
)

// Function types to match the Client methods
type GetAttestationInfoFunc func(string) (common.Address, common.Address, uint8, *big.Int, error)
type GetAgentInfoFunc func(common.Address) (string, bool, *big.Int, error)
type JuryVotesFunc func(string) (string, error)

// BuildAttestationChainTable generates a formatted ASCII table string for the chain.
func BuildAttestationChainTable(
	attestationChain [][32]byte,
	getAttestationInfo GetAttestationInfoFunc,
	getAgentInfo GetAgentInfoFunc,
	juryVotes JuryVotesFunc,
	secaasAddressStr string,
	lastN int,
) string {
	// 1. Handle Empty Chain
	if len(attestationChain) == 0 {
		// Return a simple string or a table with one row, matching Python behavior
		var buf bytes.Buffer
		table := tablewriter.NewWriter(&buf)
		table.SetHeader([]string{"info"})
		table.Append([]string{"Attestation chain is empty"})
		table.Render()
		return buf.String()
	}

	// 2. Slice the last N items
	start := 0
	if len(attestationChain) > lastN {
		start = len(attestationChain) - lastN
	}
	recentChain := attestationChain[start:]

	// 3. Setup Table
	var buf bytes.Buffer
	table := tablewriter.NewWriter(&buf)
	table.SetHeader([]string{"#", "Attestation ID", "Prover", "Verifier", "Result", "Timestamp"})
	table.SetAlignment(tablewriter.ALIGN_LEFT)
	table.SetBorder(true) // Match PrettyTable's default look

	// Parse SECaaS address for comparison
	secaasAddr := common.HexToAddress(secaasAddressStr)
	if secaasAddressStr == "" {
		// Default from Python code
		secaasAddr = common.HexToAddress("0xed9d02e382b34818e88b88a309c7fe71e65f419d")
	}

	// 4. Iterate and Build Rows
	for i, idBytes := range recentChain {
		// Decode bytes32 to string and strip null bytes
		attID := strings.TrimRight(string(idBytes[:]), "\x00")

		// Fetch Info
		proverAddr, verifierAddr, attestationResult, timestamp, err := getAttestationInfo(attID)
		
		// If fetch fails, we render an error row to avoid crashing the whole table
		if err != nil {
			table.Append([]string{
				fmt.Sprintf("%d", i+1),
				attID,
				"ERROR",
				"ERROR",
				"UNKNOWN",
				"UNKNOWN",
			})
			continue
		}

		// Status Logic
		status := "❌ FAILURE"
		if attestationResult == 2 {
			status = "✅ SUCCESS"
		}

		// Prover Info
		proverName, _, _, _ := getAgentInfo(proverAddr)
		proverInfo := fmt.Sprintf("%s - %s", proverName, proverAddr.Hex())

		// Verifier Info
		verifierName := ""
		if verifierAddr == (common.Address{}) {
			// V3: a jury cycle has no single verifier
			votes, err := juryVotes(attID)
			if err != nil {
				votes = fmt.Sprintf("ERROR: %v", err)
			}
			verifierName = "jury: " + votes
		} else if verifierAddr == secaasAddr {
			verifierName = "SECaaS"
		} else {
			vName, _, _, _ := getAgentInfo(verifierAddr)
			verifierName = vName
		}
		verifierInfo := fmt.Sprintf("%s - %s", verifierName, verifierAddr.Hex())

		// Add Row
		table.Append([]string{
			fmt.Sprintf("%d", i+1),
			attID,
			proverInfo,
			verifierInfo,
			status,
			timestamp.String(),
		})
	}

	table.Render()
	return buf.String()
}