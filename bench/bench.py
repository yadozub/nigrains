"""What the runtime costs and what it actually overlaps.

Three numbers, because three different claims are being made and each one
would be believed on the strength of the README otherwise.

**Dispatch overhead.** A call through the runtime is a dictionary lookup, a
``getattr``, a reentrancy branch and a context manager, against a plain
``await grain.method()``. That ratio is the price of addressing by identity,
and it should be paid knowingly.

**Overlap.** A reentrant grain claims its callers do not queue. That is
counted rather than timed - the highest number of callers inside the method
at once, which is the claim itself and not a proxy for it. A serialised grain
must measure exactly 1, and a reentrant one the number of callers.

**Cold activation.** The fleet is cold exactly when a job starts and asks it
for everything at once, so what matters is not one activation but a thousand
arriving together.

Run: ``uv run python bench/bench.py``
"""

from __future__ import annotations

import asyncio
import platform
import statistics
import sys
import time
from collections.abc import Awaitable, Callable

from nigrains import Grain, GrainId, Runtime

TURNS = 20
"""Event-loop turns a 'slow' method yields for.

**Not a timed sleep, and the first draft of this file got that wrong.**
Measuring overlap with ``asyncio.sleep(0.01)`` measured Windows' timer
instead: its default resolution is about 15.6 ms, so every 10 ms sleep took
one and a half ticks and the serialised grain scored 0.6x - a number about
the platform's clock, not about the lock. Yielding a fixed number of turns
costs the same on every platform and is what the runtime actually stands
between.
"""


class Hot(Grain):
    """Answers immediately. Isolates dispatch from everything else."""

    reentrant = True

    async def touch(self) -> int:
        return 1


class Serial(Grain):
    """Yields for a while, one caller at a time.

    Records the highest number of callers that were inside at once, which is
    the claim itself rather than a proxy for it.
    """

    def __init__(self, grain_id: GrainId) -> None:
        super().__init__(grain_id)
        self.inside = 0
        self.peak = 0

    async def work(self) -> None:
        self.inside += 1
        self.peak = max(self.peak, self.inside)
        for _ in range(TURNS):
            await asyncio.sleep(0)
        self.inside -= 1


class Parallel(Serial):
    """The same, declaring that overlap is fine."""

    reentrant = True


class Trivial(Grain):
    """Costs nothing to activate, so activation measures the runtime."""

    async def ping(self) -> int:
        return 1


async def _timed(work: Callable[[], Awaitable[None]], *, repeats: int = 5) -> float:
    """Runs something a few times and returns the best wall time.

    The best rather than the mean: this is a floor measurement, and on a
    machine with other things running the mean measures the other things.

    Args:
        work: What to time.
        repeats: How many runs to take the best of.

    Returns:
        Seconds.
    """
    times = []
    for _ in range(repeats):
        started = time.perf_counter()
        await work()
        times.append(time.perf_counter() - started)
    return min(times)


async def dispatch_overhead(calls: int = 100_000) -> tuple[float, float]:
    """Compares a call through the runtime with a direct await.

    Args:
        calls: How many calls per run.

    Returns:
        Seconds per direct call, and seconds per dispatched call.
    """
    runtime = Runtime()
    runtime.register("hot", Hot)
    grain_id = GrainId("hot", "a")
    await runtime.call(grain_id, "touch")  # warm: activation is not the subject

    direct = Hot(grain_id)

    async def bare() -> None:
        for _ in range(calls):
            await direct.touch()

    async def dispatched() -> None:
        for _ in range(calls):
            await runtime.call(grain_id, "touch")

    return await _timed(bare) / calls, await _timed(dispatched) / calls


async def overlap(callers: int = 200) -> dict[str, tuple[int, float]]:
    """Measures how many callers a grain lets inside, and what it costs.

    Args:
        callers: Concurrent callers on one grain.

    Returns:
        Peak simultaneous callers and wall seconds, per kind of grain.
    """
    measured: dict[str, tuple[int, float]] = {}
    for kind, behaviour in (("serialised", Serial), ("reentrant", Parallel)):
        probes: list[Serial] = []

        def build(
            grain_id: GrainId,
            kind_of: type[Serial] = behaviour,
            into: list[Serial] = probes,
        ) -> Grain:
            """Builds the grain and keeps a reference the benchmark can read.

            Both extras are bound as defaults rather than closed over: the
            loop rebinds them, and a closure would read whichever was last.
            """
            grain = kind_of(grain_id)
            into.append(grain)
            return grain

        runtime = Runtime()
        runtime.register(kind, build)
        grain_id = GrainId(kind, "a")
        await runtime.call(grain_id, "work")
        probes[0].peak = 0

        started = time.perf_counter()
        await asyncio.gather(*(runtime.call(grain_id, "work") for _ in range(callers)))
        measured[kind] = (probes[0].peak, time.perf_counter() - started)
    return measured


async def cold_fleet(grains: int = 1_000) -> tuple[float, float]:
    """Activates a whole cold fleet at once, the way a starting job does.

    Args:
        grains: How many distinct identities are called together.

    Returns:
        Seconds for the burst, and microseconds per activation.
    """
    runtime = Runtime()
    runtime.register("trivial", Trivial)

    started = time.perf_counter()
    await asyncio.gather(*(runtime.call(GrainId("trivial", str(n)), "ping") for n in range(grains)))
    wall = time.perf_counter() - started
    assert runtime.activated == grains
    return wall, wall / grains * 1e6


async def herd(callers: int = 1_000) -> float:
    """Sends many callers at one cold grain, which must activate once.

    Args:
        callers: Concurrent first calls to one identity.

    Returns:
        Seconds until all of them have an answer.
    """
    activations = 0

    class Counted(Grain):
        reentrant = True

        async def activate(self) -> None:
            nonlocal activations
            activations += 1
            for _ in range(TURNS):
                await asyncio.sleep(0)

        async def ping(self) -> int:
            return 1

    runtime = Runtime()
    runtime.register("counted", Counted)
    grain_id = GrainId("counted", "a")

    started = time.perf_counter()
    await asyncio.gather(*(runtime.call(grain_id, "ping") for _ in range(callers)))
    wall = time.perf_counter() - started
    assert activations == 1, activations
    return wall


async def main() -> None:
    """Runs every measurement and prints it with the machine it was taken on."""
    print(f"python  {platform.python_version()}  {sys.platform}  {platform.processor()}")
    print()

    bare, dispatched = await dispatch_overhead()
    print("dispatch, hot reentrant grain")
    print(f"  direct await      {bare * 1e6:8.2f} us   {1 / bare:12,.0f} calls/s")
    print(f"  runtime.call      {dispatched * 1e6:8.2f} us   {1 / dispatched:12,.0f} calls/s")
    print(f"  overhead          {(dispatched - bare) * 1e6:8.2f} us   x{dispatched / bare:.1f}")
    print()

    measured = await overlap()
    print(f"overlap, 200 callers on one grain, {TURNS} loop turns each")
    for kind, (peak, wall) in measured.items():
        print(f"  {kind:16}  peak {peak:4} inside   {wall * 1000:7.1f} ms")
    serial_wall = measured["serialised"][1]
    parallel_wall = measured["reentrant"][1]
    print(f"  reentrant is      {serial_wall / parallel_wall:8.1f}x faster here")
    print()

    wall, each = await cold_fleet()
    print("cold fleet, 1000 distinct grains called at once")
    print(f"  wall              {wall * 1000:8.1f} ms")
    print(f"  per activation    {each:8.2f} us")
    print()

    burst = await herd()
    print("thundering herd, 1000 callers meeting one cold grain")
    print(f"  wall              {burst * 1000:8.1f} ms   (one activation, asserted)")
    print()

    loop_noise = statistics.median(
        [await _timed(lambda: asyncio.sleep(0), repeats=3) for _ in range(3)]
    )
    print(f"(one bare event-loop turn: {loop_noise * 1e6:.2f} us)")


if __name__ == "__main__":
    asyncio.run(main())
