"""State that outlives an activation, and the conflict that makes it safe."""

from __future__ import annotations

import asyncio

import pytest

from nigrains import (
    ConcurrentChange,
    Grain,
    GrainId,
    InMemoryStateStore,
    Runtime,
    Stored,
)


class Basket(Grain):
    """Keeps a list, and writes it when it changes."""

    grain_type = "basket"
    persistent = True

    async def activate(self) -> None:
        self.items: list[str] = list(self.state.data or [])

    async def add(self, item: str) -> None:
        self.items.append(item)
        await self.state.save(self.items)

    async def contents(self) -> list[str]:
        return self.items

    async def forget(self) -> None:
        await self.state.clear()
        self.items = []


class Stubborn(Grain):
    """Reloads and re-applies rather than giving up, which is the whole point."""

    grain_type = "stubborn"
    persistent = True

    async def activate(self) -> None:
        self.items: list[str] = list(self.state.data or [])

    async def add(self, item: str) -> None:
        while True:
            try:
                await self.state.save([*self.items, item])
                break
            except ConcurrentChange:
                reloaded = await self.state.reload()
                self.items = list(reloaded) if reloaded else []
        self.items = list(self.state.data)


class Plain(Grain):
    """Wants nothing stored."""

    grain_type = "plain"

    async def ping(self) -> str:
        return "pong"


def _runtime(grain: type[Grain], store: InMemoryStateStore | None) -> Runtime:
    """A runtime holding one kind of grain.

    Args:
        grain: The class to register.
        store: Where state lives, or None.

    Returns:
        The runtime.
    """
    built = Runtime(idle_seconds=-1.0, sweep_seconds=1e9, state=store)
    built.register(grain)
    return built


async def test_state_outlives_the_activation() -> None:
    """The reason to keep it anywhere at all."""
    store = InMemoryStateStore()
    runtime = _runtime(Basket, store)
    await runtime.reference(Basket, "a").add("apple")

    assert await runtime.collect() == 1
    assert await runtime.reference(Basket, "a").contents() == ["apple"]


async def test_a_grain_starts_with_nothing_when_nothing_was_stored() -> None:
    runtime = _runtime(Basket, InMemoryStateStore())

    assert await runtime.reference(Basket, "a").contents() == []


async def test_two_activations_writing_produce_a_conflict_not_a_lost_update() -> None:
    """The debt this milestone exists to pay.

    Two runtimes are two activations of one identity - which a partition
    allows - and without the version the second write would replace the
    first in silence.
    """
    store = InMemoryStateStore()
    first, second = _runtime(Basket, store), _runtime(Basket, store)

    # Both read before either writes, which is what a race is. An earlier
    # draft of this test let the second activate after the first had saved,
    # so it read the new version, and there was correctly no conflict to
    # find - the test was wrong and the code was right.
    await first.reference(Basket, "a").contents()
    await second.reference(Basket, "a").contents()

    await first.reference(Basket, "a").add("from the first")

    with pytest.raises(ConcurrentChange) as raised:
        await second.reference(Basket, "a").add("from the second")

    assert raised.value.expected is None, "the second read nothing and believed nothing was there"
    assert raised.value.found is not None
    assert await first.reference(Basket, "a").contents() == ["from the first"]


async def test_a_grain_that_reloads_and_retries_gets_both_writes_in() -> None:
    """What the conflict is for: not failing, but noticing."""
    store = InMemoryStateStore()
    first, second = _runtime(Stubborn, store), _runtime(Stubborn, store)

    await first.reference(Stubborn, "a").add("one")
    await second.reference(Stubborn, "a").add("two")

    stored = await store.read(GrainId("stubborn", "a"))
    assert stored is not None
    assert stored.data == ["one", "two"]


async def test_a_conflict_leaves_the_handle_where_it_was() -> None:
    """A caller that simply retries fails the same way, deliberately."""
    store = InMemoryStateStore()
    first, second = _runtime(Basket, store), _runtime(Basket, store)
    await second.reference(Basket, "a").contents()
    await first.reference(Basket, "a").add("one")

    for _ in range(2):
        with pytest.raises(ConcurrentChange):
            await second.reference(Basket, "a").add("two")


async def test_clearing_removes_the_state() -> None:
    store = InMemoryStateStore()
    runtime = _runtime(Basket, store)
    await runtime.reference(Basket, "a").add("apple")

    await runtime.reference(Basket, "a").forget()

    assert await store.read(GrainId("basket", "a")) is None


async def test_clearing_what_somebody_else_changed_is_refused() -> None:
    store = InMemoryStateStore()
    first, second = _runtime(Basket, store), _runtime(Basket, store)
    await first.reference(Basket, "a").add("one")
    await second.reference(Basket, "a").contents()
    await first.reference(Basket, "a").add("two")

    with pytest.raises(ConcurrentChange):
        await second.reference(Basket, "a").forget()


async def test_a_grain_that_wants_no_state_is_never_read_for_one() -> None:
    """The default costs nothing: no round trip for a fleet of caches."""

    class Counting(InMemoryStateStore):
        reads = 0

        async def read(self, grain_id: GrainId) -> Stored | None:
            type(self).reads += 1
            return await super().read(grain_id)

    store = Counting()
    runtime = Runtime(idle_seconds=1e9, sweep_seconds=1e9, state=store)
    runtime.register(Plain)

    await runtime.reference(Plain, "a").ping()

    assert Counting.reads == 0


async def test_a_persistent_grain_without_a_store_is_refused_at_registration() -> None:
    """Rather than discovered at the first activation, in production."""
    runtime = Runtime()

    with pytest.raises(ValueError, match="no state store"):
        runtime.register(Basket)


async def test_asking_for_state_a_grain_never_declared_says_so() -> None:
    """An AttributeError further on would name the wrong thing."""
    runtime = _runtime(Plain, None)
    await runtime.reference(Plain, "a").ping()
    grain = next(iter(runtime._activations.values())).grain

    with pytest.raises(RuntimeError, match="has no state"):
        _ = grain.state


async def test_two_grains_do_not_share_state() -> None:
    store = InMemoryStateStore()
    runtime = _runtime(Basket, store)

    await runtime.reference(Basket, "a").add("apple")
    await runtime.reference(Basket, "b").add("pear")

    assert await runtime.reference(Basket, "a").contents() == ["apple"]
    assert await runtime.reference(Basket, "b").contents() == ["pear"]


async def test_concurrent_writers_in_one_process_still_conflict() -> None:
    """A serialised grain protects one activation, not two of them."""
    store = InMemoryStateStore()
    first, second = _runtime(Basket, store), _runtime(Basket, store)
    await first.reference(Basket, "a").contents()
    await second.reference(Basket, "a").contents()

    results = await asyncio.gather(
        first.reference(Basket, "a").add("one"),
        second.reference(Basket, "a").add("two"),
        return_exceptions=True,
    )

    assert sum(isinstance(r, ConcurrentChange) for r in results) == 1, results
