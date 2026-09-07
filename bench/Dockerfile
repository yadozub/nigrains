# A benchmark that runs where the library will.
#
# The first measurements were taken on Windows, where two of them turned out
# to be about the platform's 15.6 ms timer rather than about this package.
# Linux is where asyncio behaves the way most users will meet it, and a
# container is what makes a number comparable to the same number tomorrow.
#
# One image, every interpreter the package claims: uv fetches them, so the
# matrix costs one build rather than four.
FROM ghcr.io/astral-sh/uv:0.9.3-python3.14-bookworm-slim

WORKDIR /bench

# Dependencies first, so editing the benchmark does not refetch interpreters.
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv python install 3.11 3.12 3.13 3.14 \
    && uv sync --no-dev

COPY bench ./bench

# Overridden by the runner to pin one interpreter at a time.
ENV UV_PYTHON=3.14
ENTRYPOINT ["uv", "run", "--no-dev", "python", "bench/bench.py"]
