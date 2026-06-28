# Changelog

All notable changes to TSAM (Hephaestus reference implementation) are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning follows [Semantic Versioning](https://semver.org/): for this research
codebase, MINOR bumps mark a change in validated behavior or capability
(new/changed constraints, fixed correctness bugs, new measurement
infrastructure); PATCH would be reserved for changes with no behavioral or
contract effect (typo fixes, comment-only edits); MAJOR is reserved for a
Stage transition (e.g. Stage 0 → Stage 1 entry criteria formally met).

All versions below were produced and verified in a single continuous review
process on 2026-06-27. Each entry was verified against the full test suite
and the RVP harness before being applied — see "Verification" per entry.

---

## [0.7.0] — 2026-06-28 — Stage 1 groundwork: multi-module program representation

First slice of Stage 1 (cross-module dependency tracking). It adds the
persistent multi-module representation P_t that Stage 1's bounded active
window, demand-driven resolver, and Cross-module Consistency penalty C will
operate over in subsequent slices. This is **groundwork, not a Stage
transition**: no Stage 1 entry criteria are claimed met (that would be a
MAJOR bump per the versioning note above), and Stage 0 behaviour is
untouched — the full RVP is numerically unchanged.

### Added
- `src/tsam/module_graph.py`:
  - `ModuleDescriptor` (Stage 1 Spec §1.2): id, canonical path, exports,
    imports, capability_evidence, parse/truncation status, last_accessed.
    Immutable; reuses the Stage 0 capability-provider heuristic
    (`class_shows_capability_provider_evidence`) unchanged.
  - `ModuleGraph`: the persistent P_t — every module's descriptor (the
    bounded view) plus its parsed `ProgramGraph` (the artifact), kept in
    separate maps, with the resolved cross-module import/export structure.
    Two-pass build (assign ids → resolve import targets by exact
    canonical-path match).
  - Static extraction: `__all__`-aware export detection with re-export
    provenance (origin module recorded, for the future transitive resolver),
    module-level import extraction (from / plain / star / relative), and
    internal-vs-external resolution.
  - Derived cross-module signals, all deterministic and sorted:
    `cross_module_imports`, `dangling_internal_imports` (the cross-module
    unresolved-reference signal that will feed the C penalty),
    `capability_key_index` / `colliding_capability_keys` (the cross-module
    registration-collision signal), `importers_of`, and `structural_hash`.
- `validation/module_generators.py`: heterogeneous multi-module fixtures
  (core / Fabric provider / already-ported NeoForge provider / plain utility
  / registry archetypes), with solvable and unsolvable project generators
  carrying declared expected structure. Directly attacks the standing
  homogeneity limitation of the single-file benchmark (one pattern × N
  copies) by mixing archetypes and real cross-module references in one
  project.
- `tests/test_stage1_modules.py` (34 tests): identity/canonicalisation,
  export/import extraction (incl. `__all__`, re-export provenance, alias and
  star handling), capability evidence, clean rejection (unparseable module
  flagged not silently empty; ambiguous project rejected; export/import caps
  flagged), determinism (stable hash across builds, input-order-independent,
  excludes window state), bounded descriptor (size independent of source
  length), cross-module structure (dangling, collisions, importers), and the
  fixtures' declared expected structure.

### Design notes / deviations
- **`last_accessed` is a logical tick, not a wall-clock timestamp.** The
  Stage 1 spec (§1.2) names it a "timestamp," but a wall-clock value would
  violate the Determinism invariant. Implemented as an integer access
  counter the (future) working-set manager advances via
  `ModuleDescriptor.touched`, which refuses to move backwards. LRU ordering
  is therefore a pure function of the deterministic admission sequence.
- **Caps are hard, flagged, and conservative.** `MAX_EXPORTS_PER_MODULE` /
  `MAX_IMPORTS_PER_MODULE` (256) bound descriptor size (Invariant 5.1);
  truncation sets an explicit `*_truncated` flag so downstream resolution
  can treat the module conservatively rather than mis-resolve silently
  (Clean Rejection).
- **Separation of concerns:** the bounded descriptor and the heavy parsed
  `ProgramGraph` live in separate maps; `last_accessed` is excluded from the
  structural hash (window state, not artifact identity).

### Not in this slice (subsequent Stage 1 work)
The bounded Active Window W_t and its LRU eviction; multi-hop / transitive
import resolution (re-export chains, hop_count ≤ 3); the Cross-module
Consistency penalty C and the `(H, S, C, Q)` energy tuple; any change to
acceptance behaviour. This slice provides the representation those consume.

### Verification
94/94 unit tests pass (60 prior + 34 new). The module-layer
`structural_hash` is identical across three `PYTHONHASHSEED` values
(determinism holds under hash randomisation). Full RVP: all 5 hypotheses
pass, numerically unchanged from 0.6.0 (the new files are not on the
harness's path). Reproduce with `python -m unittest discover -s tests` and
`python validation/rvp_harness.py`.

---

## [0.6.0] — 2026-06-27 — Dangling-reference safety in the rewrite engine's own removal logic

### Fixed
**Severe**: two independent paths in the rewrite engine could delete a
binding without checking whether anything else in the file still
referenced it, producing code that parses (passes `MUST_COMPILE`) but
raises at runtime -- and the system was **accepting** that as valid
output. This is exactly the "fabricate broken output instead of
rejecting" failure mode the architecture exists to prevent, found via
adversarial testing of the 0.3.0 dangling-cleanup machinery, this time
in code built earlier in this same review rather than the original
baseline:
- `_rewrite_source_remove_dangling_fabric_statements` would delete e.g.
  `self.fabric_thing = net.fabricmc.fabric.SomeHelper.create()` even
  though `self.fabric_thing` was read later in the same class
  (`getCapability`'s body), leaving an `AttributeError` waiting to happen.
- `_rewrite_source_remove_forbidden_apis` would delete e.g.
  `from net.fabricmc import FabricHelperAlias` even though
  `FabricHelperAlias.get()` was called elsewhere in the file, leaving a
  `NameError` waiting to happen.

Fixed by `_names_bound_by_statement` / `_is_binding_referenced_elsewhere`
(new, shared): before removing an import or a statement, check whether
its binding is referenced anywhere else in the relevant scope (the whole
file for imports, the class for statements). If so, refuse to remove it
-- the corresponding constraint stays violated and the system correctly
rejects with a diagnostic, rather than silently producing broken output.
Deterministic, conservative, no attempt to also rewrite the downstream
usage site (that would require understanding intent, which Stage 0
deliberately doesn't attempt).

**Related, independently discovered while debugging the above**:
`MUST_NOT_USE_FABRIC_APIS` (`forbidden_api_set` check) used exact
set-intersection against `api_inventory()`'s fully-qualified import
targets (e.g. `"net.fabricmc.FabricHelperAlias"`), which never exactly
equals a `FABRIC_APIS` entry like `"net.fabricmc"` even though it's
obviously the same forbidden module. This let `from net.fabricmc import
X` evade the constraint entirely -- previously masked because the
rewrite engine's own broader substring-based removal cleaned such
imports up as a side effect, until the new dangling-reference guard
started legitimately blocking some of those removals and exposed the
gap underneath. Switched to substring matching, consistent with every
other forbidden-API check in the file.

### Added
- `TestDanglingReferenceSafety` (5 tests): both confirmed bugs, two
  sanity checks that legitimately-safe removals are still performed (the
  guard isn't overly conservative), and one test that the substring-match
  fix catches a `from`-import variant directly.

### Verification
60/60 unit tests pass. Full RVP: all 5 hypotheses pass, numerically
unchanged from 0.5.0 (no existing benchmark case exercises this failure
mode -- entirely new adversarial test cases were needed to find it).

---

## [0.5.0] — 2026-06-27 — Lexicographic energy ordering; documentation infrastructure

### Changed
- `EnergyBreakdown` gained `.lexicographic_key` (`tuple[float, float, float]`
  = `(hard+syntax, strong, quality)`), a tuple-comparison-based priority
  ordering. **This is now the actual decision driver** for the
  computational loop's per-step accept/revert check
  (`TSAMComputationalLoop.run`), replacing the weighted scalar comparison.
  The weighted scalar (`hard=100, strong=10, soft=1`) is **kept unchanged**
  for human-readable reporting and JSON serialization (`.total`), but it
  was only ever *incidentally* dominance-preserving — correct given the
  current bounded constraint counts (max 9 HARD, 2 STRONG), not
  structurally guaranteed. This change makes the dominance structural,
  per Formal Spec Definition 5's own stated direction ("future extensions
  are expected to treat energy as partially ordered").
- `validation/rvp_harness.py`'s H3 (Convergence) now verifies monotonicity
  on the lexicographic key (the real contract) and separately reports
  whether the legacy scalar also stayed monotone (expected to agree in
  practice; a divergence is recorded as a notable event, not a failure).

### Rationale
Not a fix for an observed defect — no divergence between scalar and
lexicographic ordering was found to be reachable within the current
constraint set and benchmark sizes (confirmed by hand-analysis: maximum
plausible single-step quality delta is far below the strong/hard weight
margins at today's scale). This is deliberate hardening: the constraint
set has grown twice already this session (0.3.0, 0.4.0) and is expected
to keep growing in Stage 1; relying on incidental weight separation rather
than a structural guarantee was exactly the kind of unexamined assumption
this review has been working to eliminate elsewhere.

### Added
- Unit tests constructing `EnergyBreakdown` values directly to prove the
  lexicographic key orders correctly in a case engineered so the weighted
  scalar would get it wrong (`TestLexicographicEnergyOrdering`).
- `CHANGELOG.md` (this file), retroactively documenting 0.1.0 through 0.5.0.
- Versioning convention established: `pyproject.toml` / `src/tsam/__init__.py
  __version__` as the single source of truth for "current version."

### Verification
55/55 unit tests pass. Full RVP: all 5 hypotheses pass; H3 reports
150/150 runs lexicographically monotone, 150/150 also scalar-monotone
(zero divergence at current scale, as predicted).

---

## [0.4.0] — 2026-06-27 — Stage 1.1: structural capability-key detection

### Changed
- Replaced the naming-convention-only capability-key heuristic
  (`_extract_capability_key_candidates`: "all-caps identifier containing
  CAP") with **structural role detection**
  (`_extract_structural_capability_keys` / `class_capability_keys`): the
  value compared against a parameter literally named `cap`, within any
  method that has one — with single-hop, same-method def-use resolution
  (`key = MY_CAP; if cap == key` resolves to `MY_CAP`). The naming
  heuristic is retained only as a fallback for classes with no
  `cap`-parameter method.
- `ProgramNode` gained `structural_capability_keys` (computed once at
  parse time, alongside `body_api_refs` / `capability_evidence`).
- `unique_capability_keys` (constraint check) and `_capability_key_for_class`
  (stub-synthesis key choice) now both call the same canonical detector —
  using two different ones in checking vs. synthesis is exactly how this
  class of bug reopens under a different name.

### Fixed
Two bugs found by deliberately adversarial testing of the 0.3.0 naming
heuristic:
- **False positive**: two classes with genuinely distinct real keys
  (`MY_CAP_A`, `MY_CAP_B`) that also each happened to reference an
  unrelated all-caps constant containing "CAP" (e.g. `MAX_CAPACITY`) were
  wrongly rejected as a key collision.
- **False negative**: two classes genuinely sharing one key, spelled in a
  style the naming regex didn't match (e.g. `kSharedCapId`, lowercase
  leading character), were silently accepted despite a real registration
  collision.

### Added
- `TestStructuralCapabilityKeyDetection` (3 tests): the false-positive
  case, the false-negative case, and single-hop alias resolution.

### Verification
53/53 unit tests pass. Full RVP unchanged numerically (no existing
benchmark case exercises a key collision); the fix is covered by the new
dedicated unit tests instead.

---

## [0.3.0] — 2026-06-27 — Gap closure: constraint-complexity scaling, dead-code removal

### Added
- `build_neoforge_constraint_graph(n_classes)` is now parameterized.
  `MUST_NOT_LEAVE_DANGLING_FABRIC_REFS` (HARD, always active) catches a
  capability-provider class that *also* retains a forbidden Fabric API
  call in its body (e.g. incidentally still registering a Fabric event
  hook) — complements `MUST_MATCH_KNOWN_PATTERN`, which only covers
  classes with no capability evidence at all.
  `MUST_HAVE_UNIQUE_CAPABILITY_KEYS` (HARD) activates only when
  `n_classes >= 2` — a cross-class property that is vacuous, not just
  trivially true, at N=1, so it is omitted rather than included-and-
  always-passing. `build_neoforge_constraint_graph_for_source(source)`
  derives `n_classes` automatically; the RVP harness and the regression
  tests were switched to use it instead of the no-argument form.
- New rewrite task `CLEAN_DANGLING_FABRIC_REFS`
  (`_rewrite_source_remove_dangling_fabric_statements`): removes
  individual leftover-Fabric-API statements from capability-provider
  class bodies, giving the previously-dead `ADAPT_CAPABILITY_BODY`
  pathway a genuinely exercised sibling.
- H1/H4 (RVP) now compute deep executive-state size via
  `prove_constant_memory()` (dedicated synthetic iteration), since no
  actual benchmark case runs long enough to fill the verification ring
  buffer — the prior "compare across warmed-up runs" approach was
  vacuously true (0 warmed-up runs to compare), the same class of
  unfalsifiable-by-construction problem found and fixed in 0.2.0,
  re-introduced by this session's own earlier patch and caught by the
  same scrutiny.
- `RVP_specification.md`'s complexity table and `test_generators.py`'s
  `COMPLEXITY_PROFILES.n_hard_constraints` updated to the real, measured
  counts (8 at L1, 9 at L2–L5) rather than the original 6/6/8/10/12
  placeholder figures, which were illustrative rather than derived from
  actual constraints.

### Fixed
- **Stub-template capability-key collision** (found via adversarial
  testing of the new `MUST_HAVE_UNIQUE_CAPABILITY_KEYS` constraint):
  `NEOFORGE_GET_CAPABILITY_TEMPLATE` / `NEOFORGE_REGISTER_CAPABILITY_TEMPLATE`
  hardcoded a single literal `MY_CAPABILITY` placeholder. When a stub had
  to be synthesized fresh for more than one class in the same file, every
  one got the *same* placeholder — a genuine collision. Fixed by
  `_capability_key_for_class`: reuse the class's own existing key (or
  derive one from the class name) rather than a shared literal.
- `_rewrite_source_adapt_capability_body`'s dead no-op replacement pair
  (`"LazyOptional.of(lambda:" → "LazyOptional.of(lambda:"`, a literal
  identity transform) removed; documented as a narrow, rarely-triggered
  heuristic pending deeper structural/dataflow analysis (Stage 1 scope).

### Verification
44/44 then 50/50 unit tests pass across this entry's sub-changes. Full
RVP: all 5 hypotheses pass, including the previously-buggy uniqueness
constraint now correctly distinguishing real collisions from coincidental
template collisions.

---

## [0.2.0] — 2026-06-27 — Phase A/B/D: multi-class correctness, energy gating, capability-provider intent

This is the milestone described by `STAGE0_RVP_SUMMARY.md` and
`STAGE0_PR_READY.md` (point-in-time summaries, left as historical
snapshots — see the note at the end of this file).

### Fixed — Phase A (multi-class rewrite engine)
Three compounding bugs in `_rewrite_source_add_method` /
`_rewrite_source_register_capability`:
1. Walked the AST, found the *first* `ClassDef`, inserted into it, and
   returned immediately — every other class in a multi-class file was
   left untouched.
2. A single global `if method_name in source` guard meant that once *any*
   class anywhere had the method, every other class was silently skipped
   too.
3. Insertion at `end_lineno - 1` (before the class's last line) spliced
   the new method into the middle of whatever statement currently sat on
   that line, silently reassigning that statement to the new method —
   this truncated `getCapability`'s trailing `return` even in the
   single-class case.

Root cause behind why these went undetected: `check_constraint`'s
`required_method` / `structural_behavior_preservation` checks asked "does
this name exist *anywhere in the graph*", not per-class. Fixed by adding
`parent_class_id` to `ProgramNode` (computed in `_populate_from_ast`,
previously a literal no-op loop) and making both checks per-class.

### Fixed — Phase B (energy/acceptance gating)
`EnergyBreakdown` gained `.gating` (hard+strong+syntax) /
`.quality` (soft, e.g. node-delta) properties. `Accept()` now gates only
on `.gating`, not the full weighted total — previously the `node_delta`
soft-optimization term alone could push total energy over the acceptance
threshold τ and reject an otherwise fully-compliant multi-class output,
purely because larger files produce a larger absolute node-count delta.

### Added — Phase D (capability-provider intent)
- `class_shows_capability_provider_evidence`: a class is only treated as a
  capability provider if it already has one of the three capability
  methods, or references `LazyOptional` / `ICapabilityProvider` /
  `BlockCapabilityRegistrar` in its body. Used to scope the stub-injection
  rewrite rules and the capability-method constraints to classes that are
  plausibly capability providers.
- `MUST_MATCH_KNOWN_PATTERN` (HARD): a class using forbidden Fabric APIs
  in its body with **no** capability evidence is rejected outright,
  rather than having an unrelated capability-method stub bolted onto it.

### Fixed — the regression Phase A's fix exposed
Fixing the multi-class bug (correctly) made the rewrite engine bolt
capability-method stubs onto *every* class missing them — including
classes with no capability semantics at all (e.g. a pure Fabric
event-bus handler), which then got wrongly **accepted** with cosmetic,
semantically-empty stubs while their real logic kept calling
now-undefined names. `MUST_MATCH_KNOWN_PATTERN` is the fix.

### Verification
39 then 44 unit tests pass across this entry's sub-changes. RVP: H1–H5 all
pass; critically, unsolvable cases correctly reject again (0/5 unsolvable
acceptance) for the *right* reason (no capability-provider pattern) rather
than the wrong one (budget exhaustion from an unrelated bug).

---

## [0.1.0] — initial baseline (pre-review)

The original Stage 0 reference implementation as established before this
review process began: `CognitiveState`, `ProgramGraph`, `ConstraintGraph`,
`TaskPlanner`, `TSAMComputationalLoop`, single fixed 6 HARD / 2 STRONG /
2 SOFT constraint graph, `rvp_harness.py` benchmark across L1–L5.

Known issues present at this version (see 0.2.0–0.5.0 above for fixes):
multi-class rewrite bugs, soft-energy-blocks-acceptance, no
capability-provider intent scoping, no constraint-complexity scaling,
naming-only capability-key detection, incidental (not structural) energy
dominance ordering.

---

## A note on the two pre-existing summary documents

`STAGE0_RVP_SUMMARY.md` and `STAGE0_PR_READY.md` (repository root) are
point-in-time snapshots written at the 0.2.0 milestone — both explicitly
list "constraint complexity was not varied" as a current limitation,
which 0.3.0 partially resolved. They are left unedited as historical
record rather than updated in place, consistent with the versioning
convention this file establishes: snapshots are immutable once written;
the current state is always described by this CHANGELOG plus
`rvp_results.json` plus the live test suite, not by editing old summaries.
A current-state summary, if wanted for sharing (PR description, paper
appendix, etc.), should be written fresh against this CHANGELOG and dated
accordingly rather than layered into the existing files.
