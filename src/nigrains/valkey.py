"""Membership kept in Valkey, or anything that speaks the same protocol.

Each node writes a key that expires, and refreshes it while it lives. The
membership is whatever keys are there.

**Nobody decides that anybody else has died.** A node stops being a member
because it stopped saying it was one, not because a peer concluded
something. That removes the hardest part of a gossip membership - a failure
detector, indirect probing so that one bad link does not evict a healthy
node, and the argument about who is right - and replaces it with a
dependency on something that has to be up anyway. It is the right trade for
a fleet that already runs a cache, and the wrong one for a fleet that does
not want a single thing to depend on.

**The lease is the only thing keeping the ring honest.** A node whose
process is frozen but whose key has not yet expired is still a member and
will still be routed to, for up to one lease. Shorter leases notice faster
and cost more traffic; the defaults notice within fifteen seconds, which
suits a fleet that is measured in nodes rather than thousands.

    membership = ValkeyMembership("http://a:8000", redis.asyncio.from_url(...))
    runtime = Runtime(cluster=Cluster(membership, HttpTransport()))

Needs `nigrains[valkey]`, which is redis-py - Valkey speaks the same
protocol, and so does Redis, so either will do.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence

DEFAULT_PREFIX = "nigrains:members"
"""Where the member keys live. One prefix per fleet: two fleets sharing a
Valkey and a prefix would each route to the other's nodes."""

DEFAULT_LEASE = 15.0
"""Seconds a member key survives without a refresh."""

DEFAULT_HEARTBEAT = 5.0
"""Seconds between refreshes.

A third of the lease, so two beats can be lost - to a slow moment, a garbage
collection, a blip - before a node that is perfectly well disappears from
the ring and its share of the fleet moves for nothing.
"""


class ValkeyMembership:
    """Membership by heartbeat, with the lease doing the deciding.

    Attributes:
        node: This node's identifier, and the thing other nodes will route
            to. With the default transport that is its base URL.
    """

    def __init__(
        self,
        node: str,
        client: Any,  # noqa: ANN401 - redis-py's client, not ours to name
        *,
        prefix: str = DEFAULT_PREFIX,
        lease: float = DEFAULT_LEASE,
        heartbeat: float = DEFAULT_HEARTBEAT,
    ) -> None:
        """Binds a membership to one node and one Valkey.

        Args:
            node: This node's identifier.
            client: An async redis-py client, already connected or willing
                to be. Not built here: which Valkey, with what
                authentication and what pool, is the deployment's business
                and not this package's.
            prefix: Where member keys live.
            lease: Seconds a member key survives unrefreshed.
            heartbeat: Seconds between refreshes.

        Raises:
            ValueError: The heartbeat is not shorter than the lease, which
                would mean a node evicting itself between beats.
        """
        if heartbeat >= lease:
            raise ValueError(
                f"a heartbeat of {heartbeat}s does not fit inside a lease of {lease}s: "
                f"this node would drop out of its own fleet between beats"
            )
        self.node = node
        self._client = client
        self._prefix = prefix
        self._lease = lease
        self._heartbeat = heartbeat
        self._members: tuple[str, ...] = (node,)
        self._beating: asyncio.Task[None] | None = None

    def members(self) -> Sequence[str]:
        """Returns the last membership seen.

        A snapshot, never a round trip: this is read on every routed call.

        Returns:
            The nodes. Before :meth:`start` it is this node alone, which is
            the right answer for a runtime that has not joined yet.
        """
        return self._members

    async def start(self) -> None:
        """Joins the fleet and begins refreshing.

        The first refresh and the first read happen before this returns, so
        a runtime that has started has a membership rather than a guess.
        """
        await self._announce()
        await self._refresh()
        self._beating = asyncio.create_task(self._beat())

    async def stop(self) -> None:
        """Leaves the fleet, rather than waiting to be noticed.

        Deleting the key means the other nodes rebalance in the time it
        takes them to read, instead of in the time it takes a lease to run
        out. A process that is killed rather than stopped gets the lease
        instead, which is what the lease is for.
        """
        if self._beating is not None:
            self._beating.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._beating
            self._beating = None
        with contextlib.suppress(Exception):
            await self._client.delete(self._key(self.node))

    async def _beat(self) -> None:
        """Refreshes the lease and re-reads the fleet, until cancelled."""
        while True:
            await asyncio.sleep(self._heartbeat)
            try:
                await self._announce()
                await self._refresh()
            except asyncio.CancelledError:
                raise
            except Exception:
                # Kept beating on purpose. A blip that loses one refresh
                # must not stop the next one, and the lease is long enough
                # to survive a few; giving up here would turn a moment of
                # trouble into leaving the fleet.
                _log_exception()

    async def _announce(self) -> None:
        """Writes this node's key with a fresh lease."""
        await self._client.set(self._key(self.node), b"", ex=int(self._lease))

    async def _refresh(self) -> None:
        """Reads who else is there.

        Scanned rather than fetched with ``KEYS``: the pattern is small
        here and the habit is not, and a library that teaches ``KEYS`` to a
        reader will be blamed for the one that runs against a large
        database.
        """
        seen: set[str] = set()
        async for key in self._client.scan_iter(match=f"{self._prefix}:*"):
            name = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            seen.add(name.removeprefix(f"{self._prefix}:"))
        # This node is a member whatever the store says: a read that missed
        # our own key - a blip, a scan racing an expiry - must not make a
        # runtime believe it is not in its own fleet and forward everything
        # somewhere else.
        seen.add(self.node)
        self._members = tuple(sorted(seen))

    def _key(self, node: str) -> str:
        """The key one node's membership lives under.

        Args:
            node: The node.

        Returns:
            The key.
        """
        return f"{self._prefix}:{node}"


def _log_exception() -> None:
    """Records a failed heartbeat without naming this module's logger twice."""
    import logging

    logging.getLogger(__name__).exception("a heartbeat failed; the lease has not run out yet")
