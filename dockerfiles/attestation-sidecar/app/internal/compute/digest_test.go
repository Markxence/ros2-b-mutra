package compute

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// TestRollingHashVectors checks that compute.RollingHashHex matches the shared
// fixtures in tests/rolling-hash-vectors.json. The same vectors are consumed
// by the Python helper test in dockerfiles/secaas/tests/test_rolling_hash.py;
// keeping them in sync is the whole point of the file.
//
// If this test fails, EITHER the Go implementation drifted from the
// construction H_i = SHA256(H_{i-1} || m), OR the fixtures were regenerated
// from a different reference. Do not "fix" by editing the JSON unless you
// have also verified the Python side still passes against the new values.
func TestRollingHashVectors(t *testing.T) {
	path := fixturesPath(t)
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read fixtures: %v", err)
	}

	var doc struct {
		Vectors []struct {
			Name     string `json:"name"`
			Seed     string `json:"seed"`
			K        uint32 `json:"k"`
			Expected string `json:"expected"`
		} `json:"vectors"`
	}
	if err := json.Unmarshal(data, &doc); err != nil {
		t.Fatalf("parse fixtures: %v", err)
	}
	if len(doc.Vectors) == 0 {
		t.Fatalf("no vectors in %s", path)
	}

	for _, v := range doc.Vectors {
		v := v
		t.Run(v.Name, func(t *testing.T) {
			got, err := RollingHashHex(v.Seed, v.K)
			if err != nil {
				t.Fatalf("RollingHashHex(%s, %d): %v", v.Seed, v.K, err)
			}
			if got != v.Expected {
				t.Errorf("K=%d\n  got      %s\n  expected %s", v.K, got, v.Expected)
			}
		})
	}
}

// TestRollingHashStep_FirstMeasurementIsIdentity asserts that for K=1 the
// rolling-hash accumulator equals the first measurement unchanged. This is
// the rétro-compat invariant: K=1 must behave exactly like single-shot mode.
func TestRollingHashStep_FirstMeasurementIsIdentity(t *testing.T) {
	var m [32]byte
	raw, _ := hex.DecodeString("8c55173fa6a85295a83378cc84dc18baf8810ee21a0d0013b77d0fbd904ebffe")
	copy(m[:], raw)

	out := RollingHash(m, 1)
	if out != m {
		t.Errorf("K=1 should be identity, got %x", out)
	}
}

// TestRollingHashStep_TwoStep verifies that RollingHash(m, 2) equals
// RollingHashStep(m, m). Critical for the prover, which folds K *fresh*
// measurements via repeated calls to RollingHashStep starting from m_1.
func TestRollingHashStep_TwoStep(t *testing.T) {
	var m [32]byte
	raw, _ := hex.DecodeString("8c55173fa6a85295a83378cc84dc18baf8810ee21a0d0013b77d0fbd904ebffe")
	copy(m[:], raw)

	chained := RollingHash(m, 2)
	stepped := RollingHashStep(m, m)
	if chained != stepped {
		t.Errorf("RollingHash(m, 2) %x != RollingHashStep(m, m) %x", chained, stepped)
	}
}

// TestRollingHashHex_AcceptsPrefix accepts both bare hex and "0x"-prefixed
// hex inputs, because the on-chain return from GetAttestationSignatures
// comes through Web3.to_hex / hex.EncodeToString with varying conventions.
func TestRollingHashHex_AcceptsPrefix(t *testing.T) {
	a, _ := RollingHashHex("8c55173fa6a85295a83378cc84dc18baf8810ee21a0d0013b77d0fbd904ebffe", 3)
	b, _ := RollingHashHex("0x8c55173fa6a85295a83378cc84dc18baf8810ee21a0d0013b77d0fbd904ebffe", 3)
	if a != b {
		t.Errorf("0x prefix changed result: %s vs %s", a, b)
	}
}

// Vector produced by bootstrap_ref_measurements.py (Python hmac/hashlib), so
// the enrolled reference and the prover's value cannot drift apart.
func TestSaltMeasurement_MatchesEnrollment(t *testing.T) {
	salt, err := MeasurementSalt("0x" + strings.Repeat("11", 32))
	if err != nil {
		t.Fatal(err)
	}
	if got := hex.EncodeToString(salt[:]); got != "8721e98bfbe4b380ede56943250f828d702604dc83801d1107d203c87539069b" {
		t.Fatalf("salt = %s", got)
	}
	var m [32]byte
	for i := range m {
		m[i] = 0x22
	}
	salted := SaltMeasurement(m, salt)
	if got := hex.EncodeToString(salted[:]); got != "da248c6e878433597910d57eaf5dbc1168d9b923646b1203f7332e4527d81e3b" {
		t.Fatalf("salted = %s", got)
	}
	if _, err := MeasurementSalt("0x1234"); err == nil {
		t.Fatal("expected an error for a short key")
	}
}

// fixturesPath resolves tests/rolling-hash-vectors.json relative to this
// source file rather than to CWD, so `go test ./...` works from anywhere.
func fixturesPath(t *testing.T) string {
	t.Helper()
	_, file, _, ok := runtime.Caller(0)
	if !ok {
		t.Fatal("cannot resolve runtime caller")
	}
	// .../app/internal/compute/digest_test.go → repo root
	return filepath.Join(filepath.Dir(file), "..", "..", "..", "..", "..", "tests", "rolling-hash-vectors.json")
}
