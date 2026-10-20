package blockchain

import (
	"strings"
	"testing"

	"github.com/ethereum/go-ethereum/accounts/abi"
	"github.com/ethereum/go-ethereum/common"
)

func TestUnpackJury(t *testing.T) {
	const def = `[{"type":"function","name":"PreviewJury","stateMutability":"view",
		"inputs":[{"name":"id","type":"bytes32"}],"outputs":[{"name":"","type":"address[]"}]}]`
	parsed, err := abi.JSON(strings.NewReader(def))
	if err != nil {
		t.Fatal(err)
	}
	want := []common.Address{common.HexToAddress("0x01"), common.HexToAddress("0x02")}
	res, err := parsed.Methods["PreviewJury"].Outputs.Pack(want)
	if err != nil {
		t.Fatal(err)
	}
	got, err := unpackJury(parsed, res)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 || got[0] != want[0] || got[1] != want[1] {
		t.Fatalf("got %v, want %v", got, want)
	}
}
