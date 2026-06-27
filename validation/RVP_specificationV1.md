# TSAM Research Validation Phase
## Formal Hypothesis Specification and Measurement Protocol

**System:** Tensor-State Associative Manifold (TSAM)  
**Phase:** Research Validation (follows Stage 0)  
**Version:** RVP-1.0  
**Date:** 2026-06-27  
**Status:** Specification — Approved for Implementation

---

## Overarching Research Question

> **Does the Computational Contract continue to hold as complexity increases?**

Stage 0 proved the foundational invariants on a narrow, controlled pattern.
The Research Validation Phase answers whether those invariants are *structural
properties of the architecture* or *artifacts of the small test case*.

This phase produces no new features. It produces **evidence**.

---

## Separation of Concerns

The architecture has two kinds of memory that must be tracked independently:

| Component | Role | Expected behavior |
|---|---|---|
| **Executive State** `S_t` | Scheduler, not knowledge | **Fixed size** regardless of graph scale or step count |
| **Program Graph** `P_t` | The artifact under transformation | **Grows** with problem complexity — this is expected and correct |

The hypotheses below are stated with this separation explicit.
Conflating the two is the most common measurement error in this class of architecture.

---

## Hypothesis H1: Executive State Scalability

**Claim:** The executive state `S_t` has a fixed memory footprint that is
independent of:
- The size of the program graph `P_t` being transformed
- The number of computational steps `t` elapsed
- The number of constraints in the constraint graph `C`

**Measured quantity:** `sizeof(S_t)` in bytes at each step.

**Protocol:**
1. Initialize `S_t` once.
2. Run N computational steps against program graphs of sizes
   {10, 50, 100, 500, 1000, 5000} nodes.
3. Record `sizeof(S_t)` after each step.

**Pass criterion:**
```
max(sizeof(S_t)) - min(sizeof(S_t)) == 0
```
across all steps and all graph sizes. Exact equality — not "approximately flat."
(The empirical baseline from Stage 0 already shows 88 bytes constant across 500 steps.)

**Falsification condition:** Any step where `sizeof(S_t)` increases.

---

## Hypothesis H2: Determinism

**Claim:** For any input `(source_code, constraint_graph, budget)`, the system
produces identical output on every invocation:
```
∀ runs r1, r2: output_hash(r1) == output_hash(r2)
```

**Measured quantity:** MD5 hash of the final program graph source across N independent runs.

**Protocol:**
1. Select K test cases spanning the complexity range.
2. For each test case, run R=10 independent invocations.
3. Compare output hashes across all R runs.

**Pass criterion:**
```
all_equal(hashes) for every test case across all R runs
```

**Falsification condition:** Any two runs on the same input produce different output hashes.

**Why this matters:** Determinism is not obvious under concurrency, hash-map
iteration order changes, or floating-point energy differences. Each must be
explicitly controlled and verified at scale.

---

## Hypothesis H3: Convergence

**Claim:** The energy function `E(P_t, C, M)` is monotonically non-increasing
across accepted rewrite steps, and the system terminates with an explicit
diagnostic whenever progress is not possible within the budget.

Formally, for the sequence of accepted rewrites `(P_0, P_1, ..., P_k)`:
```
E(P_{i+1}) ≤ E(P_i)  for all i ∈ [0, k-1]
```
and either `Accept(P_k)` or a diagnostic is emitted.

**Measured quantities:**
1. Energy trajectory across all accepted rewrite steps per run.
2. Presence/absence of explicit diagnostic on non-acceptance.
3. Convergence rate: steps to acceptance as a function of graph complexity.

**Protocol:**
1. Run the loop on test cases of increasing complexity.
2. For each accepted rewrite step, record `E(P_t)`.
3. Verify the non-increasing property.
4. Verify diagnostic is machine-readable on every non-acceptance.

**Pass criterion:**
```
E(P_{i+1}) <= E(P_i)  for all accepted rewrite steps i
non_acceptance => diagnostic is not None and "tsam_diagnostic" in diagnostic
```

**Falsification condition:**
- Any accepted rewrite step where energy increases.
- Any non-acceptance without a machine-readable diagnostic.

---

## Hypothesis H4: Resource Profile

**Claim:** Peak executive memory (the executive state `S_t` and its direct
components) remains bounded as graph complexity grows, following the
separation of concerns above.

**Measured quantities:**
1. `peak_executive_bytes`: Memory attributable to `S_t` and its fixed-size fields.
2. `peak_graph_bytes`: Memory attributable to `P_t` (expected to scale with graph size).
3. `executive_to_graph_ratio`: Should approach zero as graph size grows.

**Protocol:**
1. Use `tracemalloc` with tagged allocation to separate executive vs graph memory.
2. Run across graph sizes: {10, 50, 100, 500, 1000, 5000} nodes.
3. Record both quantities at each scale point.

**Pass criterion:**
```
peak_executive_bytes is bounded (within 10% of Stage 0 baseline)
executive_to_graph_ratio decreases as graph grows
```

**Falsification condition:**
- `peak_executive_bytes` grows proportionally with graph size.
- Any field in `S_t` that expands as a function of graph complexity.

---

## Hypothesis H5: Software Synthesis Complexity Scaling

**Claim:** The system correctly synthesizes valid outputs for increasingly
complex transformation problems, maintaining constraint satisfaction rates
and diagnostic quality as complexity increases.

**Complexity is measured on three axes:**
1. **Structural complexity**: Number of classes, methods, and cross-references in the source.
2. **Constraint complexity**: Number and priority distribution of active constraints.
3. **Transformation depth**: Number of rewrite operations required to reach the manifold.

**Measured quantities:**
1. Hard constraint satisfaction rate at each complexity level.
2. Strong constraint satisfaction rate at each complexity level.
3. Acceptance rate (fraction of solvable problems that are accepted).
4. Diagnostic quality score on rejected inputs (presence of specific, actionable fields).
5. Steps-to-acceptance as a function of complexity.

**Protocol:**
1. Define complexity levels L1 through L5 (see below).
2. For each level, generate K=10 test cases: K/2 solvable, K/2 structurally unsolvable.
3. Run the system on all cases and record all measured quantities.
4. Verify solvable → acceptance, unsolvable → clean diagnostic.

**Complexity level definitions:**

| Level | Classes | Methods | Constraints (HARD/STRONG/SOFT, active) | Expected rewrites |
|---|---|---|---|---|
| L1 | 1 | 2 | 8 / 2 / 2 | 3–5 |
| L2 | 3 | 9 | 9 / 2 / 2 | 5–10 |
| L3 | 5 | 20 | 9 / 2 / 2 | 10–20 |
| L4 | 10 | 50 | 9 / 2 / 2 | 20–40 |
| L5 | 20 | 100 | 9 / 2 / 2 | 40–80 |

**Revision note (post-Stage-0-review):** the constraint counts above were
updated from this document's original 6/6/8/10/12 HARD figures, which were
illustrative placeholders rather than figures derived from actual
constraints, and were never wired into the harness (`build_neoforge_
constraint_graph()` returned the same fixed graph at every level regardless
of what this table said). The current counts come from
`build_neoforge_constraint_graph(n_classes)`, which activates
MUST_NOT_LEAVE_DANGLING_FABRIC_REFS unconditionally (raising the L1 baseline
from 7 to 8 HARD) and MUST_HAVE_UNIQUE_CAPABILITY_KEYS once n_classes >= 2
(9 HARD at L2 and above). This is intentionally a smaller, two-tier scaling
rather than a full five-tier gradient: adding constraints purely to hit a
pre-set number per level would reintroduce exactly the kind of
unfalsifiable-by-construction problem this validation phase exists to catch.
Additional genuinely-justified cross-class constraints (consistency checks
at STRONG/SOFT priority, further HARD correctness properties) remain a
reasonable direction for Stage 1, to be added as they're identified rather
than invented to fill a quota. See `constraint_complexity_note` in
`rvp_results.json` for the live, machine-readable version of this note.

**Pass criterion:**
```
hard_satisfaction_rate >= 0.95 for solvable cases at all levels
diagnostic_quality_score >= 0.90 for rejected cases at all levels
```

**Falsification condition:**
- Hard constraint satisfaction rate drops below 0.95 for any solvable complexity level.
- Any rejected case that produces no diagnostic or an empty diagnostic.

---

## Measurement Infrastructure Requirements

The validation harness must provide:

1. **Isolated memory measurement**: Executive state and program graph memory
   measured independently. `tracemalloc` snapshots before and after each allocation.

2. **Hash-based determinism verification**: Every output hashed with MD5 before
   comparison. Hashes stored per (test_case_id, run_id) for replay.

3. **Energy trajectory logging**: Full `[E_0, E_1, ..., E_k]` vector stored
   per run, not just final value. Monotonicity checked programmatically.

4. **Diagnostic quality scoring**: A structured rubric for evaluating whether
   a diagnostic is "clean" (actionable) vs "empty" (useless).
   Minimum required fields: `tsam_diagnostic`, `reason`, `step`,
   `last_energy`, `last_distance`, `constraint_violations`.

5. **Statistical reporting**: Mean, standard deviation, min, max per metric
   across the K test cases at each complexity level.

6. **Machine-readable output**: Full JSON report suitable for inclusion in a
   research paper or technical report.

---

## What the Research Validation Phase Does NOT Do

- Does not add new rewrite rules or extend the constraint graph schema.
- Does not implement hierarchy or distributed cognition.
- Does not change the stabilizer implementation.
- Does not optimize performance.
- Does not extend to new graph domains.

All of those come after validation succeeds. Validation first, extension second.

---

## Success Definition

The Research Validation Phase passes if and only if:

```
H1_passes AND H2_passes AND H3_passes AND H4_passes AND H5_passes
```

Each hypothesis has a binary pass/fail verdict derived from its pass criterion above.
The overall phase verdict is the logical AND of all five.

If any hypothesis fails, the failure report must include:
1. The specific falsification condition that triggered.
2. The complexity level at which it first appeared.
3. The specific test case and step where it occurred.
4. A proposed architectural explanation for the failure.

Failure is valid science. A failed hypothesis tells us exactly what must be
fixed before the architecture is ready to scale.

---

**End of Research Validation Phase Specification RVP-1.0**
