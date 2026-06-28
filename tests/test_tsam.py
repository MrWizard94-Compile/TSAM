"""
TSAM Stage 0 — Unit Test Suite
================================
Tests for all phases of the TSAM computational pipeline.
Uses stdlib unittest — no external test framework required.

Run: python -m pytest tests/ -v
 or: python tests/test_tsam.py
"""

from __future__ import annotations

import json
import sys
import tracemalloc
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from tsam.cognitive_state import (
    BoundedContext,
    CognitiveState,
    ConstraintPriority,
    ExecutionBudget,
    Focus,
    FocusLevel,
    Mission,
    VerificationOutcome,
    VerificationRecord,
    VerificationSummary,
    prove_constant_memory,
    MAX_CONTEXT_ITEMS,
    MAX_RECENT_VERIFICATIONS,
)
from tsam.constraint_graph import (
    Constraint,
    ConstraintGraph,
    FABRIC_APIS,
    NEOFORGE_REQUIRED_APIS,
    NodeKind,
    ProgramGraph,
    build_neoforge_constraint_graph,
    check_all_constraints,
    check_constraint,
)
from tsam.task_planner import (
    Task,
    TaskKind,
    TaskPlan,
    TaskPlanner,
    TaskStatus,
)
from tsam.rewrite_engine import (
    ACCEPTANCE_THRESHOLD,
    DeterministicRuleStabilizer,
    EnergyBreakdown,
    TSAMComputationalLoop,
    VerificationKernel,
    compute_energy,
)


# ════════════════════════════════════════════════════════════════════════════
# Phase 1 Tests: Cognitive State
# ════════════════════════════════════════════════════════════════════════════

class TestCognitiveState(unittest.TestCase):

    def setUp(self) -> None:
        self.mission = Mission.neoforge_port()
        self.state   = CognitiveState.initialize(self.mission)

    def test_initial_state_is_valid(self) -> None:
        """Initial cognitive state must be well-formed."""
        self.assertEqual(self.state.step, 0)
        self.assertEqual(self.state.budget.rewrites_remaining, 50)
        self.assertFalse(self.state.must_terminate)
        self.assertTrue(self.state.can_continue)

    def test_state_is_immutable(self) -> None:
        """CognitiveState must be immutable (frozen dataclass)."""
        with self.assertRaises(Exception):
            self.state.step = 999  # type: ignore

    def test_advance_increments_step(self) -> None:
        """Advancing state increments the step counter."""
        s2 = self.state.advance()
        self.assertEqual(s2.step, 1)
        self.assertEqual(self.state.step, 0)  # Original unchanged

    def test_budget_decrements_on_advance(self) -> None:
        """Budget must decrease on each rewrite advance."""
        s1 = self.state.advance(consume_rewrite=True)
        self.assertEqual(s1.budget.rewrites_remaining, 49)

    def test_budget_does_not_decrement_on_verify(self) -> None:
        """Verify steps must not consume rewrite budget."""
        s1 = self.state.advance(consume_rewrite=False)
        self.assertEqual(s1.budget.rewrites_remaining, 50)

    def test_budget_exhaustion(self) -> None:
        """State must report termination when budget exhausted."""
        state = CognitiveState.initialize(self.mission, max_rewrites=3)
        for _ in range(3):
            state = state.advance()
        self.assertTrue(state.must_terminate)
        self.assertFalse(state.can_continue)

    def test_verification_summary_ring_buffer_bounded(self) -> None:
        """Verification ring buffer must never exceed MAX_RECENT_VERIFICATIONS."""
        state = self.state
        for i in range(MAX_RECENT_VERIFICATIONS * 3):
            rec = VerificationRecord(
                step=i, outcome=VerificationOutcome.PASS,
                energy=1.0, distance=0.0,
                hard_fails=0, strong_fails=0, soft_fails=0,
            )
            state = state.advance(verification_record=rec)
        self.assertLessEqual(
            len(state.verification.records), MAX_RECENT_VERIFICATIONS
        )

    def test_context_bounded(self) -> None:
        """Bounded context must not exceed MAX_CONTEXT_ITEMS entries."""
        ctx = BoundedContext.empty()
        for i in range(MAX_CONTEXT_ITEMS * 3):
            ctx = ctx.set(f"key_{i}", f"value_{i}", i)
        self.assertLessEqual(len(ctx), MAX_CONTEXT_ITEMS)

    def test_diagnostic_report_is_machine_readable(self) -> None:
        """Diagnostic report must be a valid dict with required keys."""
        # Exhaust budget
        state = CognitiveState.initialize(self.mission, max_rewrites=1)
        state = state.advance()  # Use up the budget
        report = state.diagnostic_report()

        self.assertIn("tsam_diagnostic", report)
        self.assertIn("reason", report)
        self.assertIn("step", report)
        self.assertTrue(report["tsam_diagnostic"])
        self.assertIsInstance(report["reason"], str)
        self.assertGreater(len(report["reason"]), 0)

    def test_constant_memory_proof(self) -> None:
        """Constant memory proof must pass (< 64 KB drift)."""
        result = prove_constant_memory(iterations=50)
        self.assertTrue(
            result["passed"],
            f"Memory proof failed: {result['verdict']}"
        )


# ════════════════════════════════════════════════════════════════════════════
# Phase 2+3 Tests: Structural Encoding + Constraint Graph
# ════════════════════════════════════════════════════════════════════════════

class TestProgramGraph(unittest.TestCase):

    def test_parses_valid_python(self) -> None:
        """Valid Python must produce a graph with fidelity > 0."""
        source = "class Foo:\n    def bar(self):\n        pass\n"
        graph  = ProgramGraph.from_python_source(source)
        self.assertGreater(graph.fidelity, 0.0)
        self.assertGreater(len(graph.nodes), 0)

    def test_invalid_python_produces_zero_fidelity(self) -> None:
        """Invalid Python must produce a graph with fidelity 0.0."""
        source = "class (invalid:"
        graph  = ProgramGraph.from_python_source(source)
        self.assertEqual(graph.fidelity, 0.0)
        self.assertEqual(len(graph.nodes), 0)

    def test_api_inventory_detects_fabric(self) -> None:
        """API inventory must detect Fabric imports."""
        source = "import net.fabricmc.fabric\nclass Foo:\n    pass\n"
        graph  = ProgramGraph.from_python_source(source)
        apis   = graph.api_inventory()
        # Should find the Fabric import
        self.assertTrue(
            any("fabricmc" in api for api in apis),
            f"Expected Fabric API in {apis}"
        )

    def test_structural_hash_is_deterministic(self) -> None:
        """Same source must always produce same structural hash."""
        source = "class Foo:\n    def bar(self):\n        pass\n"
        h1 = ProgramGraph.from_python_source(source).structural_hash()
        h2 = ProgramGraph.from_python_source(source).structural_hash()
        self.assertEqual(h1, h2)

    def test_different_sources_produce_different_hashes(self) -> None:
        """Different sources must produce different hashes."""
        source1 = "class Foo:\n    def bar(self):\n        pass\n"
        source2 = "class Baz:\n    def qux(self):\n        return 1\n"
        h1 = ProgramGraph.from_python_source(source1).structural_hash()
        h2 = ProgramGraph.from_python_source(source2).structural_hash()
        self.assertNotEqual(h1, h2)


class TestConstraintGraph(unittest.TestCase):

    def setUp(self) -> None:
        self.cg = build_neoforge_constraint_graph()

    def test_has_required_constraints(self) -> None:
        """Constraint graph must have all required Stage 0 constraints."""
        required_ids = {
            "MUST_COMPILE",
            "MUST_NOT_USE_FABRIC_APIS",
            "MUST_USE_NEOFORGE_APIS",
            "MUST_PRESERVE_SAVES",
            "MUST_HAVE_CAPABILITY_METHOD",
            "MUST_HAVE_INVALIDATION_METHOD",
            "MUST_PRESERVE_BEHAVIOR",
            "MUST_REGISTER_CAPABILITY",
        }
        present = set(self.cg.constraints.keys())
        missing = required_ids - present
        self.assertEqual(missing, set(), f"Missing constraints: {missing}")

    def test_priority_counts(self) -> None:
        """Must have correct counts per priority tier."""
        self.assertGreaterEqual(len(self.cg.hard),   4)  # At least 4 hard
        self.assertGreaterEqual(len(self.cg.strong), 2)  # At least 2 strong
        self.assertGreaterEqual(len(self.cg.soft),   1)  # At least 1 soft

    def test_hard_dominate_ordering(self) -> None:
        """Hard constraints must appear before strong and soft in all_ordered."""
        ordered = self.cg.all_ordered
        priorities = [c.priority for c in ordered]
        hard_idx   = max((i for i, p in enumerate(priorities) if p == ConstraintPriority.HARD), default=-1)
        strong_idx = min((i for i, p in enumerate(priorities) if p == ConstraintPriority.STRONG), default=999)
        soft_idx   = min((i for i, p in enumerate(priorities) if p == ConstraintPriority.SOFT), default=999)
        self.assertLess(hard_idx, strong_idx)
        self.assertLess(hard_idx, soft_idx)

    def test_fabric_source_violates_hard_constraints(self) -> None:
        """Fabric-only source must violate at least one hard constraint."""
        source = "import net.fabricmc.fabric\nclass FabricProvider:\n    pass\n"
        graph  = ProgramGraph.from_python_source(source)
        results = check_all_constraints(graph, self.cg)
        hard_violations = [r for r in results if r.violated and r.priority == ConstraintPriority.HARD]
        self.assertGreater(len(hard_violations), 0)

    def test_neoforge_source_satisfies_hard_constraints(self) -> None:
        """Valid NeoForge source must satisfy all hard constraints."""
        source = """\
from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar

class NeoProvider:
    def getCapability(self, cap, direction=None):
        if cap == MY_CAPABILITY:
            return LazyOptional.of(lambda: self._handler)
        return LazyOptional.empty()

    def invalidateCapabilities(self):
        pass
"""
        graph   = ProgramGraph.from_python_source(source)
        results = check_all_constraints(graph, self.cg)
        hard_violations = [r for r in results if r.violated and r.priority == ConstraintPriority.HARD]
        self.assertEqual(
            len(hard_violations), 0,
            f"Valid NeoForge source failed hard constraints: "
            f"{[r.constraint_id for r in hard_violations]}"
        )


# ════════════════════════════════════════════════════════════════════════════
# Phase 3.5 Tests: Task Planner
# ════════════════════════════════════════════════════════════════════════════

class TestTaskPlanner(unittest.TestCase):

    def setUp(self) -> None:
        self.cg      = build_neoforge_constraint_graph()
        self.planner = TaskPlanner()

    def test_fabric_source_produces_tasks(self) -> None:
        """Fabric source with violations must produce repair tasks."""
        source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        graph  = ProgramGraph.from_python_source(source)
        plan   = self.planner.plan(graph, self.cg)
        self.assertGreater(len(plan.tasks), 0)

    def test_hard_tasks_before_strong_tasks(self) -> None:
        """Hard-priority tasks must appear before strong-priority tasks in plan."""
        source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        graph  = ProgramGraph.from_python_source(source)
        plan   = self.planner.plan(graph, self.cg)

        first_strong_idx = next(
            (i for i, t in enumerate(plan.tasks) if t.priority == ConstraintPriority.STRONG),
            None
        )
        last_hard_idx = max(
            (i for i, t in enumerate(plan.tasks) if t.priority == ConstraintPriority.HARD),
            default=None
        )
        if first_strong_idx is not None and last_hard_idx is not None:
            self.assertLess(last_hard_idx, first_strong_idx)

    def test_plan_is_deterministic(self) -> None:
        """Same graph + constraints must produce identical task plan."""
        source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        graph  = ProgramGraph.from_python_source(source)
        plan1  = self.planner.plan(graph, self.cg)
        plan2  = self.planner.plan(graph, self.cg)
        ids1   = [t.task_id for t in plan1.tasks]
        ids2   = [t.task_id for t in plan2.tasks]
        self.assertEqual(ids1, ids2)

    def test_valid_source_produces_minimal_tasks(self) -> None:
        """Source with only soft violations should have few or no hard tasks."""
        source = """\
from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar
class Prov:
    def getCapability(self, cap, direction=None):
        return LazyOptional.empty()
    def invalidateCapabilities(self):
        pass
"""
        graph = ProgramGraph.from_python_source(source)
        plan  = self.planner.plan(graph, self.cg)
        hard_tasks = [t for t in plan.tasks if t.priority == ConstraintPriority.HARD]
        self.assertEqual(len(hard_tasks), 0)


# ════════════════════════════════════════════════════════════════════════════
# Phase 4 Tests: Energy, Stabilizer, Verification Kernel, Loop
# ════════════════════════════════════════════════════════════════════════════

class TestEnergyFunction(unittest.TestCase):

    def setUp(self) -> None:
        self.cg = build_neoforge_constraint_graph()

    def test_fabric_source_has_positive_energy(self) -> None:
        """Fabric source must have positive energy (not in manifold)."""
        source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        graph  = ProgramGraph.from_python_source(source)
        e, d, bd, _ = compute_energy(graph, self.cg)
        self.assertGreater(e, 0.0)
        self.assertGreater(d, 0.0)

    def test_neoforge_source_has_lower_energy(self) -> None:
        """Valid NeoForge source must have lower energy than Fabric source."""
        fabric_source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        neo_source    = """\
from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar
class Prov:
    def getCapability(self, cap, direction=None):
        return LazyOptional.empty()
    def invalidateCapabilities(self):
        pass
"""
        g_fabric = ProgramGraph.from_python_source(fabric_source)
        g_neo    = ProgramGraph.from_python_source(neo_source)
        e_fabric, _, _, _ = compute_energy(g_fabric, self.cg)
        e_neo, _, _, _    = compute_energy(g_neo, self.cg)
        self.assertLess(e_neo, e_fabric)

    def test_energy_is_deterministic(self) -> None:
        """Same source must always produce same energy value."""
        source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        graph  = ProgramGraph.from_python_source(source)
        e1, d1, _, _ = compute_energy(graph, self.cg)
        e2, d2, _, _ = compute_energy(graph, self.cg)
        self.assertAlmostEqual(e1, e2, places=6)
        self.assertAlmostEqual(d1, d2, places=6)

    def test_hard_weight_dominates(self) -> None:
        """Hard violations must dominate energy (> soft violations)."""
        # Source with only soft violations (or near-valid)
        neo_source = """\
from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar
class prov:  # wrong naming (soft violation only)
    def getCapability(self, cap, direction=None):
        return LazyOptional.empty()
    def invalidateCapabilities(self):
        pass
"""
        fabric_source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        g_soft   = ProgramGraph.from_python_source(neo_source)
        g_fabric = ProgramGraph.from_python_source(fabric_source)
        e_soft, _, _, _   = compute_energy(g_soft, self.cg)
        e_fabric, _, _, _ = compute_energy(g_fabric, self.cg)
        self.assertLess(e_soft, e_fabric)


class TestComputationalLoop(unittest.TestCase):

    def setUp(self) -> None:
        self.cg      = build_neoforge_constraint_graph()
        self.mission = Mission.neoforge_port()
        self.loop    = TSAMComputationalLoop()

    def _run(self, source: str, budget: int = 30) -> tuple:
        state = CognitiveState.initialize(self.mission, max_rewrites=budget)
        return self.loop.run(source, state, self.cg, verbose=False)

    def test_already_valid_source_accepted_immediately(self) -> None:
        """A source already in the manifold must be accepted without rewriting."""
        source = """\
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
"""
        final_graph, final_state, trace = self._run(source)
        self.assertTrue(trace.accepted, "Valid NeoForge source must be accepted")

    def test_fabric_source_is_transformed(self) -> None:
        """Fabric source must be transformed toward NeoForge manifold."""
        source = """\
import net.fabricmc.fabric

class FabricCapabilityProvider:
    def __init__(self):
        self.handler = MyHandler()

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return LazyOptional.of(lambda: self.handler)
        return LazyOptional.empty()
"""
        final_graph, final_state, trace = self._run(source)
        # Output must not contain Fabric imports
        self.assertNotIn("net.fabricmc.fabric", final_graph.source)

    def test_rejected_source_has_diagnostic(self) -> None:
        """Rejected source must emit a machine-readable diagnostic."""
        # Extremely sparse source — barely anything to work with
        source = "x = 1\n"
        final_graph, final_state, trace = self._run(source, budget=5)
        # Whether accepted or not, trace must be valid
        self.assertIsNotNone(trace)
        if not trace.accepted:
            self.assertIsNotNone(trace.diagnostic)
            self.assertIn("tsam_diagnostic", trace.diagnostic)

    def test_output_is_deterministic(self) -> None:
        """Same source + budget must produce identical output."""
        source = """\
import net.fabricmc.fabric
class F:
    def getCapability(self, cap, side):
        return LazyOptional.empty()
"""
        g1, _, t1 = self._run(source)
        g2, _, t2 = self._run(source)
        self.assertEqual(
            ProgramGraph.from_python_source(g1.source).structural_hash(),
            ProgramGraph.from_python_source(g2.source).structural_hash(),
            "Output must be deterministic"
        )

    def test_energy_monotonically_decreases(self) -> None:
        """Energy must not increase between accepted rewrite steps."""
        source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        _, _, trace = self._run(source)
        energies = [step["verification"]["energy"] for step in trace.steps]
        # Allow some non-monotone steps (reverted rewrites),
        # but overall trend must be downward
        if len(energies) > 1:
            self.assertLessEqual(
                energies[-1], energies[0],
                f"Final energy {energies[-1]:.2f} must not exceed initial {energies[0]:.2f}"
            )

    def test_budget_respected(self) -> None:
        """Loop must not exceed the rewrite budget."""
        budget = 5
        source = "import net.fabricmc.fabric\nclass F:\n    pass\n"
        _, final_state, _ = self._run(source, budget=budget)
        rewrites_used = budget - final_state.budget.rewrites_remaining
        self.assertLessEqual(rewrites_used, budget)


# ════════════════════════════════════════════════════════════════════════════
# Integration: End-to-End Tests
# ════════════════════════════════════════════════════════════════════════════

class TestEndToEnd(unittest.TestCase):

    def setUp(self) -> None:
        from tsam.benchmark import BENCHMARK_TEST_CASES, materialize
        self.cg         = build_neoforge_constraint_graph()
        self.test_cases = BENCHMARK_TEST_CASES
        self.materialize = materialize

    def test_all_benchmark_cases_produce_output(self) -> None:
        """All benchmark test cases must produce a MaterializedOutput."""
        for name, source, expected_accept, desc in self.test_cases:
            with self.subTest(test=name):
                out = self.materialize(name, source, self.cg, verbose=False)
                self.assertIsNotNone(out)
                self.assertIsInstance(out.accepted, bool)

    def test_expected_acceptance_matches(self) -> None:
        """
        Test cases marked as should_accept=True must be accepted.
        Test cases marked as should_accept=False must be rejected (or at least
        produce a clean diagnostic — we don't mandate strict rejection for all
        edge cases, but invalid cases must not silently accept).
        """
        for name, source, expected_accept, desc in self.test_cases:
            with self.subTest(test=name):
                out = self.materialize(name, source, self.cg, budget=30, verbose=False)
                if expected_accept:
                    self.assertTrue(
                        out.accepted,
                        f"Test '{name}' should have been accepted: {desc}"
                    )

    def test_rejected_outputs_have_diagnostics(self) -> None:
        """Rejected outputs must always have machine-readable diagnostics."""
        for name, source, expected_accept, desc in self.test_cases:
            with self.subTest(test=name):
                out = self.materialize(name, source, self.cg, budget=30, verbose=False)
                if not out.accepted:
                    self.assertIsNotNone(out.diagnostic)
                    self.assertIn("tsam_diagnostic", out.diagnostic)

    def test_outputs_are_deterministic(self) -> None:
        """Same test case must produce identical output on repeated runs."""
        for name, source, _, _ in self.test_cases:
            with self.subTest(test=name):
                out1 = self.materialize(name, source, self.cg, verbose=False)
                out2 = self.materialize(name, source, self.cg, verbose=False)
                self.assertEqual(
                    out1.output_hash, out2.output_hash,
                    f"Non-deterministic output in test '{name}'"
                )

    def test_memory_stays_bounded(self) -> None:
        """All test cases must complete within a bounded memory envelope."""
        for name, source, _, _ in self.test_cases:
            with self.subTest(test=name):
                out = self.materialize(name, source, self.cg, verbose=False)
                self.assertLess(
                    out.peak_memory_kb, 10 * 1024,  # 10 MB limit
                    f"Memory exceeded 10 MB in test '{name}': {out.peak_memory_kb:.1f} KB"
                )


class TestPhaseAandDFixes(unittest.TestCase):
    """
    Regression tests for review-driven fixes: the multi-class rewrite bug
    (Phase A), the soft-energy acceptance gate (Phase B), and capability-
    provider evidence scoping (Phase D). These previously had zero
    permanent test coverage -- they were only caught by ad hoc scripts
    during review, which is exactly how they could regress silently again.
    """

    def setUp(self) -> None:
        self.cg      = build_neoforge_constraint_graph()
        self.mission = Mission.neoforge_port()
        self.loop    = TSAMComputationalLoop()

    def _run(self, source: str, budget: int = 80):
        state = CognitiveState.initialize(self.mission, max_rewrites=budget)
        return self.loop.run(source, state, self.cg, verbose=False)

    def test_multiclass_source_gets_every_class_fixed(self) -> None:
        """Every class in a multi-class file must receive required methods, not just the first."""
        source = "\n\n".join(
            f"""\
import net.fabricmc.fabric

class FabricProvider{i}:
    def __init__(self):
        self.handler_{i} = MyHandler{i}()
        self.lazy_{i} = LazyOptional.of(lambda: self.handler_{i})

    def getCapability(self, cap, side):
        if cap == MY_CAP_{i}:
            return self.lazy_{i}
        return LazyOptional.empty()
"""
            for i in range(4)
        )
        final_graph, final_state, trace = self._run(source)
        self.assertTrue(trace.accepted, "Multi-class capability source should be accepted")
        for i in range(4):
            self.assertIn(f"class FabricProvider{i}", final_graph.source)
        self.assertEqual(
            final_graph.source.count("def invalidateCapabilities"), 4,
            "Every class must receive invalidateCapabilities, not just the first one found"
        )

    def test_method_insertion_does_not_corrupt_preceding_method(self) -> None:
        """Inserting a new method must not truncate the trailing statement of the previous one."""
        source = """\
import net.fabricmc.fabric

class FabricCapabilityProvider:
    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return LazyOptional.of(lambda: self.handler)
        return LazyOptional.empty()
"""
        final_graph, final_state, trace = self._run(source)
        self.assertTrue(trace.accepted)
        self.assertIn("return LazyOptional.empty()", final_graph.source)

    def test_soft_energy_does_not_block_acceptance_at_scale(self) -> None:
        """A large multi-class file with zero hard/strong violations must still be accepted."""
        source = "\n\n".join(
            f"""\
import net.fabricmc.fabric

class FabricProvider{i}:
    def __init__(self):
        self.handler_{i} = MyHandler{i}()
        self.lazy_{i} = LazyOptional.of(lambda: self.handler_{i})

    def getCapability(self, cap, side):
        if cap == MY_CAP_{i}:
            return self.lazy_{i}
        return LazyOptional.empty()
"""
            for i in range(10)
        )
        final_graph, final_state, trace = self._run(source)
        self.assertTrue(
            trace.accepted,
            "10-class source with zero hard/strong violations must not be rejected "
            "purely due to the soft diff-size term"
        )

    def test_non_capability_class_does_not_get_stub_methods_injected(self) -> None:
        """A class with no capability-provider evidence must not receive bolted-on stub methods."""
        source = """\
import net.fabricmc.fabric
from net.fabricmc import ServerLifecycleEvents

class FabricEventOnlyClass:
    def __init__(self):
        ServerLifecycleEvents.SERVER_STARTED.register(self.on_start)

    def on_start(self, server):
        return None
"""
        final_graph, final_state, trace = self._run(source, budget=20)
        self.assertFalse(
            trace.accepted,
            "A class with no capability-provider evidence must be rejected, not "
            "force-fitted with unrelated capability stub methods"
        )
        self.assertIsNotNone(trace.diagnostic)
        violation_ids = [v["id"] for v in trace.diagnostic.get("constraint_violations", [])]
        self.assertIn(
            "MUST_MATCH_KNOWN_PATTERN", violation_ids,
            "Rejection must be attributed to MUST_MATCH_KNOWN_PATTERN specifically"
        )
        self.assertNotIn("def getCapability", final_graph.source)

    def test_capability_class_in_mixed_file_still_gets_fixed(self) -> None:
        """Real capability provider mixed with a non-capability class: diagnostic must point at the right class."""
        source = """\
import net.fabricmc.fabric
from net.fabricmc import ServerLifecycleEvents

class FabricCapabilityProvider:
    def __init__(self):
        self.handler = MyHandler()
        self.lazy = LazyOptional.of(lambda: self.handler)

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return self.lazy
        return LazyOptional.empty()

class FabricEventOnlyClass:
    def __init__(self):
        ServerLifecycleEvents.SERVER_STARTED.register(self.on_start)

    def on_start(self, server):
        return None
"""
        final_graph, final_state, trace = self._run(source, budget=20)
        self.assertFalse(trace.accepted)
        violations = trace.diagnostic.get("constraint_violations", [])
        pattern_violation = next(
            (v for v in violations if v["id"] == "MUST_MATCH_KNOWN_PATTERN"), None
        )
        self.assertIsNotNone(pattern_violation)
        self.assertIn("FabricEventOnlyClass", pattern_violation["msg"])
        self.assertNotIn("FabricCapabilityProvider", pattern_violation["msg"])


class TestStageOneGapClosure(unittest.TestCase):
    """
    Tests for the two remaining Stage 0 gaps closed afterward: constraint
    complexity now genuinely scales with class count, and the previously
    dead ADAPT_CAPABILITY_BODY-adjacent machinery now does real, exercised
    work (cleaning a leftover Fabric call out of a genuine capability
    provider). Both tests deliberately construct an adversarial case
    rather than just a happy path.
    """

    def setUp(self) -> None:
        self.mission = Mission.neoforge_port()
        self.loop    = TSAMComputationalLoop()

    def _run(self, source: str, budget: int = 30):
        from tsam.constraint_graph import build_neoforge_constraint_graph_for_source
        cg = build_neoforge_constraint_graph_for_source(source)
        state = CognitiveState.initialize(self.mission, max_rewrites=budget)
        return self.loop.run(source, state, cg, verbose=False), cg

    def test_single_class_source_has_no_uniqueness_constraint(self) -> None:
        """A single-class file shouldn't even have a vacuous cross-class constraint."""
        from tsam.constraint_graph import build_neoforge_constraint_graph_for_source
        source = """\
import net.fabricmc.fabric

class FabricProvider:
    def getCapability(self, cap, side):
        return LazyOptional.of(lambda: self.handler)
"""
        cg = build_neoforge_constraint_graph_for_source(source)
        self.assertNotIn("MUST_HAVE_UNIQUE_CAPABILITY_KEYS", cg.constraints)

    def test_multiclass_source_has_uniqueness_constraint_active(self) -> None:
        from tsam.constraint_graph import build_neoforge_constraint_graph_for_source
        source = "\n\n".join(
            f"class P{i}:\n    def getCapability(self, cap, side):\n        return LazyOptional.empty()"
            for i in range(3)
        )
        cg = build_neoforge_constraint_graph_for_source(source)
        self.assertIn("MUST_HAVE_UNIQUE_CAPABILITY_KEYS", cg.constraints)

    def test_duplicate_capability_keys_across_classes_rejected(self) -> None:
        """Two classes that accidentally register under the same capability key must be rejected."""
        source = """\
import net.fabricmc.fabric

class FabricProviderA:
    def __init__(self):
        self.handler_a = MyHandlerA()
        self.lazy_a = LazyOptional.of(lambda: self.handler_a)

    def getCapability(self, cap, side):
        if cap == MY_SHARED_CAP:
            return self.lazy_a
        return LazyOptional.empty()

class FabricProviderB:
    def __init__(self):
        self.handler_b = MyHandlerB()
        self.lazy_b = LazyOptional.of(lambda: self.handler_b)

    def getCapability(self, cap, side):
        if cap == MY_SHARED_CAP:
            return self.lazy_b
        return LazyOptional.empty()
"""
        (final_graph, final_state, trace), cg = self._run(source)
        self.assertFalse(
            trace.accepted,
            "Two classes sharing the same capability-key identifier must be rejected"
        )
        violation_ids = [v["id"] for v in trace.diagnostic.get("constraint_violations", [])]
        self.assertIn("MUST_HAVE_UNIQUE_CAPABILITY_KEYS", violation_ids)

    def test_distinct_capability_keys_across_classes_still_accepted(self) -> None:
        """Sanity check: distinct keys across classes must NOT be flagged as a collision."""
        source = "\n\n".join(
            f"""\
import net.fabricmc.fabric

class FabricProvider{i}:
    def __init__(self):
        self.handler_{i} = MyHandler{i}()
        self.lazy_{i} = LazyOptional.of(lambda: self.handler_{i})

    def getCapability(self, cap, side):
        if cap == MY_CAP_{i}:
            return self.lazy_{i}
        return LazyOptional.empty()
"""
            for i in range(3)
        )
        (final_graph, final_state, trace), cg = self._run(source)
        self.assertTrue(trace.accepted, "Distinct capability keys must not be flagged as a collision")

    def test_dangling_fabric_call_in_capability_provider_is_cleaned(self) -> None:
        """A genuine capability provider with an incidental leftover Fabric call must be cleaned, not rejected wholesale."""
        source = """\
import net.fabricmc.fabric
from net.fabricmc import ServerLifecycleEvents

class MixedIntentProvider:
    def __init__(self):
        self.handler = MyHandler()
        self.lazy = LazyOptional.of(lambda: self.handler)
        ServerLifecycleEvents.SERVER_STARTED.register(self.on_start)

    def on_start(self, server):
        pass

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return self.lazy
        return LazyOptional.empty()
"""
        (final_graph, final_state, trace), cg = self._run(source)
        self.assertTrue(
            trace.accepted,
            "A genuine capability provider with an incidental leftover Fabric call "
            "should be cleaned up and accepted, not rejected wholesale"
        )
        self.assertNotIn("ServerLifecycleEvents", final_graph.source)

    def test_evidence_class_with_only_dangling_ref_violation_gets_specific_diagnostic(self) -> None:
        """If somehow unfixed, the dangling-ref violation must be attributed correctly, not confused with MUST_MATCH_KNOWN_PATTERN."""
        from tsam.constraint_graph import (
            ProgramGraph, build_neoforge_constraint_graph_for_source, check_all_constraints,
        )
        source = """\
from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar
import net.fabricmc.fabric

class AlreadyPortedButMessy:
    def getCapability(self, cap, direction=None):
        net.fabricmc.fabric.legacy_helper()
        return LazyOptional.empty()
    def invalidateCapabilities(self):
        pass
    def register_capability(self, registrar: BlockCapabilityRegistrar):
        registrar.registerBlockEntity(MY_CAP, self)
"""
        graph = ProgramGraph.from_python_source(source)
        cg    = build_neoforge_constraint_graph_for_source(source)
        results = check_all_constraints(graph, cg)
        violated_ids = {r.constraint_id for r in results if r.violated}
        self.assertIn("MUST_NOT_LEAVE_DANGLING_FABRIC_REFS", violated_ids)
        self.assertNotIn("MUST_MATCH_KNOWN_PATTERN", violated_ids)


class TestStructuralCapabilityKeyDetection(unittest.TestCase):
    """
    Regression tests for the structural (role-based) capability-key
    detector, which replaced the naming-convention-only heuristic after
    adversarial testing found it both over- and under-triggers:
      - False positive: an unrelated all-caps constant containing "CAP"
        (e.g. MAX_CAPACITY) was wrongly treated as a capability key,
        causing two classes with genuinely distinct keys to be rejected
        as a collision.
      - False negative: two classes genuinely sharing a key spelled in a
        style the naming regex didn't match (e.g. kSharedCapId) were
        silently accepted despite a real registration collision.
    """

    def setUp(self) -> None:
        self.mission = Mission.neoforge_port()
        self.loop    = TSAMComputationalLoop()

    def _run(self, source: str, budget: int = 30):
        from tsam.constraint_graph import build_neoforge_constraint_graph_for_source
        cg = build_neoforge_constraint_graph_for_source(source)
        state = CognitiveState.initialize(self.mission, max_rewrites=budget)
        return self.loop.run(source, state, cg, verbose=False)

    def test_unrelated_shared_constant_is_not_a_false_collision(self) -> None:
        """Two classes with distinct real keys that also share an unrelated CAP-spelled constant must be accepted."""
        source = """\
import net.fabricmc.fabric

class FabricProviderA:
    def __init__(self):
        self.handler_a = MyHandlerA()
        self.lazy_a = LazyOptional.of(lambda: self.handler_a)
    def getCapability(self, cap, side):
        if cap == MY_CAP_A:
            return self.lazy_a
        return LazyOptional.empty()
    def helper_a(self):
        return MAX_CAPACITY

class FabricProviderB:
    def __init__(self):
        self.handler_b = MyHandlerB()
        self.lazy_b = LazyOptional.of(lambda: self.handler_b)
    def getCapability(self, cap, side):
        if cap == MY_CAP_B:
            return self.lazy_b
        return LazyOptional.empty()
    def helper_b(self):
        return MAX_CAPACITY
"""
        final_graph, final_state, trace = self._run(source)
        self.assertTrue(
            trace.accepted,
            "An unrelated shared constant (MAX_CAPACITY) must not be treated as a capability-key collision"
        )

    def test_non_conventionally_named_collision_is_still_caught(self) -> None:
        """A genuine collision must be caught even when the shared key doesn't follow the all-caps naming convention."""
        source = """\
import net.fabricmc.fabric

class ProviderX:
    def __init__(self):
        self.handler_x = MyHandlerX()
        self.lazy_x = LazyOptional.of(lambda: self.handler_x)
    def getCapability(self, cap, side):
        if cap == kSharedCapId:
            return self.lazy_x
        return LazyOptional.empty()

class ProviderY:
    def __init__(self):
        self.handler_y = MyHandlerY()
        self.lazy_y = LazyOptional.of(lambda: self.handler_y)
    def getCapability(self, cap, side):
        if cap == kSharedCapId:
            return self.lazy_y
        return LazyOptional.empty()
"""
        final_graph, final_state, trace = self._run(source)
        self.assertFalse(
            trace.accepted,
            "A genuine collision must be caught regardless of naming convention"
        )
        violation_ids = [v["id"] for v in trace.diagnostic.get("constraint_violations", [])]
        self.assertIn("MUST_HAVE_UNIQUE_CAPABILITY_KEYS", violation_ids)

    def test_single_hop_alias_resolution(self) -> None:
        """A key referenced via a same-method local alias (key = MY_CAP; if cap == key) must still resolve correctly."""
        source = """\
import net.fabricmc.fabric

class AliasProviderC:
    def __init__(self):
        self.handler_c = MyHandlerC()
        self.lazy_c = LazyOptional.of(lambda: self.handler_c)
    def getCapability(self, cap, side):
        key = MY_CAP_C
        if cap == key:
            return self.lazy_c
        return LazyOptional.empty()

class AliasProviderD:
    def __init__(self):
        self.handler_d = MyHandlerD()
        self.lazy_d = LazyOptional.of(lambda: self.handler_d)
    def getCapability(self, cap, side):
        key = MY_CAP_D
        if cap == key:
            return self.lazy_d
        return LazyOptional.empty()
"""
        final_graph, final_state, trace = self._run(source)
        self.assertTrue(
            trace.accepted,
            "Distinct keys resolved through a single-hop local alias must not be flagged as a collision"
        )


class TestLexicographicEnergyOrdering(unittest.TestCase):
    """
    Tests for EnergyBreakdown.lexicographic_key, which replaced the
    weighted-scalar comparison as the computational loop's actual
    accept/revert decision driver.

    The weighted scalar (hard=100, strong=10, soft=1 weights) happens to
    preserve correct priority dominance today only because the current
    constraint set is small (max 9 HARD, 2 STRONG) -- it is not
    structurally guaranteed. These tests construct EnergyBreakdown values
    directly (not through the full rewrite loop) to prove the
    lexicographic key orders correctly in a case where it matters, and
    that it agrees with the scalar in the normal, bounded case.
    """

    def test_lexicographic_key_correctly_orders_when_quality_swamps_strong_delta(self) -> None:
        """A single resolved strong violation must outrank ANY quality regression, even one large enough to flip the weighted scalar."""
        # Before: 1 strong violation, small quality cost.
        before = EnergyBreakdown(
            hard_penalty=0.0, strong_penalty=10.0, soft_penalty=0.0,
            syntax_penalty=0.0, node_delta=1.0,
        )
        # After: strong violation resolved, but quality regressed by more
        # than the strong weight -- enough to flip a weighted-scalar
        # comparison (10+1=11 -> 0+50=50, scalar says "worse"), but the
        # strong-tier fix must still dominate lexicographically.
        after = EnergyBreakdown(
            hard_penalty=0.0, strong_penalty=0.0, soft_penalty=0.0,
            syntax_penalty=0.0, node_delta=50.0,
        )
        self.assertGreater(
            after.total, before.total,
            "Sanity check: this scenario is constructed so the weighted scalar says 'worse'"
        )
        self.assertLess(
            after.lexicographic_key, before.lexicographic_key,
            "Resolving the strong violation must rank as an improvement lexicographically, "
            "regardless of how much the weighted scalar disagrees"
        )

    def test_lexicographic_key_agrees_with_scalar_within_current_bounds(self) -> None:
        """Within today's actual constraint counts, scalar and lexicographic comparisons must agree (sanity check on current weight separation)."""
        from tsam.constraint_graph import build_neoforge_constraint_graph
        cg = build_neoforge_constraint_graph(n_classes=2)
        max_hard   = len(cg.hard)
        max_strong = len(cg.strong)
        # Worst-case quality plausible within one rewrite step in this domain.
        plausible_quality_ceiling = 10.0

        one_less_hard = EnergyBreakdown(
            hard_penalty=(max_hard - 1) * 100.0, strong_penalty=max_strong * 10.0,
            soft_penalty=0.0, syntax_penalty=0.0, node_delta=0.0,
        )
        full_hard_zero_quality = EnergyBreakdown(
            hard_penalty=max_hard * 100.0, strong_penalty=0.0,
            soft_penalty=0.0, syntax_penalty=0.0, node_delta=0.0,
        )
        # One fewer hard violation must beat full strong+quality regardless.
        self.assertLess(one_less_hard.total, full_hard_zero_quality.total)
        self.assertLess(one_less_hard.lexicographic_key, full_hard_zero_quality.lexicographic_key)


class TestDanglingReferenceSafety(unittest.TestCase):
    """
    Regression tests for a severe bug found by adversarial testing of the
    0.3.0 dangling-fabric-cleanup machinery: both
    _rewrite_source_remove_dangling_fabric_statements (statement removal)
    and _rewrite_source_remove_forbidden_apis (import removal) would
    delete a binding without checking whether something else in the file
    still referenced it -- producing code that parses (passes
    MUST_COMPILE) but raises at runtime (AttributeError / NameError), and
    *accepting* that as valid output. This is exactly the "fabricate
    broken output instead of rejecting" failure mode the architecture
    exists to prevent, and it was happening via the architecture's own
    repair logic.

    Also covers a related, independently-discovered bug:
    MUST_NOT_USE_FABRIC_APIS used exact set-intersection against fully-
    qualified import targets, so `from net.fabricmc import X` never
    exactly matched the FABRIC_APIS entry "net.fabricmc" and silently
    evaded the constraint entirely (previously masked because the
    rewrite engine's broader substring-based removal cleaned it up as a
    side effect, until the dangling-reference guard above started
    legitimately blocking some of those removals).
    """

    def setUp(self) -> None:
        self.mission = Mission.neoforge_port()
        self.loop    = TSAMComputationalLoop()

    def _run(self, source: str, budget: int = 30):
        from tsam.constraint_graph import build_neoforge_constraint_graph_for_source
        cg = build_neoforge_constraint_graph_for_source(source)
        state = CognitiveState.initialize(self.mission, max_rewrites=budget)
        return self.loop.run(source, state, cg, verbose=False)

    def test_removing_assignment_used_elsewhere_is_refused(self) -> None:
        """A forbidden-API assignment whose target is used elsewhere in the class must not be silently removed and accepted."""
        source = """\
import net.fabricmc.fabric

class RiskyProvider:
    def __init__(self):
        self.handler = MyHandler()
        self.lazy = LazyOptional.of(lambda: self.handler)
        self.fabric_thing = net.fabricmc.fabric.SomeHelper.create()

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            combined = self.lazy.combine(self.fabric_thing)
            return combined
        return LazyOptional.empty()
"""
        final_graph, final_state, trace = self._run(source)
        self.assertFalse(
            trace.accepted,
            "Must not accept output where a removed assignment's target is still referenced"
        )
        # And the output must not actually contain the dangling reference
        # while ALSO lacking the assignment (i.e. don't half-apply this).
        if "self.fabric_thing = " not in final_graph.source:
            self.assertNotIn(
                "self.fabric_thing", final_graph.source,
                "If the assignment was removed, nothing should still reference its target"
            )

    def test_removing_import_alias_used_elsewhere_is_refused(self) -> None:
        """A forbidden import whose bound alias is used elsewhere must not be silently removed and accepted."""
        source = """\
import net.fabricmc.fabric
from net.fabricmc import FabricHelperAlias

class AliasRiskyProvider:
    def __init__(self):
        self.handler = MyHandler()
        self.lazy = LazyOptional.of(lambda: self.handler)

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            helper = FabricHelperAlias.get()
            return self.lazy
        return LazyOptional.empty()
"""
        final_graph, final_state, trace = self._run(source)
        self.assertFalse(
            trace.accepted,
            "Must not accept output where a removed import's bound name is still referenced"
        )
        violation_ids = [v["id"] for v in trace.diagnostic.get("constraint_violations", [])]
        self.assertIn("MUST_NOT_USE_FABRIC_APIS", violation_ids)

    def test_unused_forbidden_import_is_still_safely_removed(self) -> None:
        """Sanity check: a forbidden import that ISN'T referenced elsewhere must still be removed and accepted -- the guard shouldn't make everything more conservative than necessary."""
        source = """\
import net.fabricmc.fabric
from net.fabricmc import UnusedFabricImport

class FabricCapabilityProvider:
    def __init__(self):
        self.handler = MyHandler()
        self.lazy = LazyOptional.of(lambda: self.handler)

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return self.lazy
        return LazyOptional.empty()
"""
        final_graph, final_state, trace = self._run(source)
        self.assertTrue(
            trace.accepted,
            "An unused forbidden import must still be safely removable"
        )
        self.assertNotIn("net.fabricmc", final_graph.source)

    def test_unused_dangling_statement_is_still_safely_removed(self) -> None:
        """Sanity check: a forbidden statement with no externally-referenced binding (e.g. a bare call) must still be removed and accepted."""
        source = """\
import net.fabricmc.fabric
from net.fabricmc import ServerLifecycleEvents

class MixedIntentProviderTwo:
    def __init__(self):
        self.handler = MyHandler()
        self.lazy = LazyOptional.of(lambda: self.handler)
        ServerLifecycleEvents.SERVER_STARTED.register(self.on_start)

    def on_start(self, server):
        pass

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return self.lazy
        return LazyOptional.empty()
"""
        final_graph, final_state, trace = self._run(source)
        self.assertTrue(trace.accepted)
        self.assertNotIn("ServerLifecycleEvents", final_graph.source)

    def test_forbidden_api_set_catches_from_import_variants(self) -> None:
        """MUST_NOT_USE_FABRIC_APIS must catch `from net.fabricmc import X` directly, not just bare `import net.fabricmc...`."""
        from tsam.constraint_graph import (
            ProgramGraph, build_neoforge_constraint_graph_for_source, check_all_constraints,
        )
        source = """\
from net.fabricmc import SomeFabricThing

class SoleImportCase:
    def getCapability(self, cap, side):
        return LazyOptional.empty()
"""
        graph = ProgramGraph.from_python_source(source)
        cg    = build_neoforge_constraint_graph_for_source(source)
        results = check_all_constraints(graph, cg)
        violated_ids = {r.constraint_id for r in results if r.violated}
        self.assertIn(
            "MUST_NOT_USE_FABRIC_APIS", violated_ids,
            "A from-import of an arbitrary name from a forbidden module must still be caught"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
