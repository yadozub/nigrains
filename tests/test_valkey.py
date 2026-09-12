"""Membership against a real Valkey, in a container.

A fake store would agree with whatever this module believes about
expiry, scanning and encoding, which is exactly the set of beliefs worth
checking. The container is the point.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from nigrains.valkey import ValkeyMembership

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

redis = pytest.importorskip("redis.asyncio")
testcontainers = pytest.importorskip("testcontainers.core.container")

IMAGE = "valkey/valkey:8.1.10-alpine"
PORT = 6379


@pytest.fixture(scope="session")
def valkey_url() -> Iterator[str]:
    """One Valkey for the whole file.

    A container per test would spend more time starting servers than
    checking anything; isolation comes from a prefix per test instead.
    """
    from testcontainers.core.container import DockerContainer
    from testcontainers.core.waiting_utils import wait_for_logs

    container = DockerContainer(IMAGE).with_exposed_ports(PORT)
    container.start()
    try:
        wait_for_logs(container, "Ready to accept connections", timeout=60)
        host = container.get_container_host_ip()
        yield f"redis://{host}:{container.get_exposed_port(PORT)}/0"
    finally:
        container.stop()


@pytest.fixture()
async def client(valkey_url: str) -> AsyncIterator[redis.Redis]:
    """A client that closes itself."""
    connected = redis.from_url(valkey_url)
    try:
        yield connected
    finally:
        await connected.aclose()


def _prefix(request: pytest.FixtureRequest) -> str:
    """A prefix nobody else in this file is using.

    Args:
        request: The running test.

    Returns:
        The prefix.
    """
    return f"test:{request.node.name}"


async def test_a_node_that_started_is_in_its_own_fleet(
    client: redis.Redis, request: pytest.FixtureRequest
) -> None:
    membership = ValkeyMembership("one", client, prefix=_prefix(request))

    assert membership.members() == ("one",), "and before starting, too"
    await membership.start()
    try:
        assert list(membership.members()) == ["one"]
    finally:
        await membership.stop()


async def test_nodes_find_each_other(client: redis.Redis, request: pytest.FixtureRequest) -> None:
    prefix = _prefix(request)
    first = ValkeyMembership("one", client, prefix=prefix)
    second = ValkeyMembership("two", client, prefix=prefix)

    await first.start()
    await second.start()
    try:
        # The second read after both had announced; the first has not
        # re-read yet, which is what a snapshot means.
        assert list(second.members()) == ["one", "two"]
        await first._refresh()
        assert list(first.members()) == ["one", "two"]
    finally:
        await first.stop()
        await second.stop()


async def test_leaving_is_immediate_rather_than_waiting_for_the_lease(
    client: redis.Redis, request: pytest.FixtureRequest
) -> None:
    """The other nodes rebalance in a read, not in a lease."""
    prefix = _prefix(request)
    first = ValkeyMembership("one", client, prefix=prefix)
    second = ValkeyMembership("two", client, prefix=prefix)
    await first.start()
    await second.start()

    await second.stop()
    await first._refresh()

    try:
        assert list(first.members()) == ["one"]
    finally:
        await first.stop()


async def test_a_node_that_stops_refreshing_falls_out_when_its_lease_runs_out(
    client: redis.Redis, request: pytest.FixtureRequest
) -> None:
    """The case a graceful goodbye does not cover: a process that was killed."""
    prefix = _prefix(request)
    watcher = ValkeyMembership("watcher", client, prefix=prefix)
    await watcher.start()

    # Announced by hand with a very short lease and never refreshed, which
    # is what a killed process looks like from here.
    await client.set(f"{prefix}:gone", b"", ex=1)
    await watcher._refresh()
    assert "gone" in watcher.members()

    await asyncio.sleep(1.5)
    await watcher._refresh()

    try:
        assert "gone" not in watcher.members()
    finally:
        await watcher.stop()


async def test_this_node_is_a_member_even_if_the_read_missed_it(
    client: redis.Redis, request: pytest.FixtureRequest
) -> None:
    """A runtime that believed it was not in its own fleet would forward
    everything somewhere else.
    """
    prefix = _prefix(request)
    membership = ValkeyMembership("one", client, prefix=prefix)
    await membership.start()

    await client.delete(f"{prefix}:one")
    await membership._refresh()

    try:
        assert list(membership.members()) == ["one"]
    finally:
        await membership.stop()


async def test_a_heartbeat_that_does_not_fit_its_lease_is_refused(
    client: redis.Redis,
) -> None:
    """It would mean a node dropping out of its own fleet between beats."""
    with pytest.raises(ValueError, match="does not fit inside a lease"):
        ValkeyMembership("one", client, lease=5.0, heartbeat=5.0)


async def test_two_fleets_sharing_a_valkey_do_not_see_each_other(
    client: redis.Redis, request: pytest.FixtureRequest
) -> None:
    """Which is the whole reason the prefix is a setting."""
    ours = ValkeyMembership("one", client, prefix=f"{_prefix(request)}:ours")
    theirs = ValkeyMembership("two", client, prefix=f"{_prefix(request)}:theirs")
    await ours.start()
    await theirs.start()

    try:
        assert list(ours.members()) == ["one"]
        assert list(theirs.members()) == ["two"]
    finally:
        await ours.stop()
        await theirs.stop()
