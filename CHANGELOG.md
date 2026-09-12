# Changelog

## 0.6.0 — 2026-09-12

**`stateless_workers` is now `activations_per_key`.** Breaking, and only a
name — but the name was importing the wrong idea. A grain of this kind is
still a grain: a kilobyte, built on demand, collected when idle. "Worker"
names the heavy thing this model exists to replace, a process that consumes
a queue, and in a system where both exist the process is the *host* and the
grains live inside it. One word for both makes that sentence unreadable, and
the first system to use this package has an arq `TranslateWorker` that hosts
a fleet.

`activations_per_key` says what it does in the model's own words, and the
docstring keeps Orleans' term so that anybody searching for it arrives here.

The same change writes down something the old name let pass unremarked:
**this kind works against the model's premise.** Addressing one thing by its
identity is the whole idea, and this is the kind where identity is admitted
not to matter. It is for work with no natural key. If the work has one, use
it — many grains is what this is good at, and a pool is the fallback.

## 0.5.0 — 2026-09-12

The call path is finished: a call can now be wrapped, given a deadline,
scheduled, or spread over a pool.

- **Timers.** `self.every(60.0, self.refresh)` from `activate`, and the
  runtime runs it for as long as the activation lives. Two rules that are
  the whole design: **ticking does not keep a grain alive** — otherwise one
  call becomes a grain that lives for ever — and **a tick already running
  does delay deactivation**, because finishing under a `deactivate()` that
  has released what the tick was using is the failure the in-flight count
  exists to prevent. A tick that raises is logged and the schedule
  continues.
- **Stateless workers.** `stateless_workers = 8` on the class, and eight
  activations answer for one key, in turn. The one place this package lets
  identity stop meaning one activation, and it is for work with no state to
  protect. A worker's key is the caller's with an index appended, so a
  caller naming `"a#3"` can land on a worker of `"a"` — allowed because it
  cannot matter: which worker answers is the question the kind exists to
  make uninteresting.

Dispatch costs about 0.04 µs more, for the lookup that decides whether a
kind has a pool.

## 0.4.0 — 2026-09-12

**The floor is Python 3.14.** Not for syntax — nothing here needed 3.14 to
parse. The reason is what comes next: free-threading is supported rather
than experimental from 3.14, and the unwritten parts of the roadmap that
would benefit most from it — a pool of stateless workers, a grain doing
CPU-bound work — are exactly the parts a global interpreter lock makes
pointless. Serving interpreters where the honest answer is "that will not
help you" is a promise worth not making.

It is also the reversible direction: lowering a floor later costs a release,
raising one breaks everybody who installed on the old one.

What that buys immediately is small and welcome: `type` aliases instead of
`TypeAlias`, and PEP 695 type parameters instead of a module-level
`TypeVar`. The four-version matrix earned its keep on the way out — it was
what caught a `type` statement 3.11 could not parse.

**And the benchmark's interpreter is pinned**, after removing the pin let
`uv run` fall through to the base image's CPython instead of the standalone
build uv fetches, and moved every number by twenty per cent. The bare
`await` moving too is what gave it away; two builds of one version are not
one machine.

## 0.3.1 — 2026-09-12

**`nigrains.testing` — the adversarial scenarios, packaged.** Every real
defect this package has had was found the same way, and none of those
scenarios was specific to the grain that found them: anybody who writes a
grain meets the same conditions and nobody thinks to write the test. So they
ship. `check_conformance(GrainClass, lambda ref: ref.something())` runs the
battery against your grain in your own suite.

What it checks is the grain rather than the runtime, which has its own
tests: that a herd builds one activation, that the answer survives being
deactivated and rebuilt, that declared reentrancy is real, that a spent
deadline builds nothing, and that two activations agree for a grain that
says they may exist.

**`Grain.tolerates_double_activation`**, default False, because the last of
those cannot be guessed. A cluster admits two activations of one identity
under a partition; a grain fronting immutable data is unaffected and says
so, and a grain whose state is the truth keeps the safe answer.

`idempotent` is deliberately *not* added: it would be read by a retry filter
and there is no retry filter yet.

## 0.3.0 — 2026-09-12

The first half of the call path. Both of these had to exist before a
cluster: a transport has to respect them, and adding them afterwards means
deciding their shape twice.

- **Call filters.** `Runtime(filters=[...])` wraps every call, outermost
  first. Logging, tracing, retry, authorisation and per-call timeouts belong
  here; without it they get patched onto the runtime from outside. A filter
  that does not await its `next` does not call the grain, which is how a
  cache or a refusal is written — and one gap it opens is written down: a
  filter can return something the method never could, and no type checker
  can see past the chain.
- **Deadlines that travel.** `with deadline(2.0):` limits the calls made
  inside it, including calls a grain makes to another grain, because it
  rides in a context variable rather than an argument — an argument would
  have to be threaded through grain signatures that belong to their authors.
  Nesting takes the earlier of the two. A grain can ask `remaining()` and
  shorten its own work, which is the difference between a deadline and a
  kill. `DeadlineExceeded` is a `TimeoutError`.

**Dispatch costs 0.18 µs more**, because every call now reads whether a
deadline is in force. Named rather than buried, and an attempt to claw it
back measured worse and was reverted.

Still to come in 0.3: timers, and stateless workers.

## 0.2.1 — 2026-09-12

- `Runtime.stats` — activations, failed activations, deactivations,
  evictions, calls and calls in flight. Plain counts, so a host can feed
  them to whatever it already uses; no dependency and no opinion about how
  metrics should look. A review of the first consumer had to read a private
  attribute to count activations, which is what prompted this.
- `max_activations` bounds the fleet, collecting the least recently used
  idle grain to make room. A full fleet of busy grains goes over the bound
  and says so rather than refusing a call: the grain being asked for is
  needed now, and a cache size is not worth stopping work for. The ordering
  work is skipped entirely when there is no bound.

## 0.2.0 — 2026-09-12

**A grain carries its own address, and callers get a typed reference.**
Breaking, and worth it on a package this young: `register` takes the class
rather than a name beside it, because a name given at registration and a
name looked up at the call are two sources of truth for one address.

- `Grain.grain_type` is the wire identifier, declared rather than derived -
  renaming a class is a refactor, renaming an address is a migration, and a
  default would make them the same keystroke.
- `Runtime.register(GrainClass)` — with the class as its own factory when
  the constructor wants nothing but the identity.
- `Runtime.reference(GrainClass, key)` returns something typed as the grain.
  A type checker validates every call site against the grain's own
  signatures; underneath it is still `call(id, "name", *args)`, which is the
  shape a transport needs. A method that does not exist fails where it is
  named rather than where it runs, and state is not readable through it.
- `Runtime.call` stays, for callers that have a name and not a class.

**Leaving the runtime now waits for calls in flight.** The idle sweep has
always refused to collect a grain that is answering; shutdown did not, which
put that failure at the moment it is most likely - a process stopping in the
middle of work - and ran every `deactivate()` under a running call. Bounded
by `drain_seconds`, because a grain that never finishes must not be able to
stop a process from stopping.

## 0.1.0 — 2026-09-07

First cut: the node-local half of the runtime.

- `Grain`, `GrainId` and `Runtime`: activation on demand by identity,
  deactivation of idle grains, reentrancy declared per grain class.
- Callers meeting a cold grain wait on one activation rather than starting
  several.
- A failed activation leaves nothing behind and reaches every waiting caller.
- Idleness is measured from the last call to finish, so a grain answering a
  slow call is never collected underneath its caller.
- The idle sweep yields every thousand grains and confirms idleness again
  before deactivating, so collecting a large fleet is not one long stall:
  100 000 idle grains went from an 79 ms block to a 6 ms longest pause.
- An activation keeps neither its resolved activation event nor a lock it
  has no use for; both are made or dropped when they stop mattering, which
  is 1066 bytes of runtime bookkeeping per grain rather than 1228.
- Callers waiting on a cold grain wait on an Event rather than on a shared
  future, so one of them being cancelled no longer cancels the activation
  everybody else was waiting for. A caller whose activation was cancelled by
  somebody else's task retries rather than inheriting the cancellation.
- A grain that has not finished activating is never treated as idle, so an
  `activate()` slower than the idle span is no longer indistinguishable from
  abandonment.

Not here yet, and named in the README rather than implied away: persistence,
supervision, and distribution. Single activation is trivially true while
there is one node, and will be best effort when there is not.
