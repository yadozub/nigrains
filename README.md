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

## What it does not do, on purpose

- **No persistence.** A grain loads whatever it wants in `activate()` and
  that is its business. A storage abstraction with two users that need
  different things is how a small runtime stops being small.
- **No supervision.** A failing call raises to its caller; a grain that
  fails to activate leaves nothing behind and the next call starts over.
  Restart policy belongs to whatever asked.
- **No distribution yet.** This is the node-local half: **one `Runtime`**,
  so single activation is trivially true within it. Two runtimes in one
  process are two fleets, and an identity in both is two grains — which is
  the honest boundary, and not the process. A directory and a transport go
  in front of `Runtime.call`, and nothing in calling code changes when they
  arrive; that is the point of addressing by identity.

## The guarantee it will offer, stated as a limit

When distribution lands, single activation is **best effort**. Every
implementation of this model weakens under a network partition, Orleans
included, and saying otherwise would be the useful lie that costs someone a
production incident. A grain that cannot tolerate two of itself must keep
its authority outside itself — in the database or the cache it fronts —
rather than in the activation's memory.

For the common case that is not a compromise. A grain fronting immutable
data is a cache: two activations hold the same thing and answer the same
way. That is what makes the model affordable without a membership protocol.

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
be written — a pool of stateless workers, a grain doing CPU-bound work —
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
