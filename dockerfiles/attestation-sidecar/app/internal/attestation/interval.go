package attestation

import (
	"crypto/rand"
	"encoding/binary"
	"math"
	"time"
)

// nextInterval draws a delay from an exponential law of the given mean.
// The law is memoryless: the time since the last measurement says nothing
// about the next one, so an attacker who sees a measurement on the ledger
// cannot time a restore before the next. crypto/rand never returns an error
// since Go 1.24: it aborts the program instead, so a failed draw cannot fall
// back to a predictable delay.
func nextInterval(mean time.Duration) time.Duration {
	var b [8]byte
	rand.Read(b[:])
	// u is uniform in (0, 1]: 53 random bits plus one, so log(u) is finite.
	u := float64(binary.BigEndian.Uint64(b[:])>>11+1) / (1 << 53)
	return time.Duration(-math.Log(u) * float64(mean))
}
