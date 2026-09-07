# Benchmarks

Run them yourself: `bench/run.sh` builds a container and measures on every
interpreter this package claims. Two CPUs and 2 GiB, pinned, because a
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

| Python | direct | `runtime.call` | throughput |
|---|---|---|---|
| 3.11 | 0.12 µs | 1.70 µs | 588k calls/s |
| 3.12 | 0.09 µs | 1.42 µs | 706k calls/s |
| 3.13 | 0.08 µs | 1.32 µs | 756k calls/s |
| 3.14 | 0.08 µs | 1.23 µs | **812k calls/s** |

A little over a microsecond, which is roughly one event-loop turn. That is
noise beside anything a grain would realistically do — a query, a file, a
model call — and it is not noise beside nothing. A grain whose method is a
dictionary lookup should not have been a grain.

**Dispatch does not degrade with the size of the fleet**, which is the
premise of the whole model and therefore the thing most worth checking:

| activated grains | 3.14 |
|---|---|
| 1 | 1.27 µs |
| 1 000 | 1.29 µs |
| 100 000 | 1.30 µs |

Aggregate over a whole fleet in flight at once — 1000 grains, 100 calls each
— is 797k calls/s on 3.14, so nothing is lost by spreading the work out.

## Overlap

200 concurrent callers on one grain. Counted, not timed: the number is how
many were inside the method at the same moment.

| | callers inside at once |
|---|---|
| serialised (the default) | **1** |
| reentrant | **200** |

## Activation

| | 3.11 | 3.14 |
|---|---|---|
| 1000 cold grains, called at once | 8.10 µs each | **5.42 µs each** |
| 1000 callers meeting one cold grain | one activation | one activation |
| runtime bookkeeping per activation | 1126 B | **1066 B** |

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
| in one go | 81.0 ms | **81.1 ms** |
| chunked (current) | 83.6 ms | **6.9 ms** |

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
