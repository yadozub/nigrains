# Changelog

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
- An activation keeps neither its resolved activation future nor a lock it
  has no use for; both are made or dropped when they stop mattering, which
  is 1058 bytes of runtime bookkeeping per grain rather than 1228.

Not here yet, and named in the README rather than implied away: persistence,
supervision, and distribution. Single activation is trivially true while
there is one node, and will be best effort when there is not.
