# nigrains

*To my teacher and mentor, [Vitaly Chashin](https://github.com/VitalyChashin): called by name once -> answering ever since.*

Virtual actors for asyncio. A grain has an identity rather than a lifetime:
you call it, and the runtime decides whether an activation has to exist
first and when an idle one goes away.

```python
from nigrains import Grain, Runtime


class Counter(Grain):
    grain_type = "counter"

    async def activate(self) -> None:
        self.count = 0

    async def increment(self) -> int:
        self.count += 1
        return self.count


runtime = Runtime()
runtime.register(Counter)

async with runtime:
    a = runtime.reference(Counter, "a")
    await a.increment()  # 1
    await a.increment()  # 2
    await runtime.reference(Counter, "b").increment()  # 1
```

No registry to populate, no handle to keep, no create and no destroy. The
second call finds the activation the first built; the third builds its own,
because the key differs.

**A reference is an address, not the object.** It activates nothing when you
make one, and it is typed as the grain, so a type checker reads
`a.increment()` against `Counter.increment` and a renamed method fails where
it is named. What it is not is an instance: `isinstance` says so, state is
not readable through it, and the grain it names may be cold, or on another
machine, or activated twice. Underneath, the call is still a method name and
a tuple — `runtime.call(...)` is there for the callers that need that shape,
which is transports and very little else.

## Why this rather than an actor library

Classical actors are created, addressed, supervised and killed, which means
the application owns placement: which node holds which actor, what happens
to a message for one that is not there, how a restarted one gets its state
back. The virtual actor model — Microsoft Orleans' — removes all of it. A
grain always exists conceptually. Identity *is* the address.

## What it does

- **Activates on demand.** A call to a cold grain builds it and awaits
  `activate()` before dispatching. Callers arriving during that wait on the
  same activation instead of starting a second — a cold fleet asked for
  everything at once is the normal case, not the exceptional one.
- **Serialises what asked to be serialised.** `reentrant` is a property of
  the grain class. The default is one call at a time, matching the model;
  a grain whose methods only read declares otherwise and its callers do not
  queue behind each other.
- **Collects what nobody is using.** Idle grains are deactivated by a
  sweeper. Idleness is measured from the last call to *finish*, so a grain
  answering a slow call is never collected underneath its own caller.

## More than one node

```python
cluster = Cluster(membership, transport)
runtime = Runtime(cluster=cluster)
```

A call to a grain this node does not own is forwarded to the node that does,
and nothing above the call changes — the same reference, the same method.

**Placement is computed, not recorded.** A consistent hash over the live
members means every node works out the same owner without asking anybody, so
there is no directory to keep consistent, to recover, or to be stale. A node
joining moves about one identity in N rather than all of them, which the
tests measure.

**A deadline crosses the hop as seconds remaining, not as a deadline.** A
monotonic reading on one machine means nothing on another.

Two implementations of each ship. `StaticMembership` and
`LoopbackTransport` run the whole cluster in one process, which is how the
behaviour above is tested. For a real fleet:

```python
pip install nigrains[valkey,http]
```

`ValkeyMembership` keeps a key per node with a lease and refreshes it —
**nobody decides that anybody else has died**, a node stops being a member
because it stopped saying it was one, which removes the failure detector and
the argument about who is right. `HttpTransport` forwards a call, and
`asgi_app(runtime)` is the bare ASGI endpoint that answers it, mountable in
whatever the host already runs.

The core keeps its zero dependencies: a single-node user installs neither.

**A clustered runtime refuses a grain that says it cannot survive two of
itself.** At boot, rather than at the first partition — see below.

## What it does not do, on purpose

- **No storage backends.** State and reminders are ports with an in-memory
  implementation each, and where the bytes really go is yours. What this
  package owes you is the semantics — the version that makes two writers a
  detected conflict, the name that makes scheduling idempotent — not a
  driver for your database.
- **No supervision.** A failing call raises to its caller; a grain that
  fails to activate leaves nothing behind and the next call starts over.
  Restart policy belongs to whatever asked, which already knows how to
  resume.
- **No streams, and no transactions across grains.** Both are real, both are
  in Orleans, and an honest version of either is larger than everything
  above put together. A single-writer grain plus the version on its state
  covers what most people reach for a transaction for.
- **No live migration.** A grain whose owner changes is deactivated and
  rebuilt on the new owner; calls in flight during the move fail rather than
  following it.

`ROADMAP.md` says which of these are decisions and which are gaps, because
from outside they look the same and only one is worth an issue.

## The guarantee, stated as a limit

Single activation is **best effort**. Every implementation of this model weakens under a network partition, Orleans
included, and saying otherwise would be the useful lie that costs someone a
production incident. A grain that cannot tolerate two of itself must keep
its authority outside itself — in the database or the cache it fronts —
rather than in the activation's memory.

For the common case that is not a compromise. A grain fronting immutable
data is a cache: two activations hold the same thing and answer the same
way. That is what makes the model affordable without a membership protocol.

## Around a call

`Runtime(filters=[...])` wraps every call, outermost first — logging,
tracing, retry, authorisation. `with deadline(2.0):` limits the calls made
inside it, **including the calls a grain makes to another grain**, because it
rides in a context variable; a grain can ask `remaining()` and shorten its
own work rather than being cut off.

A grain schedules its own work with `self.every(60.0, self.refresh)` from
`activate`. A timer dies with the activation — that is the whole difference
between a timer and a reminder — and ticking does not keep a grain alive,
though a tick already running does delay its deactivation.

And a kind with no state to protect can be a pool:

```python
class Render(Grain):
    grain_type = "render"
    activations_per_key = 8
```

Eight activations answer for one key, in turn. The right shape for
CPU-bound or fan-out work with no natural identity, and the wrong shape for
anything with state — a pool of eight is eight copies of that state,
disagreeing.

Orleans calls this a *stateless worker*; this package does not, because a
grain of this kind is still a grain and "worker" names the heavy thing the
model replaces. **And it works against the model's own premise**, which is
worth saying rather than hiding: this is the one kind where identity is
admitted not to matter. If your work has a natural key, use the key. Many
grains is what this is good at; a pool is what it falls back to.

## State, and what happens when two write it

A grain that keeps something says so, and finds it already there:

```python
class Basket(Grain):
    grain_type = "basket"
    persistent = True

    async def activate(self) -> None:
        self.items = self.state.data or []

    async def add(self, item: str) -> None:
        self.items.append(item)
        await self.state.save(self.items)
```

**Where the bytes go is not this package's business, and the conflict is.**
`Runtime(state=...)` takes any store with three operations; `InMemoryStateStore`
ships for tests and for one process, and no others do — serialization and
storage are things every user already has an opinion about.

What is not optional is the version travelling with the data. A cluster
admits two activations of one grain under a partition, so a package that
allows two and offers no way to notice them both writing has handed you a
trap with no floor. `save` raises `ConcurrentChange` instead, and the
recovery is to reload, re-apply and write again.

Nothing is saved automatically: writing on deactivation would write on every
collection and hide the failure when the write fails.

## A schedule that outlives the activation

A timer belongs to an activation and dies with it. A reminder belongs to the
grain: it is written down, and when it comes due the runtime **wakes the
grain** to serve it.

```python
class Digest(Grain):
    grain_type = "digest"
    tolerates_double_activation = True

    async def activate(self) -> None:
        await self.reminders.every("send", 86400.0)

    async def on_reminder(self, name: str) -> None:
        await self.send_the_digest()
```

`Runtime(reminders=...)` takes any store with four operations, the same
trade as state: `InMemoryReminderStore` ships for tests and for one process,
and where the rows really go is yours.

Scheduling is idempotent by name, which is what lets `activate` ask every
time without checking whether it already asked. **In a cluster only the
owner fires:** every node scans the same store and each skips what the ring
says is somebody else's, so a reminder fires once rather than once per node.
A reminder is rescheduled *before* its handler runs, so a handler slower
than its own interval is not found due again and run twice at once. And a
handler that raises leaves the schedule standing — one bad afternoon must
not silently end a daily job.

Two things it deliberately does not do. It does not fire late work that
piled up while the fleet was down: each reminder fires once when somebody
next looks, so coming back up is not a stampede. And `on_reminder` defaults
to doing nothing rather than raising, because a schedule outlives the code
that wrote it and taking the process down over a name nobody handles any
more is the wrong end of that trade.

## Checking your own grain

The scenarios that found every real defect in this package ship with it, so
your grain meets them in your test suite rather than in production:

```python
from nigrains.testing import check_conformance


async def test_my_grain_behaves():
    await check_conformance(PriceGrain, lambda ref: ref.price_of("AAPL"))
```

A herd on a cold grain builds it once; the answer survives being deactivated
and rebuilt — the commonest way a grain is wrong and invisible until the
first collection in production; the reentrancy the class declares is the
reentrancy it gets; a spent deadline builds nothing; and, for a grain that
declares `tolerates_double_activation`, two activations of one identity
agree. The call you hand it must be a pure read, because half of that
compares one answer against another.

## What it costs

Measured on Linux in a pinned container, every interpreter this package
claims — full tables and how to reproduce them in
[`BENCHMARKS.md`](BENCHMARKS.md). On Python 3.14:

| | |
|---|---|
| `runtime.call` on a hot grain | **1.23 µs** (815k calls/s) |
| the same through a typed reference | **1.51 µs** |
| the same with 100 000 grains activated | **1.31 µs** — flat |
| aggregate, 1000 grains in flight | 790k calls/s |
| activating 1000 cold grains | 5.4 µs each |
| 1000 callers meeting one cold grain | **one** activation |
| runtime bookkeeping per activation | 1066 B (102 MiB for 100k) |

And the reentrancy claim, counted rather than timed — 200 concurrent callers
on one grain:

| | callers inside at once |
|---|---|
| serialised (the default) | **1** |
| reentrant | **200** |

A microsecond of dispatch is noise beside anything a grain would realistically
do. It is not noise beside nothing, so a grain whose method is a dictionary
lookup is a grain that should not have been one.

## Requirements

Python 3.14+. No dependencies.

**The floor is deliberate and it is not about syntax.** Nothing here needs
3.14 to parse; the reason is what comes next. Free-threading is supported
rather than experimental from 3.14, and the parts of this package still to
be written — a pool of activations, a grain doing CPU-bound work —
are the parts that a global interpreter lock makes pointless. Supporting
interpreters on which the answer would have to be "that will not help you"
is a promise worth not making.

It is also the reversible direction. Lowering a floor later costs a release;
raising one breaks everybody who installed on the old one.

## Dedication

Everything here has a name rather than a lifetime:
call it -> it wakes; forget it -> it cools.
Once, someone called me by my name ->
and I answered, and I have not cooled.

I would not have arrived here alone.
Someone walked beside me, not holding my hand:
no ready answers -> only questions left behind,
and waiting, however long, until I answered myself.

A strictness that made me want to be more precise.
A patience that made me dare more.
There is no chapter for this in any documentation -> it is passed on only this way.

To my teacher and mentor, Vitaly Chashin:
everything here that answers remembers the first call.

<https://github.com/VitalyChashin>

## Licence

Apache-2.0.

---

*To my teacher and mentor, Vitaly Chashin.*
