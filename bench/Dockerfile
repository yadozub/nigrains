# A benchmark that runs where the library will.
#
# The first measurements were taken on Windows, where two of them turned out
# to be about the platform's 15.6 ms timer rather than about this package.
# Linux is where asyncio behaves the way most users will meet it, and a
# container is what makes a number comparable to the same number tomorrow.
#
# The one interpreter the package claims.
FROM ghcr.io/astral-sh/uv:0.9.3-python3.14-bookworm-slim

WORKDIR /bench

# Dependencies first, so editing the benchmark does not resolve them again.
COPY pyproject.toml README.md ./
COPY src ./src
RUN uv python install 3.14 && uv sync --no-dev

COPY bench ./bench

# Pinned, and not redundant even though the image carries 3.14 already.
# Removing this line once switched `uv run` from the standalone CPython uv
# fetches to the one in the base image, and every number moved by twenty
# per cent - including the bare `await`, which is how it was caught. Two
# builds of the same version are not the same machine, and a benchmark
# whose interpreter changes silently measures the packaging.
ENV UV_PYTHON=3.14

ENTRYPOINT ["uv", "run", "--no-dev", "python", "bench/bench.py"]
