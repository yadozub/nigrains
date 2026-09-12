"""The cluster, with no network under it.

Everything a cluster does apart from the wire - placement, forwarding, a
grain activating on its owner and nowhere else, the ground moving under a
running fleet - is exercised here in one process. What is left for a real
transport is serialization and failure.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest

from nigrains import (
    Cluster,
    Grain,
    GrainId,
    LoopbackTransport,
    Ring,
    Runtime,
    StaticMembership,
    UnreachableNodeError,
    deadline,
)


class Tenant(Grain):
    """Says which node it woke up on.

    The node is remembered on the activation rather than in a table beside
    it. An earlier draft kept a dictionary and cleared it between phases of
    a test, which lost the answer for every grain that was already
    activated and therefore never built again - the test failed and the
    runtime was right.
    """

    grain_type = "tenant"
    tolerates_double_activation = True

    def __init__(self, grain_id: GrainId, node: str) -> None:
        super().__init__(grain_id)
        self.node = node

    async def where(self) -> str:
        return self.node

    async def slowly(self, seconds: float) -> str:
        await asyncio.sleep(seconds)
        return "done"


class Anxious(Grain):
    """Says it cannot survive two of itself."""

    grain_type = "anxious"

    async def ping(self) -> str:
        return "pong"


def _builder(node: str) -> Callable[[GrainId], Grain]:
    """A factory that builds tenants belonging to one node.

    Args:
        node: Where the grains will be built.

    Returns:
        The factory.
    """

    def build(grain_id: GrainId) -> Grain:
        return Tenant(grain_id, node)

    return build


def _fleet(*nodes: str) -> dict[str, Runtime]:
    """Builds one runtime per node, all sharing one loopback transport.

    Args:
        *nodes: The node names.

    Returns:
        The runtimes by node name.
    """
    transport = LoopbackTransport()
    runtimes: dict[str, Runtime] = {}
    for node in nodes:
        runtime = Runtime(
            idle_seconds=1e9,
            sweep_seconds=1e9,
            cluster=Cluster(StaticMembership(node, nodes), transport),
        )
        runtime.register(Tenant, _builder(node))
        runtimes[node] = runtime
        transport.nodes[node] = runtime
    return runtimes


def _cluster_of(runtime: Runtime) -> Cluster:
    """The cluster a runtime was built with.

    Args:
        runtime: The runtime.

    Returns:
        Its cluster.
    """
    cluster = runtime._cluster
    assert cluster is not None
    return cluster


def _transport_of(fleet: dict[str, Runtime]) -> LoopbackTransport:
    """The transport the fleet shares.

    Args:
        fleet: The runtimes.

    Returns:
        Their transport.
    """
    transport = _cluster_of(next(iter(fleet.values()))).transport
    assert isinstance(transport, LoopbackTransport)
    return transport


def _key_owned_by(runtime: Runtime, *, mine: bool) -> str:
    """Finds a key this runtime does, or does not, own.

    Args:
        runtime: Whose point of view.
        mine: True for a key it owns, False for one it does not.

    Returns:
        The key.
    """
    cluster = _cluster_of(runtime)
    return next(
        key
        for key in (f"k{n}" for n in range(200))
        if cluster.is_mine(GrainId(Tenant.grain_type, key)) is mine
    )


def test_a_ring_gives_every_node_the_same_answer() -> None:
    """The reason there is no directory to keep consistent."""
    first = Ring(["a", "b", "c"])
    second = Ring(["c", "a", "b"])

    for key in (f"grain/{n}" for n in range(200)):
        assert first.owner(key) == second.owner(key)


def test_a_ring_spreads_identities_evenly() -> None:
    """What the replica count buys, measured rather than assumed."""
    ring = Ring(["a", "b", "c", "d"])
    counts: dict[str, int] = {}
    for key in (f"grain/{n}" for n in range(4000)):
        owner = ring.owner(key)
        counts[owner] = counts.get(owner, 0) + 1

    assert set(counts) == {"a", "b", "c", "d"}
    assert min(counts.values()) > 700, counts
    assert max(counts.values()) < 1300, counts


def test_adding_a_node_moves_a_share_and_not_everything() -> None:
    """The property a consistent hash exists for."""
    before = Ring(["a", "b", "c"])
    after = Ring(["a", "b", "c", "d"])
    keys = [f"grain/{n}" for n in range(4000)]

    moved = [key for key in keys if before.owner(key) != after.owner(key)]

    assert 0.15 < len(moved) / len(keys) < 0.35, len(moved)
    assert all(after.owner(key) == "d" for key in moved), (
        "everything that moved went to the new node, rather than shuffling among the old"
    )


def test_a_ring_needs_somebody_on_it() -> None:
    with pytest.raises(ValueError, match="at least one member"):
        Ring([])
    with pytest.raises(ValueError, match="at least one point"):
        Ring(["a"], replicas=0)


async def test_a_grain_activates_on_its_owner_and_nowhere_else() -> None:
    fleet = _fleet("one", "two", "three")
    keys = [f"k{n}" for n in range(30)]

    for key in keys:
        answered = {await fleet[node].reference(Tenant, key).where() for node in fleet}
        assert len(answered) == 1, f"{key} answered from more than one node: {answered}"

    built = sum(runtime.activated for runtime in fleet.values())
    assert built == len(keys), "one activation each, wherever the caller stood"


async def test_a_call_to_a_grain_elsewhere_is_forwarded_once() -> None:
    fleet = _fleet("one", "two")
    mine = _key_owned_by(fleet["one"], mine=True)
    theirs = _key_owned_by(fleet["one"], mine=False)

    await fleet["one"].reference(Tenant, mine).where()
    assert fleet["one"].stats.forwarded == 0

    await fleet["one"].reference(Tenant, theirs).where()
    assert fleet["one"].stats.forwarded == 1
    assert fleet["two"].activated == 1


async def test_moving_the_ground_moves_a_share_of_the_grains() -> None:
    """A node joining takes its share, and callers follow without asking."""
    fleet = _fleet("one", "two")
    keys = [f"k{n}" for n in range(40)]
    for key in keys:
        await fleet["one"].reference(Tenant, key).where()

    transport = _transport_of(fleet)
    for runtime in fleet.values():
        membership = _cluster_of(runtime).membership
        assert isinstance(membership, StaticMembership)
        membership.replace(["one", "two", "three"])
    third = Runtime(
        idle_seconds=1e9,
        sweep_seconds=1e9,
        cluster=Cluster(StaticMembership("three", ["one", "two", "three"]), transport),
    )
    third.register(Tenant, _builder("three"))
    transport.nodes["three"] = third

    answered = {key: await fleet["one"].reference(Tenant, key).where() for key in keys}
    moved = [key for key, node in answered.items() if node == "three"]

    assert moved, "the new node took a share"
    assert len(moved) < len(keys), "and only a share"


async def test_a_deadline_survives_the_hop_as_seconds_not_as_a_reading() -> None:
    """A monotonic reading on one machine means nothing on another."""
    fleet = _fleet("one", "two")
    theirs = _key_owned_by(fleet["one"], mine=False)

    with pytest.raises(TimeoutError), deadline(0.05):
        await fleet["one"].reference(Tenant, theirs).slowly(5.0)


async def test_a_node_nobody_is_listening_on_says_so() -> None:
    transport = LoopbackTransport()
    runtime = Runtime(
        idle_seconds=1e9,
        sweep_seconds=1e9,
        cluster=Cluster(StaticMembership("one", ["one", "ghost"]), transport),
    )
    runtime.register(Tenant, _builder("one"))
    transport.nodes["one"] = runtime
    theirs = _key_owned_by(runtime, mine=False)

    with pytest.raises(UnreachableNodeError, match="ghost"):
        await runtime.reference(Tenant, theirs).where()


async def test_a_grain_that_cannot_survive_two_is_refused_by_a_cluster() -> None:
    """At boot, rather than at the first partition."""
    runtime = Runtime(cluster=Cluster(StaticMembership("one"), LoopbackTransport()))

    with pytest.raises(ValueError, match="does not tolerate double activation"):
        runtime.register(Anxious)


async def test_the_same_grain_is_welcome_without_a_cluster() -> None:
    """The refusal is about the deployment, not about the grain."""
    Runtime().register(Anxious)


async def test_membership_is_started_and_stopped_with_the_runtime() -> None:
    class Counting(StaticMembership):
        started = 0
        stopped = 0

        async def start(self) -> None:
            type(self).started += 1

        async def stop(self) -> None:
            type(self).stopped += 1

    runtime = Runtime(cluster=Cluster(Counting("one"), LoopbackTransport()))
    async with runtime:
        assert (Counting.started, Counting.stopped) == (1, 0)

    assert (Counting.started, Counting.stopped) == (1, 1)


async def test_a_delivered_call_is_not_routed_again() -> None:
    """Otherwise two nodes disagreeing for a moment bounce a call between them."""
    fleet = _fleet("one", "two")
    theirs = _key_owned_by(fleet["one"], mine=False)

    # Delivered to the wrong node on purpose: it serves the call anyway.
    answer: Any = await fleet["one"].deliver(GrainId(Tenant.grain_type, theirs), "where", (), {})

    assert answer == "one"
    assert fleet["one"].stats.forwarded == 0
