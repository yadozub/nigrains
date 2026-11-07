/-!
A port of `nigrains.cluster.Ring`, the placement half of the cluster, and
the two theorems this experiment owes: that a reordered membership is the
same ring, and that a joining node takes only what moved to it.

The hash is not modelled. Every theorem below is true for every fixed
choice of one, so `pos` is a parameter and `_position`'s BLAKE2b is one
witness among all functions `String → Position`. What that buys is honesty
about the scope: nothing here is about hashing, everything is about the
shape built around it.

The sorts are hand-rolled rather than taken from the library, so that every
lemma the proofs need is either in this file or on the sorry ledger below.

`sorry` is a ledger, not a gap that slipped in: each one names a theorem
this experiment still owes, and `lake build` succeeds while owing it.
-/

namespace Nigrains

abbrev Position := Nat

/-- Insert into a sorted list, before the first element it does not precede. -/
def insertBy (le : α → α → Bool) (a : α) : List α → List α
  | [] => [a]
  | b :: bs => if le a b then a :: b :: bs else b :: insertBy le a bs

/-- Insertion sort: chosen over the library's merge sort so that the lemmas
about it live here, visible, rather than in a module this experiment would
then be trusting. -/
def sortBy (le : α → α → Bool) : List α → List α
  | [] => []
  | a :: as => insertBy le a (sortBy le as)

def strLe (a b : String) : Bool := decide (a ≤ b)

/-- The order Python's tuple sort gives: by position, ties by node name. -/
def pairLe (a b : Position × String) : Bool :=
  a.1 < b.1 || (a.1 == b.1 && strLe a.2 b.2)

/-- `sorted(set(members))`, as the Python ring builds it: the members,
without repetition, in name order. -/
def canon (members : List String) : List String :=
  sortBy strLe members.eraseDups

/-- Every node's points on the ring: `replicas` of them, at positions only
the hash knows, in ring order. -/
def pointsOf (pos : String → Position) (members : List String) (replicas : Nat) :
    List (Position × String) :=
  sortBy pairLe <| (canon members).flatMap fun n =>
    (List.range replicas).map fun i => (pos s!"{n}#{i}", n)

/-- The node whose point first follows `x` on the ring, wrapping to the
first point when `x` is past them all. This is Python's
`_nodes[bisect(_points, position) % length]`.

An empty ring answers `none` where the Python one raises; the error path
carries no theorem.
-/
def ownerAt (pts : List (Position × String)) (x : Position) : Option String :=
  match pts.dropWhile (fun p => p.1 ≤ x) with
  | p :: _ => some p.2
  | [] => pts.head?.map (·.2)

/-- Who owns a key: the ring the membership makes, asked where the hash
puts the key. -/
def owner (pos : String → Position) (members : List String) (replicas : Nat)
    (key : String) : Option String :=
  ownerAt (pointsOf pos members replicas) (pos key)

/-! ## The ledger

Two theorems, both stated and neither owed to chance: the first is the
correctness of the reorder cache in `Cluster.owner_of`, the second is the
property a consistent hash exists for, said once as an assertion over 4000
keys in the tests and once as a fact here.
-/

/-- Order and repetition are the membership's business, not the ring's:
the same nodes in any order, with or without duplicates, place every key
identically. Behind this one sentence sits the cache `Cluster.owner_of`
trusts when it declines to rebuild. -/
theorem owner_ignores_order_and_repetition
    (pos : String → Position) (replicas : Nat) (m₁ m₂ : List String)
    (h : m₁.Perm m₂) (key : String) :
    owner pos m₁ replicas key = owner pos m₂ replicas key := by
  sorry

/-- A node joining moves a share and nothing else: for every key, either
the owner is unchanged, or the key moved to the joiner. What moves never
shuffles among the old nodes. -/
theorem a_joiner_takes_only_what_moved
    (pos : String → Position) (replicas : Nat) (old : List String)
    (new : String) (hnew : new ∉ old) (key : String) :
    owner pos old replicas key = owner pos (new :: old) replicas key
      ∨ owner pos (new :: old) replicas key = some new := by
  sorry

end Nigrains
