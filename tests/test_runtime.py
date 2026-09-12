"""What the runtime promises, checked by running it.

The interesting cases are all about time and overlap: two callers meeting a
cold grain, a slow call meeting the sweeper, a reentrant grain against a
serialised one. None of them needs a clock that actually advances, so the
runtime takes its clock as a parameter and these tests spend no seconds.
"""

from __future__ import annotations

import asyncio

import pytest

from nigrains import Grain, GrainId, GrainNotRegisteredError, NoSuchGrainMethodError, Runtime


class Clock:
    """A monotonic clock that only moves when told to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Counter(Grain):
    """Keeps a number, and records what the runtime did to it."""

    grain_type = "counter"

    activations = 0
    deactivations = 0

    async def activate(self) -> None:
        Counter.activations += 1
        self.count = 0

    async def deactivate(self) -> None:
        Counter.deactivations += 1

    async def increment(self) -> int:
        self.count += 1
        return self.count

    async def _private(self) -> str:
        return "not yours"


class Slow(Grain):
    """Answers only when released, so overlap can be arranged exactly."""

    grain_type = "slow"

    def __init__(self, grain_id: GrainId) -> None:
        super().__init__(grain_id)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.concurrent = 0
        self.peak = 0
        self.deactivated_while_busy = False

    async def deactivate(self) -> None:
        self.deactivated_while_busy = self.concurrent > 0

    async def work(self) -> None:
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        self.entered.set()
        await self.release.wait()
        self.concurrent -= 1


class SlowReentrant(Slow):
    """The same, but declaring that overlap is fine."""

    grain_type = "slow_reentrant"

    reentrant = True


class SlowProbe(Slow):
    """A serialised grain under its own address, so a test can hold it.

    One class is one address now, so a test wanting a handle on the
    activation registers a kind of its own rather than the same class twice.
    """

    grain_type = "slow_probe"


class ReentrantProbe(SlowReentrant):
    """The reentrant half of the same arrangement."""

    grain_type = "reentrant_probe"


class SlowToActivate(Grain):
    """Blocks in activate() until let go."""

    grain_type = "slow_activate"

    started = 0
    release = asyncio.Event()

    async def activate(self) -> None:
        SlowToActivate.started += 1
        await SlowToActivate.release.wait()

    async def ping(self) -> str:
        return "pong"


class Broken(Grain):
    """Refuses to activate."""

    grain_type = "broken"

    attempts = 0

    async def activate(self) -> None:
        Broken.attempts += 1
        raise RuntimeError("no")

    async def ping(self) -> str:
        return "pong"


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def runtime(clock: Clock) -> Runtime:
    Counter.activations = 0
    Counter.deactivations = 0
    built = Runtime(idle_seconds=100.0, sweep_seconds=1000.0, clock=clock)
    built.register(Counter)
    built.register(Slow)
    built.register(SlowReentrant)
    built.register(SlowToActivate)
    built.register(Broken)
    return built


async def test_a_call_to_a_cold_grain_activates_it(runtime: Runtime) -> None:
    assert await runtime.call(GrainId("counter", "a"), "increment") == 1
    assert Counter.activations == 1


async def test_the_same_identity_reaches_the_same_activation(runtime: Runtime) -> None:
    """State survives between calls, which is the whole point of identity."""
    grain = GrainId("counter", "a")

    assert await runtime.call(grain, "increment") == 1
    assert await runtime.call(grain, "increment") == 2
    assert Counter.activations == 1


async def test_different_keys_are_different_grains(runtime: Runtime) -> None:
    assert await runtime.call(GrainId("counter", "a"), "increment") == 1
    assert await runtime.call(GrainId("counter", "b"), "increment") == 1
    assert Counter.activations == 2


async def test_callers_meeting_a_cold_grain_wait_for_one_activation(runtime: Runtime) -> None:
    """The thundering herd is the normal case: a job asks the fleet for
    everything at once, and every one of those grains is cold.
    """
    SlowToActivate.started = 0
    SlowToActivate.release = asyncio.Event()
    grain = GrainId("slow_activate", "a")

    calls = [asyncio.create_task(runtime.call(grain, "ping")) for _ in range(5)]
    await asyncio.sleep(0)
    SlowToActivate.release.set()

    assert await asyncio.gather(*calls) == ["pong"] * 5
    assert SlowToActivate.started == 1


async def test_a_failed_activation_leaves_nothing_behind(runtime: Runtime) -> None:
    """Otherwise the next caller meets a half-built grain and cannot say so."""
    Broken.attempts = 0
    grain = GrainId("broken", "a")

    for _ in range(2):
        with pytest.raises(RuntimeError):
            await runtime.call(grain, "ping")

    assert Broken.attempts == 2
    assert runtime.activated == 0


async def test_a_failed_activation_reaches_every_waiting_caller(runtime: Runtime) -> None:
    Broken.attempts = 0
    grain = GrainId("broken", "a")

    results = await asyncio.gather(
        *(runtime.call(grain, "ping") for _ in range(3)), return_exceptions=True
    )

    assert all(isinstance(result, RuntimeError) for result in results)


async def test_a_non_reentrant_grain_runs_one_call_at_a_time(runtime: Runtime) -> None:
    """The model's default, and what lets grain state need no lock."""
    grains: list[Slow] = []
    runtime.register(SlowProbe, lambda grain_id: _remember(grains, SlowProbe(grain_id)))
    grain = GrainId(SlowProbe.grain_type, "a")

    calls = [asyncio.create_task(runtime.call(grain, "work")) for _ in range(3)]
    await _settle()

    assert grains[0].peak == 1

    grains[0].release.set()
    await asyncio.gather(*calls)
    assert grains[0].peak == 1


async def test_a_reentrant_grain_lets_its_callers_overlap(runtime: Runtime) -> None:
    """A grain whose methods only read must not queue concurrent searches.

    This is the case the fleet exists for, and the model's default would
    get it exactly wrong: three lookups into one memory would run one after
    another for no reason but a lock nobody needed.
    """
    grains: list[Slow] = []
    runtime.register(ReentrantProbe, lambda grain_id: _remember(grains, ReentrantProbe(grain_id)))
    grain = GrainId(ReentrantProbe.grain_type, "a")

    calls = [asyncio.create_task(runtime.call(grain, "work")) for _ in range(3)]
    await _settle()

    assert grains[0].peak == 3

    grains[0].release.set()
    await asyncio.gather(*calls)


async def test_an_unregistered_kind_is_refused_by_name(runtime: Runtime) -> None:
    with pytest.raises(GrainNotRegisteredError, match="ghost"):
        await runtime.call(GrainId("ghost", "a"), "ping")


async def test_a_method_that_is_not_there_is_refused(runtime: Runtime) -> None:
    with pytest.raises(NoSuchGrainMethodError, match="decrement"):
        await runtime.call(GrainId("counter", "a"), "decrement")


async def test_a_private_name_is_not_reachable_from_a_call(runtime: Runtime) -> None:
    """A call arrives from outside, and one day from off this node."""
    with pytest.raises(NoSuchGrainMethodError):
        await runtime.call(GrainId("counter", "a"), "_private")


async def test_something_that_is_not_callable_is_refused(runtime: Runtime) -> None:
    """`count` is state, not behaviour, and naming it must not read it."""
    await runtime.call(GrainId("counter", "a"), "increment")

    with pytest.raises(NoSuchGrainMethodError):
        await runtime.call(GrainId("counter", "a"), "count")


async def test_an_idle_grain_is_collected(runtime: Runtime, clock: Clock) -> None:
    await runtime.call(GrainId("counter", "a"), "increment")
    clock.advance(101.0)

    assert await runtime.collect() == 1
    assert runtime.activated == 0
    assert Counter.deactivations == 1


async def test_a_grain_still_answering_is_not_idle(runtime: Runtime, clock: Clock) -> None:
    """Idleness is measured from the last call to finish, so a slow grain is
    never deactivated underneath its own caller.
    """
    grains: list[Slow] = []
    runtime.register(SlowProbe, lambda grain_id: _remember(grains, SlowProbe(grain_id)))
    call = asyncio.create_task(runtime.call(GrainId(SlowProbe.grain_type, "a"), "work"))
    await _settle()
    clock.advance(1000.0)

    assert await runtime.collect() == 0

    grains[0].release.set()
    await call


async def test_a_collected_grain_starts_over_when_called_again(
    runtime: Runtime, clock: Clock
) -> None:
    grain = GrainId("counter", "a")
    assert await runtime.call(grain, "increment") == 1
    clock.advance(101.0)
    await runtime.collect()

    assert await runtime.call(grain, "increment") == 1
    assert Counter.activations == 2


async def test_leaving_the_runtime_deactivates_everything(clock: Clock) -> None:
    Counter.activations = 0
    Counter.deactivations = 0
    runtime = Runtime(idle_seconds=100.0, sweep_seconds=1000.0, clock=clock)
    runtime.register(Counter)

    async with runtime:
        await runtime.call(GrainId("counter", "a"), "increment")
        await runtime.call(GrainId("counter", "b"), "increment")

    assert runtime.activated == 0
    assert Counter.deactivations == 2


async def test_a_grain_that_cannot_tidy_up_still_goes(runtime: Runtime, clock: Clock) -> None:
    """A failing deactivate() must not let a grain keep itself alive."""

    class Stubborn(Grain):
        grain_type = "stubborn"

        async def deactivate(self) -> None:
            raise RuntimeError("no")

        async def ping(self) -> str:
            return "pong"

    runtime.register(Stubborn)
    await runtime.call(GrainId("stubborn", "a"), "ping")
    clock.advance(101.0)

    assert await runtime.collect() == 1
    assert runtime.activated == 0


async def test_registering_a_kind_twice_is_refused(runtime: Runtime) -> None:
    """Replacing it silently would leave activations of the old kind
    answering calls meant for the new one.
    """
    with pytest.raises(ValueError, match="counter"):
        runtime.register(Counter)


async def _settle() -> None:
    """Lets every task that can run without waiting on anything real run.

    A fixed number of loop turns rather than a sleep with a duration: what
    is being waited for is other coroutines reaching their first real
    suspension, and on one event loop that takes turns, not time.
    """
    for _ in range(50):
        await asyncio.sleep(0)


def _remember(store: list[Slow], grain: Slow) -> Slow:
    store.append(grain)
    return grain


async def test_a_grain_called_during_a_sweep_is_not_collected(
    runtime: Runtime, clock: Clock
) -> None:
    """The sweep yields between chunks, so what it listed can change under it.

    Without the second check a grain that went stale, then was called while
    the sweep was still walking, would be deactivated out from under its
    caller - the exact failure the in-flight count exists to prevent, moved
    from one call to the next.
    """
    grain = GrainId("counter", "a")
    await runtime.call(grain, "increment")
    clock.advance(101.0)

    # Used again, after the cutoff was passed but before anything collects.
    clock.advance(-1.0)
    await runtime.call(grain, "increment")

    assert await runtime.collect() == 0
    assert runtime.activated == 1


async def test_the_activation_future_is_dropped_once_it_has_resolved(
    runtime: Runtime,
) -> None:
    """It exists to make a cold start single, and a fleet pays for it per grain."""
    grain = GrainId("counter", "a")
    await runtime.call(grain, "increment")

    activation = runtime._activations[grain]
    assert activation.ready is None


async def test_a_reentrant_grain_never_gets_a_lock(runtime: Runtime) -> None:
    """One object per grain that nothing would ever acquire, across a fleet."""
    grains: list[Slow] = []
    runtime.register(ReentrantProbe, lambda grain_id: _remember(grains, ReentrantProbe(grain_id)))
    grain = GrainId(ReentrantProbe.grain_type, "a")
    grains_before = len(grains)
    task = asyncio.create_task(runtime.call(grain, "work"))
    await _settle()

    assert runtime._activations[grain].lock is None
    assert len(grains) == grains_before + 1

    grains[0].release.set()
    await task


async def test_a_serialised_grain_gets_its_lock_on_first_call(runtime: Runtime) -> None:
    """Made when it is first needed rather than when the grain is built."""
    grain = GrainId("counter", "a")
    await runtime.call(grain, "increment")

    assert runtime._activations[grain].lock is not None


async def test_one_caller_giving_up_does_not_take_the_others_with_it(
    runtime: Runtime,
) -> None:
    """The finding that held up publication, and the reason for an Event.

    A shared future is one object that every waiter awaits directly, so
    cancelling any one waiting task cancels the future - the activation
    everybody else waited for, and an InvalidStateError in the coroutine
    that goes on to resolve it. An `asyncio.timeout` around a call to a cold
    grain is enough to do it, which makes this an ordinary Tuesday and not
    an exotic interleaving.
    """
    SlowToActivate.started = 0
    SlowToActivate.release = asyncio.Event()
    grain = GrainId("slow_activate", "a")

    first = asyncio.create_task(runtime.call(grain, "ping"))
    waiters = [asyncio.create_task(runtime.call(grain, "ping")) for _ in range(3)]
    await _settle()

    waiters[0].cancel()
    await asyncio.gather(waiters[0], return_exceptions=True)
    SlowToActivate.release.set()

    assert await first == "pong"
    assert await waiters[1] == "pong"
    assert await waiters[2] == "pong"
    assert SlowToActivate.started == 1
    # And the grain is still usable afterwards, rather than answering every
    # later call with somebody else's cancellation.
    assert await runtime.call(grain, "ping") == "pong"


async def test_a_cancelled_activation_is_retried_rather_than_inherited(
    runtime: Runtime,
) -> None:
    """A waiter must not be handed a cancellation it never asked for.

    When the task that happened to start the activation is cancelled, the
    others are not cancelled - they are waiting. Raising CancelledError at
    them would be indistinguishable, to a TaskGroup or a timeout, from their
    own cancellation.
    """
    SlowToActivate.started = 0
    SlowToActivate.release = asyncio.Event()
    grain = GrainId("slow_activate", "a")

    first = asyncio.create_task(runtime.call(grain, "ping"))
    await _settle()
    second = asyncio.create_task(runtime.call(grain, "ping"))
    await _settle()

    first.cancel()
    await asyncio.gather(first, return_exceptions=True)
    SlowToActivate.release.set()

    assert await second == "pong"
    assert SlowToActivate.started == 2, "the second caller started its own activation"


async def test_a_grain_still_activating_is_never_idle(runtime: Runtime, clock: Clock) -> None:
    """The second finding: activation is not idleness.

    A cold entry has no calls counted and a timestamp from the moment it was
    created, so an activate() slower than the idle span looked exactly like
    abandonment - and the sweep deactivated a grain whose own first caller
    was still waiting for it.
    """
    SlowToActivate.started = 0
    SlowToActivate.release = asyncio.Event()
    grain = GrainId("slow_activate", "a")

    call = asyncio.create_task(runtime.call(grain, "ping"))
    await _settle()
    clock.advance(1_000.0)

    assert await runtime.collect() == 0
    assert runtime.activated == 1

    SlowToActivate.release.set()
    assert await call == "pong"


async def test_a_reference_calls_the_grain_it_names(runtime: Runtime) -> None:
    """The ordinary way to call a grain, and what a type checker sees."""
    counter = runtime.reference(Counter, "a")

    assert await counter.increment() == 1
    assert await counter.increment() == 2
    assert await runtime.reference(Counter, "b").increment() == 1


async def test_a_reference_holds_nothing_and_activates_nothing(runtime: Runtime) -> None:
    """Making one is an address, not a handle."""
    counter = runtime.reference(Counter, "a")
    assert runtime.activated == 0

    await counter.increment()
    assert runtime.activated == 1


async def test_a_method_that_is_not_there_fails_where_it_is_named(runtime: Runtime) -> None:
    """The point of a reference: the failure lands on the line, not in the call.

    A renamed grain method is otherwise a runtime error at whatever moment
    the caller happens to run, which on a fleet is the worst moment.
    """
    counter = runtime.reference(Counter, "a")

    with pytest.raises(NoSuchGrainMethodError, match="decrement"):
        # The type checker flags this line, which is the guarantee working:
        # a reference is typed as the grain, so a method that does not exist
        # is caught before anything runs. The test is here for the callers
        # who are not type-checked, and for the ones who ignore it.
        counter.decrement  # type: ignore[attr-defined]  # noqa: B018


async def test_state_is_not_readable_through_a_reference(runtime: Runtime) -> None:
    """`count` is state, and state does not travel; only calls do."""
    await runtime.call(GrainId(Counter.grain_type, "a"), "increment")
    counter = runtime.reference(Counter, "a")

    with pytest.raises(NoSuchGrainMethodError):
        counter.count  # noqa: B018


async def test_a_private_method_is_not_reachable_through_a_reference(runtime: Runtime) -> None:
    """A reference is a call from outside, and one day from off this node."""
    counter = runtime.reference(Counter, "a")

    with pytest.raises(NoSuchGrainMethodError):
        counter._private  # noqa: B018


async def test_a_grain_without_an_address_is_refused_at_both_ends() -> None:
    """A wire identifier is declared, never derived, so its absence is an error."""

    class Anonymous(Grain):
        async def ping(self) -> str:
            return "pong"

    with pytest.raises(ValueError, match="grain_type"):
        Runtime().reference(Anonymous, "a")
    with pytest.raises(ValueError, match="grain_type"):
        Runtime().register(Anonymous)


async def test_a_grain_is_its_own_factory_when_it_wants_only_an_identity(
    clock: Clock,
) -> None:
    """Most grains take nothing else, and saying so twice is noise."""
    Counter.activations = 0
    built = Runtime(idle_seconds=100.0, sweep_seconds=1000.0, clock=clock)
    built.register(Counter)

    assert await built.reference(Counter, "a").increment() == 1


async def test_leaving_waits_for_a_call_still_in_flight() -> None:
    """The sweep refuses to collect a busy grain; leaving used not to.

    That put the failure the in-flight count exists to prevent at the one
    moment it is most likely - a process stopping in the middle of work -
    and ran every deactivate() under a running call.
    """
    grains: list[Slow] = []
    runtime = Runtime(idle_seconds=100.0, sweep_seconds=1000.0, drain_seconds=5.0)
    runtime.register(SlowProbe, lambda grain_id: _remember(grains, SlowProbe(grain_id)))

    async with runtime:
        call = asyncio.create_task(runtime.call(GrainId(SlowProbe.grain_type, "a"), "work"))
        await _settle()
        assert grains[0].concurrent == 1
        grains[0].release.set()
        await call

    assert grains[0].deactivated_while_busy is False


async def test_leaving_does_not_wait_for_ever() -> None:
    """A grain that never finishes must not stop a process from stopping."""
    grains: list[Slow] = []
    runtime = Runtime(idle_seconds=100.0, sweep_seconds=1000.0, drain_seconds=0.0)
    runtime.register(SlowProbe, lambda grain_id: _remember(grains, SlowProbe(grain_id)))

    call = asyncio.create_task(runtime.call(GrainId(SlowProbe.grain_type, "a"), "work"))
    await _settle()

    async with runtime:
        pass

    assert runtime.activated == 0
    assert grains[0].deactivated_while_busy is True
    grains[0].release.set()
    await call


async def test_the_counters_say_what_the_runtime_has_been_doing(
    runtime: Runtime, clock: Clock
) -> None:
    """A host reporting on a fleet should not have to read private attributes."""
    counter = runtime.reference(Counter, "a")
    await counter.increment()
    await counter.increment()
    await runtime.reference(Counter, "b").increment()

    stats = runtime.stats
    assert (stats.activated, stats.activations, stats.calls) == (2, 2, 3)
    assert (stats.deactivations, stats.evictions, stats.in_flight) == (0, 0, 0)

    clock.advance(101.0)
    await runtime.collect()

    assert runtime.stats.activated == 0
    assert runtime.stats.deactivations == 2
    assert runtime.stats.activations == 2, "the total does not go down when grains are collected"


async def test_a_failed_activation_is_counted_apart_from_a_failed_call(
    runtime: Runtime,
) -> None:
    """Nothing was built, and the next call will try again - a different fact."""
    Broken.attempts = 0

    with pytest.raises(RuntimeError):
        await runtime.reference(Broken, "a").ping()

    assert runtime.stats.failed_activations == 1
    assert runtime.stats.activated == 0


async def test_calls_in_flight_are_visible_while_they_run(runtime: Runtime) -> None:
    """The number a host watches when it wants to know if anything is stuck."""
    grains: list[Slow] = []
    runtime.register(SlowProbe, lambda grain_id: _remember(grains, SlowProbe(grain_id)))
    calls = [
        asyncio.create_task(runtime.call(GrainId(SlowProbe.grain_type, str(n)), "work"))
        for n in range(3)
    ]
    await _settle()

    assert runtime.stats.in_flight == 3

    for grain in grains:
        grain.release.set()
    await asyncio.gather(*calls)
    assert runtime.stats.in_flight == 0


async def test_a_bounded_fleet_collects_the_least_recently_used(clock: Clock) -> None:
    """What makes it a cache rather than a set that only grows."""
    Counter.activations = 0
    runtime = Runtime(idle_seconds=1e9, sweep_seconds=1e9, max_activations=2, clock=clock)
    runtime.register(Counter)

    await runtime.reference(Counter, "a").increment()
    clock.advance(1.0)
    await runtime.reference(Counter, "b").increment()
    clock.advance(1.0)
    # Touching "a" makes "b" the least recently used.
    await runtime.reference(Counter, "a").increment()
    clock.advance(1.0)
    await runtime.reference(Counter, "c").increment()

    assert runtime.activated == 2
    assert runtime.stats.evictions == 1
    # "a" stayed, so it kept counting; "b" was the one that went.
    assert await runtime.reference(Counter, "a").increment() == 3
    # And asking for "b" again builds it afresh - at the cost of evicting
    # whatever is now least wanted, which is why this is the last thing the
    # test asks. An earlier draft asked it first and then expected "a" to
    # have survived, which the eviction it had just caused made untrue.
    assert await runtime.reference(Counter, "b").increment() == 1
    assert runtime.stats.evictions == 2


async def test_a_full_fleet_of_busy_grains_is_never_a_refused_call() -> None:
    """The grain being asked for is needed now; a cache size is not.

    Going over the bound is the lesser wrong: refusing would stop work to
    honour a number, which is the wrong way round.
    """
    grains: list[Slow] = []
    runtime = Runtime(idle_seconds=1e9, sweep_seconds=1e9, max_activations=1)
    runtime.register(SlowProbe, lambda grain_id: _remember(grains, SlowProbe(grain_id)))

    first = asyncio.create_task(runtime.call(GrainId(SlowProbe.grain_type, "a"), "work"))
    await _settle()
    second = asyncio.create_task(runtime.call(GrainId(SlowProbe.grain_type, "b"), "work"))
    await _settle()

    assert runtime.activated == 2, "both are busy, so neither could be evicted"
    assert runtime.stats.evictions == 0

    for grain in grains:
        grain.release.set()
    await asyncio.gather(first, second)


async def test_an_unbounded_fleet_evicts_nothing(clock: Clock) -> None:
    """The default, and the ordering work is skipped entirely for it."""
    runtime = Runtime(idle_seconds=1e9, sweep_seconds=1e9, clock=clock)
    runtime.register(Counter)

    for key in "abcdef":
        await runtime.reference(Counter, key).increment()

    assert runtime.activated == 6
    assert runtime.stats.evictions == 0
