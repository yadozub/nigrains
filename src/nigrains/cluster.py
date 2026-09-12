"""Where a grain lives when there is more than one place it could.

Two ports and a ring. Nothing here opens a socket or takes a dependency:
this is the half of a cluster that can be got wrong in a single process, and
it is written first so that the half needing several processes has nothing
left to get wrong but the wire.

**Placement is computed, not recorded.** A directory that some node
maintains is a thing to keep consistent, to recover, and to be wrong about.
A consistent hash over the live members is none of those: every node
computes the same owner for the same identity without asking anybody, and
the answer changes only when the membership does. There is no directory, so
there is nothing to garbage-collect and nothing to be stale. What it costs
is that a node joining or leaving moves a share of the identities - about
one in N for N nodes, which the tests measure rather than assume.

**Membership is a snapshot, read without awaiting.** Routing happens on
every call, and a call that had to await the membership before it could
route would pay for the cluster on the local path too. An implementation
refreshes in the background between :meth:`Membership.start` and
:meth:`Membership.stop`; what routing reads is whatever was last seen.

**A deadline does not cross a wire as a deadline.** It is a reading of one
machine's monotonic clock and means nothing on another. What travels is the
seconds remaining, and the far side starts a deadline of its own from that.
Anything else is a clock-skew bug waiting for a busy afternoon.

**What this does not decide is liveness.** A membership that says a dead
node is alive will route to it and the call will fail; that is the
membership's problem, and staying out of it is this module's.
"""

from __future__ import annotations

import hashlib
from bisect import bisect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

    from nigrains.grain import GrainId

_DIGEST_BYTES = 8
"""Bytes of the hash kept as a ring position. Sixty-four bits keeps a few
thousand points apart with room to spare, and fewer bytes makes the integers
smaller and the sort cheaper."""

DEFAULT_REPLICAS = 128
"""Points each node occupies on the ring.

The number that decides how evenly identities spread. One point per node
leaves the shares lumpy, because the gaps between random points vary widely;
a hundred and twenty-eight flattens that for a few thousand integers on a
cluster of any size worth having. Measured in the tests rather than taken on
trust.
"""


@runtime_checkable
class Membership(Protocol):
    """Who is in the cluster, from this node's point of view.

    Attributes:
        node: This node's own identifier. Stable for the life of the
            process and unique in the cluster; anything else makes
            placement disagree with itself.
    """

    node: str

    def members(self) -> Sequence[str]:
        """Returns the nodes believed to be alive, including this one.

        Read on every routed call, so it must not await and must not be
        expensive. An implementation that learns membership from elsewhere
        keeps a snapshot and refreshes it in the background.

        Returns:
            The node identifiers. Order does not matter; the ring sorts.
        """
        ...

    async def start(self) -> None:
        """Begins whatever keeps the snapshot current."""
        ...

    async def stop(self) -> None:
        """Stops it, and leaves the cluster if that is a thing this does."""
        ...


@runtime_checkable
class Transport(Protocol):
    """How a call reaches a grain this node does not hold."""

    async def send(
        self,
        node: str,
        grain_id: GrainId,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        timeout: float | None,
    ) -> Any:  # noqa: ANN401 - whatever the grain returns, which is not ours to name
        """Delivers one call to another node and returns its answer.

        The call arrives unpacked because a transport has to put it on a
        wire: data rather than a bound call is the whole reason
        :meth:`~nigrains.runtime.Runtime.call` is spelled the way it is.

        Args:
            node: Where to send it.
            grain_id: Which grain.
            method: Which of its methods.
            args: Positional arguments.
            kwargs: Keyword arguments.
            timeout: **Seconds remaining**, not a deadline. A deadline is a
                reading of this machine's clock and means nothing on
                another; the far side starts its own from this number.

        Returns:
            Whatever the grain returned.

        Raises:
            Exception: Whatever the grain raised, as faithfully as the
                transport can manage. A transport that turns every remote
                failure into one of its own makes every remote grain
                untestable.
        """
        ...


class Ring:
    """A consistent hash over the nodes, giving every identity an owner.

    Built when the membership changes and read on every routed call, so it
    is built eagerly and looked up with a binary search.

    Attributes:
        members: The nodes this ring was built from, sorted and unique.
    """

    __slots__ = ("_nodes", "_points", "members")

    def __init__(self, members: Sequence[str], *, replicas: int = DEFAULT_REPLICAS) -> None:
        """Places every node on the ring.

        Args:
            members: The nodes to place. Duplicates are ignored and order
                does not matter, because the positions come from the names.
            replicas: Points per node.

        Raises:
            ValueError: No members, or a non-positive number of replicas.
                An empty ring owns nothing and would answer a lookup with
                an index error further on.
        """
        if replicas < 1:
            raise ValueError(f"a ring needs at least one point per node, got {replicas}")
        unique = sorted(set(members))
        if not unique:
            raise ValueError("a ring needs at least one member")
        self.members = tuple(unique)
        placed = sorted(
            (_position(f"{node}#{index}"), node) for node in unique for index in range(replicas)
        )
        self._points = [point for point, _ in placed]
        self._nodes = [node for _, node in placed]

    def owner(self, key: str) -> str:
        """Returns the node that owns an identity.

        Args:
            key: The identity, rendered as a string.

        Returns:
            The owning node - the same answer on every node holding the
            same membership, which is what makes a directory unnecessary.
        """
        return self._nodes[bisect(self._points, _position(key)) % len(self._nodes)]

    def __repr__(self) -> str:
        """Names the membership rather than the points.

        Returns:
            A short description.
        """
        return f"<Ring over {len(self.members)} nodes, {len(self._points)} points>"


def _position(key: str) -> int:
    """Places a string on the ring.

    BLAKE2b because it is in the standard library, fast, and even over short
    strings. Nothing here is a security decision; a non-cryptographic hash
    would serve as well if one were also in the standard library.

    Args:
        key: What to place.

    Returns:
        A ring position.
    """
    return int.from_bytes(
        hashlib.blake2b(key.encode("utf-8"), digest_size=_DIGEST_BYTES).digest(), "big"
    )


@dataclass(slots=True)
class Cluster:
    """What a runtime needs in order to route a call off this node.

    Attributes:
        membership: Who is alive.
        transport: How to reach them.
        replicas: Points per node on the ring. **The same on every node**,
            or they disagree about who owns what - which is why it lives
            here beside the membership rather than being a runtime option
            somebody could set differently in one deployment.
    """

    membership: Membership
    transport: Transport
    replicas: int = DEFAULT_REPLICAS
    _ring: Ring | None = field(default=None, init=False, repr=False)
    _seen: tuple[str, ...] = field(default=(), init=False, repr=False)

    def owner_of(self, grain_id: GrainId) -> str:
        """Returns the node that should hold a grain.

        The ring is rebuilt only when the membership has actually changed,
        because this runs on every routed call and building a ring is
        thousands of hashes.

        Args:
            grain_id: The identity to place.

        Returns:
            The owning node.
        """
        members = tuple(self.membership.members())
        if self._ring is None or members != self._seen:
            self._ring = Ring(members, replicas=self.replicas)
            self._seen = members
        return self._ring.owner(str(grain_id))

    def is_mine(self, grain_id: GrainId) -> bool:
        """Whether this node should hold a grain.

        Args:
            grain_id: The identity to place.

        Returns:
            True when this node owns it.
        """
        return self.owner_of(grain_id) == self.membership.node


class StaticMembership:
    """A membership that changes only when somebody says so.

    For one node, and for a test that wants to move the ground under a
    running fleet.

    Attributes:
        node: This node's identifier.
    """

    def __init__(self, node: str, members: Sequence[str] | None = None) -> None:
        """Fixes the membership at construction.

        Args:
            node: This node's identifier.
            members: Everybody, including this node. Defaults to this node
                alone.
        """
        self.node = node
        self._members = tuple(members) if members is not None else (node,)

    def members(self) -> Sequence[str]:
        """Returns the current membership.

        Returns:
            The nodes.
        """
        return self._members

    def replace(self, members: Sequence[str]) -> None:
        """Changes the membership, the way a real one changes by itself.

        Args:
            members: The new set.
        """
        self._members = tuple(members)

    async def start(self) -> None:
        """Nothing to start."""

    async def stop(self) -> None:
        """Nothing to stop."""


class LoopbackTransport:
    """Delivers to another runtime in this process.

    Every behaviour of a cluster that is not the wire itself - placement,
    forwarding, a grain activating on its owner and nowhere else, the ground
    moving under a running fleet - is exercised through this, in one
    process, with no sockets and nothing to wait for. What is left for a
    real transport to get wrong is serialization and failure, which is a
    much smaller thing to be careful about.

    Attributes:
        nodes: Runtimes by node identifier. A node missing from here is a
            node this transport cannot reach, which is a useful thing to
            arrange on purpose.
    """

    def __init__(self, nodes: dict[str, Any] | None = None) -> None:
        """Starts with whichever nodes are known.

        Args:
            nodes: Runtimes by node identifier; more may be added later,
                since a runtime and its transport are usually built in that
                order.
        """
        self.nodes: dict[str, Any] = dict(nodes or {})

    async def send(
        self,
        node: str,
        grain_id: GrainId,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        timeout: float | None,
    ) -> Any:  # noqa: ANN401 - whatever the grain returns
        """Hands the call to the runtime on that node.

        Args:
            node: Where to send it.
            grain_id: Which grain.
            method: Which of its methods.
            args: Positional arguments.
            kwargs: Keyword arguments.
            timeout: Seconds remaining.

        Returns:
            Whatever the grain returned.

        Raises:
            UnreachableNodeError: Nothing is listening on that node - which
                in this transport means it is not in ``nodes``, and in a
                real one means the same thing with more steps.
        """
        runtime = self.nodes.get(node)
        if runtime is None:
            raise UnreachableNodeError(node)
        return await runtime.deliver(grain_id, method, args, kwargs, timeout=timeout)


class UnreachableNodeError(ConnectionError):
    """Nothing answered on the node a call was placed to.

    A ``ConnectionError`` because that is what it is, whatever the transport
    is made of.

    Attributes:
        node: The node that did not answer.
    """

    def __init__(self, node: str) -> None:
        """Names the node.

        Args:
            node: The unreachable node.
        """
        super().__init__(f"no node {node!r} to deliver to")
        self.node = node
