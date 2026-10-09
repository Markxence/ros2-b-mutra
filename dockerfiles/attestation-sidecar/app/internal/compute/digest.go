package compute

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"os"
	"strings"

	"app/internal/utils"
)

// MeasurementSalt derives the per-agent salt from the agent's private key:
// HMAC-SHA256(key, "d-mutra/measurement-salt"). Only the agent and whoever
// enrolled it can compute it.
func MeasurementSalt(privateKeyHex string) ([32]byte, error) {
	var salt [32]byte
	key, err := hex.DecodeString(strings.TrimPrefix(privateKeyHex, "0x"))
	if err != nil || len(key) != 32 {
		return salt, fmt.Errorf("invalid private key for salt derivation (len=%d): %v", len(key), err)
	}
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte("d-mutra/measurement-salt"))
	copy(salt[:], mac.Sum(nil))
	return salt, nil
}

// SaltMeasurement returns SHA256(m || salt). It is what goes on chain in
// place of m, so that the ledger does not reveal which binary an agent runs.
func SaltMeasurement(m, salt [32]byte) [32]byte {
	return RollingHashStep(m, salt)
}

// RollingHash returns the K-iterated chained SHA-256 over a static seed:
//
//	H_1 = m
//	H_i = SHA256( H_{i-1} || m )   for i in 2..K
//
// Concatenation is byte-level (32 + 32 = 64 bytes input to SHA-256), NOT hex
// string concat. Both the prover (which folds K *fresh* measurements via
// RollingHashStep) and the verifier (which folds the static reference K times
// via this function) must agree on this construction. K must be >= 1; K=1
// returns m unchanged (the identity case, equivalent to single-shot mode).
func RollingHash(m [32]byte, k uint32) [32]byte {
	if k <= 1 {
		return m
	}
	h := m
	for i := uint32(2); i <= k; i++ {
		var buf [64]byte
		copy(buf[:32], h[:])
		copy(buf[32:], m[:])
		h = sha256.Sum256(buf[:])
	}
	return h
}

// RollingHashStep advances a rolling-hash accumulator by one step:
//
//	step(prev, mNext) = SHA256( prev || mNext )
//
// Used by the prover to fold K *fresh* measurements together. The first
// measurement is the initial accumulator (H_1 = m_1), each subsequent fresh
// measurement is folded via this function (H_i = step(H_{i-1}, m_i)).
func RollingHashStep(prev, mNext [32]byte) [32]byte {
	var buf [64]byte
	copy(buf[:32], prev[:])
	copy(buf[32:], mNext[:])
	return sha256.Sum256(buf[:])
}

// RollingHashHex is RollingHash with hex string I/O. Accepts the seed with or
// without "0x" prefix; returns the result as bare hex (no prefix).
func RollingHashHex(mHex string, k uint32) (string, error) {
	mHex = strings.TrimPrefix(strings.ToLower(mHex), "0x")
	raw, err := hex.DecodeString(mHex)
	if err != nil {
		return "", fmt.Errorf("invalid hex seed: %w", err)
	}
	if len(raw) != 32 {
		return "", fmt.Errorf("rolling hash seed must be 32 bytes, got %d", len(raw))
	}
	var m [32]byte
	copy(m[:], raw)
	out := RollingHash(m, k)
	return hex.EncodeToString(out[:]), nil
}

// AttestationDigest is the triplet a prover would submit on-chain
// (combined_hash) along with its constituents, useful for bootstrapping
// reference measurements before deployment.
type AttestationDigest struct {
	RobotHash    string `json:"robot_hash"`
	SidecarHash  string `json:"attestation_sidecar_hash"`
	LibsHash     string `json:"libs_hash,omitempty"`
	CombinedHash string `json:"combined_hash"`
}

// SelfIntegrityParams mirrors config.SelfIntegrityConfig but kept here to
// avoid importing config (which would create an import cycle once prover.go
// is refactored to use this helper).
type SelfIntegrityParams struct {
	Enabled           bool
	CmdName           string
	TextSectionSize   int
	TextSectionOffset int
}

// ComputeAttestationDigest performs a single measurement cycle and returns
// the same triplet the prover would submit. It mirrors the body of
// RunProverLogicContinuousMode's per-cycle computation (sha256 over hex
// concatenation of robot/sidecar/libs digests). Kept as a standalone helper
// so the /digest endpoint and the prover loop stay bit-for-bit consistent.
func ComputeAttestationDigest(
	cmdName string,
	textSectionSize int,
	offset int,
	textSectionPrefix string,
	si SelfIntegrityParams,
) (AttestationDigest, error) {
	var out AttestationDigest

	var (
		robotHash string
		err       error
	)
	if textSectionPrefix != "" {
		robotHash, err = ComputeProgramHashWithPrefix(cmdName, textSectionSize, textSectionPrefix)
	} else {
		robotHash, err = ComputeProgramHash(cmdName, textSectionSize, offset)
	}
	if err != nil {
		return out, err
	}
	out.RobotHash = robotHash
	out.CombinedHash = robotHash

	if si.Enabled {
		sidecarHash, err := ComputeProgramHash(si.CmdName, si.TextSectionSize, si.TextSectionOffset)
		if err != nil {
			return out, err
		}
		out.SidecarHash = sidecarHash
		sum := sha256.Sum256([]byte(robotHash + sidecarHash))
		out.CombinedHash = hex.EncodeToString(sum[:])
	}

	if utils.GetEnvBool("ENABLE_LIBS_HASH", false) {
		exePath, err := os.Readlink("/proc/self/exe")
		if err == nil {
			libs, err := ComputeLoadedLibrariesHash(os.Getpid(), exePath)
			if err == nil && libs.Aggregate != "" {
				out.LibsHash = libs.Aggregate
				sum := sha256.Sum256([]byte(robotHash + out.SidecarHash + libs.Aggregate))
				out.CombinedHash = hex.EncodeToString(sum[:])
			}
		}
	}

	return out, nil
}
