package attestation

import (
	"testing"
	"time"
)

func TestNextIntervalIsExponential(t *testing.T) {
	const n = 10000
	mean := 10 * time.Second
	var sum time.Duration
	for i := 0; i < n; i++ {
		d := nextInterval(mean)
		if d <= 0 {
			t.Fatalf("draw %d is not positive: %v", i, d)
		}
		sum += d
	}
	// The standard error of the mean is mean/sqrt(n) = 1 %, so 5 % is five sigmas.
	got := sum / n
	if got < mean*95/100 || got > mean*105/100 {
		t.Fatalf("mean of %d draws is %v, want %v within 5 %%", n, got, mean)
	}
}
