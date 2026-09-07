# nigrains

Virtual actors for asyncio. A grain has an identity rather than a lifetime:
you call it, and the runtime decides whether an activation has to exist
first and when an idle one goes away.

```python
from nigrains import Grain, GrainId, Runtime


class Counter(Grain):
    async def activate(self) -> None:
        self.count = 0

    async def increment(self) -> int:
        self.count += 1
        return self.count


runtime = Runtime()
runtime.register("counter", Counter)

async with runtime:
    await runtime.call(GrainId("counter", "a"), "increment")  # 1
    await runtime.call(GrainId("counter", "a"), "increment")  # 2
    await runtime.call(GrainId("counter", "b"), "increment")  # 1
```

No registry to populate, no handle to keep, no create and no destroy. The
second call finds the activation the first built; the third builds its own,
because the key differs.

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
- **No distribution yet.** This is the node-local half: one process, so
  single activation is trivially true. A directory and a transport go in
  front of `Runtime.call`, and nothing in calling code changes when they
  arrive — which is the point of addressing by identity.

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

## Requirements

Python 3.11+ (that floor is `typing.Self`, and nothing else in here reaches
past it). No dependencies. The suite runs on 3.11, 3.12, 3.13 and 3.14.

## Licence

Apache-2.0.
