# Benchmarks

Run them yourself: `bench/run.sh` builds a container and measures.

**The interpreter is pinned inside that container, and it matters more than
it looks.** Dropping the pin once let `uv run` fall through to the CPython in
the base image instead of the standalone build uv fetches, and every number
moved by twenty per cent — including the bare `await`, which is the line
that gave it away. Two builds of one version are not one machine. Two CPUs and 2 GiB, pinned, because a
benchmark whose numbers move with whatever else the machine is doing measures
the machine.

Everything below is Linux (Debian bookworm-slim, container, 2 CPUs). Each
timing is **the fastest of several runs**, not a mean: a slower run measures
whatever else the machine was doing at the time, and the question here is
what the code costs when nothing is in its way. Read the shapes, not the
digits — the digits are this machine's.

## Dispatch

What addressing by identity costs, against a plain `await grain.method()` on
a grain you already hold.

| Python | direct await | `runtime.call` | `reference.method()` |
|---|---|---|---|
| 3.14 | 0.08 µs | **1.54 µs** | **1.85 µs** |

**Dispatch got 0.18 µs slower in 0.3.0** and the trade is named rather than
buried: every call now reads whether a deadline is in force, and the fast
path — no filters, no deadline — is a branch on top of that. An attempt to
claw it back by reading the context variable directly instead of through its
accessor measured *worse* (1.48 µs), which is inside the noise, so it was
reverted rather than kept as a lucky-looking number.

Earlier releases carried rows for 3.11 to 3.13. The floor is 3.14 from
0.4.0, and those rows are gone rather than left to rot.

A typed reference costs about 0.3 µs more than the raw call. It resolves a
method name against the class once and caches the caller on the reference,
so the price is one extra `await` layer rather than a lookup per call —
resolving it per call measured 2.30 µs against 1.23, which is why it does not.

A little over a microsecond, which is roughly one event-loop turn. That is
noise beside anything a grain would realistically do — a query, a file, a
model call — and it is not noise beside nothing. A grain whose method is a
dictionary lookup should not have been a grain.

**Dispatch does not degrade with the size of the fleet**, which is the
premise of the whole model and therefore the thing most worth checking:

| activated grains | 3.14 |
|---|---|
| 1 | 1.26 µs |
| 1 000 | 1.28 µs |
| 100 000 | 1.31 µs |

Aggregate over a whole fleet in flight at once — 1000 grains, 100 calls each
— is 790k calls/s on 3.14, so nothing is lost by spreading the work out.

## Overlap

200 concurrent callers on one grain. Counted, not timed: the number is how
many were inside the method at the same moment.

| | callers inside at once |
|---|---|
| serialised (the default) | **1** |
| reentrant | **200** |

## Activation

| | 3.14 |
|---|---|
| 1000 cold grains, called at once | **5.36 µs each** |
| 1000 callers meeting one cold grain | one activation |
| runtime bookkeeping per activation | **1066 B** |

A hundred thousand activated grains cost about **102 MiB** of the runtime's
own structures, before a grain holds anything of its own. That is the number
to start from when asking how many one node can keep.

**A grain still activating is never collected**, however long it has been
sitting there — its call count is zero and its timestamp is the moment it was
created, which used to look exactly like abandonment. A review found the
sweep deactivating a grain whose own first caller was still waiting for it.

## Sweeping idle grains

The sweep is the one operation linear in the size of the fleet. It is chunked,
and the two numbers say why:

| 100 000 idle grains, 3.14 | total | longest stall |
|---|---|---|
| in one go | 79.7 ms | **79.7 ms** |
| chunked (current) | 82.0 ms | **6.4 ms** |

The total is what nobody waits for. The stall is what a caller waits — one
uninterrupted block with every other coroutine behind it. Chunking trades a
few per cent of total time for an order of magnitude off the thing anybody
actually feels.

## Two ways this benchmark was wrong first

Both are kept here because the next person to measure this will reach for the
same tools.

**Overlap was measured with `asyncio.sleep(0.01)` and wall time.** On Windows
the default timer resolution is about 15.6 ms, so a 10 ms sleep took one and a
half ticks and the serialised grain scored *0.6x* — slower than serial, which
is meaningless. The number was about the platform's clock. Counting callers
costs the same everywhere.

**Memory per activation counted the tasks that made the activations.** The
reading was taken right after a `gather` of 100 000 calls, before anything
collected them, which put finished coroutines into the cost of a grain. One
`gc.collect()` moved it from 1228 to 1058 bytes, and only the second number is
about this package.
