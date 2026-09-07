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
import gc
import platform
import statistics
import sys
import time
import tracemalloc
from collections.abc import Awaitable, Callable

from nigrains import Grain, GrainId, Runtime
from nigrains import runtime as runtime_module

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


async def dispatch_at_scale(
    fleet: tuple[int, ...] = (1, 1_000, 100_000),
) -> list[tuple[int, float]]:
    """Measures dispatch with a small, a large and a very large fleet activated.

    The activation table is a dict, so this should be flat. "Should be" is
    why it is measured: a runtime whose dispatch degraded with the number of
    live grains would be one whose whole premise - address anything, cheaply
    - stopped holding exactly when it mattered.

    Args:
        fleet: How many grains to have activated for each measurement.

    Returns:
        Fleet size and seconds per dispatched call.
    """
    calls = 20_000
    results = []
    for size in fleet:
        runtime = Runtime()
        runtime.register("hot", Hot)
        await asyncio.gather(*(runtime.call(GrainId("hot", str(n)), "touch") for n in range(size)))
        target = GrainId("hot", str(size - 1))

        async def hammer(grain_id: GrainId = target, on: Runtime = runtime) -> None:
            for _ in range(calls):
                await on.call(grain_id, "touch")

        results.append((size, await _timed(hammer, repeats=3) / calls))
    return results


async def memory_per_activation(grains: int = 100_000) -> float:
    """Measures what the runtime spends to keep one grain activated.

    The grain here holds nothing, so what is measured is the runtime's own
    bookkeeping - the identity, the activation record, the future, the lock
    if there is one. Whatever a real grain holds is added to this, and the
    answer to "how many can one node keep" starts here.

    Args:
        grains: How many to activate.

    Returns:
        Bytes per activation.
    """
    runtime = Runtime()
    runtime.register("trivial", Trivial)

    gc.collect()
    tracemalloc.start()
    before = tracemalloc.get_traced_memory()[0]
    await asyncio.gather(*(runtime.call(GrainId("trivial", str(n)), "ping") for n in range(grains)))
    # Without this the reading includes 100 000 finished tasks the gather
    # has not let go of yet, and calls them the cost of an activation. The
    # first version of this measurement did exactly that.
    gc.collect()
    after = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    assert runtime.activated == grains
    return (after - before) / grains


async def sweep_cost(grains: int = 100_000, *, chunk: int | None = None) -> tuple[float, float]:
    """Measures collecting a large idle fleet, and what it does to the loop.

    **Total time is the less interesting of the two numbers**, and taking it
    alone was misleading: chunking the sweep made the total *worse* - the
    yields cost something - while making the thing that actually hurt
    disappear, which was one uninterrupted stall with every other coroutine
    waiting behind it. So a heartbeat runs alongside and records the longest
    gap between its own turns, which is what a caller would have felt.

    Args:
        grains: How many idle grains to collect.
        chunk: Override the runtime's chunk size, so the same run can
            measure what the sweep did before it was chunked. Reaches into
            a private constant, which a benchmark may do and nothing else
            should.

    Returns:
        Seconds for the sweep, and the longest loop stall during it.
    """
    original = runtime_module._SWEEP_CHUNK
    if chunk is not None:
        runtime_module._SWEEP_CHUNK = chunk
    try:
        return await _swept(grains)
    finally:
        runtime_module._SWEEP_CHUNK = original


async def _swept(grains: int) -> tuple[float, float]:
    """Runs one sweep with a heartbeat beside it.

    Args:
        grains: How many idle grains to collect.

    Returns:
        Seconds for the sweep, and the longest loop stall during it.
    """
    clock = _Frozen()
    runtime = Runtime(idle_seconds=1.0, sweep_seconds=1e9, clock=clock)
    runtime.register("trivial", Trivial)
    await asyncio.gather(*(runtime.call(GrainId("trivial", str(n)), "ping") for n in range(grains)))
    clock.now += 10.0

    stalls: list[float] = []
    beating = True

    async def heartbeat() -> None:
        last = time.perf_counter()
        while beating:
            await asyncio.sleep(0)
            now = time.perf_counter()
            stalls.append(now - last)
            last = now

    pulse = asyncio.create_task(heartbeat())
    await asyncio.sleep(0)

    started = time.perf_counter()
    collected = await runtime.collect()
    wall = time.perf_counter() - started

    beating = False
    await pulse
    assert collected == grains, collected
    return wall, max(stalls)


class _Frozen:
    """A clock the benchmark moves by hand, so a sweep needs no waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def throughput(grains: int = 1_000, per_grain: int = 100) -> float:
    """Measures aggregate calls per second over a whole fleet at once.

    Closer to what a job does than any single-grain figure: many identities,
    many callers, everything in flight together.

    Args:
        grains: How many distinct grains.
        per_grain: Calls to each.

    Returns:
        Calls per second.
    """
    runtime = Runtime()
    runtime.register("hot", Hot)
    ids = [GrainId("hot", str(n)) for n in range(grains)]
    await asyncio.gather(*(runtime.call(grain_id, "touch") for grain_id in ids))

    async def drive(grain_id: GrainId) -> None:
        for _ in range(per_grain):
            await runtime.call(grain_id, "touch")

    started = time.perf_counter()
    await asyncio.gather(*(drive(grain_id) for grain_id in ids))
    return grains * per_grain / (time.perf_counter() - started)


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

    print("dispatch against fleet size")
    for size, seconds in await dispatch_at_scale():
        print(f"  {size:>7,} activated  {seconds * 1e6:8.2f} us   {1 / seconds:12,.0f} calls/s")
    print()

    print("aggregate throughput, 1000 grains x 100 calls, all in flight")
    print(f"  {await throughput():,.0f} calls/s")
    print()

    per_grain = await memory_per_activation()
    print("keeping grains activated (the runtime's own bookkeeping)")
    print(f"  per activation    {per_grain:8.0f} bytes")
    print(f"  100k grains       {per_grain * 100_000 / 1024 / 1024:8.1f} MiB")
    print()

    swept, stall = await sweep_cost()
    whole, whole_stall = await sweep_cost(chunk=10**9)
    print("sweeping 100k idle grains")
    print(f"  chunked      total {swept * 1000:7.1f} ms   longest stall {stall * 1000:7.2f} ms")
    print(
        f"  in one go    total {whole * 1000:7.1f} ms   longest stall {whole_stall * 1000:7.2f} ms"
    )
    print("  (the stall is what a caller waits; the total is what nobody waits for)")
    print()

    loop_noise = statistics.median(
        [await _timed(lambda: asyncio.sleep(0), repeats=3) for _ in range(3)]
    )
    print(f"(one bare event-loop turn: {loop_noise * 1e6:.2f} us)")


if __name__ == "__main__":
    asyncio.run(main())
