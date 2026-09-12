"""The HTTP transport, both halves, over a real client and a real ASGI app.

No sockets: httpx speaks to the application directly. Everything else is
real - the JSON on the wire, the status codes, the failures coming back as
failures - so what a socket would add is latency and a class of network
error, and neither of those is what this file is for.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

from nigrains import (
    Cluster,
    ConcurrentChange,
    Grain,
    GrainId,
    Runtime,
    StaticMembership,
    deadline,
)
from nigrains.http import HttpTransport, asgi_app
from nigrains.wire import RemoteError

NODES = ("http://one", "http://two")


class Adder(Grain):
    """Answers, refuses, or takes its time, on request."""

    grain_type = "adder"
    tolerates_double_activation = True

    def __init__(self, grain_id: GrainId, node: str) -> None:
        super().__init__(grain_id)
        self.node = node

    async def add(self, left: int, right: int) -> dict[str, object]:
        return {"sum": left + right, "on": self.node}

    async def refuse(self, how: str) -> None:
        if how == "known":
            raise ConcurrentChange(str(self.id), "1", "2")
        raise ValueError("something of my own")

    async def slowly(self, seconds: float) -> str:
        await asyncio.sleep(seconds)
        return "done"

    async def echo(self, value: object) -> object:
        return value


def _fleet() -> dict[str, Runtime]:
    """Two nodes reaching each other over HTTP, with no socket in the way.

    Returns:
        The runtimes by node address.
    """
    apps: dict[str, httpx.ASGITransport] = {}
    runtimes: dict[str, Runtime] = {}
    for node in NODES:
        runtime = Runtime(
            idle_seconds=1e9,
            sweep_seconds=1e9,
            cluster=Cluster(
                StaticMembership(node, NODES),
                HttpTransport(client=httpx.AsyncClient(transport=_Router(apps))),
            ),
        )
        runtime.register(Adder, _builder(node))
        runtimes[node] = runtime
        apps[node] = httpx.ASGITransport(app=asgi_app(runtime))
    return runtimes


def _builder(node: str) -> Callable[[GrainId], Grain]:
    """A factory building adders that belong to one node.

    Args:
        node: Where they live.

    Returns:
        The factory.
    """

    def build(grain_id: GrainId) -> Grain:
        return Adder(grain_id, node)

    return build


class _Router(httpx.AsyncBaseTransport):
    """Sends a request to whichever node's application it is addressed to.

    Standing in for a network, and only for the part of a network that
    decides which process a request reaches.
    """

    def __init__(self, apps: dict[str, httpx.ASGITransport]) -> None:
        self._apps = apps

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        node = f"{request.url.scheme}://{request.url.host}"
        app = self._apps.get(node)
        if app is None:
            raise httpx.ConnectError(f"no node at {node}")
        return await app.handle_async_request(request)


def _elsewhere(runtime: Runtime) -> str:
    """A key this runtime does not own.

    Args:
        runtime: Whose point of view.

    Returns:
        The key.
    """
    cluster = runtime._cluster
    assert cluster is not None
    return next(
        key
        for key in (f"k{n}" for n in range(200))
        if not cluster.is_mine(GrainId(Adder.grain_type, key))
    )


async def test_a_call_crosses_the_wire_and_comes_back() -> None:
    fleet = _fleet()
    here = fleet[NODES[0]]
    key = _elsewhere(here)

    answer = await here.reference(Adder, key).add(2, 3)

    assert answer == {"sum": 5, "on": NODES[1]}
    assert here.stats.forwarded == 1
    assert fleet[NODES[1]].activated == 1


async def test_arguments_survive_as_what_json_can_carry() -> None:
    fleet = _fleet()
    here = fleet[NODES[0]]
    key = _elsewhere(here)

    assert await here.reference(Adder, key).echo({"a": [1, 2, None], "b": "x"}) == {
        "a": [1, 2, None],
        "b": "x",
    }


async def test_something_json_cannot_carry_fails_on_the_sending_side() -> None:
    """Better here than as something unreadable on the far one."""
    fleet = _fleet()
    here = fleet[NODES[0]]
    key = _elsewhere(here)

    with pytest.raises(TypeError):
        await here.reference(Adder, key).echo({1, 2, 3})


async def test_a_failure_both_ends_know_crosses_as_itself() -> None:
    fleet = _fleet()
    here = fleet[NODES[0]]
    key = _elsewhere(here)

    with pytest.raises(ConcurrentChange):
        await here.reference(Adder, key).refuse("known")


async def test_a_failure_only_the_far_side_knows_arrives_as_a_remote_one() -> None:
    """Its name and message survived; its class did not, and this says so."""
    fleet = _fleet()
    here = fleet[NODES[0]]
    key = _elsewhere(here)

    with pytest.raises(RemoteError) as raised:
        await here.reference(Adder, key).refuse("mine")

    assert raised.value.type_name == "ValueError"
    assert raised.value.message == "something of my own"


async def test_a_deadline_crosses_and_is_enforced_over_there() -> None:
    fleet = _fleet()
    here = fleet[NODES[0]]
    key = _elsewhere(here)

    with pytest.raises(TimeoutError), deadline(0.05):
        await here.reference(Adder, key).slowly(5.0)


async def test_a_node_that_is_not_listening_is_a_connection_error() -> None:
    runtime = Runtime(
        idle_seconds=1e9,
        sweep_seconds=1e9,
        cluster=Cluster(
            StaticMembership("http://one", ["http://one", "http://ghost"]),
            HttpTransport(client=httpx.AsyncClient(transport=_Router({}))),
        ),
    )
    runtime.register(Adder, _builder("http://one"))
    key = _elsewhere(runtime)

    with pytest.raises(httpx.ConnectError):
        await runtime.reference(Adder, key).add(1, 1)


async def test_the_endpoint_serves_what_it_is_given_without_routing() -> None:
    """The sender decided; deciding again here would let a call bounce."""
    fleet = _fleet()
    here = fleet[NODES[0]]
    key = _elsewhere(here)
    app = httpx.ASGITransport(app=asgi_app(here))

    async with httpx.AsyncClient(transport=app, base_url=NODES[0]) as client:
        response = await client.post(
            "/nigrains/call",
            json={
                "type": "adder",
                "key": key,
                "method": "add",
                "args": [1, 1],
                "kwargs": {},
                "timeout": None,
            },
        )

    assert response.status_code == 200
    assert response.json()["value"] == {"sum": 2, "on": NODES[0]}
    assert here.stats.forwarded == 0
