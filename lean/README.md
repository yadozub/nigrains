# The Lean experiment

This directory exists to answer one question: *where does formal
verification break, on this codebase, concretely?* It lives on a branch
because the answer is allowed to be "nowhere useful", and a dead
experiment must be cheap to delete.

**What is here.** A port of `nigrains.cluster.Ring` — the placement half,
the pure part — with the hash as a parameter rather than modelled, and two
theorems about it:

- `owner_ignores_order_and_repetition`: the correctness of the reorder
  cache in `Cluster.owner_of`;
- `a_joiner_takes_only_what_moved`: the property a consistent hash exists
  for, which the tests measure over 4000 keys and this states for all of
  them.

**The sorry ledger.** Every `sorry` in `Ring.lean` is a theorem stated but
not yet owed its proof; `lake build` succeeds while owing them. The ledger
shrinks or the experiment failed honestly. It shrank once: the reorder
theorem is proven, in core Lean with no Batteries — the route through the
library's `Sorted`/`eq_of_perm_of_sorted` needed Batteries, the route that
worked needed none of it, only that the string order is total, antisymmetric
and transitive, which core provides. A differential check under a shared
hash (FNV-1a substituted on both sides) matched the Python `Ring` on 700 of
700 owners, so the proof is about a model that computes what the production
code computes.

**The walls, named in advance** so that hitting one is a result and not a
surprise:

1. *The port gap* — these are theorems about a model written to resemble
   the Python, not about the Python. Closing that gap means formalizing
   Python semantics, which is a career, not an experiment.
2. *Interleavings* — the runtime's real invariants ("no await between the
   check and the assignment") need a semantics of the event loop; nobody
   has one. This experiment never goes near that wall on purpose.
3. *The toolchain* — Lean in CI is a tax this branch pays alone.

**The contract.** Nothing in this directory merges to `main` as code. What
can come back is knowledge: the theorems as documentation, and — if the
ledger empties — property tests on the Python side that encode the same
claims, which is the only form a proof can take in a zero-dependency
runtime.

To build:

    elan default leanprover/lean4:stable   # once
    cd lean && lake build
