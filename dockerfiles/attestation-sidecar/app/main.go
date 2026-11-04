package main

import (
	"context"
	"fmt"
	"os"
	"os/signal"
	"strconv"
	"sync"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"

	"app/internal/attestation"
	"app/internal/blockchain"
	"app/internal/compute"
	"app/internal/config"
	"app/internal/logger"
	"app/internal/storage"
	"app/internal/utils"
)

// StatusResponse represents the JSON structure returned by /status
type StatusResponse struct {
	Status     string `json:"status"` // "idle", "running", or "finished"
	OneShot    bool   `json:"oneshot"`
	LastResult string `json:"last_result,omitempty"`
}

// lastResult tracks the last attestation result for /status
var lastResult string

// -----------------------------------------------------------------------------
// App State
// -----------------------------------------------------------------------------

type AppState struct {
	Config           *config.AppConfig
	BlockchainClient *blockchain.BlockchainClient
	Storage          storage.StorageBackend

	// Runtime Control
	mu                 sync.Mutex
	measuring          bool
	cancelFunc         context.CancelFunc
	workersWg          sync.WaitGroup
	activeAttestations *sync.Map
}

// -----------------------------------------------------------------------------
// Core Logic
// -----------------------------------------------------------------------------

func startAttestation(state *AppState) {
	state.mu.Lock()
	defer state.mu.Unlock()

	// Reset lastResult at the start (protected by mutex)
	lastResult = ""

	if state.measuring {
		return
	}

	if state.Config.ExportEnabled {
		if state.Config.ResultsFile == "" {
			state.Config.ResultsFile = config.GetNextRunJSON(state.Config.ResultsDir, state.Config.Agent.Name)
			logger.Info("Results file selected (new run): %s", state.Config.ResultsFile)
		}
		if err := utils.EnsureResultsInitialized(state.Config.ResultsDir, state.Config.Agent.Name, state.Config.ResultsFile); err != nil {
			logger.Error("Failed to initialize results file: %v", err)
			state.measuring = false
			return
		}
	}

	var salt *[32]byte
	if state.Config.SaltMeasurement {
		s, err := compute.MeasurementSalt(state.Config.Agent.PrivateKey)
		if err != nil {
			logger.Error("Failed to derive the measurement salt: %v", err)
			return
		}
		salt = &s
	}

	ctx, cancel := context.WithCancel(context.Background())
	state.cancelFunc = cancel
	state.measuring = true

	// 1. Map Config -> Prover Config
	proverCfg := attestation.ProverConfig{
		Salt:                   salt,
		CmdName:                state.Config.Agent.CmdName,
		TextSectionSize:        state.Config.Agent.TextSectionSize,
		Offset:                 state.Config.Agent.Offset,
		TextSectionPrefix:      state.Config.Agent.TextSectionPrefix,
		Threshold:              state.Config.ProverThreshold,
		IterQThreshold:         state.Config.IterQThreshold,
		AgentName:              state.Config.Agent.Name,
		ExportEnabled:          state.Config.ExportEnabled,
		ResultsDir:             state.Config.ResultsDir,
		MemoryStorageFile:      state.Config.MemoryStorageFile,
		ResultsFile:            state.Config.ResultsFile,
		OneShot:                   state.Config.OneShot,
		WaitForTxConfirmations:    state.Config.VerifierWaitTx,
		WaitForVerificationResult: state.Config.WaitForVerificationResult,
		SelfIntegrity: attestation.SelfIntegrityInfo{
			Enabled:           state.Config.SelfIntegrity.Enabled,
			CmdName:           state.Config.SelfIntegrity.CmdName,
			TextSectionSize:   state.Config.SelfIntegrity.TextSectionSize,
			TextSectionOffset: state.Config.SelfIntegrity.TextSectionOffset,
			TextSectionPrefix: state.Config.SelfIntegrity.TextSectionPrefix,
			ExpectedSHA256:    state.Config.SelfIntegrity.ExpectedSHA256,
		},
	}

	state.workersWg.Add(1)
	go func() {
		attestation.RunProverLogicContinuousMode(
			ctx.Done(),
			state.BlockchainClient,
			state.Storage,
			proverCfg,
			state.activeAttestations,
			&state.workersWg,
		)
		// OneShot: prover self-completes — wait for all background attestation
		// goroutines to finish exporting before marking the file as stopped.
		// MarkExperimentStop sets Stopped=true in the JSON; ExportAttestationTimesJSON
		// silently drops writes when Stopped=true, so the order must be:
		//   all exports done → MarkExperimentStop → transition to "finished"
		//
		// We cannot use workersWg.Wait() here because it also includes the verifier
		// goroutine, which never exits unless context is cancelled — causing a deadlock.
		// Instead, poll activeAttestations (emptied by RunProverAndCleanup's defer after
		// ProcessProverAttestation returns) to know when all exports are written.
		//
		// IMPORTANT: do NOT cancel the context here. In a multi-robot run the smart
		// contract may elect THIS robot as the verifier for another robot's attestation
		// after SECaaS's lastSuccess becomes the most recent timestamp. If we cancel the
		// context now the verifier goroutine stops and that ReadyForEvaluation event is
		// never handled, causing the other robot's prover to time out waiting for close.
		// The context is cancelled later by stopAttestation() when /stop is called.
		if state.Config.OneShot {
			// 1. Wait for RunProverAndCleanup goroutines to finish (exports written).
			//    activeAttestations.Delete is called in RunProverAndCleanup's defer,
			//    which runs after ProcessProverAttestation (and its export) returns.
			deadline := time.Now().Add(90 * time.Second)
			for time.Now().Before(deadline) {
				count := 0
				state.activeAttestations.Range(func(_, _ interface{}) bool {
					count++
					return true
				})
				if count == 0 {
					break
				}
				time.Sleep(50 * time.Millisecond)
			}

			state.mu.Lock()
			defer state.mu.Unlock()
			lastResult = "success"
			state.measuring = false
			if state.Config.ExportEnabled {
				utils.MarkExperimentStop(state.Config.ResultsDir, state.Config.Agent.Name, state.Config.ResultsFile)
				state.Config.ResultsFile = ""
			}
		}
	}()

	// 2. Map Config -> Verifier Config
	verifierCfg := attestation.VerifierConfig{
		ParticipantName:        state.Config.Agent.Name,
		ResultsDir:             state.Config.ResultsDir,
		ResultsFile:            state.Config.ResultsFile,
		ExportEnabled:          state.Config.ExportEnabled,
		WaitForTxConfirmations: state.Config.VerifierWaitTx,
		EventCheckpointDir:     state.Config.EventCheckpointDir,
		EventConfirmations:     state.Config.EventConfirmations,
		EventBatchSize:         state.Config.EventBatchSize,
		EventPollInterval:      state.Config.EventPollInterval,
		EventLookback:          state.Config.EventLookback,
	}

	state.workersWg.Add(1)
	go func() {
		attestation.RunVerifierLogicSequential(
			ctx,
			state.BlockchainClient,
			verifierCfg,
			&state.workersWg,
		)
	}()
}

func stopAttestation(state *AppState) {
	state.mu.Lock()
	defer state.mu.Unlock()

	// Set lastResult to "stopped" if not already set (protected by mutex)
	if lastResult == "" {
		lastResult = "stopped"
	}

	// Always cancel the context so background goroutines (e.g. RunProverAndCleanup
	// goroutines that outlive the prover loop in ONE_SHOT mode) are signalled to
	// exit immediately rather than running until their 60 s internal timeout.
	if state.cancelFunc != nil {
		state.cancelFunc()
	}

	// In OneShot mode the prover goroutine already set measuring=false and
	// cleaned up — this guard makes /stop a safe no-op in that case.
	if !state.measuring {
		return
	}

	done := make(chan struct{})
	go func() {
		state.workersWg.Wait()
		close(done)
	}()

	select {
	case <-done:
		// Clean join
	case <-time.After(5 * time.Second):
		logger.Warn("Timeout waiting for workers to join")
	}

	state.measuring = false

	if state.Config.ExportEnabled {
		utils.MarkExperimentStop(state.Config.ResultsDir, state.Config.Agent.Name, state.Config.ResultsFile)
		state.Config.ResultsFile = ""
	}
}

// -----------------------------------------------------------------------------
// Main Entry Point
// -----------------------------------------------------------------------------

func main() {
	// 1. Load Centralized Config
	cfg, err := config.Load()
	if err != nil {
		logger.Error("Failed to load configuration: %v", err)
		os.Exit(1)
	}

	// Validate IterQ threshold: must match the contract's MAX_ITER_COUNT
	// otherwise SendEvidence reverts. K=0 is meaningless; K=1 is single-shot.
	if cfg.IterQThreshold < 1 || cfg.IterQThreshold > 10000 {
		logger.Error("ITERQ_THRESHOLD=%d is out of range [1, 10000]", cfg.IterQThreshold)
		os.Exit(1)
	}
	logger.Info("IterQ threshold (K) = %d", cfg.IterQThreshold)

	// ---------------------------------------------------------
	// 1.5 Compute Self-Integrity Info (Auto-Detection)
	// ---------------------------------------------------------
	if cfg.SelfIntegrityEnabled {
		logger.Info("Computing self-integrity info from /proc/self/exe...")

		selfInfo, err := compute.ComputeSelfIntegrityInfo(32) // 32 byte prefix
		if err != nil {
			logger.Warn("Failed to compute self-integrity info: %v", err)
			logger.Warn("Self-integrity check will be DISABLED.")
			cfg.SelfIntegrity.Enabled = false
		} else {
			selfPid := os.Getpid()
			textOffset, err := compute.FindTextSectionOffsetInMemory(selfPid, selfInfo.TextSectionPrefix)
			if err != nil {
				logger.Warn("Failed to find .text offset in memory: %v", err)
				logger.Warn("Self-integrity check will be DISABLED.")
				cfg.SelfIntegrity.Enabled = false
			} else {
				cfg.SelfIntegrity.Enabled = true
				cfg.SelfIntegrity.CmdName = "attestation-si"
				cfg.SelfIntegrity.TextSectionSize = selfInfo.Size
				cfg.SelfIntegrity.TextSectionOffset = textOffset
				cfg.SelfIntegrity.TextSectionPrefix = selfInfo.TextSectionPrefix
				cfg.SelfIntegrity.ExpectedSHA256 = selfInfo.SHA256

				logger.Info("Self-integrity ENABLED: size=%d, offset=%d, SHA256=%s...",
					cfg.SelfIntegrity.TextSectionSize,
					cfg.SelfIntegrity.TextSectionOffset,
					cfg.SelfIntegrity.ExpectedSHA256[:16])
			}
		}
	} else {
		logger.Info("Self-integrity check explicitly disabled via config.")
		cfg.SelfIntegrity.Enabled = false
	}
	// ---------------------------------------------------------

	// 2. Initialize Blockchain Client
	bcClient, err := blockchain.NewBlockchainClient(
		cfg.Agent.EthAddress,
		cfg.Agent.PrivateKey,
		cfg.Agent.EthNodeURL,
		cfg.Agent.ContractAddress,
		cfg.Agent.ContractABI,
	)
	if err != nil {
		logger.Error("Failed to init blockchain client: %v", err)
		os.Exit(1)
	}
	defer bcClient.Close()

	// 3. Initialize Storage
	var store storage.StorageBackend
	if cfg.UseRedis {
		store = storage.NewRedisStorageBackend("localhost", 6379, 0)
	} else {
		store = storage.NewMemoryStorageBackend()
	}

	// 4. Initialize State
	appState := &AppState{
		Config:             cfg,
		BlockchainClient:   bcClient,
		Storage:            store,
		activeAttestations: &sync.Map{},
	}

	// 5. Handle Background Tasks (Registration / Display)
	if cfg.DisplayMode {
		n := utils.GetEnvInt("CHAIN_DISPLAY_N", 10)
		sec := utils.GetEnvInt("CHAIN_DISPLAY_SEC", 30)
		go bcClient.PeriodicAttestationChainDisplay(n, sec)
	} else {
		isReg, err := bcClient.IsRegistered()
		if err != nil {
			logger.Error("Failed to check registration: %v", err)
		} else if !isReg {
			logger.Info("Registering agent '%s' (%s) ...", cfg.Agent.Name, cfg.Agent.EthAddress)
			tx, err := bcClient.RegisterAgent(cfg.Agent.Name, true, 30)
			if err != nil {
				logger.Error("Registration failed: %v", err)
				os.Exit(1)
			}
			logger.Info("%s registered (Tx: %s)", cfg.Agent.Name, tx)
		}

		if cfg.AutoStart {
			startAttestation(appState)
		}
	}

	logger.Info(logger.GreenText("Startup completed for participant '%s'"), cfg.Agent.Name)

	// 6. API Server (Gin)
	gin.SetMode(gin.ReleaseMode)
	r := gin.New()
	r.Use(gin.Recovery())

	r.GET("/", func(c *gin.Context) {
		c.JSON(200, gin.H{"status": "running", "agent": cfg.Agent.Name})
	})

	r.GET("/status", func(c *gin.Context) {
		appState.mu.Lock()
		defer appState.mu.Unlock()
		resp := StatusResponse{
			OneShot:    cfg.OneShot,
			LastResult: lastResult,
		}
		if appState.measuring {
			resp.Status = "running"
		} else if cfg.OneShot && lastResult == "success" {
			resp.Status = "finished"
		} else {
			resp.Status = "idle"
		}
		c.JSON(200, resp)
	})

	r.GET("/digest", func(c *gin.Context) {
		si := compute.SelfIntegrityParams{
			Enabled:           cfg.SelfIntegrity.Enabled,
			CmdName:           cfg.SelfIntegrity.CmdName,
			TextSectionSize:   cfg.SelfIntegrity.TextSectionSize,
			TextSectionOffset: cfg.SelfIntegrity.TextSectionOffset,
		}
		d, err := compute.ComputeAttestationDigest(
			cfg.Agent.CmdName,
			cfg.Agent.TextSectionSize,
			cfg.Agent.Offset,
			cfg.Agent.TextSectionPrefix,
			si,
		)
		if err != nil {
			c.JSON(500, gin.H{"error": err.Error()})
			return
		}
		c.JSON(200, d)
	})

	r.POST("/start", func(c *gin.Context) {
		startAttestation(appState)
		c.JSON(200, gin.H{"message": "Attestation started."})
	})

	r.POST("/stop", func(c *gin.Context) {
		stopAttestation(appState)
		c.JSON(200, gin.H{"message": "Attestation stopped."})
	})

	// POST /mode  {"one_shot": true|false}
	// Switches between startup (one-shot) and continuous mode at runtime.
	// Rejected while an attestation is in progress.
	// Resets lastResult so /status returns "idle" cleanly after a mode change.
	r.POST("/mode", func(c *gin.Context) {
		var body struct {
			OneShot bool `json:"one_shot"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			c.JSON(400, gin.H{"error": "invalid body: expected {\"one_shot\": true|false}"})
			return
		}

		appState.mu.Lock()
		defer appState.mu.Unlock()

		if appState.measuring {
			c.JSON(409, gin.H{"error": "cannot change mode while attestation is running"})
			return
		}

		appState.Config.OneShot = body.OneShot
		lastResult = "" // reset so /status returns "idle" after a mode change

		mode := "continuous"
		if body.OneShot {
			mode = "startup"
		}
		logger.Info("Mode switched to %s (one_shot=%v)", mode, body.OneShot)
		c.JSON(200, gin.H{"message": fmt.Sprintf("Mode set to %s.", mode), "one_shot": body.OneShot})
	})

	// POST /config  {"results_dir": "...", "one_shot": true|false, "export_enabled": true|false, "attestation_interval_ms": N, "iterq_threshold": K}
	// Partial update — only fields present in the body are applied.
	// Rejected while an attestation is in progress.
	// results_dir change also clears ResultsFile so the next /start picks a fresh run number.
	// one_shot change resets lastResult so /status returns "idle" cleanly.
	// attestation_interval_ms takes effect on the next measurement cycle (updates ATTESTATION_INTERVAL_MS env var).
	// iterq_threshold takes effect on the next measurement cycle (updates appState.Config.IterQThreshold in-process).
	r.POST("/config", func(c *gin.Context) {
		var body struct {
			ResultsDir             *string `json:"results_dir"`
			ResultsFile            *string `json:"results_file"`
			OneShot                *bool   `json:"one_shot"`
			ExportEnabled          *bool   `json:"export_enabled"`
			AttestationIntervalMs  *int    `json:"attestation_interval_ms"`
			IterQThreshold         *int    `json:"iterq_threshold"`
		}
		if err := c.ShouldBindJSON(&body); err != nil {
			c.JSON(400, gin.H{"error": "invalid body"})
			return
		}
		if body.ResultsDir == nil && body.ResultsFile == nil && body.OneShot == nil && body.ExportEnabled == nil && body.AttestationIntervalMs == nil && body.IterQThreshold == nil {
			c.JSON(400, gin.H{"error": "no recognized fields: use results_dir, results_file, one_shot, export_enabled, attestation_interval_ms, iterq_threshold"})
			return
		}

		appState.mu.Lock()
		defer appState.mu.Unlock()

		if appState.measuring {
			c.JSON(409, gin.H{"error": "cannot change config while attestation is running"})
			return
		}

		applied := gin.H{}

		if body.ResultsDir != nil {
			if *body.ResultsDir == "" {
				c.JSON(400, gin.H{"error": "results_dir must not be empty"})
				return
			}
			appState.Config.ResultsDir = *body.ResultsDir
			appState.Config.ResultsFile = "" // force fresh run-number selection on next /start
			applied["results_dir"] = *body.ResultsDir
			logger.Info("Results dir set to: %s", *body.ResultsDir)
		}

		if body.ResultsFile != nil {
			appState.Config.ResultsFile = *body.ResultsFile
			applied["results_file"] = *body.ResultsFile
			logger.Info("Results file set to: %s", *body.ResultsFile)
		}

		if body.OneShot != nil {
			appState.Config.OneShot = *body.OneShot
			lastResult = ""
			mode := "continuous"
			if *body.OneShot {
				mode = "startup"
			}
			applied["one_shot"] = *body.OneShot
			logger.Info("Mode switched to %s (one_shot=%v)", mode, *body.OneShot)
		}

		if body.ExportEnabled != nil {
			appState.Config.ExportEnabled = *body.ExportEnabled
			applied["export_enabled"] = *body.ExportEnabled
			logger.Info("Export enabled set to: %v", *body.ExportEnabled)
		}

		if body.AttestationIntervalMs != nil {
			ms := *body.AttestationIntervalMs
			if ms < 0 {
				ms = 0
			}
			os.Setenv("ATTESTATION_INTERVAL_MS", strconv.Itoa(ms))
			applied["attestation_interval_ms"] = ms
			logger.Info("Attestation interval set to: %dms", ms)
		}

		if body.IterQThreshold != nil {
			k := *body.IterQThreshold
			if k < 1 {
				c.JSON(400, gin.H{"error": "iterq_threshold must be >= 1"})
				return
			}
			if k > 10000 {
				c.JSON(400, gin.H{"error": "iterq_threshold must be <= 10000"})
				return
			}
			appState.Config.IterQThreshold = uint32(k)
			applied["iterq_threshold"] = k
			logger.Info("IterQ threshold (K) set to: %d", k)
		}

		c.JSON(200, gin.H{"applied": applied})
	})

	// 7. Graceful Shutdown
	srvPort := 8000
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		if err := r.Run(fmt.Sprintf(":%d", srvPort)); err != nil {
			logger.Error("Server error: %v", err)
		}
	}()

	logger.Info("Server listening on port %d", srvPort)
	<-quit

	logger.Info("Shutting down...")
	stopAttestation(appState)
	logger.Info("Bye.")
}