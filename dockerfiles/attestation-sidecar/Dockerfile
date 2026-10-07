# ---------------------------------------------------------
# Stage 1: Builder
# ---------------------------------------------------------
FROM golang:1.25-bookworm AS builder

WORKDIR /src

# Copy the application source code
COPY app/ ./app/

# Initialize Go Module (if not already done locally)
# We move into the folder where main.go resides
WORKDIR /src/app

# Dependencies are pinned in app/go.mod and app/go.sum.
RUN go mod download \
    && CGO_ENABLED=0 go build -o /bin/attestation-sidecar . \
    && CGO_ENABLED=0 go build -o /bin/txbench ./cmd/txbench \
    && CGO_ENABLED=0 go build -o /bin/txbench-parallel ./cmd/txbench-parallel

# ---------------------------------------------------------
# Stage 2: Runtime
# ---------------------------------------------------------
FROM debian:bookworm-slim

# Set bash as default shell
SHELL ["/bin/bash", "-c"]
ENV DEBIAN_FRONTEND=noninteractive

# Install runtime dependencies
# - binutils: REQUIRED for 'readelf' (used by app/internal/compute/hash.go)
# - ca-certificates: REQUIRED for HTTPS blockchain connections
# - redis-tools: Optional, useful for debugging
RUN apt-get update && apt-get install -y --no-install-recommends \
    binutils \
    ca-certificates \
    redis-tools \
    procps \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy the compiled binary from the builder stage
COPY --from=builder /bin/attestation-sidecar .
COPY --from=builder /bin/txbench .
COPY --from=builder /bin/txbench-parallel .

# Expose the port configured in main.go (Default 8000)
# Note: Your Python version used 8080, but your Go main.go uses 8000.
EXPOSE 8000

# Run the binary
ENTRYPOINT ["./attestation-sidecar"]