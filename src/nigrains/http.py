"""A transport over HTTP, and the endpoint that answers it.

Two halves that never meet in one process except in a test: a client that
turns a forwarded call into a POST, and an ASGI application that turns the
POST back into a call. Both need `nigrains[http]`, which is httpx and
nothing else - the server half is a bare ASGI callable, so it mounts inside
whatever the host already runs and this package stays out of the business of
choosing a web framework.

**A node's identifier is its address by default**, which is the smallest
thing that works: a fleet whose members are ``http://a:8000`` and
``http://b:8000`` needs no lookup at all. A deployment that would rather
name its nodes passes a resolver.

    runtime = Runtime(cluster=Cluster(membership, HttpTransport()))
    app = asgi_app(runtime)          # mount at /nigrains, or anywhere

**The path is not configurable and the reason is symmetry.** Both halves
agree on one path because they ship together; making it settable would let
a deployment configure one end and not the other, and the failure would be
a 404 at the first forwarded call rather than at boot.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from nigrains.grain import GrainId
from nigrains.wire import Codec, JsonCodec, failure_from_wire, failure_to_wire

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, MutableMapping

    import httpx

    from nigrains.runtime import Runtime

PATH = "/nigrains/call"
"""Where a forwarded call arrives. Fixed on both sides; see the module
docstring for why it is not a setting."""

_OK = 200
_FAILED = 500


class HttpTransport:
    """Forwards a call to another node over HTTP.

    Attributes:
        codec: How the call and its answer are rendered.
    """

    def __init__(
        self,
        *,
        codec: Codec | None = None,
        resolve: Callable[[str], str] | None = None,
        client: httpx.AsyncClient | None = None,
        timeout: float = 30.0,
    ) -> None:
        """Builds a transport.

        Args:
            codec: How to render a call. JSON by default, which is enough
                for most grains and not for all of them.
            resolve: Turns a node identifier into a base URL. By default a
                node's identifier *is* its base URL.
            client: The HTTP client to use. One is made on first use when
                this is not given, and closed with :meth:`aclose`; passing
                one is how a deployment shares a connection pool, sets
                verification, or points the whole thing at a test.
            timeout: Seconds allowed for a forwarded call that carries no
                deadline of its own. A call with a deadline uses whatever
                is left of it instead.
        """
        self.codec = codec or JsonCodec()
        self._resolve = resolve or (lambda node: node)
        self._client = client
        self._owned = client is None
        self._timeout = timeout

    async def send(
        self,
        node: str,
        grain_id: GrainId,
        method: str,
        args: tuple[Any, ...],
        kwargs: dict[str, Any],
        timeout: float | None,
    ) -> Any:  # noqa: ANN401 - whatever the grain returns
        """Posts one call to another node.

        Args:
            node: Where to send it.
            grain_id: Which grain.
            method: Which of its methods.
            args: Positional arguments.
            kwargs: Keyword arguments.
            timeout: Seconds remaining, or None when the caller set no
                deadline.

        Returns:
            Whatever the grain returned.

        Raises:
            BaseException: Whatever the grain raised, rebuilt where both
                ends know the class and wrapped in
                :class:`~nigrains.wire.RemoteError` where they do not.
        """
        body = self.codec.encode(
            {
                "type": grain_id.type,
                "key": grain_id.key,
                "method": method,
                "args": list(args),
                "kwargs": kwargs,
                "timeout": timeout,
            }
        )
        response = await self._http().post(
            f"{self._resolve(node).rstrip('/')}{PATH}",
            content=body,
            headers={"content-type": "application/octet-stream"},
            timeout=self._timeout if timeout is None else max(timeout, 0.0),
        )
        answer = self.codec.decode(response.content)
        if response.status_code == _OK:
            return answer["value"]
        raise failure_from_wire(answer["error"])

    def _http(self) -> httpx.AsyncClient:
        """The client, made on first use when one was not supplied.

        Returns:
            The client.
        """
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        """Closes the client, if this transport made it."""
        if self._owned and self._client is not None:
            await self._client.aclose()
            self._client = None


def asgi_app(
    runtime: Runtime, *, codec: Codec | None = None
) -> Callable[
    [MutableMapping[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]:
    """Builds the endpoint that serves forwarded calls.

    A bare ASGI application, so it mounts in Starlette, FastAPI, or anything
    else the host already runs, and this package needs no opinion about web
    frameworks.

    **It serves whatever it is given, without checking placement.** That is
    :meth:`~nigrains.runtime.Runtime.deliver`'s rule and it is deliberate:
    the sender decided, and deciding again here would let a call bounce
    between two nodes that disagree about the membership for a moment.

    **It is not authentication.** Anything that can reach this endpoint can
    call any grain on this node with any arguments. Put it where only the
    fleet can reach it, or in front of whatever the host already uses to say
    who may knock.

    Args:
        runtime: The runtime that will serve the calls.
        codec: How the call and its answer are rendered. Must be the same
            one the other nodes send with.

    Returns:
        The ASGI application.
    """
    used = codec or JsonCodec()

    async def app(
        scope: MutableMapping[str, Any],
        receive: Callable[[], Awaitable[Any]],
        send: Callable[[Any], Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":  # pragma: no cover - lifespan and websockets
            return
        body = await _read(receive)
        try:
            request = used.decode(body)
            value = await runtime.deliver(
                GrainId(request["type"], request["key"]),
                request["method"],
                tuple(request["args"]),
                dict(request["kwargs"]),
                timeout=request["timeout"],
            )
        except asyncio.CancelledError:
            # Never an answer. A cancellation is this task being told to
            # stop, not the grain failing, and rendering it as a response
            # would swallow it: the caller would read a strange error and
            # whoever cancelled would be told the work had finished.
            raise
        except BaseException as exc:
            # Every other failure is an answer: this is the far end of a
            # transport, and a traceback that escapes reaches the ASGI
            # server rather than the caller who is waiting for it.
            await _respond(send, _FAILED, used.encode({"error": failure_to_wire(exc)}))
            return
        await _respond(send, _OK, used.encode({"value": value}))

    return app


async def _read(receive: Callable[[], Awaitable[Any]]) -> bytes:
    """Reads a whole request body.

    Args:
        receive: The ASGI receive channel.

    Returns:
        The body.
    """
    body = b""
    while True:
        message = await receive()
        chunk: bytes = message.get("body", b"")
        body += chunk
        if not message.get("more_body", False):
            return body


async def _respond(send: Callable[[Any], Awaitable[None]], status: int, body: bytes) -> None:
    """Sends one response.

    Args:
        send: The ASGI send channel.
        status: The status code.
        body: The body.
    """
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/octet-stream")],
        }
    )
    await send({"type": "http.response.body", "body": body})
