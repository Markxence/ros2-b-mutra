package attestation

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"sync"
	"time"

	"app/internal/blockchain"
	"app/internal/compute"
	"app/internal/logger"
	"app/internal/utils"

	"github.com/ethereum/go-ethereum/core/types"
)

// VerifierConfig holds all configuration required by the Verifier.
type VerifierConfig struct {
	ParticipantName        string
	ResultsDir             string
	ResultsFile            string
	ExportEnabled          bool
	WaitForTxConfirmations bool

	// Event Watcher Config
	EventCheckpointDir string
	EventConfirmations uint64
	EventBatchSize     uint64
	EventPollInterval  float64
	EventLookback      uint64
}

// verificationJob represents a task in the worker queue
type verificationJob struct {
	AttestationID string
	SeedTs        map[string]interface{}
	Juror         bool   // V3: vote if drawn in the jury of the cycle
	EvidenceBlock uint64 // block of the ReadyForEvaluation event
}

// RunVerifierLogicSequential starts the verifier event loop and worker.
func RunVerifierLogicSequential(
	ctx context.Context,
	client *blockchain.BlockchainClient,
	cfg VerifierConfig,
	stopWg *sync.WaitGroup,
) {
	defer stopWg.Done()

	eventName := blockchain.EventReadyForEvaluation // Ensure this constant exists in types.go

	// Setup Checkpoint Path
	cpName := "att_started_cp_agent_seq.json"
	cpPath := filepath.Join(cfg.EventCheckpointDir, cpName)

	// Create Event Watcher
	watcher, err := blockchain.NewEventWatcher(
		client,
		eventName,
		cpPath,
		cfg.EventConfirmations,
		cfg.EventBatchSize,
		cfg.EventPollInterval,
	)
	if err != nil {
		logger.Error("Verifier failed to create EventWatcher: %v", err)
		return
	}

	// NOTE: In Python, you manually set from_block with lookback here.
	// The Go EventWatcher auto-loads checkpoint or defaults to latest.
	// If you strictly need the lookback on first run, we rely on the watcher's robust default.

	logger.Info("Subscribed to attestation events with EventWatcher...")

	// Worker Queue (Channel)
	// Python: work_q = Queue()
	workChan := make(chan verificationJob, 100) // Buffer 100

	// Deduplication Set
	enqueuedIDs := make(map[string]bool)
	var enqLock sync.Mutex

	// ---------------------------------------------------------
	// Worker Goroutine
	// ---------------------------------------------------------
	go func() {
		for {
			select {
			case <-ctx.Done():
				logger.Info("Verifier worker stopped")
				return
			case job, ok := <-workChan:
				if !ok {
					logger.Info("Verifier worker stopped")
					return
				}
				start := time.Now()

				selected := true
				if job.Juror {
					var err error
					selected, err = inJury(ctx, client, job.AttestationID, job.EvidenceBlock)
					if err != nil {
						logger.Error("[Juror-%s] Failed to read the jury: %v", utils.ShortAttID(job.AttestationID, 4), err)
						selected = false
					}
				}
				if selected {
					logger.Info("Processing attestation '%s'", job.AttestationID)
					processVerifierAttestation(ctx, client, cfg, job.AttestationID, job.SeedTs, job.Juror)

					elapsed := time.Since(start).Seconds()
					logPrefix := fmt.Sprintf("[Verifier-%s]", utils.ShortAttID(job.AttestationID, 4))
					logger.Info(logger.GreenText(fmt.Sprintf("%s completed in %.2fs", logPrefix, elapsed)))
				}

				// Remove from dedup set
				enqLock.Lock()
				delete(enqueuedIDs, job.AttestationID)
				enqLock.Unlock()
			}
		}
	}()

	// ---------------------------------------------------------
	// Event Handler Callback
	// ---------------------------------------------------------
	handle := func(log types.Log, parsedData map[string]interface{}) error {
		arrivalTs := utils.NowMs()
		arrivalP := utils.PerfNs()

		// Extract Attestation ID
		// It usually comes as [32]byte from the watcher. Convert to string.
		var attID string
		if idVal, ok := parsedData["id"]; ok { // Assumes event arg is named "id"
			if idBytes, ok := idVal.([32]byte); ok {
				// Strip null bytes
				attID = strings.TrimRight(string(idBytes[:]), "\x00")
			} else if idStr, ok := idVal.(string); ok {
				// fallback if it was parsed as string/hash
				attID = idStr
			}
		}

		if attID == "" {
			return fmt.Errorf("could not extract attestation ID from event")
		}

		// Check if I am the verifier
		isVerifier, err := client.IsVerifier(attID)
		if err != nil {
			logger.Error("IsVerifier check failed for %s: %v", attID, err)
			return nil // Don't crash handler
		}

		// With a jury (V3) no single verifier is elected: the worker checks
		// whether this agent was drawn once the evidence block is sealed.
		juror := !isVerifier && client.HasJury()
		if isVerifier || juror {
			enqLock.Lock()
			if !enqueuedIDs[attID] {
				enqueuedIDs[attID] = true

				seed := map[string]interface{}{
					"ready_for_evaluation_received": arrivalTs,
					"p_ready_for_evaluation_received": arrivalP,
				}

				// Non-blocking send to avoid stalling watcher if queue is full
				select {
				case workChan <- verificationJob{AttestationID: attID, SeedTs: seed, Juror: juror, EvidenceBlock: log.BlockNumber}:
				default:
					logger.Warn("Verifier queue full, dropping %s", attID)
				}
			}
			enqLock.Unlock()
		}
		return nil
	}

	// Run Watcher in a goroutine; close workChan once it exits so the worker
	// drains any remaining buffered jobs and then stops cleanly.
	watcherDone := make(chan struct{})
	go func() {
		defer close(watcherDone)
		watcher.Run(ctx, handle)
	}()

	// Block until context cancelled, then wait for watcher to finish before
	// closing workChan (guarantees no send-on-closed-channel panic).
	<-ctx.Done()
	<-watcherDone
	close(workChan)
	logger.Info("Verifier logic stopped")
}

// inJury waits until the evidence block is sealed, since the contract only
// draws the jury from the next block on, then tells whether this agent is in it.
func inJury(ctx context.Context, client *blockchain.BlockchainClient, attID string, evidenceBlock uint64) (bool, error) {
	for {
		head, err := client.BlockNumber()
		if err != nil {
			return false, err
		}
		if head > evidenceBlock {
			break
		}
		select {
		case <-ctx.Done():
			return false, ctx.Err()
		case <-time.After(proverPollInterval()):
		}
	}
	jury, err := client.PreviewJury(attID)
	if err != nil {
		return false, err
	}
	names := make([]string, len(jury))
	selected := false
	for i, j := range jury {
		names[i] = j.Hex()
		selected = selected || j == client.Address()
	}
	logPrefix := fmt.Sprintf("[Juror-%s]", utils.ShortAttID(attID, 4))
	if selected {
		logger.Info("%s Drawn in the jury: %s", logPrefix, strings.Join(names, ", "))
	} else {
		logger.Debug("%s Not drawn, jury: %s", logPrefix, strings.Join(names, ", "))
	}
	return selected, nil
}

// cycleClosed tells whether the other jurors already closed the cycle.
func cycleClosed(client *blockchain.BlockchainClient, attID string) bool {
	state, err := client.GetAttestationState(attID)
	return err == nil && state == blockchain.Closed
}

// processVerifierAttestation contains the core business logic
func processVerifierAttestation(
	ctx context.Context,
	client *blockchain.BlockchainClient,
	cfg VerifierConfig,
	attestationID string,
	seedTs map[string]interface{},
	juror bool,
) {
	timestamps := make(map[string]interface{})
	// Copy seed
	for k, v := range seedTs {
		timestamps[k] = v
	}

	timestamps["verifier_start"] = utils.NowMs()
	timestamps["p_verifier_start"] = utils.PerfNs()
	logPrefix := fmt.Sprintf("[Verifier-%s]", utils.ShortAttID(attestationID, 4))
	if juror {
		logPrefix = fmt.Sprintf("[Juror-%s]", utils.ShortAttID(attestationID, 4))
		if cycleClosed(client, attestationID) {
			logger.Info("%s Cycle already closed by the other jurors, no vote", logPrefix)
			return
		}
	}
	logger.Info("%s SECaaS Oracle finished. Retrieving signatures for evaluation...", logPrefix)

	// 1. Retrieve Signatures
	timestamps["get_signatures_start"] = utils.NowMs()
	timestamps["p_get_signatures_start"] = utils.PerfNs()

	freshSig, refSig, iterCount, err := client.GetAttestationSignatures(attestationID)
	if err != nil {
		logger.Error("%s Failed to get signatures: %v", logPrefix, err)
		return
	}

	timestamps["p_get_signatures_finished"] = utils.PerfNs()
	timestamps["get_signatures_finished"] = utils.NowMs()

	// 2. Evaluation (compute)
	timestamps["verify_compute_start"] = utils.NowMs()
	timestamps["p_verify_compute_start"] = utils.PerfNs()

	// Re-derive the chained reference: under the no-tamper hypothesis the
	// binary is static, so all K m_i = refSig and the prover's H_K equals
	// chain(refSig, K). For K=1 this is identity. Must stay bit-for-bit
	// identical to compute.RollingHashStep used in the prover.
	derivedRef, derr := compute.RollingHashHex(refSig, iterCount)
	if derr != nil {
		logger.Error("%s Failed to derive rolling reference (K=%d): %v", logPrefix, iterCount, derr)
		return
	}
	// freshSig is "0x"-prefixed (Web3 convention); derivedRef is bare hex.
	freshNorm := strings.TrimPrefix(strings.ToLower(freshSig), "0x")
	isSuccess := strings.EqualFold(freshNorm, derivedRef)
	// For testing
	// isSuccess = true

	timestamps["p_verify_compute_finished"] = utils.PerfNs()
	timestamps["verify_compute_finished"] = utils.NowMs()

	// 3. Submit Result
	statusIcon := "❌ FAILURE"
	if isSuccess {
		statusIcon = "✅ SUCCESS"
	}
	if !juror {
		logger.Info("%s Attestation closed (result: %s)", logPrefix, statusIcon)
	}

	timestamps["verification_result"] = isSuccess
	timestamps["result_sent"] = utils.NowMs()
	timestamps["p_send_result_start"] = utils.PerfNs()

	// Wait logic based on config
	timeout := 60 // default timeout
	if juror {
		_, err = client.Vote(attestationID, isSuccess, cfg.WaitForTxConfirmations, timeout)
		switch {
		case err == nil:
			logger.Info("%s Vote sent (verdict: %s)", logPrefix, statusIcon)
		case cycleClosed(client, attestationID):
			logger.Info("%s Cycle closed by the other jurors before this vote", logPrefix)
		default:
			logger.Error("%s Failed to vote: %v", logPrefix, err)
		}
	} else {
		_, err = client.SendAttestationResult(attestationID, isSuccess, cfg.WaitForTxConfirmations, timeout)
		if err != nil {
			logger.Error("%s Failed to send result: %v", logPrefix, err)
			// We continue to record what we can
		}
	}

	timestamps["p_send_result_finished"] = utils.PerfNs()

	if cfg.WaitForTxConfirmations {
			timestamps["p_send_result_finished_tx_confirmed"] = utils.PerfNs()
			timestamps["result_sent_tx_confirmed"] = utils.NowMs()
	}

	timestamps["p_verifier_finished"] = utils.PerfNs()
	timestamps["verifier_finished"] = utils.NowMs()

	// 5. Summary Log (simplified)
	logger.Debug("%s summary: result=%v | total_dur=%dms",
		logPrefix, isSuccess,
		timestamps["verifier_finished"].(int64)-timestamps["verifier_start"].(int64))

	// 6. Export
	if cfg.ExportEnabled {
		utils.ExportAttestationTimesJSON(
			cfg.ParticipantName,
			attestationID,
			"verifier",
			timestamps,
			cfg.ResultsDir,
			cfg.ResultsFile, // Can be empty, helper handles it
		)
	}
}