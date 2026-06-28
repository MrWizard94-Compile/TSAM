Document 1: TSAM Stage 1 Architectural Specification (Detailed Final v1.2)
TSAM Stage 1 — Cross-Module Dependency Tracking
Architectural Specification v1.2 (Detailed Final)
Research Goal
The objective of Stage 1 is to determine whether bounded cross-module reasoning can preserve the computational contract established in Stage 0 while extending the architecture from single-file to multi-module software transformations.

1. Formulating the Multi-Hop Cross-Module Dataflow Array
1.1 Design Philosophy
Stage 0 proved that a deterministic, bounded-state rewrite engine can operate reliably on single-file inputs. Real software, however, is organized across modules with import/export relationships. Naively expanding the analysis to the full transitive closure of dependencies would violate the core invariant of bounded executive state. Stage 1 therefore adopts a deliberately approximate but bounded approach to cross-module reasoning.
The guiding principle is:
Only materialize and reason about the portion of the dependency graph that is relevant to the current rewrite focus, while making the approximation limits explicit and measurable.
1.2 Formal Data Structures
ModuleDescriptor
$$m = (id, path\_hash, exports, imports, capability\_evidence, last\_accessed\_timestamp)
$$

id: Deterministic SHA-256 of the module’s canonical filesystem path.
exports: Set of symbols the module publicly exports (capped).
imports: Set of (target_module_id, imported_symbol) pairs.
capability_evidence: Boolean flag indicating structural evidence that the module contains capability-provider classes.
last_accessed_timestamp: Used for LRU-based eviction decisions.

Cross-Module Edge
$$e = (source\_module\_id, target\_module\_id, symbol, hop\_count, resolved\_key\_or\_unresolved)
$$

hop_count: Number of import hops traversed (hard maximum of 3).
resolved_key_or_unresolved: Either a resolved canonical capability key or a marker indicating an unresolved external reference.

Resolved Capability Key
$$k = (declaring\_module\_id, key\_name, referencing\_class\_ids)
$$
1.3 Bounded Multi-Hop Resolution Rules
Resolution follows these deterministic rules:

Direct Import Resolution — When module $  A  $ imports symbol $  s  $ from module $  B  $, attempt resolution inside $  B  $.
Transitive Resolution — If $  B  $ re-exports $  s  $ from module $  C  $, follow the chain only while hop_count < 3.
Hard Termination — Any resolution reaching hop_count == 3 is immediately terminated. The reference is recorded as an unresolved external reference and contributes to the Cross-module Consistency penalty $  C  $.
No Iterative Fixed-Point Analysis — The system performs no global fixed-point computation. All resolutions are single-pass and demand-driven from the current planning focus.

This guarantees that the computational cost of resolving any single symbol is bounded by a small constant rather than growing with project size or dependency depth.
1.4 The Active Window $  W_t  $
We define the Active Window as a formal subset of the full ProgramGraph:
$$W_t \subset P_t \quad \text{with} \quad |W_t| \ll |P_t|$$
All planning, rewriting, verification, and energy calculations in Stage 1 operate exclusively over $  W_t  $. The full ProgramGraph $  P_t  $ is treated as persistent, read-only storage.

2. Guarding Invariant 5.1 (O(1) Memory Bounds)
2.1 Executive State Payload
The Stage 1 executive state is defined as:
$$S_t = (M_t, C_t, F_t, V_t, B_t, R_t, W_t)$$
where $  W_t  $ is the bounded active window.
2.2 Hard Cardinality Limits

























ComponentHard LimitPurposeActive Modules32Keeps memory usage predictableActive Cross-Module Edges128Bounds resolution complexityResolved Capability Keys64Prevents unbounded key accumulation
These are hard limits. Exceeding any limit triggers immediate LRU eviction.
2.3 Working-Set Lifecycle

Admission: A module enters $  W_t  $ when referenced by the current Focus or by a dependency of an active module.
Eviction: After each rewrite pass, modules not referenced in the current Focus or the last 4 verification records become eviction candidates. LRU is applied.
Re-resolution on Access: Evicted modules have their cross-module edges dropped. Re-resolution occurs lazily on next access and is accounted for in planner cost.

2.4 Drift Prevention
Memory drift is prevented through:

Periodic bounded context flushes.
Strict separation between persistent ProgramGraph and transient executive window.
No accumulation of historical cross-module state beyond the active window.


3. Expanding the Lexicographic Energy Manifold
3.1 Stage 1 Energy Tuple
We define the energy key as the ordered 4-tuple:
$$\text{EnergyKey} = (H, S, C, Q)$$
Component Roles:

$  H  $: Hard + Syntax penalty (includes dangling references from unresolved cross-module symbols).
$  S  $: Strong penalty.
$  C  $: Cross-module Consistency Penalty — counts unresolved external references and cross-module capability key collisions.
$  Q  $: Quality / Soft delta (node count, naming, formatting, etc.).

3.2 Lexicographic Ordering
The tuple is compared in the order $  H \succ S \succ C \succ Q  $. This gives structural consistency violations (C) priority over pure quality optimizations (Q).
3.3 Termination Properties
Because $  C  $ dominates $  Q  $, the planner cannot indefinitely optimize quality while ignoring cross-module inconsistencies. Combined with the hard bounds on $  W_t  $, the number of distinct reachable energy states within one planning cycle is finite. Therefore, the planner is expected to reach a fixed point (no improving rewrite exists within the current window) in a number of iterations bounded by $  |W_t|  $.

4. Component Interactions

The Planner reasons only over the current $  W_t  $.
The Rewrite Engine only modifies nodes belonging to modules currently in $  W_t  $.
The Verification Kernel evaluates both local and visible cross-module constraints.
The Working-Set Manager maintains $  W_t  $ and performs eviction between passes.

This tight integration ensures that memory, resolution cost, and energy calculations remain bounded throughout execution.

End of TSAM Stage 1 Architectural Specification (v1.2 – Detailed Final)