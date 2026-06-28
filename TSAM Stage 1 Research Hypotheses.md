TSAM Stage 1 — Research Hypotheses
Final Version 1.2
Date: 2026-06-27
Status: Final

Research Goal
The objective of Stage 1 is to determine whether bounded cross-module reasoning can preserve the computational contract established in Stage 0 while extending the architecture from single-file to multi-module software transformations.

H1-S1: Executive State Boundedness
Claim:
The executive state remains strictly bounded when operating on multi-module projects through the active working-set window mechanism.
Measured Quantities:

Peak executive memory usage (via tracemalloc).
Average occupancy of Active Modules, Active Edges, and Resolved Keys throughout rewrite sessions.
Frequency and cost of module re-resolution after eviction.

Pass Criterion:

Executive memory usage remains within a small constant factor (≤ 3×) of the Stage 0 baseline as project size increases.
Average working-set sizes remain substantially below their hard caps, demonstrating effective LRU behavior.

Falsification Condition:

Executive memory grows proportionally with project size, or the working-set eviction policy fails to keep average occupancy meaningfully below capacity.


H2-S1: Determinism under Cross-Module Resolution
Claim:
Cross-module key resolution and working-set operations produce fully deterministic outcomes.
Measured Quantities:

Cryptographic hash of the final ProgramGraph.
Cryptographic hash of the Execution Trace.
Final set of resolved capability keys.

Execution Trace Event Grammar:
textCopyLOAD(module_id)
RESOLVE(key)
PLAN(task)
REWRITE(node_id)
VERIFY
EVICT(module_id)
Pass Criterion:

The final ProgramGraph, Execution Trace, and resolved capability key set are identical across independent runs on the same input.

Falsification Condition:

Different runs produce different execution traces or different resolved key sets.


H3-S1: Impact of 3-Hop Approximation on Resolution Quality
Claim:
The 3-hop resolution limit introduces a measurable but bounded reduction in resolution quality.
Measured Quantities:

Resolution Recall$$\text{Recall} = \frac{\text{Keys resolved within 3 hops}}{\text{All resolvable keys (unrestricted baseline)}}$$
Resolution Precision$$\text{Precision} = \frac{\text{Correctly resolved keys}}{\text{All keys resolved by the 3-hop mechanism}}$$

Pass Criterion:

Resolution Recall ≥ 95% on realistic multi-module projects.
Resolution Precision remains reasonably high (no excessive false positives).

Falsification Condition:

Recall falls significantly below the target threshold, or real capability-key collisions within the hop limit go undetected.


H4-S1: Convergence under the (H, S, C, Q) Energy Ordering
Claim:
The 4-tuple lexicographic energy ordering produces strictly monotonic improvement on committed rewrites and is expected to terminate under the bounded planning model.
Definitions:

A committed rewrite is defined as a rewrite that is actually written back to the ProgramGraph (not merely evaluated as a candidate).

Measured Quantities:

Lexicographic energy trajectory across iterations.
Whether every committed rewrite strictly decreases the tuple (H, S, C, Q).
Number of iterations required to reach a fixed point.

Pass Criterion:

Every committed rewrite strictly decreases the energy tuple.
All benchmark executions reach a fixed point within a number of iterations bounded by the size of the active working set.
No oscillation or repeated rewrites without energy improvement is observed.

Falsification Condition:

A committed rewrite fails to strictly decrease the energy tuple, or the planner fails to reach a fixed point in practice.


H5-S1: Stability of Working-Set Eviction
Claim:
The LRU-style eviction policy maintains acceptable overhead without causing thrashing or excessive re-resolution work.
Measured Quantities:

Re-resolution overhead as a percentage of total planner iterations.
Frequency of repeated load/evict cycles on the same module within a single planning cycle.

Pass Criterion:

Re-resolution overhead remains below an engineering threshold of approximately 30%. (Preliminary profiling indicated that planner throughput begins degrading noticeably above this level; the threshold is treated as an empirical engineering target for Stage 1 validation.)

Falsification Condition:

Overhead consistently exceeds the threshold and measurably degrades planner performance, or thrashing behavior is observed.


H6-S1: Working-Set Completeness
Claim:
The bounded working-set window is generally sufficient for effective planning. Premature eviction does not cause a significant number of spurious planner failures.
Measured Quantities:

Number of rewrite failures or rejections caused by missing context (required module was evicted).
Recovery rate after a module is reloaded.
Window Locality$$\text{Window Locality} = \frac{\text{Planner decisions requiring no module reload}}{\text{Total planner decisions}}$$

Pass Criterion:

Failures caused by premature eviction remain below 5% of total planner decisions.
Window Locality is reasonably high.

Falsification Condition:

A significant fraction of planner failures or rejections are caused by modules being evicted too early.


H7-S1: Incremental Scaling Efficiency
Claim:
A localized change to a single module triggers analysis and rewriting work that scales with the size of the affected dependency neighborhood rather than the size of the entire project.
Measured Quantities (after a single localized edit):

Number of modules analyzed
Number of modules rewritten
Number of modules verified
Number of edge resolutions performed

Pass Criterion:

Work performed scales with the size of the affected dependency neighborhood, not with total project size.

Falsification Condition:

Small, localized changes repeatedly trigger near-global analysis or rewriting.


Benchmark Project Topologies
All hypotheses shall be evaluated across the following dependency graph families to ensure structural diversity:

Linear chains — Stresses the 3-hop resolution limit.
Balanced trees — Represents hierarchical project structures.
Hub-and-spoke — Common in projects with shared libraries or core utilities.
Sparse random graphs — Represents loosely coupled modules.
Dense dependency clusters — Represents tightly coupled subsystems.


End of TSAM Stage 1 Research Hypotheses (Final v1.2)