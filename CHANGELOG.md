# Changelog

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
