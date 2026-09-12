#!/bin/sh
# Runs the benchmark in a container, on the interpreter the package claims.
#
# Pinned CPUs and memory, because a benchmark whose numbers move with whatever
# else the machine is doing is a benchmark that measures the machine.
set -eu

IMAGE=nigrains-bench
CPUS=${CPUS:-2}
MEMORY=${MEMORY:-2g}

docker build -q -f bench/Dockerfile -t "$IMAGE" . >/dev/null

docker run --rm --cpus "$CPUS" --memory "$MEMORY" "$IMAGE"
