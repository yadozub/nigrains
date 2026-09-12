# Roadmap

What this package will grow, what it will not, and why. Written so that a
reader can tell a gap from a decision, because the two look identical from
outside and only one of them is worth filing an issue about.

**The ambition changed on 2026-09-12.** This began as the smallest runtime
that served one system. It is now meant to be the virtual-actor library
Python does not have — an absence that has already cost its author time on a
real project, and is unlikely to have cost only him. The shape is still
fixed: **a runtime, not a framework.** Everything below either serves
addressing a grain by identity or gets refused.

Nothing here ships without the test that would find its failure. A runtime
for concurrency that cannot be trusted is worse than none, because it gets
trusted anyway.

---

## Shipped

**0.1.0** — activation on demand by identity; one activation under a herd;
reentrancy declared per class; deactivation by idleness, swept in chunks.

**0.2.0** — a grain carries its own address, so `register` takes the class;
`Runtime.reference(GrainClass, key)` is typed as the grain, closing the one
place this package was objectively weaker than its equivalents elsewhere;
leaving the runtime waits for calls in flight.

**0.2.1** — `Runtime.stats`; `max_activations` with least-recently-used
eviction.

---

## 0.3 — the call path, finished

All local, all cheap, and all of it must exist **before** the cluster: a
transport has to respect these, and adding them afterwards means doing them
twice.

**Call filters.** A hook around every call, in and out. This is where
logging, tracing, retry, authorisation and per-call timeouts belong, and
where users will otherwise reach for monkey-patching. One ordered list, each
filter seeing the identity, the method and the arguments.

**Cancellation that means something.** A caller giving up cancels the
coroutine, and that is enough while everything is in one process. It stops
being enough the moment a call crosses a node, so the shape is decided here:
a call carries a deadline, a grain can read it, and the transport carries it
later.

**Timers.** A grain doing something on a schedule while it is activated.
Nearly free, and honest about what it is — it dies with the activation,
which is the whole difference between a timer and a reminder.

**Stateless workers.** A grain kind where identity does *not* mean one
activation: a pool answers, sized to the work. The right shape for
CPU-bound or fan-out work with no state to protect, and the wrong shape for
everything else, which the documentation will say in those words.

---

## 0.3.x — `nigrains.testing`, a conformance kit

**Every real defect this package has had was found the same way: an
adversarial scenario.** A cancelled waiter poisoning a shared activation. A
sweep collecting a grain mid-activation. A shutdown running under a live
call. None of them is specific to the grain that found them — anybody who
writes a grain meets the same three, and nobody thinks to test for them.

So the scenarios ship. A user hands their grain class and one representative
call, and gets the battery run against it:

- a herd on a cold grain activates it once;
- a waiter that gives up does not take the activation with it;
- nothing is deactivated under a running call;
- the reentrancy the class declares is the reentrancy it gets — overlap
  where it claims overlap, and none where it does not;
- a deadline already gone refuses before anything is built;
- and, for a grain that says it can, two activations of one identity answer
  the same.

Watching overlap turned out to need care, and the first attempt was wrong in
a way worth keeping here: a call filter sits *outside* dispatch, so it counts
callers queueing for a serialised grain's lock rather than callers inside it,
and a correctly serialised grain failed. The count belongs in a subclass of
the grain, and the test written to prove the scenario could fail proved
instead that it always did.

**One declaration comes with it**, because the last scenario cannot be
guessed: `Grain.tolerates_double_activation`, default False. A grain
fronting immutable data says True and means it; everything else keeps the
safe answer.

`idempotent` is *not* added yet. It would be read by a retry filter, there
is no retry filter, and a declaration nothing reads is the thing this
package refuses to ship.

---

## 0.4 — state, with the concurrency answer attached

**This was refused in the first draft of this file, and the refusal was half
right.** What Orleans gives is a storage provider, serialization, and an
ETag. The first two are boilerplate anybody can write and saying so was
fair. The third is not boilerplate — it is the answer to *what happens when
two activations of one grain both write* — and refusing it while also
admitting that double activation is possible under a partition leaves a trap
with no floor under it.

So: a port of two operations and one error.

    read(grain_id)                    -> (data, version)
    write(grain_id, data, expected)   -> version, or ConcurrentChange

An in-memory implementation ships for tests. **Storage backends do not:**
where to put bytes is the part every user already has an opinion about, and
serialization stays theirs. What this package owes them is the semantics.

---

## 0.5 — the cluster, without a network

The design errors live here and are cheapest here, so this is a milestone of
its own.

**There is nothing here to garbage-collect, and that is a design win worth
naming.** Placement is computed from the membership rather than recorded in
a directory, so there are no stale entries to expire, no table to recover
and nothing to be inconsistent about. The question "what collects the
directory" has the best possible answer: there is no directory.

The same question about *activations* has a different answer, and the
analogy fails there for a precise reason: a tracing collector needs a
reachability graph, and addressing by identity means every grain is
reachable from everywhere - `GrainId(type, key)` can be constructed out of
thin air. There is nothing to mark. Idleness and least-recently-used are not
a poor substitute for reachability; they are the only thing the model
permits, and they are the same generational bet a collector makes anyway.

**And the runtime refuses to start clustered with a grain that cannot
survive it.** The roadmap admits below that single activation is best effort
under a partition; a grain whose class says
`tolerates_double_activation = False` is then incompatible with the
deployment, and the honest moment to say so is boot, not the first split.
This is where that check lands, because this is where there is finally a
cluster to refuse.

Ports for **membership** (who is alive), **directory** (who holds what) and
**transport** (how a call reaches another node), with placement by
consistent hash over the live members — every node computes the same answer
without asking anyone, and adding a node moves a share of the identities
rather than all of them. A loopback transport exercises the whole path in
one process, so every behaviour below is tested before a socket exists.

---

## 0.6 — the cluster, for real

Optional extras, so the core keeps its zero dependencies and a single-node
user pays nothing for a cluster they do not run:

- `nigrains[valkey]` — membership by heartbeat with a lease, and the
  directory beside it.
- `nigrains[http]` — a call to a grain this node does not hold, forwarded to
  the one that does.

**The cost here is not the code, it is the proof.** A cluster cannot be
tested with unit tests: it needs several processes, a node killed in the
middle of a call, and a partition. That means Docker and multi-process
integration tests, and that is the bulk of this milestone. Without them,
"the cluster works" is a claim.

**No node decides alone that another has died.** A node that cannot reach a
peer asks the others; the peer leaves the ring only when nobody can reach
it. This is the mark phase of a collector borrowed on purpose - liveness as
reachability from several roots rather than from one - and the reason is not
courtesy to the unreachable node but the cost of being wrong: a false death
rebalances, and a rebalance moves grains. One bad link between two nodes
must not move the fleet.

The same borrowing carries a warning. A collector has a stop-the-world
pause; a cluster's is the rebalance. The discipline that made the idle sweep
safe applies here too - incremental, and bounded in how long anybody waits.

**What it will guarantee:** a grain is reachable by identity from any node;
a node joining or leaving moves a share of the fleet and not all of it; a
call to a grain elsewhere costs one hop and behaves like a local one.

**What it will not guarantee, stated as a limit rather than discovered as a
bug:** *single activation under a network partition.* Two halves of a split
cluster will each believe they own a share, and each will be right about its
own half. Every implementation of this model weakens here, Orleans included,
and its documentation says so. This is exactly why 0.4 comes first: a grain
whose state is the truth needs the conflict to be detectable, and after 0.4
it is.

**And it will not move a live activation.** A grain whose owner changes is
deactivated and rebuilt on the new owner; calls in flight during the move
fail rather than being forwarded. Handing over a live activation is a much
harder problem, and buying it with complexity in a first cluster is the
wrong trade.

---

## 0.7 — reminders

A schedule that survives deactivation: a timer, plus 0.4 to remember it,
plus a cluster to decide whose turn it is. Last because it cannot be
earlier, not because it matters least.

---

## Out of scope, decided rather than deferred

**Streams.** Orleans has a pub/sub subsystem with providers, rewindable
subscriptions and backpressure. It is a second product wearing the same hat,
and an honest version of it is larger than everything above put together.

**Transactions across grains.** ACID over several grains needs a transaction
manager and a two-phase protocol. It is the one Orleans feature that would
dwarf this runtime, and a single-writer grain plus the optimistic
concurrency of 0.4 covers what most people reach for it for.

**A cross-language wire protocol.** Other implementations let a grain in one
language be called from another. Worthwhile, and out of scope: this exists
because Python had no virtual actors, not to become a platform.

**Supervision and restart policy.** A failing call raises to its caller; a
grain that fails to activate leaves nothing behind. What to do about it
belongs to whatever asked, which already knows how to resume. Supervision
trees earn their weight when the runtime owns the process; this one owns a
fleet inside somebody else's.

**Grain versioning and rolling upgrade.** Real, and a problem of a size this
package does not have yet. When two versions of one grain must coexist in a
cluster, it goes above the line.

---

## How to read a gap here

If something is not in this file, it has not been considered — say so in an
issue. If it is here under "out of scope", the reasoning is written down,
and disagreeing with the reasoning is the useful conversation.
