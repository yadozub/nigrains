"""Schedules that outlive the activation that asked for them."""

from __future__ import annotations

import asyncio

import pytest

from nigrains import (
    Cluster,
    Grain,
    GrainId,
    InMemoryReminderStore,
    LoopbackTransport,
    Reminder,
    Runtime,
    StaticMembership,
)


class Clock:
    """A clock that moves only when told to."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Digest(Grain):
    """Asks to be woken, and counts the wakings."""

    grain_type = "digest"
    tolerates_double_activation = True

    fired: list[tuple[str, str]] = []  # noqa: RUF012 - shared across activations on purpose

    async def activate(self) -> None:
        await self.reminders.every("send", 100.0)

    async def on_reminder(self, name: str) -> None:
        Digest.fired.append((self.id.key, name))

    async def ping(self) -> str:
        return "pong"


class Once(Grain):
    """Asks to be woken exactly once."""

    grain_type = "once"
    tolerates_double_activation = True

    fired: list[str] = []  # noqa: RUF012 - shared on purpose

    async def activate(self) -> None:
        await self.reminders.once("kick", 100.0)

    async def on_reminder(self, name: str) -> None:
        Once.fired.append(name)

    async def ping(self) -> str:
        return "pong"


class Broken(Grain):
    """Its reminder always fails."""

    grain_type = "broken_reminder"
    tolerates_double_activation = True

    attempts = 0

    async def activate(self) -> None:
        await self.reminders.every("try", 100.0)

    async def on_reminder(self, name: str) -> None:
        Broken.attempts += 1
        raise RuntimeError("no")

    async def ping(self) -> str:
        return "pong"


class Quiet(Grain):
    """Asks for nothing."""

    grain_type = "quiet"
    tolerates_double_activation = True

    async def ping(self) -> str:
        return "pong"


class Picky(Grain):
    """Schedules whatever it is told to, so the refusals can be seen."""

    grain_type = "picky"
    tolerates_double_activation = True

    async def repeat(self, seconds: float) -> None:
        await self.reminders.every("bad", seconds)

    async def delay(self, seconds: float) -> None:
        await self.reminders.once("bad", seconds)


def _runtime(
    grain: type[Grain],
    store: InMemoryReminderStore,
    clock: Clock,
    *,
    cluster: Cluster | None = None,
    idle_seconds: float = 1e9,
) -> Runtime:
    """A runtime holding one kind of grain and one reminder store.

    Args:
        grain: The class to register.
        store: Where reminders live.
        clock: The clock to drive.
        cluster: A cluster, when the test is about placement.
        idle_seconds: How long before a grain counts as idle.

    Returns:
        The runtime.
    """
    built = Runtime(
        idle_seconds=idle_seconds,
        sweep_seconds=1e9,
        reminders=store,
        cluster=cluster,
        clock=clock,
    )
    built.register(grain)
    return built


async def test_a_reminder_fires_when_it_comes_due() -> None:
    Digest.fired.clear()
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Digest, store, clock)
    await runtime.reference(Digest, "a").ping()

    assert await runtime.fire_due_reminders() == 0, "not before it is due"

    clock.advance(101.0)
    assert await runtime.fire_due_reminders() == 1
    assert Digest.fired == [("a", "send")]


async def test_a_reminder_outlives_the_activation_that_asked_for_it() -> None:
    """The whole difference between a reminder and a timer."""
    Digest.fired.clear()
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Digest, store, clock, idle_seconds=-1.0)
    await runtime.reference(Digest, "a").ping()

    assert await runtime.collect() == 1
    assert runtime.activated == 0

    clock.advance(101.0)
    assert await runtime.fire_due_reminders() == 1
    assert Digest.fired == [("a", "send")]
    assert runtime.activated == 1, "the grain was woken to serve it"


async def test_a_repeating_reminder_is_rescheduled_and_fires_again() -> None:
    Digest.fired.clear()
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Digest, store, clock)
    await runtime.reference(Digest, "a").ping()

    clock.advance(101.0)
    await runtime.fire_due_reminders()
    assert await runtime.fire_due_reminders() == 0, "rescheduled, not left due"

    clock.advance(101.0)
    await runtime.fire_due_reminders()
    assert len(Digest.fired) == 2


async def test_a_one_shot_reminder_is_forgotten_after_it_fires() -> None:
    Once.fired.clear()
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Once, store, clock)
    await runtime.reference(Once, "a").ping()

    clock.advance(101.0)
    assert await runtime.fire_due_reminders() == 1
    clock.advance(1000.0)
    assert await runtime.fire_due_reminders() == 0
    assert Once.fired == ["kick"]


async def test_asking_twice_by_the_same_name_moves_the_schedule() -> None:
    """Which is what lets activate ask every time without checking."""
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Digest, store, clock, idle_seconds=-1.0)
    await runtime.reference(Digest, "a").ping()
    await runtime.collect()
    await runtime.reference(Digest, "a").ping()

    assert len(await store.listed(GrainId(Digest.grain_type, "a"))) == 1


async def test_a_reminder_that_fails_keeps_its_schedule() -> None:
    """One bad afternoon must not silently end a daily job."""
    Broken.attempts = 0
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Broken, store, clock)
    await runtime.reference(Broken, "a").ping()

    clock.advance(101.0)
    await runtime.fire_due_reminders()
    clock.advance(101.0)
    await runtime.fire_due_reminders()

    assert Broken.attempts == 2


async def test_a_cancelled_reminder_does_not_fire() -> None:
    Digest.fired.clear()
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Digest, store, clock)
    await runtime.reference(Digest, "a").ping()

    await store.drop(GrainId(Digest.grain_type, "a"), "send")
    clock.advance(101.0)

    assert await runtime.fire_due_reminders() == 0
    assert Digest.fired == []


async def test_only_the_owner_fires_it() -> None:
    """Every node scans the same store; each fires its own share and no more.

    Counted per node rather than in total, because a total is satisfied by
    one node firing everything - which is what a fleet without the
    ownership check does, and it is wrong for a reason a total cannot see:
    two nodes scanning at the same moment would each find the same reminder
    due and each wake the grain for it.
    """
    Digest.fired.clear()
    clock, store = Clock(), InMemoryReminderStore()
    transport = LoopbackTransport()
    nodes = ("one", "two")
    clusters = {node: Cluster(StaticMembership(node, nodes), transport) for node in nodes}
    fleet = {node: _runtime(Digest, store, clock, cluster=clusters[node]) for node in nodes}
    for node, runtime in fleet.items():
        transport.nodes[node] = runtime

    keys = [f"k{n}" for n in range(12)]
    for key in keys:
        await fleet["one"].reference(Digest, key).ping()
    clock.advance(101.0)

    fired = {node: await runtime.fire_due_reminders() for node, runtime in fleet.items()}

    owed = {
        node: sum(clusters[node].is_mine(GrainId(Digest.grain_type, key)) for key in keys)
        for node in nodes
    }
    assert min(owed.values()) > 0, "a ring that put everything on one node proves nothing"
    assert fired == owed
    assert len(Digest.fired) == 12, "and every reminder fired, once"


async def test_a_grain_asking_for_reminders_without_a_store_says_so() -> None:
    runtime = Runtime(idle_seconds=1e9, sweep_seconds=1e9)
    runtime.register(Digest)

    with pytest.raises(RuntimeError, match="no store for them"):
        await runtime.reference(Digest, "a").ping()


async def test_a_reminder_for_a_grain_with_no_handler_is_not_an_error() -> None:
    """A schedule outlives the code that made it, including its handler."""
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Quiet, store, clock)
    await store.put(Reminder(GrainId(Quiet.grain_type, "a"), "orphan", 0.0, None))
    clock.advance(1.0)

    assert await runtime.fire_due_reminders() == 1
    assert await runtime.fire_due_reminders() == 0, "and it was forgotten, not retried"


async def test_an_interval_that_cannot_work_is_refused() -> None:
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Picky, store, clock)
    picky = runtime.reference(Picky, "a")

    with pytest.raises(ValueError, match="never stop firing"):
        await picky.repeat(0.0)
    with pytest.raises(ValueError, match="already late"):
        await picky.delay(-1.0)


async def test_a_runtime_with_no_reminder_store_fires_nothing() -> None:
    """Rather than refusing: nobody asked it to keep a schedule."""
    runtime = Runtime(idle_seconds=1e9, sweep_seconds=1e9)
    runtime.register(Quiet)

    assert await runtime.fire_due_reminders() == 0


class Watchful(Grain):
    """Looks at its own schedule from inside the handler."""

    grain_type = "watchful"
    tolerates_double_activation = True

    seen: list[float] = []  # noqa: RUF012 - shared on purpose

    async def activate(self) -> None:
        await self.reminders.every("look", 100.0)

    async def on_reminder(self, name: str) -> None:
        Watchful.seen.extend(row.due for row in await self.reminders.listed())

    async def ping(self) -> str:
        return "pong"


async def test_a_repeating_reminder_is_rescheduled_before_its_handler_runs() -> None:
    """Or a handler slower than the interval is found due and run twice."""
    Watchful.seen.clear()
    clock, store = Clock(), InMemoryReminderStore()
    runtime = _runtime(Watchful, store, clock)
    await runtime.reference(Watchful, "a").ping()

    clock.advance(101.0)
    await runtime.fire_due_reminders()

    assert Watchful.seen == [201.0], "already moved on while the handler was running"


async def test_the_runtime_scans_by_itself() -> None:
    """The wiring, on a real clock: nobody has to call the scan."""
    Digest.fired.clear()
    store = InMemoryReminderStore()
    runtime = Runtime(
        idle_seconds=1e9, sweep_seconds=1e9, reminders=store, reminder_scan_seconds=0.01
    )
    runtime.register(Digest)

    async with runtime:
        await runtime.reference(Digest, "a").ping()
        await store.put(Reminder(GrainId(Digest.grain_type, "a"), "now", 0.0, None))
        for _ in range(200):
            await asyncio.sleep(0.01)
            if Digest.fired:
                break

    assert Digest.fired == [("a", "now")]
