"""Work on a schedule that outlives the activation that asked for it.

A timer belongs to an activation and dies with it. A reminder belongs to the
grain: it is written down, it survives collection and restart, and when it
comes due the runtime **wakes the grain** to serve it. That is the whole
difference, and it is why this needed a store and a cluster before it could
be written at all.

    class Digest(Grain):
        grain_type = "digest"
        tolerates_double_activation = True

        async def activate(self) -> None:
            await self.reminders.every("send", 86400.0)

        async def on_reminder(self, name: str) -> None:
            await self.send_the_digest()

Scheduling is idempotent by name: asking for the same name again moves the
schedule rather than adding a second one, which is what lets ``activate``
ask for it every time without thinking about whether it already exists.

**In a cluster, only the owner fires.** Every node scans, and each one skips
what the ring says is somebody else's - so a reminder fires once even though
several nodes are looking at the same store. When the membership changes the
new owner picks it up on its next scan, and the old one stops: nothing is
handed over, because nothing was held.

**A reminder that comes due while nothing is running does not fire late, it
fires next time somebody looks.** There is no daemon here beyond the scan a
runtime already runs, so a fleet that is entirely down does not accumulate a
backlog to stampede through on the way up: each reminder fires once when the
fleet returns, whatever it missed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nigrains.grain import GrainId


@dataclass(frozen=True, slots=True)
class Reminder:
    """One thing a grain asked to be woken for.

    Attributes:
        grain: Who to wake.
        name: What to call it, and the key that makes scheduling
            idempotent.
        due: When it should fire, on the runtime's clock.
        interval: Seconds until the next one after this fires, or None for
            a reminder that fires once and is forgotten.
    """

    grain: GrainId
    name: str
    due: float
    interval: float | None


@runtime_checkable
class ReminderStore(Protocol):
    """Where reminders are written down.

    Small on purpose. A store that can add, remove and answer "what is due"
    is enough, and every operation is keyed by grain and name so that
    asking twice is the same as asking once.
    """

    async def put(self, reminder: Reminder) -> None:
        """Writes a reminder, replacing any of the same name for that grain.

        Args:
            reminder: What to write.
        """
        ...

    async def drop(self, grain: GrainId, name: str) -> None:
        """Removes a reminder, if it is there.

        Args:
            grain: Whose.
            name: Which one.
        """
        ...

    async def due(self, before: float) -> Sequence[Reminder]:
        """Returns everything due at or before a moment.

        Args:
            before: The moment, on the runtime's clock.

        Returns:
            The reminders, in no particular order.
        """
        ...

    async def listed(self, grain: GrainId) -> Sequence[Reminder]:
        """Returns what one grain has scheduled.

        Args:
            grain: Whose.

        Returns:
            Its reminders.
        """
        ...


class InMemoryReminderStore:
    """Reminders in a dictionary, for tests and for one process.

    Survives deactivation, which is most of what a reminder is for, and does
    not survive a restart, which is the rest of it. Ships for the same
    reason the in-memory state store does, and with the same warning.
    """

    def __init__(self) -> None:
        """Starts empty."""
        self._rows: dict[tuple[GrainId, str], Reminder] = {}

    async def put(self, reminder: Reminder) -> None:
        """Writes a reminder, replacing one of the same name.

        Args:
            reminder: What to write.
        """
        self._rows[reminder.grain, reminder.name] = reminder

    async def drop(self, grain: GrainId, name: str) -> None:
        """Removes a reminder.

        Args:
            grain: Whose.
            name: Which one.
        """
        self._rows.pop((grain, name), None)

    async def due(self, before: float) -> Sequence[Reminder]:
        """Returns everything due at or before a moment.

        Args:
            before: The moment.

        Returns:
            The reminders.
        """
        return [row for row in list(self._rows.values()) if row.due <= before]

    async def listed(self, grain: GrainId) -> Sequence[Reminder]:
        """Returns one grain's reminders.

        Args:
            grain: Whose.

        Returns:
            Its reminders.
        """
        return [row for row in self._rows.values() if row.grain == grain]


class GrainReminders:
    """One grain's handle on its own schedule.

    Attached by the runtime before ``activate`` runs, so a grain can ask for
    a reminder in the same breath as it loads its state.
    """

    __slots__ = ("_clock", "_grain_id", "_store")

    def __init__(self, store: ReminderStore, grain_id: GrainId, clock: object) -> None:
        """Binds a handle to one grain.

        Args:
            store: Where reminders are kept.
            grain_id: Whose schedule this is.
            clock: The runtime's clock, so a reminder is due on the same
                reading the scan compares against.
        """
        self._store = store
        self._grain_id = grain_id
        self._clock = clock

    async def every(self, name: str, seconds: float) -> None:
        """Asks to be woken every so often, from now.

        Idempotent by name: asking again moves the schedule rather than
        adding a second one, which is what lets ``activate`` ask every time
        without checking.

        Args:
            name: What to call it. Arrives back as the argument to
                ``on_reminder``.
            seconds: How long between firings. The first is one interval
                from now, not immediately - a grain that wants something
                done at once does it in ``activate``.

        Raises:
            ValueError: A non-positive interval, which would ask the scan
                to fire it for ever without moving.
        """
        if seconds <= 0:
            raise ValueError(f"a reminder every {seconds}s would never stop firing")
        await self._store.put(Reminder(self._grain_id, name, self._now() + seconds, seconds))

    async def once(self, name: str, seconds: float) -> None:
        """Asks to be woken once, and then forgotten.

        Args:
            name: What to call it.
            seconds: How long from now.

        Raises:
            ValueError: A non-positive delay.
        """
        if seconds <= 0:
            raise ValueError(f"a reminder in {seconds}s is already late")
        await self._store.put(Reminder(self._grain_id, name, self._now() + seconds, None))

    async def cancel(self, name: str) -> None:
        """Forgets a reminder.

        Args:
            name: Which one. Cancelling one that is not there is not an
                error - a grain should not have to remember whether it
                asked.
        """
        await self._store.drop(self._grain_id, name)

    async def listed(self) -> Sequence[Reminder]:
        """Returns what this grain has scheduled.

        Returns:
            Its reminders.
        """
        return await self._store.listed(self._grain_id)

    def _now(self) -> float:
        """The runtime's current time.

        Returns:
            The reading.
        """
        clock = self._clock
        assert callable(clock)
        return float(clock())
