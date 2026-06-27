"""
TSAM Research Validation Phase — Measurement Harness
=====================================================
Implements the measurement protocol for all five hypotheses
defined in RVP_specification.md.

H1: Executive state scalability (sizeof(S_t) constant)
H2: Determinism (same input → same hash, 10 independent runs)
H3: Convergence (energy monotonically non-increasing on accepted steps)
H4: Resource profile (executive memory bounded as graph grows)
H5: Software synthesis (constraint satisfaction rates across L1–L5)

Usage:
    python validation/rvp_harness.py
    (or: python -m validation.rvp_harness, from the project root)

Output:
    - Console report (human-readable)
    - rvp_results.json (machine-readable, for research paper)

KNOWN LIMITATION (documented per review, not yet fixed):
    H5 is specified (RVP_specification.md) as testing complexity along
    two axes: structural complexity (classes/methods) AND constraint
    complexity (number/priority of active constraints). This harness
    only varies structural complexity — build_neoforge_constraint_graph()
    returns the same fixed 6 HARD / 2 STRONG / 2 SOFT constraint graph at
    every level, regardless of the n_hard_constraints/n_strong_constraints
    fields declared in each ComplexityProfile. The constraint-complexity
    axis is therefore untested. See `constraint_complexity_note` in the
    output report.
"""

from __future__ import annotations

import gc
import hashlib
import json
import math
import sys
import time
import tracemalloc
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

# Add project root AND src/ to path so this resolves whether run directly
# (`python validation/rvp_harness.py`) or as a module (`python -m validation.rvp_harness`).
# The previous version only added src/, which made `tsam` importable but left
# `from validation.test_generators import ...` unresolved under direct execution.
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).parent.parent))

from tsam.cognitive_state import (
    CognitiveState,
    Mission,
    VerificationRecord,
    VerificationOutcome,
    MAX_CONTEXT_ITEMS,
    MAX_RECENT_VERIFICATIONS,
    prove_constant_memory,
)
from tsam.constraint_graph import (
    ProgramGraph,
    build_neoforge_constraint_graph,
)
from tsam.rewrite_engine import (
    TSAMComputationalLoop,
    compute_energy,
)
from validation.test_generators import (
    ComplexityLevel,
    ComplexityProfile,
    COMPLEXITY_PROFILES,
    TestCase,
    generate_all_levels,
    generate_level,
    generate_solvable,
    generate_unsolvable,
)


# ---------------------------------------------------------------------------
# Measurement primitives
# ---------------------------------------------------------------------------

def _executive_state_bytes(state: CognitiveState) -> int:
    """
    Shallow size of the executive state object — sys.getsizeof's report of
    the CognitiveState wrapper's own slots. Kept for backward comparison
    with earlier RVP reports, but see _deep_executive_state_bytes: this
    number is constant by construction for any frozen/slots dataclass with
    this field shape, so on its own it cannot detect a regression in the
    bounded substructures (ring buffer, context) — it would report the
    same value even if those grew without bound.
    """
    return sys.getsizeof(state)


def _deep_executive_state_bytes(state: CognitiveState) -> int:
    """
    Recursively measure the executive state's actual referenced footprint
    (Mission, BoundedContext + its entries, VerificationSummary + its
    ring-buffer records, ExecutionBudget, ResourceSnapshot), not just the
    outer wrapper. This is the measurement actually capable of catching a
    real regression in the constant-memory guarantee — sys.getsizeof alone
    cannot, since it never looks past the CognitiveState object's own slots.

    Caller should compare this only across runs whose ring buffer and
    context are both at their cap (see RunResult.ring_buffer_full /
    .context_full) — before that point, the deep size is legitimately still
    warming up, exactly as already documented in
    cognitive_state.prove_constant_memory()'s warmup-cutoff handling.
    """
    seen: set[int] = set()

    def _size(obj: object) -> int:
        if id(obj) in seen:
            return 0
        seen.add(id(obj))
        total = sys.getsizeof(obj)
        if isinstance(obj, tuple):
            for item in obj:
                total += _size(item)
        elif hasattr(obj, "__slots__"):
            for slot_name in obj.__slots__:
                if hasattr(obj, slot_name):
                    total += _size(getattr(obj, slot_name))
        return total

    return _size(state)


def _output_hash(source: str) -> str:
    return hashlib.md5(source.encode("utf-8")).hexdigest()


def _diagnostic_quality_score(diagnostic: dict | None) -> float:
    if diagnostic is None:
        return 0.0
    required = {
        "tsam_diagnostic", "reason", "step",
        "last_energy", "last_distance", "constraint_violations",
    }
    present = sum(1 for f in required if f in diagnostic)
    return present / len(required)


def _energy_is_monotone(energy_trajectory: list[float]) -> tuple[bool, int]:
    for i in range(1, len(energy_trajectory)):
        if energy_trajectory[i] > energy_trajectory[i - 1] + 1e-9:
            return False, i
    return True, -1


# ---------------------------------------------------------------------------
# Per-run result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    case_id:             str
    level:               ComplexityLevel
    solvable:            bool
    run_index:           int

    accepted:            bool
    output_hash:         str
    energy_trajectory:   list[float]
    steps_taken:         int
    hard_fails_final:    int
    strong_fails_final:  int

    # H1 / H4 — shallow (legacy) measurement
    executive_state_bytes_initial:  int
    executive_state_bytes_final:    int
    # H1 / H4 — deep measurement (actually capable of failing)
    executive_state_deep_bytes_initial: int
    executive_state_deep_bytes_final:   int
    ring_buffer_full:    bool   # True once verification ring buffer is at cap
    context_full:        bool   # True once bounded context is at cap
    peak_tracemalloc_bytes:         int

    energy_monotone:     bool
    monotone_violation:  int

    diagnostic:          dict | None
    diagnostic_quality:  float

    elapsed_ms:          float


@dataclass
class HypothesisResult:
    hypothesis:     str
    passed:         bool
    verdict:        str
    evidence:       dict
    violations:     list[dict]


@dataclass
class RVPReport:
    h1:      HypothesisResult
    h2:      HypothesisResult
    h3:      HypothesisResult
    h4:      HypothesisResult
    h5:      HypothesisResult
    overall: bool
    run_results: list[RunResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        def hyp_dict(h: HypothesisResult) -> dict:
            return {
                "hypothesis": h.hypothesis,
                "passed":     h.passed,
                "verdict":    h.verdict,
                "evidence":   h.evidence,
                "violations": h.violations,
            }
        return {
            "tsam_rvp_report": True,
            "overall_passed":  self.overall,
            "hypotheses": {
                "H1_scalability":  hyp_dict(self.h1),
                "H2_determinism":  hyp_dict(self.h2),
                "H3_convergence":  hyp_dict(self.h3),
                "H4_resource":     hyp_dict(self.h4),
                "H5_synthesis":    hyp_dict(self.h5),
            },
            "run_count": len(self.run_results),
            "summary": {
                "total_test_cases": len({r.case_id for r in self.run_results}),
                "total_runs":       len(self.run_results),
            },
            "constraint_complexity_note": (
                "build_neoforge_constraint_graph() returns the same fixed "
                "6 HARD / 2 STRONG / 2 SOFT constraint graph at every complexity "
                "level. The n_hard_constraints / n_strong_constraints / "
                "n_soft_constraints fields on ComplexityProfile (6/6/8/10/12 "
                "hard across L1-L5 per RVP_specification.md) are declared but "
                "not wired into the harness. H5 therefore currently validates "
                "only the structural-complexity axis (classes/methods), not "
                "the constraint-complexity axis. Treat H5's level-scaling claim "
                "as scoped to structural complexity only until this is implemented."
            ),
        }


# ---------------------------------------------------------------------------
# Core runner
# ---------------------------------------------------------------------------

def _run_one(
    case:      TestCase,
    run_index: int,
    budget:    int = 80,
) -> RunResult:
    cg      = build_neoforge_constraint_graph()
    mission = Mission.neoforge_port()

    tracemalloc.start()
    t_start = time.perf_counter()

    state = CognitiveState.initialize(mission, max_rewrites=budget)
    initial_state_bytes      = _executive_state_bytes(state)
    initial_state_deep_bytes = _deep_executive_state_bytes(state)

    loop  = TSAMComputationalLoop()
    final_graph, final_state, trace = loop.run(
        case.source, state, cg, verbose=False
    )

    t_end = time.perf_counter()
    _, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    final_state_bytes      = _executive_state_bytes(final_state)
    final_state_deep_bytes = _deep_executive_state_bytes(final_state)

    energy_traj = [
        step["verification"]["energy"]
        for step in trace.steps
    ]

    from tsam.constraint_graph import check_all_constraints
    from tsam.cognitive_state import ConstraintPriority
    final_results = check_all_constraints(final_graph, cg)
    hard_fails    = sum(
        1 for r in final_results
        if r.violated and r.priority == ConstraintPriority.HARD
    )
    strong_fails  = sum(
        1 for r in final_results
        if r.violated and r.priority == ConstraintPriority.STRONG
    )

    monotone, viol_idx = _energy_is_monotone(energy_traj)
    diag               = trace.diagnostic
    diag_quality       = _diagnostic_quality_score(diag)

    return RunResult(
        case_id                        = case.case_id,
        level                          = case.level,
        solvable                       = case.solvable,
        run_index                      = run_index,
        accepted                       = trace.accepted,
        output_hash                    = _output_hash(final_graph.source),
        energy_trajectory              = energy_traj,
        steps_taken                    = len(trace.steps),
        hard_fails_final               = hard_fails,
        strong_fails_final             = strong_fails,
        executive_state_bytes_initial  = initial_state_bytes,
        executive_state_bytes_final    = final_state_bytes,
        executive_state_deep_bytes_initial = initial_state_deep_bytes,
        executive_state_deep_bytes_final   = final_state_deep_bytes,
        ring_buffer_full                = len(final_state.verification.records) == MAX_RECENT_VERIFICATIONS,
        context_full                    = len(final_state.context.entries) == MAX_CONTEXT_ITEMS,
        peak_tracemalloc_bytes         = peak_bytes,
        energy_monotone                = monotone,
        monotone_violation             = viol_idx,
        diagnostic                     = diag,
        diagnostic_quality             = diag_quality,
        elapsed_ms                     = (t_end - t_start) * 1000,
    )


# ---------------------------------------------------------------------------
# Hypothesis evaluators
# ---------------------------------------------------------------------------

def _eval_h1(all_runs: list[RunResult]) -> HypothesisResult:
    """
    H1: sizeof(S_t) constant regardless of graph size or step count.

    An earlier revision of this check derived "warmed up" status from the
    benchmark's own per-case runs (ring buffer + context both at cap). That
    doesn't work: every case in this benchmark terminates in well under
    MAX_RECENT_VERIFICATIONS (8) steps -- solvable cases in ~6, rejected
    ones in ~4 -- so the ring buffer and context never actually reach
    their caps during a real RVP run. A criterion that can never be
    exercised passes vacuously ("0 warmed-up runs" -> trivially flat),
    which is exactly the kind of unfalsifiable PASS this whole review has
    been trying to eliminate -- now found in this harness's own patch
    rather than in the original code.

    Fix: call prove_constant_memory() directly (already implemented and
    unit-tested in cognitive_state.py), which deliberately drives the
    state past its warmup point via synthetic iterations and measures
    real (tracemalloc) memory growth after that point. That is the actual
    falsifiable test. Per-case deep/shallow sizes are still recorded as
    evidence for transparency, but no longer decide the verdict.
    """
    proof = prove_constant_memory(iterations=200)

    shallow_sizes = sorted(set(
        [r.executive_state_bytes_initial for r in all_runs] +
        [r.executive_state_bytes_final   for r in all_runs]
    ))
    deep_sizes = sorted(set(
        [r.executive_state_deep_bytes_initial for r in all_runs] +
        [r.executive_state_deep_bytes_final   for r in all_runs]
    ))
    max_steps_seen = max((r.steps_taken for r in all_runs), default=0)

    return HypothesisResult(
        hypothesis = "H1: Executive State Scalability",
        passed     = proof["passed"],
        verdict    = (
            f"{proof['verdict']} [via prove_constant_memory(), 200 synthetic iterations -- "
            f"no RVP benchmark case reached the {MAX_RECENT_VERIFICATIONS}-step ring-buffer cap "
            f"on its own (max steps seen across all cases: {max_steps_seen}), so the verdict is "
            f"based on the dedicated synthetic proof, not the benchmark cases themselves]"
        ),
        evidence   = {
            "constant_memory_proof":      proof,
            "max_benchmark_steps_seen":   max_steps_seen,
            "ring_buffer_cap":            MAX_RECENT_VERIFICATIONS,
            "shallow_sizes_for_reference": shallow_sizes,
            "deep_sizes_for_reference":    deep_sizes,
        },
        violations = [] if proof["passed"] else [
            {"type": "constant_memory_proof_failed", "proof": proof}
        ],
    )


def _eval_h2(all_runs: list[RunResult], n_runs_per_case: int) -> HypothesisResult:
    from collections import defaultdict
    hashes_by_case: dict[str, list[str]] = defaultdict(list)
    for r in all_runs:
        hashes_by_case[r.case_id].append(r.output_hash)

    violations = []
    for case_id, hashes in hashes_by_case.items():
        if len(set(hashes)) > 1:
            violations.append({
                "case_id":       case_id,
                "unique_hashes": sorted(set(hashes)),
                "run_count":     len(hashes),
            })

    passed  = len(violations) == 0
    n_cases = len(hashes_by_case)

    return HypothesisResult(
        hypothesis = "H2: Determinism",
        passed     = passed,
        verdict    = (
            f"PASS: {n_cases} cases × {n_runs_per_case} runs all produced identical hashes"
            if passed else
            f"FAIL: {len(violations)}/{n_cases} cases produced non-identical hashes"
        ),
        evidence   = {
            "cases_tested":   n_cases,
            "runs_per_case":  n_runs_per_case,
            "non_det_cases":  len(violations),
        },
        violations = violations,
    )


def _eval_h3(all_runs: list[RunResult]) -> HypothesisResult:
    monotone_violations = [
        {
            "case_id":           r.case_id,
            "level":             r.level.name,
            "violation_at_step": r.monotone_violation,
            "trajectory":        r.energy_trajectory,
        }
        for r in all_runs
        if not r.energy_monotone and len(r.energy_trajectory) > 1
    ]

    diag_violations = [
        {
            "case_id":       r.case_id,
            "level":         r.level.name,
            "accepted":      r.accepted,
            "has_diagnostic": r.diagnostic is not None,
            "diag_quality":   r.diagnostic_quality,
        }
        for r in all_runs
        if not r.accepted and r.diagnostic_quality < 0.8
    ]

    all_violations = monotone_violations + diag_violations
    passed = len(all_violations) == 0

    n_checked  = len(all_runs)
    n_monotone = sum(1 for r in all_runs if r.energy_monotone or len(r.energy_trajectory) <= 1)
    n_diag_ok  = sum(1 for r in all_runs if r.accepted or r.diagnostic_quality >= 0.8)

    return HypothesisResult(
        hypothesis = "H3: Convergence",
        passed     = passed,
        verdict    = (
            f"PASS: {n_monotone}/{n_checked} runs monotone; {n_diag_ok}/{n_checked} have clean diagnostics"
            if passed else
            f"FAIL: {len(monotone_violations)} monotonicity violations, "
            f"{len(diag_violations)} diagnostic quality failures"
        ),
        evidence   = {
            "runs_checked":       n_checked,
            "monotone_runs":      n_monotone,
            "clean_diag_runs":    n_diag_ok,
            "monotone_violations": len(monotone_violations),
            "diag_violations":    len(diag_violations),
        },
        violations = all_violations[:20],
    )


def _eval_h4(all_runs: list[RunResult]) -> HypothesisResult:
    from collections import defaultdict
    peaks_by_level: dict[str, list[int]] = defaultdict(list)
    for r in all_runs:
        peaks_by_level[r.level.name].append(r.peak_tracemalloc_bytes)

    avg_peaks = {
        level: sum(peaks) / len(peaks)
        for level, peaks in peaks_by_level.items()
    }

    # Executive state flatness: H4's actual claim is "memory doesn't grow
    # with graph size", which is testable here by comparing the deep
    # executive size across complexity LEVELS at fixed solvability (every
    # solvable case takes the same number of steps regardless of level --
    # 6 here -- and every rejected case takes the same number too -- 4 --
    # so this is a fair apples-to-apples comparison). The earlier
    # ring-buffer-full filter (shared with H1) is never satisfied by this
    # benchmark's short runs and would make this comparison vacuous too;
    # H1 now covers the "many steps" axis on its own via
    # prove_constant_memory(), so H4 only needs to cover the "graph size" axis.
    from collections import defaultdict as _dd
    deep_by_solv_level: dict[tuple[bool, str], list[int]] = _dd(list)
    for r in all_runs:
        if r.run_index == 0:
            deep_by_solv_level[(r.solvable, r.level.name)].append(r.executive_state_deep_bytes_final)

    per_solvability_deltas: dict[str, int] = {}
    for solvable_flag, label in ((True, "solvable"), (False, "unsolvable")):
        sizes = [vals[0] for (s, _lvl), vals in deep_by_solv_level.items() if s == solvable_flag and vals]
        if sizes:
            per_solvability_deltas[label] = max(sizes) - min(sizes)
    exec_delta = max(per_solvability_deltas.values()) if per_solvability_deltas else 0
    exec_flat  = exec_delta == 0

    level_order = ["L1", "L2", "L3", "L4", "L5"]
    present_levels = [l for l in level_order if l in avg_peaks]
    if len(present_levels) >= 2:
        first_peak = avg_peaks[present_levels[0]]
        last_peak  = avg_peaks[present_levels[-1]]
        growth_ratio = last_peak / first_peak if first_peak > 0 else float("inf")
        memory_bounded = growth_ratio <= 30.0
    else:
        growth_ratio   = 1.0
        memory_bounded = True

    passed     = exec_flat and memory_bounded
    violations = []
    if not exec_flat:
        violations.append({
            "type": "executive_not_flat",
            "delta_bytes_by_solvability": per_solvability_deltas,
        })
    if not memory_bounded:
        violations.append({
            "type": "memory_growth_excessive",
            "growth_ratio": round(growth_ratio, 2),
            "limit": 30.0,
        })

    return HypothesisResult(
        hypothesis = "H4: Resource Profile",
        passed     = passed,
        verdict    = (
            f"PASS: Executive state flat across L1-L5 at fixed solvability "
            f"({per_solvability_deltas}); total memory growth ratio = {growth_ratio:.1f}×"
            if passed else
            f"FAIL: exec_delta_by_solvability={per_solvability_deltas}, growth_ratio={growth_ratio:.1f}×"
        ),
        evidence   = {
            "executive_delta_bytes_by_solvability": per_solvability_deltas,
            "executive_flat":        exec_flat,
            "avg_peak_kb_by_level":  {
                k: round(v / 1024, 1) for k, v in avg_peaks.items()
            },
            "growth_ratio_L1_to_L5": round(growth_ratio, 2),
            "growth_limit":          30.0,
        },
        violations = violations,
    )


def _eval_h5(all_runs: list[RunResult]) -> HypothesisResult:
    from collections import defaultdict

    solvable_by_level: dict[str, list[RunResult]] = defaultdict(list)
    for r in all_runs:
        if r.solvable and r.run_index == 0:
            solvable_by_level[r.level.name].append(r)

    sat_rate_by_level: dict[str, float] = {}
    violations = []

    for level_name, runs in solvable_by_level.items():
        n_hard_ok = sum(1 for r in runs if r.hard_fails_final == 0)
        rate = n_hard_ok / len(runs) if runs else 0.0
        sat_rate_by_level[level_name] = rate
        if rate < 0.95:
            violations.append({
                "type":    "hard_satisfaction_below_threshold",
                "level":   level_name,
                "rate":    round(rate, 4),
                "threshold": 0.95,
                "n_cases": len(runs),
                "n_ok":    n_hard_ok,
            })

    # Acceptance rate is now ALSO checked explicitly — under the old harness
    # this was reported but not a falsification condition, which is exactly
    # how a 0/5 acceptance rate at L3-L5 coexisted with a PASS verdict.
    for level_name, runs in solvable_by_level.items():
        n_acc = sum(1 for r in runs if r.accepted)
        rate = n_acc / len(runs) if runs else 0.0
        if rate < 0.95:
            violations.append({
                "type":       "acceptance_rate_below_threshold",
                "level":      level_name,
                "rate":       round(rate, 4),
                "threshold":  0.95,
                "n_cases":    len(runs),
                "n_accepted": n_acc,
            })

    rejected_runs = [r for r in all_runs if not r.accepted and r.run_index == 0]
    diag_failures = [
        r for r in rejected_runs
        if r.diagnostic_quality < 0.80
    ]
    if diag_failures:
        for r in diag_failures:
            violations.append({
                "type":          "diagnostic_quality_below_threshold",
                "case_id":       r.case_id,
                "level":         r.level.name,
                "diag_quality":  round(r.diagnostic_quality, 4),
                "threshold":     0.80,
            })

    passed = len(violations) == 0

    accept_by_level: dict[str, str] = {}
    for level_name, runs in solvable_by_level.items():
        n_acc = sum(1 for r in runs if r.accepted)
        accept_by_level[level_name] = f"{n_acc}/{len(runs)}"

    avg_diag_quality = (
        sum(r.diagnostic_quality for r in rejected_runs) / len(rejected_runs)
        if rejected_runs else 1.0
    )

    return HypothesisResult(
        hypothesis = "H5: Software Synthesis Complexity Scaling",
        passed     = passed,
        verdict    = (
            f"PASS: hard sat rates {sat_rate_by_level}, acceptance rates {accept_by_level}, "
            f"avg diag quality {avg_diag_quality:.2f}"
            if passed else
            f"FAIL: {len(violations)} violation(s) across levels"
        ),
        evidence   = {
            "hard_satisfaction_by_level": {
                k: round(v, 4) for k, v in sat_rate_by_level.items()
            },
            "acceptance_solvable_by_level": accept_by_level,
            "rejected_cases":             len(rejected_runs),
            "avg_diagnostic_quality":     round(avg_diag_quality, 4),
            "diag_quality_failures":      len(diag_failures),
        },
        violations = violations,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_rvp(
    n_cases_per_level: int = 5,
    n_determinism_runs: int = 3,
    budget:             int = 80,
    verbose:            bool = True,
) -> RVPReport:
    if verbose:
        print("=" * 72)
        print("TSAM — RESEARCH VALIDATION PHASE")
        print("=" * 72)
        print(f"Config: {n_cases_per_level} cases/level, "
              f"{n_determinism_runs} determinism runs, "
              f"budget={budget}")
        print()

    all_runs: list[RunResult] = []

    for level in ComplexityLevel:
        cases = generate_level(
            level,
            n_solvable   = n_cases_per_level,
            n_unsolvable = n_cases_per_level,
        )
        profile = COMPLEXITY_PROFILES[level]

        if verbose:
            print(f"── Level {level.name} ──────────────────────────────────────────────")
            print(f"   Profile: {profile.n_classes} class(es), "
                  f"{profile.n_methods_per_class} methods/class, "
                  f"{profile.n_hard_constraints}H/{profile.n_strong_constraints}S constraints "
                  f"[note: constraint count NOT actually varied by harness — see report]")

        for case in cases:
            for run_i in range(n_determinism_runs):
                result = _run_one(case, run_index=run_i, budget=budget)
                all_runs.append(result)

                if verbose and run_i == 0:
                    accept_sym = "✓" if result.accepted  else "✗"
                    solv_sym   = "S" if result.solvable  else "U"
                    mono_sym   = "↓" if result.energy_monotone else "!"
                    e_init = result.energy_trajectory[0]  if result.energy_trajectory else float("inf")
                    e_final = result.energy_trajectory[-1] if result.energy_trajectory else float("inf")
                    print(f"   [{solv_sym}] {result.case_id[-18:]}  "
                          f"{accept_sym} {'accept' if result.accepted else 'reject':6}  "
                          f"E {e_init:.0f}→{e_final:.0f}  "
                          f"steps={result.steps_taken:3}  "
                          f"mono={mono_sym}  "
                          f"deep_exec={result.executive_state_deep_bytes_final}B  "
                          f"{result.elapsed_ms:.1f}ms")
            gc.collect()

        if verbose:
            print()

    if verbose:
        print("── Evaluating Hypotheses ─────────────────────────────────────────────")

    h1 = _eval_h1(all_runs)
    h2 = _eval_h2(all_runs, n_determinism_runs)
    h3 = _eval_h3(all_runs)
    h4 = _eval_h4(all_runs)
    h5 = _eval_h5(all_runs)

    overall = h1.passed and h2.passed and h3.passed and h4.passed and h5.passed

    if verbose:
        for hyp in [h1, h2, h3, h4, h5]:
            status = "✓✓ PASS" if hyp.passed else "✗✗ FAIL"
            print(f"  [{status}] {hyp.hypothesis}")
            print(f"          {hyp.verdict}")
            if hyp.violations:
                print(f"          Violations: {len(hyp.violations)}")
                for v in hyp.violations[:3]:
                    print(f"            {v}")
            print()

        print("=" * 72)
        overall_label = "✓✓ ALL HYPOTHESES VALIDATED" if overall else "✗✗ VALIDATION FAILURES DETECTED"
        print(f"OVERALL: {overall_label}")
        print("=" * 72)

    report = RVPReport(
        h1          = h1,
        h2          = h2,
        h3          = h3,
        h4          = h4,
        h5          = h5,
        overall     = overall,
        run_results = all_runs,
    )
    return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    report = run_rvp(
        n_cases_per_level   = 5,
        n_determinism_runs  = 3,
        budget              = 80,
        verbose             = True,
    )

    out_path = Path(__file__).parent / "rvp_results.json"
    with open(out_path, "w") as f:
        json.dump(report.to_dict(), f, indent=2)

    print(f"\nMachine-readable report written to: {out_path}")
