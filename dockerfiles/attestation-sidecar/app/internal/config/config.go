package config

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strconv"

	"app/internal/utils"
)

// ---------------------------------------------------------
// Shared Struct Definitions
// ---------------------------------------------------------

// SelfIntegrityConfig holds settings for the optional self-check.
type SelfIntegrityConfig struct {
	Enabled           bool   `json:"enabled"`
	CmdName           string `json:"cmd_name"`
	TextSectionSize   int    `json:"text_section_size"`
	TextSectionOffset int    `json:"offset"`
	TextSectionPrefix []byte `json:"text_section_prefix"`
	ExpectedSHA256    string `json:"expected_sha256"`
}

// ---------------------------------------------------------
// JSON File Structure
// ---------------------------------------------------------

// AgentConfig represents the raw config.json file content.
type AgentConfig struct {
	Name              string      `json:"name"`
	EthAddress        string      `json:"eth_address"`
	PrivateKey        string      `json:"private_key"`
	EthNodeURL        string      `json:"eth_node_url"`
	ContractAddress   string      `json:"contract_address"`
	ContractABI       interface{} `json:"contract_abi"`
	CmdName           string      `json:"cmd_name"`
	TextSectionSize   int         `json:"text_section_size"`
	Offset            int         `json:"offset"`
	TextSectionPrefix string      `json:"text_section_prefix"`
}

// ---------------------------------------------------------
// Aggregated App Config
// ---------------------------------------------------------

// AppConfig holds the Agent config plus all Environment overrides
type AppConfig struct {
	// Source Data (JSON)
	Agent AgentConfig

	// File Paths & IO (Env/Calculated)
	ResultsDir        string
	ResultsFile       string
	ExportEnabled     bool
	MemoryStorageFile string

	// Runtime Flags (Env)
	UseRedis                  bool
	AutoStart                 bool
	OneShot                   bool
	DisplayMode               bool
	WaitForVerificationResult bool

	// Attestation Params (Env)
	ProverThreshold      int    // legacy, unused
	IterQThreshold       uint32 // K = number of fresh measurements folded into one on-chain attestation
	SelfIntegrityEnabled bool
	SaltMeasurement      bool // send SHA256(m || salt) instead of m (contract V2)
	
	// ADDED: Storage for the auto-discovered integrity info
	SelfIntegrity SelfIntegrityConfig 

	// Verifier Params (Env)
	VerifierWaitTx     bool
	EventCheckpointDir string
	EventConfirmations uint64
	EventBatchSize     uint64
	EventPollInterval  float64
	EventLookback      uint64
}

// ---------------------------------------------------------
// Loaders
// ---------------------------------------------------------

func LoadAgentConfig(path string) (*AgentConfig, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return nil, err
	}
	var cfg AgentConfig
	if err := json.Unmarshal(data, &cfg); err != nil {
		return nil, err
	}
	return &cfg, nil
}

func Load() (*AppConfig, error) {
	configPath := utils.GetEnvStr("CONFIG_PATH", "")
	if configPath == "" {
		return nil, fmt.Errorf("CONFIG_PATH environment variable is required")
	}

	agentCfg, err := LoadAgentConfig(configPath)
	if err != nil {
		return nil, fmt.Errorf("failed to load agent config: %v", err)
	}

	if agentCfg.Name == "" {
		agentCfg.Name = "agent"
	}

	appCfg := &AppConfig{
		Agent:             *agentCfg,
		ResultsDir:        utils.GetEnvStr("RESULTS_DIR", "/experiments/data/attestation-times"),
		ExportEnabled:     utils.GetEnvBool("EXPORT_RESULTS", false),
		MemoryStorageFile: utils.GetEnvStr("MEMORY_STORAGE_FILE", ""),
		UseRedis:          utils.GetEnvBool("USE_REDIS", false),
		AutoStart:         utils.GetEnvBool("AUTO_START", false),
		OneShot:                   utils.GetEnvBool("ONE_SHOT", false),
		DisplayMode:               utils.GetEnvBool("CHAIN_DISPLAY", false),
		WaitForVerificationResult: utils.GetEnvBool("WAIT_FOR_VERIFICATION_RESULT", true),

		ProverThreshold:      utils.GetEnvInt("PROVER_THRESHOLD", 300),
		IterQThreshold:       uint32(utils.GetEnvInt("ITERQ_THRESHOLD", 1)),
		SelfIntegrityEnabled: utils.GetEnvBool("SELF_INTEGRITY_ENABLED", false),
		SaltMeasurement:      utils.GetEnvBool("SALT_MEASUREMENT", false),

		// Initialize Empty SelfIntegrity (Will be populated in main.go)
		SelfIntegrity: SelfIntegrityConfig{
			Enabled: false,
		},

		VerifierWaitTx:     utils.GetEnvBool("WAIT_FOR_TX_CONFIRMATIONS", false),
		EventCheckpointDir: utils.GetEnvStr("EVENT_CHECKPOINT_DIR", "/checkpoints"),
		EventConfirmations: uint64(utils.GetEnvInt("EVENT_CONFIRMATIONS", 1)),
		EventBatchSize:     uint64(utils.GetEnvInt("EVENT_BATCH_SIZE", 1000)),
		EventPollInterval:  utils.GetEnvFloat("EVENT_POLL_INTERVAL", 1.0),
		EventLookback:      uint64(utils.GetEnvInt("EVENT_LOOKBACK_BLOCKS", 0)),
	}

	if appCfg.ExportEnabled {
		runID := os.Getenv("RUN_ID")
		if runID != "" {
			appCfg.ResultsFile = filepath.Join(appCfg.ResultsDir, fmt.Sprintf("%s-run%s.json", appCfg.Agent.Name, runID))
		} else {
			appCfg.ResultsFile = GetNextRunJSON(appCfg.ResultsDir, appCfg.Agent.Name)
		}
	}

	return appCfg, nil
}

func GetNextRunJSON(resultsDir string, participant string) string {
	utils.EnsureDir(resultsDir)
	pattern := filepath.Join(resultsDir, fmt.Sprintf("%s-run*.json", participant))

	files, _ := filepath.Glob(pattern)
	maxIdx := 0
	re := regexp.MustCompile(fmt.Sprintf(`%s-run(\d+)\.json$`, regexp.QuoteMeta(participant)))

	for _, f := range files {
		baseName := filepath.Base(f)
		matches := re.FindStringSubmatch(baseName)
		if len(matches) == 2 {
			if idx, err := strconv.Atoi(matches[1]); err == nil {
				if idx > maxIdx {
					maxIdx = idx
				}
			}
		}
	}
	return filepath.Join(resultsDir, fmt.Sprintf("%s-run%d.json", participant, maxIdx+1))
}