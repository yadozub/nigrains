"""A kind where identity does not mean one activation."""

from __future__ import annotations

import asyncio
from typing import Any

from nigrains import Grain, Runtime


class Pooled(Grain):
    """Four of these answer for one key, one call at a time each."""

    grain_type = "pooled"
    activations_per_key = 4

    async def activate(self) -> None:
        self.entered = 0
        self.release = asyncio.Event()

    async def work(self) -> str:
        self.entered += 1
        await self.release.wait()
        return self.id.key

    async def who(self) -> str:
        return self.id.key


class Alone(Grain):
    """The model's own rule: one activation for one key."""

    grain_type = "alone"

    async def who(self) -> str:
        return self.id.key


def _runtime(grain: type[Grain], probe: list[Any] | None = None) -> Runtime:
    """A runtime holding one kind of grain.

    Args:
        grain: The class to register.
        probe: Filled with each activation built, when a test needs them.

    Returns:
        The runtime.
    """
    built = Runtime(idle_seconds=1e9, sweep_seconds=1e9)
    if probe is None:
        built.register(grain)
    else:
        built.register(grain, lambda grain_id: _kept(probe, grain(grain_id)))
    return built


def _kept(store: list[Any], grain: Grain) -> Grain:
    """Remembers an activation.

    Args:
        store: Where to keep it.
        grain: The activation.

    Returns:
        The same grain.
    """
    store.append(grain)
    return grain


async def test_a_pool_spreads_callers_over_its_workers() -> None:
    reference = _runtime(Pooled).reference(Pooled, "a")

    answered = [await reference.who() for _ in range(8)]

    assert sorted(set(answered)) == ["a#0", "a#1", "a#2", "a#3"]
    assert answered == ["a#0", "a#1", "a#2", "a#3"] * 2, "in turn, not at random"


async def test_a_pool_answers_as_many_calls_at_once_as_it_has_workers() -> None:
    """The point of the kind: four serialised workers answer four at a time
    where one serialised grain answers one.
    """
    grains: list[Pooled] = []
    runtime = _runtime(Pooled, grains)
    reference = runtime.reference(Pooled, "a")

    calls = [asyncio.create_task(reference.work()) for _ in range(4)]
    for _ in range(50):
        await asyncio.sleep(0)

    assert len(grains) == 4, "four workers were built"
    assert all(grain.entered == 1 for grain in grains), "each is inside one call"

    for grain in grains:
        grain.release.set()
    assert sorted(await asyncio.gather(*calls)) == ["a#0", "a#1", "a#2", "a#3"]


async def test_a_fifth_caller_waits_for_a_worker() -> None:
    """A pool of four is a pool of four, not a promise of unlimited hands."""
    grains: list[Pooled] = []
    runtime = _runtime(Pooled, grains)
    reference = runtime.reference(Pooled, "a")

    calls = [asyncio.create_task(reference.work()) for _ in range(5)]
    for _ in range(50):
        await asyncio.sleep(0)

    assert sum(grain.entered for grain in grains) == 4, "the fifth is queued"

    for grain in grains:
        grain.release.set()
    await asyncio.gather(*calls)


async def test_different_keys_get_pools_of_their_own() -> None:
    runtime = _runtime(Pooled)

    assert await runtime.reference(Pooled, "a").who() == "a#0"
    assert await runtime.reference(Pooled, "b").who() == "b#0"
    assert runtime.activated == 2


async def test_a_grain_without_a_pool_keeps_the_model_s_rule() -> None:
    """The default, and the identity a caller used is the one that answers."""
    runtime = _runtime(Alone)

    assert await runtime.reference(Alone, "a").who() == "a"
    assert await runtime.reference(Alone, "a").who() == "a"
    assert runtime.stats.activations == 1


async def test_a_negative_pool_is_refused_at_registration() -> None:
    """A number that cannot mean anything is refused where it is written."""
    import pytest

    class Nonsense(Grain):
        grain_type = "nonsense"
        activations_per_key = -1

        async def who(self) -> str:
            return self.id.key

    with pytest.raises(ValueError, match="negative number of activations"):
        Runtime().register(Nonsense)
