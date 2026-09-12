# Roadmap

What this package will grow, what it will not, and why. Written so that a
reader can tell a gap from a decision, because the two look identical from
outside and only one of them is worth filing an issue about.

The shape of the thing is fixed: **a virtual-actor runtime, not a
framework.** Everything below either serves addressing a grain by identity
or gets refused.

---

## Shipped in 0.2.0

~~**Draining in-flight calls on shutdown.**~~ Shipped. `collect()` refused
to deactivate a grain that was answering and leaving the runtime did not,
which put that failure at the moment it is most likely. This was a defect
rather than a feature, and it was found by reading the two paths next to
each other.

~~**Typed references.**~~ Shipped. This was the one place the package was
objectively weaker than its equivalents elsewhere, and it is closed:
`Runtime.reference(GrainClass, key)` is typed as the grain, so every call
site is checked against the grain's own signatures, while the dispatch
underneath stays a name and a tuple — the shape a transport needs.

---

## 0.2.x — small, and none of it changes the model

**Observability without a dependency.** How many grains are activated, how
many were activated and collected since start, how many calls are in flight,
how long activation took. Counters on the runtime and an optional callback,
so a host can feed them to whatever it already uses. `activated` is the only
one today, and a test had to reach into a private attribute for the rest.

**A bound on the fleet.** A node will activate as many grains as it is
asked about, and a hundred thousand cost about 100 MiB of bookkeeping before
a grain holds anything of its own. A maximum, with least-recently-used
eviction, turns an unbounded cache into a cache.

---

## 0.3.0 — the cluster

The reason to use this model at all, and the largest thing here. It is
described in detail because the honest part is what it will *not* promise.

**Four pieces, each behind a port, with one implementation shipped as an
optional extra:**

- **Membership** — which nodes are alive. A heartbeat with a lease, so a
  node that dies stops being in the set without anybody deciding it did.
- **Directory** — where a grain is activated. Not a table anybody maintains:
  placement is a consistent hash of the identity over the live members, so
  every node computes the same answer without asking, and adding a node
  moves a share of the identities rather than all of them.
- **Transport** — a call to a grain this node does not hold, forwarded to
  the one that does. The same `Runtime.call` signature, which is why it is
  stringly typed.
- **Lease** — best-effort single activation.

**What it will guarantee:** a grain is reachable by identity from any node;
a node joining or leaving moves a share of the fleet and not all of it; a
call to a grain elsewhere costs one network hop and behaves like a local
one.

**What it will not guarantee, stated as a limit rather than discovered as a
bug:** *single activation under a network partition.* Two halves of a split
cluster will each believe they own a share, and both will be right about
their own half. Every implementation of this model weakens here — Orleans
included, and its documentation says so. A grain that cannot tolerate two of
itself must keep its authority outside the activation: in the database or
the cache it fronts. For the common case, a grain fronting immutable data,
two activations are two caches that answer the same way, and that is what
makes the whole model affordable without a consensus protocol.

**What it will also not do in the first version:** move an activation
without losing it. A grain whose owner changes because the membership
changed is deactivated and rebuilt on the new owner, and calls in flight
during the move fail rather than being forwarded. Handing over a live
activation is a much harder problem and would be bought with complexity the
first version should not carry.

**No dependency in the core.** The membership and directory need shared
state and the transport needs a network, so both arrive as extras -
`nigrains[valkey]`, `nigrains[http]` - and the core keeps its zero
dependencies. A single-node user pays nothing for a cluster they do not run.

---

## Later, or never

**Timers.** A grain doing something on a schedule while it is activated is
easy and nearly free. It is not here because nothing has needed it, and a
feature with no user is a feature with no test that matters.

**Reminders** — a schedule that survives deactivation — need persistence,
and see below.

**Persistence: no.** Orleans has grain storage; this will not. A grain loads
whatever it wants in `activate()` and that is its own business, because the
two users we can imagine want different things: one rebuilds from a row
store, another reads a counter from a cache. An abstraction over two users
who disagree is how a small runtime stops being small.

**Supervision and restart policy: no.** A failing call raises to its caller
and a grain that fails to activate leaves nothing behind. What to do about
it belongs to whatever asked, which in every system we have seen already
knows how to resume. Supervision trees are worth their weight when the
runtime owns the whole process; this one owns a fleet inside somebody
else's.

**Exactly-once activation: no.** See the cluster section. It is not a
missing feature, it is a different system.

**Cross-language grains.** Other implementations of this model speak a wire
protocol so that a grain in one language can be called from another. That is
a large and worthwhile thing and it is out of scope: this exists because
Python had no virtual actors, not to become a platform.

---

## How to read a gap here

If something is not in this file, it has not been considered. Say so in an
issue. If something is here under "no", the reasoning is written down and
disagreeing with the reasoning is the useful conversation.
