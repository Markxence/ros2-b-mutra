package attestation

import (
    "crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"sync"
	"time"

	"app/internal/blockchain"
	"app/internal/compute"
	"app/internal/logger"
	// "app/internal/metrics"
	"app/internal/storage"
	"app/internal/utils"
)

// SelfIntegrityInfo holds self-integrity check configuration
type SelfIntegrityInfo struct {
	Enabled           bool
	CmdName           string
	TextSectionSize   int
	TextSectionOffset int
	TextSectionPrefix []byte
	ExpectedSHA256    string
}

// ProverConfig holds configuration for the prover
type ProverConfig struct {
	CmdName           string
	TextSectionSize   int
	Offset            int
	TextSectionPrefix string // Hex string of first N bytes of .text section
	Threshold         int    // Kept for config compatibility, but logic uses interval
	IterQThreshold    uint32 // K = fresh measurements folded per on-chain attestation
	Salt              *[32]byte // nil = unsalted measurements (contracts LV and RR)
	AgentName         string
	ExportEnabled     bool
	ResultsDir        string
	ResultsFile       string
	MemoryStorageFile string
	SelfIntegrity     SelfIntegrityInfo
	OneShot                    bool
	WaitForTxConfirmations     bool
	WaitForVerificationResult  bool
}

// ProverState holds the runtime state of the prover
type ProverState struct {
	Digests      []string
	DigestsCount int
	FinalDigest  string
	Measuring    bool
	mu           sync.Mutex
}

// ProcessProverAttestation processes a single attestation as prover
func ProcessProverAttestation(
	bc *blockchain.BlockchainClient,
	attestationID string,
	measurement string,
	iterCount uint32,
	stopCh <-chan struct{},
	exportEnabled bool,
	participantName string,
	resultsDir string,
	resultsFile string,
	startTs int64,
	startP int64,
	waitForTx bool,
	waitForResult bool,
) {
	timestamps := make(map[string]interface{})

	// 1. Capture Prover Start (from the loop's start time)
	timestamps["prover_start"] = startTs
	timestamps["p_prover_start"] = startP

	logPrefix := fmt.Sprintf("[Prover-%s]", utils.ShortAttID(attestationID, 4))

	// 2. Send Evidence
	timestamps["evidence_sent"] = utils.NowMs()
	timestamps["p_send_evidence_start"] = utils.PerfNs()

	// Send evidence with wait=true and timeout=60s. iterCount carries K for
	// the on-chain record so the verifier can re-derive the chained reference.
	_, err := bc.SendEvidence(attestationID, measurement, iterCount, waitForTx, 60)

	timestamps["p_send_evidence_finished"] = utils.PerfNs()

	// Only record confirmation if we actually waited for it!
    if waitForTx {
        timestamps["p_send_evidence_finished_tx_confirmed"] = timestamps["p_send_evidence_finished"]
        timestamps["evidence_sent_tx_confirmed"] = utils.NowMs()
    }

	if err != nil {
		logger.Error("%s Failed to send evidence: %v", logPrefix, err)
		return
	}

	logger.Info("%s Evidence sent (attestation started)", logPrefix)

	if !waitForResult {
		timestamps["p_prover_finished"] = utils.PerfNs()
		timestamps["prover_finished"] = utils.NowMs()
		if exportEnabled {
			utils.ExportAttestationTimesJSON(participantName, attestationID, "prover", timestamps, resultsDir, resultsFile)
		}
		return
	}

	// 3. Wait to become Prover (Poll Loop)
	//
	// Two exit conditions:
	//   a) IsProver returns true  → normal path (Open state, we are the elected prover)
	//   b) IsProver returns ErrAttestationClosed → SECaaS resolved atomically
	//      (startup/one-shot mode where lastSuccess=0 so SECaaS is always verifier).
	//      In this case the attestation is already Closed; step 4 will immediately
	//      confirm Closed and we proceed to read the result.  Without this check the
	//      loop spins for the full 60 s timeout because IsProver always returns
	//      (false, nil) for Closed attestations.
	startWait := time.Now()
	timeout := 60 * time.Second

	for {
		isProver, err := bc.IsProver(attestationID)
		if err == nil && isProver {
			break
		}
		if errors.Is(err, blockchain.ErrAttestationClosed) {
			logger.Info("%s Attestation already closed (SECaaS resolved atomically); proceeding to result retrieval.", logPrefix)
			break
		}

		select {
		case <-stopCh:
			logger.Info("%s Stopping while waiting to become prover.", logPrefix)
			return
		default:
		}

		if time.Since(startWait) > timeout {
			logger.Error("%s Timeout while waiting to become prover.", logPrefix)
			return
		}
		time.Sleep(proverPollInterval())
	}

	logger.Info("%s Waiting for verification result...", logPrefix)

	// 4. Wait for Attestation to Close
	startWait = time.Now()
	for {
		state, err := bc.GetAttestationState(attestationID)
		if err == nil && state == blockchain.Closed {
			break
		}

		select {
		case <-stopCh:
			logger.Info("%s Stopping while waiting for attestation to close.", logPrefix)
			return
		default:
		}

		if time.Since(startWait) > timeout {
			logger.Error("%s Timeout while waiting for attestation to close.", logPrefix)
			return
		}
		time.Sleep(proverPollInterval())
	}

	timestamps["p_result_received"] = utils.PerfNs()
	timestamps["result_received"] = utils.NowMs()

	// 5. Retrieve Result
	_, verifier, resultEnum, ts, err := bc.GetAttestationInfo(attestationID)
	if err != nil {
		logger.Error("%s Failed to read attestation info: %v", logPrefix, err)
	} else {
		// Enum: 0=None, 1=Failure, 2=Success
		isSuccess := (resultEnum == 2)
		statusIcon := "❌ FAILURE"
		if isSuccess {
			statusIcon = "✅ SUCCESS"
		}
		logger.Info("%s Attestation Complete", logPrefix)
		logger.Info("%s Result: %s", logPrefix, statusIcon)
		logger.Info("%s Verified by: %s at %v", logPrefix, verifier.Hex(), ts)
	}

	timestamps["p_prover_finished"] = utils.PerfNs()
	timestamps["prover_finished"] = utils.NowMs()

	// 6. Export Results
	if exportEnabled {
		utils.ExportAttestationTimesJSON(
			participantName,
			attestationID,
			"prover",
			timestamps,
			resultsDir,
			resultsFile, // result file path handled by helper if empty
		)
	}
}

// RunProverAndCleanup runs the prover and cleans up
func RunProverAndCleanup(
	bc *blockchain.BlockchainClient,
	attestationID string,
	measurement string,
	startTs int64,
	startP int64,
	stopCh <-chan struct{},
	config ProverConfig,
	activeAttestations *sync.Map,
) {
	start := time.Now()
	logPrefix := fmt.Sprintf("[Prover-%s]", utils.ShortAttID(attestationID, 4))

	defer func() {
		elapsed := time.Since(start)
		logger.Info(logger.GreenText(fmt.Sprintf("%s completed in %.2fs", logPrefix, elapsed.Seconds())))
		activeAttestations.Delete(attestationID)
	}()

	ProcessProverAttestation(
		bc, attestationID, measurement, config.IterQThreshold, stopCh,
		config.ExportEnabled, config.AgentName, config.ResultsDir, config.ResultsFile,
		startTs, startP, config.WaitForTxConfirmations, config.WaitForVerificationResult,
	)
}

// measureCombinedOnce performs a single integrity measurement cycle and
// returns the combined digest as a hex string (the same "fresh measurement
// m_i" that, in K=1 mode, would be sent as-is on-chain). Kept identical to
// the optimized variant's helper for ABI/result parity.
func measureCombinedOnce(config ProverConfig) (combinedDigest, robotHash, sidecarHash, libsHash string, err error) {
	if config.TextSectionPrefix != "" {
		robotHash, err = compute.ComputeProgramHashWithPrefix(
			config.CmdName,
			config.TextSectionSize,
			config.TextSectionPrefix,
		)
	} else {
		robotHash, err = compute.ComputeProgramHash(
			config.CmdName,
			config.TextSectionSize,
			config.Offset,
		)
	}
	if err != nil {
		return "", "", "", "", err
	}
	combinedDigest = robotHash

	if config.SelfIntegrity.Enabled {
		sidecarHash, err = compute.ComputeProgramHash(
			config.SelfIntegrity.CmdName,
			config.SelfIntegrity.TextSectionSize,
			config.SelfIntegrity.TextSectionOffset,
		)
		if err != nil {
			return "", robotHash, "", "", err
		}
		if sidecarHash != config.SelfIntegrity.ExpectedSHA256 {
			logger.Warn("[Prover] ⚠️ Self-integrity mismatch!")
		}
		sum := sha256.Sum256([]byte(robotHash + sidecarHash))
		combinedDigest = hex.EncodeToString(sum[:])
	}

	if utils.GetEnvBool("ENABLE_LIBS_HASH", false) {
		exePath, e := os.Readlink("/proc/self/exe")
		if e == nil {
			libs, e2 := compute.ComputeLoadedLibrariesHash(os.Getpid(), exePath)
			if e2 == nil && libs.Aggregate != "" {
				libsHash = libs.Aggregate
				sum := sha256.Sum256([]byte(robotHash + sidecarHash + libsHash))
				combinedDigest = hex.EncodeToString(sum[:])
			}
		}
		expected := utils.GetEnvStr("EXPECTED_LIBS_HASH", "")
		if expected != "" && libsHash != expected {
			logger.Warn("[Prover] ⚠️ Loaded libs hash mismatch!")
		}
	}
	return combinedDigest, robotHash, sidecarHash, libsHash, nil
}

// RunProverLogicContinuousMode runs the continuous prover loop.
//
// Per cycle: takes K = config.IterQThreshold fresh measurements spaced by
// ATTESTATION_INTERVAL_MS, folds them into a single rolling hash H_K via
// byte-level chained SHA-256 (compute.RollingHashStep), and submits one
// SendEvidence(id, H_K, K) on-chain. K=1 reduces to single-shot.
func RunProverLogicContinuousMode(
	stopCh <-chan struct{},
	bc *blockchain.BlockchainClient,
	store storage.StorageBackend,
	config ProverConfig,
	activeAttestations *sync.Map,
	wg *sync.WaitGroup,
) {
	defer wg.Done()

	K := config.IterQThreshold
	if K < 1 {
		K = 1
	}
	logger.Info("Starting attestation process in continuous mode (IterQ K=%d)...", K)

	state := &ProverState{
		Digests:   make([]string, 0),
		Measuring: true,
	}

	for {
		select {
		case <-stopCh:
			logger.Info("Attestation process stopped")
			return
		default:
		}

		if !state.Measuring {
			time.Sleep(25 * time.Millisecond)
			continue
		}

		batchStartTs := utils.NowMs()
		batchStartP := utils.PerfNs()

		// Fold K fresh measurements into a rolling-hash accumulator.
		var H [32]byte
		var lastCombined, lastRobotHash, lastSidecarHash, lastLibsHash string
		failed := false
		for i := uint32(1); i <= K; i++ {
			if i > 1 {
				select {
				case <-stopCh:
					logger.Info("Attestation process stopped")
					return
				case <-time.After(attestationInterval()):
				}
			}

			combined, robotHash, sidecarHash, libsHash, err := measureCombinedOnce(config)
			if err != nil {
				if compute.IsComputeHashError(err) {
					logger.Debug("Hash computation error at iter %d/%d: %v", i, K, err)
				} else {
					logger.Error("Error in attestation process at iter %d/%d: %v", i, K, err)
				}
				failed = true
				break
			}
			lastCombined, lastRobotHash, lastSidecarHash, lastLibsHash = combined, robotHash, sidecarHash, libsHash

			raw, err := hex.DecodeString(combined)
			if err != nil || len(raw) != 32 {
				logger.Error("Invalid combined digest at iter %d/%d (len=%d): %v", i, K, len(raw), err)
				failed = true
				break
			}
			var m [32]byte
			copy(m[:], raw)
			if config.Salt != nil {
				m = compute.SaltMeasurement(m, *config.Salt)
			}

			if i == 1 {
				H = m
			} else {
				H = compute.RollingHashStep(H, m)
			}
			logger.Debug("[Prover] iter %d/%d: m=%s...%s", i, K, combined[:16], combined[len(combined)-16:])
		}
		if failed {
			time.Sleep(1 * time.Second)
			continue
		}

		select {
		case <-stopCh:
			logger.Info("Attestation process stopped")
			return
		default:
		}

		freshSig := hex.EncodeToString(H[:])
		state.FinalDigest = freshSig
		if K == 1 {
			logger.Info("[Prover] Final digest (K=1): %s...%s",
				freshSig[:16], freshSig[len(freshSig)-16:])
		} else {
			logger.Info("[Prover] Rolling-hash digest (K=%d): %s...%s",
				K, freshSig[:16], freshSig[len(freshSig)-16:])
		}

		store.PushFinalDigest("final_digests:"+config.AgentName, map[string]interface{}{
			"final_digest":    lastCombined,
			"sidecar_hash":    lastSidecarHash,
			"libs_hash":       lastLibsHash,
			"robot_hash":      lastRobotHash,
			"combined_digest": freshSig,
			"iter_count":      K,
			"timestamp":       float64(time.Now().Unix()),
		})

		attestationID := utils.GenerateAttestationID()

		wg.Add(1)
		go func(attID, evidence string, startTs, startP int64) {
			defer wg.Done()
			RunProverAndCleanup(
				bc, attID, evidence, startTs, startP, stopCh, config, activeAttestations,
			)
		}(attestationID, freshSig, batchStartTs, batchStartP)

		activeAttestations.Store(attestationID, true)

		if memStor, ok := store.(*storage.MemoryStorageBackend); ok && config.MemoryStorageFile != "" {
			memStor.ExportToFile(config.MemoryStorageFile)
		}

		if config.OneShot {
			logger.Info("One-shot attestation completed; stopping prover loop")
			return
		}

		// Inter-cycle sleep — the period between m_K of this cycle and m_1 of
		// the next equals the SSP exactly, with a 500ms floor that only applies
		// when SSP < 500ms (e.g. SSP=0 back-to-back mode) so consecutive
		// SendEvidence submissions stay spaced enough to avoid prover-account
		// nonce races. For SSP >= 500ms the cycle period is precisely the SSP,
		// so detection latency models can use P = SSP without a +0.5s constant.
		sleep := attestationInterval()
		if sleep < 500*time.Millisecond {
			sleep = 500 * time.Millisecond
		}
		select {
		case <-stopCh:
			logger.Info("Attestation process stopped")
			return
		case <-time.After(sleep):
		}
	}
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// attestationInterval uses utils.GetEnvInt to respect environment configuration
// attestationInterval is the sleep between consecutive measurements (SSP).
// ATTESTATION_INTERVAL_MS=0 is explicitly allowed and means "no sleep" — the
// prover loop runs back-to-back measurements, bounded only by the cgroup
// CPU_LIMIT. Negative values are coerced to 0 for safety.
func attestationInterval() time.Duration {
	ms := utils.GetEnvInt("ATTESTATION_INTERVAL_MS", 0)
	if ms < 0 {
		ms = 0
	}
	return time.Duration(ms) * time.Millisecond
}

// proverPollInterval is the sleep between successive RPC state-check calls
// while waiting for IsProver / AttestationClosed. Keeping this at 200ms caps
// the fleet-wide RPC load to ~640 calls/s at N=64 (vs ~8500 at 15ms), which
// prevents Besu JSON-RPC saturation that causes prover timeouts at large N.
// Tunable via PROVER_POLL_INTERVAL_MS; defaults to 200ms.
func proverPollInterval() time.Duration {
	ms := utils.GetEnvInt("PROVER_POLL_INTERVAL_MS", 200)
	if ms < 10 {
		ms = 10
	}
	return time.Duration(ms) * time.Millisecond
}