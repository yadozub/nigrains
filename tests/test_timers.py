"""A grain doing something on a schedule, and what that does not buy it."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from nigrains import Grain, Runtime

TICK = 0.01


class Ticking(Grain):
    """Counts its own ticks."""

    grain_type = "ticking"

    async def activate(self) -> None:
        self.ticks = 0
        self.every(TICK, self.beat)

    async def beat(self) -> None:
        self.ticks += 1

    async def count(self) -> int:
        return self.ticks


class Failing(Grain):
    """Its schedule raises, every time."""

    grain_type = "failing"

    async def activate(self) -> None:
        self.attempts = 0
        self.every(TICK, self.beat)

    async def beat(self) -> None:
        self.attempts += 1
        raise RuntimeError("no")

    async def count(self) -> int:
        return self.attempts


class SlowTick(Grain):
    """A tick that does not finish until it is let go."""

    grain_type = "slow_tick"

    async def activate(self) -> None:
        self.inside = False
        self.finished = False
        self.release = asyncio.Event()
        self.every(TICK, self.beat)

    async def beat(self) -> None:
        self.inside = True
        await self.release.wait()
        self.finished = True

    async def ping(self) -> str:
        return "pong"


async def _until(condition: Callable[[], bool], *, within: float = 2.0) -> None:
    """Waits for something to become true, rather than for a fixed span.

    **Sleeping for three intervals and hoping is how a timer test becomes
    flaky**, and this one did: the whole suite on a loaded machine gave the
    event loop less than the arithmetic assumed, and a passing test failed
    once in a hundred runs. Waiting for the condition takes the same time
    when the machine is idle and does not lie when it is not.

    Args:
        condition: What is being waited for.
        within: Seconds before giving up, generous enough that reaching it
            means something is actually wrong.

    Raises:
        AssertionError: It never became true.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if condition():
            return
        await asyncio.sleep(TICK / 4)
    raise AssertionError(f"condition never held within {within}s")


def _runtime(
    grain: type[Grain],
    probe: list[Any] | None = None,
    *,
    idle_seconds: float = 1e9,
    drain_seconds: float = 30.0,
) -> Runtime:
    """A runtime holding one grain, optionally handing back the activation.

    Args:
        grain: The class to register.
        probe: Filled with each activation the runtime builds, for a test
            that needs to look inside one.
        idle_seconds: How long before a grain counts as idle.
        drain_seconds: How long leaving waits for work in flight.

    Returns:
        The runtime.
    """
    built = Runtime(idle_seconds=idle_seconds, sweep_seconds=1e9, drain_seconds=drain_seconds)
    if probe is None:
        built.register(grain)
    else:
        built.register(grain, lambda grain_id: _kept(probe, grain(grain_id)))
    return built


def _kept(store: list[Any], grain: Grain) -> Grain:
    """Remembers the activation the runtime just built.

    Args:
        store: Where to keep it.
        grain: The activation.

    Returns:
        The same grain.
    """
    store.append(grain)
    return grain


async def test_a_timer_runs_while_the_grain_is_activated() -> None:
    grains: list[Ticking] = []
    runtime = _runtime(Ticking, grains)
    counter = runtime.reference(Ticking, "a")

    assert await counter.count() == 0, "the first tick is one interval away, not immediate"

    await _until(lambda: grains[0].ticks >= 2)


async def test_a_timer_stops_with_the_activation() -> None:
    """It dies with the grain, which is the whole difference from a reminder."""
    grains: list[Ticking] = []
    runtime = _runtime(Ticking, grains, idle_seconds=-1.0)

    await runtime.reference(Ticking, "a").count()
    await _until(lambda: grains[0].ticks >= 1)
    assert await runtime.collect() == 1

    stopped_at = grains[0].ticks
    await asyncio.sleep(TICK * 3)
    assert grains[0].ticks == stopped_at, "the schedule stopped with the activation"


async def test_a_timer_does_not_keep_its_grain_alive() -> None:
    """Ticking is not being used. Otherwise one call is a grain for ever."""
    runtime = _runtime(Ticking, idle_seconds=-1.0)
    await runtime.reference(Ticking, "a").count()
    await _until(lambda: bool(runtime.activated))

    assert await runtime.collect() == 1
    assert runtime.activated == 0


async def test_a_tick_in_progress_delays_deactivation() -> None:
    """The same failure as deactivating under a call, one step sideways."""
    grains: list[SlowTick] = []
    runtime = _runtime(SlowTick, grains, idle_seconds=-1.0, drain_seconds=1.0)

    async with runtime:
        await runtime.reference(SlowTick, "a").ping()
        await _until(lambda: grains[0].inside)
        grains[0].release.set()

    assert grains[0].finished, "shutdown waited for the tick rather than cutting it"


async def test_a_tick_that_raises_does_not_stop_the_schedule() -> None:
    """A timer that died silently would be worse; one that took the grain
    down with it, worse still.
    """
    grains: list[Failing] = []
    runtime = _runtime(Failing, grains)
    counter = runtime.reference(Failing, "a")
    # Activating it first: a reference is an address, and the grain does not
    # exist - so nor does its schedule - until something calls it.
    assert await counter.count() == 0
    await _until(lambda: grains[0].attempts >= 2)
    assert runtime.activated == 1, "the grain is still there"


async def test_a_grain_without_timers_starts_none() -> None:
    """The common case pays nothing for the feature."""

    class Plain(Grain):
        grain_type = "plain"

        async def ping(self) -> str:
            return "pong"

    runtime = _runtime(Plain)
    await runtime.reference(Plain, "a").ping()

    activation = next(iter(runtime._activations.values()))
    assert activation.timers == []
