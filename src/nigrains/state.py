"""State a grain keeps somewhere else, and what happens when two write it.

**This package refuses to be a storage library and insists on being a
concurrency one.** Where the bytes go is the part every user already has an
opinion about, and serialization is theirs; a port over two operations is
all that is owed there. What is *not* optional is the third thing Orleans'
grain storage gives and the first two do not: a version, so that two
activations of one grain writing the same state produce a detected conflict
rather than a lost update.

That is not a nicety here, it is a debt. The cluster admits that single
activation is best effort under a partition - two halves each believe they
own a share, and each is right about its own half. A package that allows
two activations and offers no way to notice them both writing has handed
its users a trap with no floor under it.

**Nothing is saved automatically.** A grain writes when it decides its state
is worth writing, because the alternative - writing on deactivation - writes
on every collection, hides the failure when the write fails, and turns an
eviction into a round trip. Explicit is a line of code; implicit is a
surprise.

The shape a grain uses::

    class Basket(Grain):
        grain_type = "basket"
        persistent = True

        async def activate(self) -> None:
            self.items = self.state.data or []

        async def add(self, item: str) -> None:
            self.items.append(item)
            await self.state.save(self.items)

And the shape recovery takes, which is the whole reason the version exists::

        async def add(self, item: str) -> None:
            while True:
                try:
                    self.items.append(item)
                    await self.state.save(self.items)
                    break
                except ConcurrentChange:
                    self.items = await self.state.reload() or []

A note for whoever edits this file: the example above once contained a bare
``return`` on its own line, and the docstring formatter read it as a Google
section header and rewrote it to ``Return:``, quietly turning the example
into nonsense. Examples in these docstrings avoid a line that is only a
section keyword.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from nigrains.errors import GrainError

if TYPE_CHECKING:
    from nigrains.grain import GrainId

type Version = str
"""An opaque token naming one version of one grain's state.

A string because that is what every store that already does this calls an
ETag, and because a caller must never do arithmetic on it. What it means
inside is the store's business; what it means outside is "the version I read
is still the version there"."""


class ConcurrentChange(GrainError):  # noqa: N818 - it names the situation, not
    # a category of fault, and reads at the catch site as the thing that
    # happened rather than as a suffix
    """Somebody else wrote this state since it was read.

    The whole reason a version travels with the data. Two activations of one
    grain are possible - a cluster says so plainly - and without this the
    second write would silently replace the first.

    A grain catching this reloads, re-applies what it was doing, and writes
    again. A grain that catches it and gives up has still done better than a
    grain that never saw it.

    Attributes:
        grain: Which grain's state.
        expected: The version the writer held.
        found: The version that is actually there, or None when the state
            has been deleted since.
    """

    def __init__(self, grain: str, expected: Version | None, found: Version | None) -> None:
        """Names what was expected and what was found.

        Args:
            grain: Identity of the grain whose state this is.
            expected: The version the writer held.
            found: What is there now.
        """
        super().__init__(
            f"state of {grain} changed under a write: expected {expected!r}, found {found!r}"
        )
        self.grain = grain
        self.expected = expected
        self.found = found


@dataclass(frozen=True, slots=True)
class Stored:
    """What a store hands back.

    Attributes:
        data: Whatever was saved. This package never looks inside it.
        version: The version it was saved as.
    """

    data: Any
    version: Version


@runtime_checkable
class StateStore(Protocol):
    """Where a grain's state lives.

    Three operations, and the version in all of them. An implementation
    that ignores ``expected`` compiles, runs, and removes the only reason
    this port exists - so an implementation is not finished until it has a
    test where two writers race and one of them loses.
    """

    async def read(self, grain_id: GrainId) -> Stored | None:
        """Reads a grain's state.

        Args:
            grain_id: Whose state.

        Returns:
            What is stored, or None when nothing is.
        """
        ...

    async def write(self, grain_id: GrainId, data: Any, expected: Version | None) -> Version:  # noqa: ANN401
        """Writes a grain's state, if nobody else has.

        Args:
            grain_id: Whose state.
            data: What to store.
            expected: The version the writer read, or None when it read
                nothing and believes nothing is there.

        Returns:
            The version the state now has.

        Raises:
            ConcurrentChange: What is stored is not what the writer read.
        """
        ...

    async def delete(self, grain_id: GrainId, expected: Version | None) -> None:
        """Removes a grain's state, if nobody else has changed it.

        Args:
            grain_id: Whose state.
            expected: The version the caller read.

        Raises:
            ConcurrentChange: What is stored is not what the caller read.
        """
        ...


class InMemoryStateStore:
    """A store in a dictionary, for tests and for one process.

    **Not a cache and not durable**: it lives and dies with the process, so
    a grain using it survives deactivation and does not survive a restart.
    That is enough for a test and enough for a single-node deployment that
    only needs state to outlive an activation, and it is not enough for
    anything else - which is why the package ships this one and no other.
    """

    def __init__(self) -> None:
        """Starts empty."""
        self._rows: dict[GrainId, Stored] = {}
        self._next = 0

    async def read(self, grain_id: GrainId) -> Stored | None:
        """Reads a grain's state.

        Args:
            grain_id: Whose state.

        Returns:
            What is stored, or None.
        """
        return self._rows.get(grain_id)

    async def write(self, grain_id: GrainId, data: Any, expected: Version | None) -> Version:  # noqa: ANN401
        """Writes a grain's state, if the version still matches.

        Args:
            grain_id: Whose state.
            data: What to store.
            expected: The version the writer read.

        Returns:
            The new version.

        Raises:
            ConcurrentChange: The stored version is not the expected one.
        """
        current = self._rows.get(grain_id)
        found = current.version if current else None
        if found != expected:
            raise ConcurrentChange(str(grain_id), expected, found)
        self._next += 1
        stored = Stored(data=data, version=str(self._next))
        self._rows[grain_id] = stored
        return stored.version

    async def delete(self, grain_id: GrainId, expected: Version | None) -> None:
        """Removes a grain's state, if the version still matches.

        Args:
            grain_id: Whose state.
            expected: The version the caller read.

        Raises:
            ConcurrentChange: The stored version is not the expected one.
        """
        current = self._rows.get(grain_id)
        found = current.version if current else None
        if found != expected:
            raise ConcurrentChange(str(grain_id), expected, found)
        self._rows.pop(grain_id, None)


class GrainState:
    """One grain's handle on its own state.

    Attached by the runtime before ``activate`` runs, so a grain can read
    what it had without knowing where it was kept.

    Attributes:
        data: What was read, or None when nothing was stored. Whatever the
            grain last saved through this handle, after that.
        version: The version currently held, and the one a save will offer
            as its expectation. None means "nothing was there".
    """

    __slots__ = ("_grain_id", "_store", "data", "version")

    # Annotated as well as slotted. Inferring these from the assignments in
    # __init__ gives `data` the type `Any | None`, and a caller doing
    # anything with it then has to prove it is not None - which is exactly
    # the noise `Any` exists to spare them.
    data: Any
    version: Version | None

    def __init__(self, store: StateStore, grain_id: GrainId, stored: Stored | None) -> None:
        """Binds a handle to one grain's state.

        Args:
            store: Where it lives.
            grain_id: Whose state.
            stored: What was read on activation.
        """
        self._store = store
        self._grain_id = grain_id
        self.data = stored.data if stored else None
        self.version = stored.version if stored else None

    async def save(self, data: Any) -> None:  # noqa: ANN401 - the grain's own
        """Writes, if nobody else has written since this was read.

        Args:
            data: What to store.

        Raises:
            ConcurrentChange: Somebody else wrote first. The handle is left
                holding the version it had, so a caller that reloads and
                tries again is doing the right thing and a caller that
                simply retries will fail the same way - deliberately.
        """
        self.version = await self._store.write(self._grain_id, data, self.version)
        self.data = data

    async def reload(self) -> Any:  # noqa: ANN401 - the grain's own
        """Reads again, taking whatever is there now.

        What a grain does after a conflict, before re-applying its change.

        Returns:
            The data now stored, or None.
        """
        stored = await self._store.read(self._grain_id)
        self.data = stored.data if stored else None
        self.version = stored.version if stored else None
        return self.data

    async def clear(self) -> None:
        """Removes the state, if nobody else has changed it.

        Raises:
            ConcurrentChange: Somebody else wrote first.
        """
        await self._store.delete(self._grain_id, self.version)
        self.data = None
        self.version = None
