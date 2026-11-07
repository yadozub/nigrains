/-!
A port of `nigrains.cluster.Ring`, the placement half of the cluster, and
the two theorems this experiment owes: that a reordered membership is the
same ring, and that a joining node takes only what moved to it.

The hash is not modelled. Every theorem below is true for every fixed
choice of one, so `pos` is a parameter and `_position`'s BLAKE2b is one
witness among all functions `String → Position`. What that buys is honesty
about the scope: nothing here is about hashing, everything is about the
shape built around it.

The sorts and the dedup are hand-rolled rather than taken from the
library, so that every lemma the proofs need is in this file and visible
to a reader; core Lean's `eraseDups` would have worked too, but its
lemmas live in Batteries and this experiment owes no dependencies.

The first theorem is proven; the second is still on the ledger, and
`sorry` there names what this experiment still owes.
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

/-- Insert only if absent: the dedup half of `canon`, as a single step. -/
def insertNew (a : String) (l : List String) : List String :=
  if a ∈ l then l else insertBy strLe a l

/-- `sorted(set(members))`, as the Python ring builds it: the members,
without repetition, in name order. Dedup by insert-if-absent rather than
core's `eraseDups`, so the lemmas are all here. -/
def canon : List String → List String
  | [] => []
  | a :: as => insertNew a (canon as)

theorem canon_cons (a : String) (as : List String) :
    canon (a :: as) = insertNew a (canon as) := rfl

theorem insertNew_pos {a : String} {l : List String} (h : a ∈ l) :
    insertNew a l = l := by
  simp [insertNew, h]

theorem insertNew_neg {a : String} {l : List String} (h : a ∉ l) :
    insertNew a l = insertBy strLe a l := by
  simp [insertNew, h]

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

/-! ## The toolkit

Everything the proof needs, in this file, in reading order: what insertion
does to membership, that the string order is total, that two inserts into
one list commute, and that none of it can tell a permutation from its
result.
-/

theorem insertBy_perm (le : α → α → Bool) (a : α) :
    ∀ l : List α, (insertBy le a l).Perm (a :: l)
  | [] => List.Perm.refl _
  | b :: bs => by
      by_cases h : le a b = true
      · simp only [insertBy, if_pos h]
        exact List.Perm.refl _
      · simp only [insertBy, if_neg h]
        exact List.Perm.trans (List.Perm.cons b (insertBy_perm le a bs))
          (List.Perm.swap a b bs)

theorem mem_insertBy {le : α → α → Bool} {a x : α} {l : List α} :
    x ∈ insertBy le a l ↔ x = a ∨ x ∈ l :=
  (List.Perm.mem_iff (insertBy_perm le a l)).trans List.mem_cons

theorem mem_canonic (a : String) : ∀ m : List String, a ∈ canon m ↔ a ∈ m
  | [] => by simp [canon]
  | b :: bs => by
      by_cases hb : b ∈ canon bs
      · have hb' : b ∈ bs := (mem_canonic b bs).1 hb
        have heq : canon (b :: bs) = canon bs := by
          rw [canon_cons, insertNew_pos hb]
        rw [heq]
        constructor
        · intro h
          exact List.mem_cons.2 (Or.inr ((mem_canonic a bs).1 h))
        · intro h
          rcases List.mem_cons.1 h with rfl | h
          · exact hb
          · exact (mem_canonic a bs).2 h
      · have heq : canon (b :: bs) = insertBy strLe b (canon bs) := by
          rw [canon_cons, insertNew_neg hb]
        rw [heq, mem_insertBy]
        constructor
        · intro h
          rcases h with rfl | h
          · exact List.mem_cons.2 (Or.inl rfl)
          · exact List.mem_cons.2 (Or.inr ((mem_canonic a bs).1 h))
        · intro h
          rcases List.mem_cons.1 h with rfl | h
          · exact Or.inl rfl
          · exact Or.inr ((mem_canonic a bs).2 h)

/-- The order on strings, as the Bool comparator sees it. -/
def SLe (a b : String) : Prop := strLe a b = true

theorem strLe_iff {a b : String} : SLe a b ↔ a ≤ b := by
  rw [SLe, strLe, decide_eq_true_iff]

theorem strLe_total (a b : String) : SLe a b ∨ SLe b a :=
  (String.le_total a b).imp strLe_iff.2 strLe_iff.2

theorem strLe_antisymm {a b : String} (h1 : SLe a b) (h2 : SLe b a) : a = b :=
  String.le_antisymm (strLe_iff.1 h1) (strLe_iff.1 h2)

theorem strLe_trans {a b c : String} (h1 : SLe a b) (h2 : SLe b c) : SLe a c :=
  strLe_iff.2 (String.le_trans (strLe_iff.1 h1) (strLe_iff.1 h2))

theorem strLe_true {a b : String} (h : SLe a b) : strLe a b = true := h

theorem strLe_false {a b : String} (h : ¬ SLe a b) : strLe a b = false := by
  cases h' : strLe a b with
  | true => exact absurd h' h
  | false => rfl

/-- The one classical fact: two inserts into the same list commute. The
hypothesis says which of the two comes first when both land; totality
covers the other direction at the wrapper below. -/
theorem insertBy_comm_aux : ∀ {a b : String}, SLe a b → ∀ l : List String,
    insertBy strLe a (insertBy strLe b l) = insertBy strLe b (insertBy strLe a l)
  | a, b, hab, [] => by
      by_cases hba : SLe b a
      · have hab' : a = b := strLe_antisymm hab hba
        subst hab'; rfl
      · simp [insertBy, strLe_true hab, strLe_false hba]
  | a, b, hab, x :: xs => by
      by_cases hbx : SLe b x
      · have hax := strLe_trans hab hbx
        by_cases hba : SLe b a
        · have hab' : a = b := strLe_antisymm hab hba
          subst hab'; rfl
        · simp [insertBy, strLe_true hab, strLe_true hbx, strLe_true hax, strLe_false hba]
      · by_cases hax : SLe a x
        · by_cases hba : SLe b a
          · have hab' : a = b := strLe_antisymm hab hba
            subst hab'; rfl
          · simp [insertBy, strLe_false hbx, strLe_true hax,
              strLe_false hba]
        · simp [insertBy, strLe_true hab, strLe_false hbx, strLe_false hax]
          rw [insertBy_comm_aux hab xs]

theorem insertBy_comm (a b : String) (l : List String) :
    insertBy strLe a (insertBy strLe b l) = insertBy strLe b (insertBy strLe a l) := by
  rcases strLe_total a b with h | h
  · exact insertBy_comm_aux h l
  · exact (insertBy_comm_aux h l).symm

/-- Dedup-and-sort cannot tell a permutation from its result - the whole
of theorem one, at the membership level. Cons and trans are one-step;
swap is where the work lives, and insertBy_comm is the work. -/
theorem canonic_perm : ∀ {m₁ m₂ : List String}, m₁.Perm m₂ → canon m₁ = canon m₂
  | _, _, .nil => rfl
  | _, _, @List.Perm.cons _ a l₁ l₂ h => by
      have ih := canonic_perm h
      by_cases hal : a ∈ l₁
      · have h1 : a ∈ canon l₁ := (mem_canonic a l₁).2 hal
        have h2 : a ∈ canon l₂ :=
          (mem_canonic a l₂).2 ((List.Perm.mem_iff (a := a) h).1 hal)
        rw [canon_cons, insertNew_pos h1, canon_cons, insertNew_pos h2, ih]
      · have h2l : a ∉ l₂ := fun hx => hal ((List.Perm.mem_iff (a := a) h).2 hx)
        have h1 : a ∉ canon l₁ := fun hx => hal ((mem_canonic a l₁).1 hx)
        have h2 : a ∉ canon l₂ := fun hx => h2l ((mem_canonic a l₂).1 hx)
        rw [canon_cons, insertNew_neg h1, canon_cons, insertNew_neg h2, ih]
  | _, _, .swap a b l => by
      by_cases hal : a ∈ l
      · by_cases hbl : b ∈ l
        · have h1 : a ∈ canon (b :: l) :=
            (mem_canonic a (b :: l)).2 (List.mem_cons.2 (Or.inr hal))
          have h2 : b ∈ canon l := (mem_canonic b l).2 hbl
          have h3 : b ∈ canon (a :: l) :=
            (mem_canonic b (a :: l)).2 (List.mem_cons.2 (Or.inr hbl))
          have h4 : a ∈ canon l := (mem_canonic a l).2 hal
          have eL : canon (a :: b :: l) = canon l := by
            rw [canon_cons, insertNew_pos h1, canon_cons, insertNew_pos h2]
          have eR : canon (b :: a :: l) = canon l := by
            rw [canon_cons, insertNew_pos h3, canon_cons, insertNew_pos h4]
          rw [eL, eR]
        · have h4 : a ∈ canon l := (mem_canonic a l).2 hal
          have h2 : b ∉ canon l := fun hx => hbl ((mem_canonic b l).1 hx)
          have h1 : a ∈ canon (b :: l) :=
            (mem_canonic a (b :: l)).2 (List.mem_cons.2 (Or.inr hal))
          have h3 : b ∉ canon (a :: l) := fun hx => by
            rcases List.mem_cons.1 ((mem_canonic b (a :: l)).1 hx) with e | hx'
            · subst e; exact hbl hal
            · exact hbl hx'
          have eL : canon (a :: b :: l) = insertBy strLe b (canon l) := by
            rw [canon_cons, insertNew_pos h1, canon_cons, insertNew_neg h2]
          have eA : canon (a :: l) = canon l := by
            rw [canon_cons, insertNew_pos h4]
          have eR : canon (b :: a :: l) = insertBy strLe b (canon l) := by
            rw [canon_cons, insertNew_neg h3, canon_cons, insertNew_pos h4]
          rw [eL, eR]
      · by_cases hbl : b ∈ l
        · have h4 : b ∈ canon l := (mem_canonic b l).2 hbl
          have h2 : a ∉ canon l := fun hx => hal ((mem_canonic a l).1 hx)
          have h1 : b ∈ canon (a :: l) :=
            (mem_canonic b (a :: l)).2 (List.mem_cons.2 (Or.inr hbl))
          have h3 : a ∉ canon (b :: l) := fun hx => by
            rcases List.mem_cons.1 ((mem_canonic a (b :: l)).1 hx) with e | hx'
            · subst e; exact hal hbl
            · exact hal hx'
          have eL : canon (a :: b :: l) = insertBy strLe a (canon l) := by
            rw [canon_cons, insertNew_neg h3, canon_cons, insertNew_pos h4]
          have eR : canon (b :: a :: l) = insertBy strLe a (canon l) := by
            rw [canon_cons, insertNew_pos h1, canon_cons, insertNew_neg h2]
          rw [eL, eR]
        · by_cases hab : a = b
          · subst hab; rfl
          · have h1 : a ∉ canon (b :: l) := fun hx => by
              rcases List.mem_cons.1 ((mem_canonic a (b :: l)).1 hx) with e | hx'
              · subst e; exact hab rfl
              · exact hal hx'
            have h2 : b ∉ canon (a :: l) := fun hx => by
              rcases List.mem_cons.1 ((mem_canonic b (a :: l)).1 hx) with e | hx'
              · subst e; exact hab rfl
              · exact hbl hx'
            have h3 : b ∉ canon l := fun hx => hbl ((mem_canonic b l).1 hx)
            have h4 : a ∉ canon l := fun hx => hal ((mem_canonic a l).1 hx)
            have eL : canon (a :: b :: l)
                = insertBy strLe a (insertBy strLe b (canon l)) := by
              rw [canon_cons, insertNew_neg h1, canon_cons, insertNew_neg h3]
            have eR : canon (b :: a :: l)
                = insertBy strLe b (insertBy strLe a (canon l)) := by
              rw [canon_cons, insertNew_neg h2, canon_cons, insertNew_neg h4]
            rw [eL, eR]
            exact (insertBy_comm a b (canon l)).symm
  | _, _, .trans h₁ h₂ => (canonic_perm h₁).trans (canonic_perm h₂)

/-! ## The ledger
-/

/-- Order and repetition are the membership's business, not the ring's:
the same nodes in any order, with or without duplicates, place every key
identically. Behind this one sentence sits the cache `Cluster.owner_of`
trusts when it declines to rebuild. -/
theorem owner_ignores_order_and_repetition
    (pos : String → Position) (replicas : Nat) (m₁ m₂ : List String)
    (h : m₁.Perm m₂) (key : String) :
    owner pos m₁ replicas key = owner pos m₂ replicas key := by
  have hc : canon m₁ = canon m₂ := canonic_perm h
  simp [owner, pointsOf, hc]

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
