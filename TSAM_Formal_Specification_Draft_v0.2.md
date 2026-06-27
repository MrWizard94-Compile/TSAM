# TSAM Formal Specification Draft v0.2

**Title:** Constraint-Driven Deterministic Cognitive Architectures for Verified Software Synthesis  
**System:** Tensor-State Associative Manifold (TSAM)  
**Version:** Draft v0.2  
**Date:** 2026-06-26  
**Status:** Refined formal contract with mathematical invariants, state evolution, and explicit non-goals.

**Core Thesis**  
TSAM defines a computational contract in which progress is made by **converging toward verified, constraint-satisfying structures** rather than by predicting the next symbol. Computation is a closed dynamical process governed by a single invariant: at every step the system must either reduce its distance to a verified manifold or terminate with an explicit diagnostic.

---

## The Computational Contract (Elevated from Invariant I1)

**Computational Contract C1**

At every computation step \( t \), given current program graph \( P_t \) and current cognitive state \( S_t \), the system must produce a successor state \( (S_{t+1}, P_{t+1}) \) such that one of the following holds:

1. **Progress**: The distance to the nearest verified solution manifold strictly decreases:
   \[
   d(P_{t+1}, \mathcal{M}) < d(P_t, \mathcal{M})
   \]
   where \( d(\cdot, \mathcal{M}) \) is a well-defined graph distance to the manifold, **or**

2. **Termination**: The computation terminates and emits an explicit, machine-readable diagnostic explaining why further progress is not possible within the remaining budget.

This contract is the single root principle. Every subsystem, operator, and design decision must be justifiable as either enabling progress under C1 or providing the required diagnostic on termination. There is no wandering, no speculative continuation, and no silent failure.

---

## Core Mathematical Objects

### Definition 1: Cognitive State (Refined)

The cognitive state at step \( t \) is the tuple

\[
S_t = (M_t, C_t, F_t, V_t, B_t, R_t)
\]

- \( M_t \): **Mission** — the persistent high-level intent or constraint set being pursued across multiple tasks.
- \( C_t \): **Context** — bounded relevant history and environmental facts.
- \( F_t \): **Focus** — the current region of the graph or subsystem under active transformation.
- \( V_t \): **Verification Summary** — compact record of recent verification outcomes and energy components.
- \( B_t \): **Execution Budget** — remaining rewrite/repair attempts before forced termination.
- \( R_t \): **Resources** — monitored memory, time, and power consumption (enforces constant footprint).

The cognitive state functions as an **executive scheduler**, not as a knowledge store.

### Definition 2: Constraint Graph (with Priority Levels)

A **Constraint Graph** \( C = (V, E, \Lambda, \Pi) \) is a directed attributed graph where:

- \( V \): nodes representing program elements or requirements
- \( E \): edges representing relations
- \( \Lambda \): labeling function assigning constraints to nodes/edges
- \( \Pi \): priority function assigning each constraint one of three levels:
  - **Hard**: Must be satisfied (e.g., must compile, must preserve saves, must use only allowed APIs). Violation causes immediate rejection.
  - **Strong**: Should be satisfied; violation increases energy significantly and requires justification.
  - **Soft**: Desirable but negotiable (e.g., naming style, formatting). Used for tie-breaking and optimization.

The planner uses priority levels to establish a natural optimization hierarchy. Hard constraints dominate all others.

### Definition 3: Verified Solution Manifold (Renamed)

A **Verified Solution Manifold** \( \mathcal{M} = (G, \mathcal{R}, \mathcal{V}, \mathcal{I}) \) defines the bounded space of acceptable solutions:

- \( G \): space of well-formed graphs
- \( \mathcal{R} \): rewrite operators
- \( \mathcal{V} \): verification operators
- \( \mathcal{I} \): invariants that must hold for membership in the manifold

The manifold defines the **space of acceptable solutions**, not a store of knowledge. An artifact belongs to \( \mathcal{M} \) if and only if it passes every operator in \( \mathcal{V} \) and satisfies every invariant in \( \mathcal{I} \).

### Definition 4: Distance to Manifold

Let \( d(P, \mathcal{M}) \) be a non-negative real-valued function measuring the distance of program graph \( P \) to the nearest verified solution in manifold \( \mathcal{M} \). This distance combines structural distance, constraint violation severity (weighted by priority), and verification failure magnitude.

The distance function must be monotonic with respect to progress under the Computational Contract: successful application of rewrite and stabilization operators must not increase distance.

### Definition 5: Energy Function

The energy of a candidate graph \( P \) is the (initially scalar) weighted sum

\[
E(P, C, \mathcal{M}) = \sum_{i} w_i \cdot E_i(P, C, \mathcal{M})
\]

where components \( E_i \) include compiler errors, hard/strong/soft constraint violations, and structural instability. Future extensions are expected to treat energy as **partially ordered** (lexicographic or multi-objective) so that, for example, any compiler failure strictly dominates improvements in style or performance.

### Definition 6: Acceptance

\[
\text{Accept}(P) \iff \mathcal{V}(P) = \text{PASS} \land E(P, C, \mathcal{M}) < \tau \land d(P, \mathcal{M}) = 0
\]

where \( \tau \) is the domain acceptance threshold. Acceptance requires both verification passage and zero distance to the manifold.

### Definition 7: Energy-Based Structural Stabilization Operator

The stabilization operator \( \mathcal{S} \) is any procedure (neural, symbolic, or hybrid) that, given \( P \) and active constraints, produces \( P' \) satisfying:

\[
E(P', C, \mathcal{M}) \leq E(P, C, \mathcal{M}) \quad \text{and} \quad d(P', \mathcal{M}) \leq d(P, \mathcal{M})
\]

**The stabilizer is an interchangeable module.** TSAM requires the existence of an effective energy/distance minimizer; it does not prescribe any particular implementation (modern Hopfield dynamics, other associative memory techniques, or purely symbolic methods are all admissible provided they respect the Computational Contract).

---

## State Evolution Equation

The cognitive state and program graph evolve according to a transition function:

\[
(S_{t+1}, P_{t+1}) = F(S_t, P_t, V_t, C_t)
\]

where:
- \( S_t \): current cognitive state
- \( P_t \): current program graph
- \( V_t \): verification results from the previous step
- \( C_t \): active constraint graph

The function \( F \) encapsulates planning, transformation, stabilization, and decision logic. It must be defined such that every application either satisfies the Computational Contract or produces a terminating diagnostic.

This equation is the effective "CPU instruction" of the TSAM computational model.

---

## The Computational Loop (Dynamical System)

```
Observe (Intent + Current Graph + Cognitive State)
    ↓
Constrain (Construct / Update Constraint Graph with priorities)
    ↓
Plan (Decompose into ordered tasks respecting priority hierarchy)
    ↓
Transform (Apply rewrite operators)
    ↓
Stabilize (Energy/distance minimization via interchangeable stabilizer)
    ↓
Verify (Run verification operators → update energy, distance, Verification Summary)
    ↓
Decide
    ├── Accept(P) → Emit artifact + Update Cognitive State
    └── Else if Budget allows → Repair (bounded) and continue loop
         Else → Terminate with explicit diagnostic (per Computational Contract C1)
```

This loop contains no neural terminology, no token prediction, and no autoregressive generation. It is a deterministic dynamical system whose attractors are verified, constraint-satisfying program structures.

---

## Epistemological Layer (Belief → Fact Progression) — Expanded

TSAM maintains an explicit distinction between provisional and verified knowledge:

- **Belief**: An assumption or pattern currently treated as likely but not yet verified (e.g., probable gameplay intent, common naming conventions, likely architectural patterns).
- **Fact**: An element that has successfully passed the relevant verification operators in \( \mathcal{V} \) (syntax rules, API contracts, type invariants, compiler rules, behavior preservation checks).

**Transition Rule**: The Verification Kernel is responsible for converting beliefs into facts. Every successful verification pass strengthens the epistemic status of the involved elements. Failed verifications may demote previously held beliefs or trigger diagnostic termination.

This distinction is a first-class computational primitive. Beliefs may influence planning and stabilization, but only facts may be relied upon for final acceptance under the Computational Contract.

---

## Non-Goals (Explicit Boundaries for Stage 0)

TSAM Stage 0 does **not** attempt to:

- Model unrestricted natural language conversation or open-ended dialogue.
- Claim or demonstrate general intelligence.
- Guarantee globally optimal solutions (only locally improving progress under the Computational Contract).
- Eliminate the need for domain expertise or curated manifolds.
- Replace formal verification tools; it incorporates and orchestrates them.
- Provide unbounded context or perfect long-term memory (it maintains bounded cognitive state only).
- Operate on arbitrary codebases without a pre-defined verified solution manifold for the target domain.

These boundaries are deliberate. They keep Stage 0 focused on falsifiable claims about constant-memory, deterministic, constraint-driven synthesis within a narrow but realistic software engineering pattern.

---

## Interface Independence and Extensibility

All core definitions (Cognitive State, Constraint Graph, Verified Solution Manifold, Stabilization Operator, Verification Operators, distance, energy) are specified independently of concrete implementation technology. This enables:

- Swapping the stabilizer implementation
- Changing the underlying graph domain (ASTs, CFGs, circuit graphs, mechanical assemblies, workflows)
- Extending verification operators without altering the Computational Contract
- Hierarchical or distributed realizations in later stages

---

## Stage 0 Instantiation (v0.1 Prototype Requirements)

For the initial reference implementation:

- Graph domain: Simplified Python AST representing NeoForge-style capability + event patterns
- Stabilizer: Discrete/continuous energy minimization over structural signatures (with logged fidelity metric)
- Verification operators: Parse + structural checks, hard/strong constraint satisfaction, basic behavior preservation
- Distance and energy: Initially scalar; priority-weighted constraint violations
- Acceptance: Hard constraints satisfied + energy below threshold + distance effectively zero + budget not exhausted

All Stage 0 claims must be experimentally measurable via the automated benchmark harness (compilation success, constraint satisfaction, memory constancy, determinism, diagnostic quality).

---

## Research Program Roadmap (Post Stage 0)

After successful Stage 0 demonstration, the program proceeds as:

1. Formal Specification refinement + proofs of key properties
2. Reference implementation + verification benchmark
3. Published experimental results on Stage 0 benchmark
4. Exploration of alternative stabilizers
5. Extension to additional graph domains
6. Hierarchical and distributed realizations
7. Hardware acceleration considerations for constraint evaluation, graph rewriting, and verification primitives

---

## Summary

TSAM defines a deterministic computational contract:

> Computation progresses by iteratively converging toward verified, constraint-satisfying structures (reducing distance to the manifold) or terminates with an explicit diagnostic. There is no wandering and no speculation.

This contract is realized through a closed dynamical loop whose state evolution is governed by the transition function \( F \). The architecture is defined by precise mathematical objects whose interfaces are independent of any particular neural or symbolic implementation.

This specification provides the minimal rigorous foundation required to implement a falsifiable Stage 0 prototype and to serve as the starting point for a credible research program in constraint-driven deterministic cognitive architectures for verified software synthesis.

---

**End of Formal Specification Draft v0.2**