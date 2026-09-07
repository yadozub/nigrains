#!/bin/sh
# Runs the benchmark on every interpreter the package claims, in a container.
#
# Pinned CPUs and memory, because a benchmark whose numbers move with whatever
# else the machine is doing is a benchmark that measures the machine.
set -eu

IMAGE=nigrains-bench
CPUS=${CPUS:-2}
MEMORY=${MEMORY:-2g}

docker build -q -f bench/Dockerfile -t "$IMAGE" . >/dev/null

for version in 3.11 3.12 3.13 3.14; do
    echo "============================================================"
    docker run --rm --cpus "$CPUS" --memory "$MEMORY" \
        -e "UV_PYTHON=$version" "$IMAGE"
done
