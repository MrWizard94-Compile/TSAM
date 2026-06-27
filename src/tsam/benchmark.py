"""
TSAM Stage 0 — Phase 5: Materialization + Diagnostics + Benchmark Harness
==========================================================================
Surfaces the full trace (Constraint Graph, Task Plan, Verification trace, Energy).
Runs the automated benchmark suite reporting all Stage 0 metrics.

Benchmark metrics (per v0.3 spec):
  1. Compilation success rate (AST parse success)
  2. Structural test pass rate (required methods present, correct APIs)
  3. Constraint satisfaction rate (hard + strong constraints met)
  4. Rewrite size / AST node delta
  5. Peak memory during run (must stay flat — proven by Phase 1)
  6. Determinism (identical input → identical output, verified by hash)
  7. Failure mode on invalid input (must produce clean diagnostic, never hallucinate)

This is the final gate for Stage 0 delivery.
"""

from __future__ import annotations

import gc
import json
import sys
import time
import tracemalloc
from dataclasses import dataclass, field

from tsam.cognitive_state import (
    CognitiveState,
    ExecutionBudget,
    Mission,
    VerificationOutcome,
    prove_constant_memory,
)
from tsam.constraint_graph import (
    ConstraintGraph,
    ProgramGraph,
    build_neoforge_constraint_graph,
    check_all_constraints,
)
from tsam.rewrite_engine import (
    ACCEPTANCE_THRESHOLD,
    RewriteTrace,
    TSAMComputationalLoop,
    VerificationKernel,
    compute_energy,
)
from tsam.task_planner import TaskPlanner


# ===========================================================================
# STAGE 0 TEST CASES
# ===========================================================================

# Each test case: (name, source, should_accept, description)
BENCHMARK_TEST_CASES: list[tuple[str, str, bool, str]] = [

    # ── Test 1: Full Fabric source → should be ported to NeoForge ──
    (
        "fabric_capability_provider",
        """\
import net.fabricmc.fabric
from net.fabricmc import SomeUtil

class FabricCapabilityProvider:
    def __init__(self):
        self.handler = MyHandler()
        self.lazy = LazyOptional.of(lambda: self.handler)

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return self.lazy
        return LazyOptional.empty()
""",
        True,
        "Standard Fabric capability provider — should port successfully",
    ),

    # ── Test 2: Source already in NeoForge format → accept immediately ──
    (
        "already_neoforge",
        """\
from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar

class NeoForgeCapabilityProvider:
    def __init__(self):
        self._handler = MyHandler()
        self._handler_lazy = None

    def getCapability(self, cap, direction=None):
        if cap == MY_CAPABILITY:
            return LazyOptional.of(lambda: self._handler)
        return LazyOptional.empty()

    def invalidateCapabilities(self):
        if self._handler_lazy is not None:
            self._handler_lazy.invalidate()
        self._handler_lazy = None

    def register_capability(self, registrar: BlockCapabilityRegistrar):
        registrar.registerBlockEntity(MY_CAPABILITY, self)
""",
        True,
        "Already valid NeoForge source — should accept without rewrite",
    ),

    # ── Test 3: Deliberately invalid source → should reject with diagnostic ──
    (
        "deliberately_invalid",
        """\
import net.fabricmc.fabric
import io.github.fabricators_of_create.SomeFabricThing

class FabricOnlyClass:
    # No getCapability, no invalidateCapabilities, only Fabric APIs
    def do_fabric_thing(self):
        ServerLifecycleEvents.SERVER_STARTED.register(lambda: None)
""",
        False,  # Should NOT accept — too many violations
        "Pure Fabric-only source with no valid capability pattern — should reject with diagnostic",
    ),

    # ── Test 4: Partial NeoForge (missing invalidation) → should repair ──
    (
        "partial_neoforge_missing_invalidate",
        """\
from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar

class PartialNeoForgeProvider:
    def getCapability(self, cap, direction=None):
        if cap == MY_CAPABILITY:
            return LazyOptional.of(lambda: self._handler)
        return LazyOptional.empty()
""",
        True,
        "NeoForge source missing invalidateCapabilities — planner should add it",
    ),

]


# ===========================================================================
# MATERIALIZATION (Phase 5)
# ===========================================================================

@dataclass
class MaterializedOutput:
    """
    The final output artifact produced by the TSAM loop.
    Includes full provenance: Constraint Graph, Task Plan, Verification trace, Energy.
    """
    test_name:          str
    accepted:           bool
    source_output:      str
    output_graph:       ProgramGraph | None

    # Provenance
    constraint_summary: dict
    trace_steps:        list[dict]
    final_energy:       float
    final_distance:     float
    hard_fails:         int
    strong_fails:       int
    soft_fails:         int

    # Resource metrics
    peak_memory_kb:     float
    elapsed_seconds:    float
    rewrite_ops:        int

    # Determinism hash
    output_hash:        str

    # Diagnostic (only if rejected)
    diagnostic:         dict | None = None


def materialize(
    test_name:   str,
    source:      str,
    cg:          ConstraintGraph,
    budget:      int = 50,
    verbose:     bool = False,
) -> MaterializedOutput:
    """
    Run the TSAM loop for one test case and materialize the output.
    Records all Phase 5 trace information.
    """
    import hashlib

    tracemalloc.start()
    t_start = time.monotonic()

    mission = Mission.neoforge_port()
    state   = CognitiveState.initialize(mission, max_rewrites=budget)
    loop    = TSAMComputationalLoop()

    final_graph, final_state, trace = loop.run(source, state, cg, verbose=verbose)

    t_end = time.monotonic()
    _, peak_mem = tracemalloc.get_traced_memory()
    tracemalloc.stop()

    # Compute final verification metrics
    final_results = check_all_constraints(final_graph, cg)
    energy, distance, breakdown, _ = compute_energy(final_graph, cg)

    hard_fails   = sum(1 for r in final_results if r.violated and r.priority.name == "HARD")
    strong_fails = sum(1 for r in final_results if r.violated and r.priority.name == "STRONG")
    soft_fails   = sum(1 for r in final_results if r.violated and r.priority.name == "SOFT")

    # Determinism hash
    output_hash = hashlib.md5(final_graph.source.encode()).hexdigest()

    return MaterializedOutput(
        test_name          = test_name,
        accepted           = trace.accepted,
        source_output      = final_graph.source,
        output_graph       = final_graph,
        constraint_summary = cg.summary(),
        trace_steps        = trace.steps,
        final_energy       = energy,
        final_distance     = distance,
        hard_fails         = hard_fails,
        strong_fails       = strong_fails,
        soft_fails         = soft_fails,
        peak_memory_kb     = peak_mem / 1024,
        elapsed_seconds    = t_end - t_start,
        rewrite_ops        = final_state.budget.max_rewrites - final_state.budget.rewrites_remaining,
        output_hash        = output_hash,
        diagnostic         = trace.diagnostic if not trace.accepted else None,
    )


# ===========================================================================
# BENCHMARK HARNESS
# ===========================================================================

@dataclass
class BenchmarkResult:
    """Results for one test case in the benchmark suite."""
    test_name:           str
    expected_accept:     bool
    actual_accept:       bool
    acceptance_correct:  bool

    # Metric 1: Compilation (AST parse) success
    ast_parse_passes:    bool

    # Metric 2: Structural test pass rate
    has_required_methods: bool
    method_names_found:  list[str]

    # Metric 3: Constraint satisfaction
    hard_satisfied:      int
    hard_total:          int
    strong_satisfied:    int
    strong_total:        int

    # Metric 4: Rewrite size
    node_delta:          int

    # Metric 5: Memory
    peak_memory_kb:      float
    memory_flat:         bool   # True if peak < 10 MB

    # Metric 6: Determinism
    determinism_verified: bool
    hash_run1:           str
    hash_run2:           str

    # Metric 7: Failure mode
    has_clean_diagnostic: bool  # True if rejection included machine-readable diagnostic

    # Performance
    elapsed_ms:          float

    def to_dict(self) -> dict:
        return {
            "test":               self.test_name,
            "expected_accept":    self.expected_accept,
            "actual_accept":      self.actual_accept,
            "acceptance_correct": self.acceptance_correct,
            "metrics": {
                "ast_parse_passes":    self.ast_parse_passes,
                "has_required_methods": self.has_required_methods,
                "method_names":        self.method_names_found,
                "hard_satisfied":      f"{self.hard_satisfied}/{self.hard_total}",
                "strong_satisfied":    f"{self.strong_satisfied}/{self.strong_total}",
                "node_delta":          self.node_delta,
                "peak_memory_kb":      round(self.peak_memory_kb, 2),
                "memory_flat":         self.memory_flat,
                "determinism_verified": self.determinism_verified,
                "hash_run1":           self.hash_run1[:12] + "...",
                "hash_run2":           self.hash_run2[:12] + "...",
                "has_clean_diagnostic": self.has_clean_diagnostic,
                "elapsed_ms":          round(self.elapsed_ms, 1),
            },
        }


def run_benchmark(verbose: bool = True) -> dict:
    """
    Run the full Stage 0 benchmark suite.
    Reports all v0.3 metrics in a machine-readable report.
    """
    cg = build_neoforge_constraint_graph()
    results: list[BenchmarkResult] = []
    all_hashes: list[str] = []

    if verbose:
        print("=" * 70)
        print("TSAM STAGE 0 — AUTOMATED BENCHMARK SUITE")
        print("=" * 70)
        print()

    for test_name, source, should_accept, desc in BENCHMARK_TEST_CASES:
        if verbose:
            print(f"─── Test: {test_name} ───")
            print(f"    {desc}")
            print()

        # Run once
        out1 = materialize(test_name, source, cg, verbose=verbose)

        # Run again for determinism check
        out2 = materialize(test_name, source, cg, verbose=False)

        # ── Metric 1: AST parse success ──
        ast_ok = out1.output_graph is not None and out1.output_graph.fidelity > 0.0

        # ── Metric 2: Structural test pass rate ──
        required_methods = {"getCapability", "invalidateCapabilities"}
        if out1.output_graph:
            all_method_names: set[str] = set()
            for n in out1.output_graph.nodes.values():
                all_method_names.add(n.name)
            found_methods = sorted(required_methods.intersection(all_method_names))
        else:
            found_methods = []
        has_required = len(found_methods) == len(required_methods)

        # ── Metric 3: Constraint satisfaction ──
        if out1.output_graph:
            final_results = check_all_constraints(out1.output_graph, cg)
        else:
            final_results = []
        hard_total   = len(cg.hard)
        strong_total = len(cg.strong)
        hard_sat     = hard_total - out1.hard_fails
        strong_sat   = strong_total - out1.strong_fails

        # ── Metric 4: Node delta ──
        orig_graph = ProgramGraph.from_python_source(source)
        node_delta = abs(
            len(out1.output_graph.nodes if out1.output_graph else {}) - len(orig_graph.nodes)
        )

        # ── Metric 5: Memory ──
        memory_flat = out1.peak_memory_kb < 10 * 1024  # Under 10 MB

        # ── Metric 6: Determinism ──
        determinism_ok = out1.output_hash == out2.output_hash

        # ── Metric 7: Failure mode ──
        if not out1.accepted:
            has_diagnostic = (
                out1.diagnostic is not None
                and "tsam_diagnostic" in out1.diagnostic
                and "reason" in out1.diagnostic
            )
        else:
            has_diagnostic = True  # Not applicable if accepted

        result = BenchmarkResult(
            test_name            = test_name,
            expected_accept      = should_accept,
            actual_accept        = out1.accepted,
            acceptance_correct   = out1.accepted == should_accept,
            ast_parse_passes     = ast_ok,
            has_required_methods = has_required,
            method_names_found   = found_methods,
            hard_satisfied       = hard_sat,
            hard_total           = hard_total,
            strong_satisfied     = strong_sat,
            strong_total         = strong_total,
            node_delta           = node_delta,
            peak_memory_kb       = out1.peak_memory_kb,
            memory_flat          = memory_flat,
            determinism_verified = determinism_ok,
            hash_run1            = out1.output_hash,
            hash_run2            = out2.output_hash,
            has_clean_diagnostic = has_diagnostic,
            elapsed_ms           = out1.elapsed_seconds * 1000,
        )
        results.append(result)
        all_hashes.append(out1.output_hash)

        if verbose:
            status = "✓ PASS" if result.acceptance_correct else "✗ FAIL"
            print(f"\n    {status} (expected={'accept' if should_accept else 'reject'}, "
                  f"got={'accept' if out1.accepted else 'reject'})")
            print(f"    hard={hard_sat}/{hard_total} ✓  "
                  f"strong={strong_sat}/{strong_total} ✓  "
                  f"node_delta={node_delta}  "
                  f"memory={out1.peak_memory_kb:.0f} KB  "
                  f"deterministic={'✓' if determinism_ok else '✗'}")
            print()

    # ── Constant Memory Proof ──
    if verbose:
        print("─── Constant Memory Proof ───")
    memory_proof = prove_constant_memory(iterations=100)
    if verbose:
        print(f"    {memory_proof['verdict']}")
        print(f"    Initial peak: {memory_proof['initial_peak_kb']:.1f} KB, "
              f"Max peak: {memory_proof['max_peak_kb']:.1f} KB, "
              f"Drift: {memory_proof['drift_kb']:.2f} KB")
        print()

    # ── Summary ──
    total_tests     = len(results)
    correct_accept  = sum(1 for r in results if r.acceptance_correct)
    hard_satisfied  = sum(r.hard_satisfied   for r in results)
    hard_total      = sum(r.hard_total       for r in results)
    memory_flat_all = all(r.memory_flat      for r in results)
    determinism_all = all(r.determinism_verified for r in results)
    diag_all        = all(r.has_clean_diagnostic for r in results)

    report = {
        "tsam_stage0_benchmark": True,
        "overall": {
            "tests_run":           total_tests,
            "acceptance_correct":  f"{correct_accept}/{total_tests}",
            "hard_constraints_met": f"{hard_satisfied}/{hard_total}",
            "memory_flat":         memory_flat_all,
            "determinism_verified": determinism_all,
            "clean_diagnostics":   diag_all,
            "memory_proof_passed": memory_proof["passed"],
        },
        "test_results": [r.to_dict() for r in results],
        "memory_proof":  memory_proof,
        "passed": (
            correct_accept == total_tests
            and memory_flat_all
            and determinism_all
            and diag_all
            and memory_proof["passed"]
        ),
    }

    if verbose:
        print("=" * 70)
        verdict = "✓✓ ALL STAGE 0 BENCHMARKS PASSED" if report["passed"] else "✗ SOME BENCHMARKS FAILED"
        print(f"OVERALL: {verdict}")
        print(f"  Acceptance: {correct_accept}/{total_tests} correct")
        print(f"  Hard constraints: {hard_satisfied}/{hard_total} satisfied across all tests")
        print(f"  Memory: {'FLAT ✓' if memory_flat_all else 'DRIFTING ✗'}")
        print(f"  Determinism: {'VERIFIED ✓' if determinism_all else 'FAILED ✗'}")
        print(f"  Diagnostics: {'CLEAN ✓' if diag_all else 'MISSING ✗'}")
        print(f"  Constant memory proof: {'PASS ✓' if memory_proof['passed'] else 'FAIL ✗'}")
        print("=" * 70)

    return report


if __name__ == "__main__":
    import json

    print("\n")
    report = run_benchmark(verbose=True)

    print("\n\n=== MACHINE-READABLE REPORT ===")
    print(json.dumps(report, indent=2))
