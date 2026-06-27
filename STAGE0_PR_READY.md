# TSAM Stage 0 + Research Validation Phase — Summary

**Date:** 2026-06-27  
**Status:** Validated Baseline  
**Version:** 1.1 (Peer-Review Draft)

## Reference Implementation

Stage 0 is implemented by **Hephaestus v0.1**, the reference implementation of the TSAM architecture. Throughout this document, “TSAM” refers to the computational architecture and formal specification, while “Hephaestus” refers to the software implementation used for the reported experiments.

## Executive Summary

Stage 0 of TSAM implements a deterministic, non-transformer architecture for constraint-driven software synthesis. It consists of a fixed-memory executive state machine, a priority-aware constraint graph, a deterministic task planner, and an energy-based rewrite/verify loop that either converges on a verified output or terminates with an explicit machine-readable diagnostic.

The Research Validation Phase (RVP) empirically tested whether the core architectural invariants hold as structural complexity increases. After targeted refinement during validation, the system demonstrates:

- Bounded executive state independent of graph size
- Deterministic execution across repeated runs of the evaluated benchmark suite
- Monotonic energy descent on accepted steps with clean rejection diagnostics
- Proper separation between executive memory and program graph memory
- Correct acceptance of solvable cases and rejection of structurally unsolvable cases within the evaluated benchmark domain

## Scope of Stage 0

Stage 0 provides a complete, working reference implementation for porting capability provider patterns from Fabric to NeoForge 1.20.1. It includes:

- **Cognitive State (`S_t`)**: Strictly bounded tuple with ring-buffered verification history.
- **Program Graph (`P_t`)**: AST-derived structural encoding with deterministic signatures.
- **Constraint Graph**: Three-tier priority model (HARD / STRONG / SOFT).
- **Task Planner**: Deterministic decomposition with priority-tier topological ordering.
- **Rewrite + Verification Loop**: Energy function and verification kernel running inside the rewrite loop.
- **Acceptance Criterion**: `Accept(P) ⟺ V(P) = PASS ∧ E < τ ∧ d(P, M) = 0`, with mandatory diagnostic emission on failure.

## Benchmark Suite

The RVP used synthetic test cases generated across five structural complexity levels (L1–L5). Each level varies the number of classes and methods while holding the constraint graph fixed.

- **Solvable cases**: Fabric-style capability providers that should be transformable to valid NeoForge code.
- **Unsolvable cases**: Pure Fabric event-bus code with no capability provider structure (structurally contradictory under current rewrite rules).

Test cases were generated deterministically using seeded generators to ensure reproducibility. Full details of the generators are available in `validation/test_generators.py`.

## Research Validation Phase Results

Five hypotheses were evaluated across 50 test cases (25 solvable + 25 unsolvable) with 3 independent runs each (150 total executions).

### Summary of Key Metrics

| Level | Classes | Methods/Class | Solvable Acceptance | Unsolvable Rejection | Hard Constraint Satisfaction | Avg. Peak Memory (KB) | Avg. Steps (Accepted) |
| ----- | ------- | ------------- | ------------------- | -------------------- | ---------------------------- | --------------------- | --------------------- |
| L1    | 1       | 2             | 5/5                 | 5/5                  | 1.0                          | 83.3                  | 3–5                   |
| L2    | 3       | 3             | 5/5                 | 5/5                  | 1.0                          | 181.4                 | 5–10                  |
| L3    | 5       | 4             | 5/5                 | 5/5                  | 1.0                          | 301.0                 | 10–20                 |
| L4    | 10      | 5             | 5/5                 | 5/5                  | 1.0                          | 607.5                 | 20–40                 |
| L5    | 20      | 5             | 5/5                 | 5/5                  | 1.0                          | 1220.5                | 40–80                 |

### Hypothesis Results

| Hypothesis                                    | Result                                         | Summary                                                                                                                                                                                                            |
| --------------------------------------------- | ---------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **H1: Executive State Scalability**           | PASS                                           | Executive state remains bounded (verified via `prove_constant_memory()` using tracemalloc). Shallow `sys.getsizeof` remained constant at 88 bytes; deep memory usage plateaued within expected ring-buffer limits. |
| **H2: Determinism**                           | PASS                                           | All 50 cases × 3 runs produced identical output hashes.                                                                                                                                                            |
| **H3: Convergence**                           | PASS                                           | Every accepted rewrite sequence exhibited monotonic non-increasing energy. Every rejected execution terminated with a machine-readable diagnostic containing constraint violations and energy trajectory.          |
| **H4: Resource Profile**                      | PASS                                           | Executive memory delta was 0 bytes across all levels and solvability types. Graph memory scaled ~14.6× from L1 to L5.                                                                                              |
| **H5: Software Synthesis Complexity Scaling** | **PASS within the evaluated benchmark domain** | Solvable cases accepted at 5/5 across L1–L5. Structurally unsolvable cases correctly rejected with high-quality diagnostics.                                                                                       |

## Reproducibility

All experiments were conducted using:

- **Python**: 3.12
- **Hardware**: Consumer GPU (NVIDIA GTX 1660 Ti class) with 6 GB VRAM
- **Repository**: Hephaestus v0.1 (reference implementation)
- **Execution**: `python validation/rvp_harness.py` from the project root
- **Randomness**: Fully deterministic via seeded test case generators

The full benchmark harness, test generators, and results are included in the repository under `validation/`.

## Key Limitations

- Only structural complexity was varied. Constraint complexity was held constant (fixed 6 HARD / 2 STRONG / 2 SOFT graph).
- Results are specific to the current rewrite rule set and the NeoForge capability provider domain.
- Soft optimization terms were decoupled from hard acceptance gating.
- No direct comparison against other deterministic program synthesis systems was performed in this validation phase.

## Lessons Learned

- Executive-state separation held under increasing structural load.
- Global constraint checking masked multi-class rewrite defects; per-element verification was required.
- Unconditional method injection on non-capability classes produced semantically broken but syntactically plausible code.
- Fixed scalar energy thresholds do not scale with graph size; soft costs must be separated from acceptance criteria.
- Validation quality improved iteratively as measurement methodology was refined.

## What Stage 0 + RVP Establishes

The validated baseline demonstrates that a deterministic, constraint-driven synthesis architecture can maintain bounded executive state, enforce monotonic progress with clean diagnostics, and correctly distinguish solvable from structurally unsolvable inputs within the evaluated benchmark domain.

The primary outcome of Stage 0 is validation of the computational contract, not demonstration of general software synthesis capability.

## Conclusion

Stage 0 provides a working, validated reference implementation of a deterministic constraint-driven software synthesis system. The Research Validation Phase confirms that its core invariants hold across increasing structural complexity within the evaluated benchmark domain, with transparent documentation of scope and limitations.

This constitutes a credible foundation for further research rather than a claim of general capability.

---

**End of Summary**
