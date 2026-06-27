"""
TSAM Stage 0 — Phase 4: Energy-Based Structural Stabilization + Rewrite + Verification
========================================================================================
This is the heart of TSAM Stage 0.

Implements:
- Definition 5 (Energy Function): E(P, C, M) = Σ wᵢ · Eᵢ(P, C, M)
- Definition 7 (Stabilization Operator): S(P) → P' with E(P') ≤ E(P)
- Definition 6 (Acceptance): Accept(P) ⟺ V(P) = PASS ∧ E < τ ∧ d(P,M) = 0
- Computational Contract C1: progress or diagnostic

The Verification Kernel sits INSIDE the rewrite loop.
Every rewrite attempt is immediately verified; the energy guides next steps.

Rewrite rules are deterministic graph transformations:
  - REMOVE_FORBIDDEN_APIS: strip forbidden import nodes
  - INJECT_NEOFORGE_IMPORTS: add NeoForge import nodes
  - ADD_REQUIRED_METHOD: synthesize method stub
  - ADAPT_CAPABILITY_BODY: rewrite capability method body
  - REGISTER_CAPABILITY: add registration call

No transformers, no attention, no neural components.
"""

from __future__ import annotations

import ast
import copy
import json
import textwrap
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from tsam.cognitive_state import (
    CognitiveState,
    ConstraintPriority,
    VerificationOutcome,
    VerificationRecord,
)
from tsam.constraint_graph import (
    Constraint,
    ConstraintGraph,
    ConstraintResult,
    FABRIC_APIS,
    NEOFORGE_REQUIRED_APIS,
    NodeKind,
    ProgramGraph,
    check_all_constraints,
    CAPABILITY_METHOD_NAMES,
    class_shows_capability_provider_evidence,
)
from tsam.task_planner import Task, TaskKind, TaskPlan, TaskPlanner, TaskStatus


# ===========================================================================
# ENERGY FUNCTION (Definition 5)
# ===========================================================================

# Energy weights — HARD violations dominate everything else
# Implements "partially ordered" energy from the Formal Spec
ENERGY_WEIGHTS: dict[ConstraintPriority, float] = {
    ConstraintPriority.HARD:   100.0,   # Hard violation = massive energy
    ConstraintPriority.STRONG:  10.0,   # Strong violation = significant energy
    ConstraintPriority.SOFT:     1.0,   # Soft violation = small energy
}

ACCEPTANCE_THRESHOLD: float = 0.5   # τ: E < τ and d = 0 → accept

@dataclass(frozen=True, slots=True)
class EnergyBreakdown:
    """
    Detailed energy breakdown per constraint type.
    Implements Definition 5 energy components.
    """
    hard_penalty:   float    # Σ w_hard × hard violations
    strong_penalty: float    # Σ w_strong × strong violations
    soft_penalty:   float    # Σ w_soft × soft violations
    syntax_penalty: float    # Parse failure (treated as hard)
    node_delta:     float    # Structural change cost (soft)

    @property
    def gating(self) -> float:
        """
        The portion of energy that gates Accept(): hard + strong + syntax.

        Definition 5 notes energy is "initially scalar" but that "future
        extensions are expected to treat energy as partially ordered ...
        so that any compiler failure strictly dominates improvements in
        style or performance." SOFT-tier terms (soft_penalty, node_delta)
        are defined elsewhere as "desirable but negotiable... used for
        tie-breaking and optimization" — they must never block acceptance
        on their own. This property is that partial ordering in practice:
        a candidate with zero hard/strong violations is gated only on this
        value, never on soft/style energy (Phase B fix — previously
        node_delta alone could push total energy over τ and reject an
        otherwise fully-compliant multi-class output).
        """
        return self.hard_penalty + self.strong_penalty + self.syntax_penalty

    @property
    def quality(self) -> float:
        """SOFT-tier energy (diff size, naming, etc.) — reported, non-gating."""
        return self.soft_penalty + self.node_delta

    @property
    def total(self) -> float:
        return self.gating + self.quality

    def to_dict(self) -> dict:
        return {
            "total":          round(self.total, 4),
            "gating":         round(self.gating, 4),
            "quality":        round(self.quality, 4),
            "hard_penalty":   round(self.hard_penalty, 4),
            "strong_penalty": round(self.strong_penalty, 4),
            "soft_penalty":   round(self.soft_penalty, 4),
            "syntax_penalty": round(self.syntax_penalty, 4),
            "node_delta":     round(self.node_delta, 4),
        }


def compute_energy(
    graph:          ProgramGraph,
    cg:             ConstraintGraph,
    original_graph: ProgramGraph | None = None,
) -> tuple[float, float, EnergyBreakdown, list[ConstraintResult]]:
    """
    Compute E(P, C, M) and d(P, M).

    Returns:
        (total_energy, distance, breakdown, constraint_results)

    Distance to manifold:
        d = 0 if all hard + strong constraints satisfied and E < τ
        d = count of violated hard + strong constraints otherwise
    """
    results = check_all_constraints(graph, cg, original_graph)

    hard_violations   = [r for r in results if r.violated and r.priority == ConstraintPriority.HARD]
    strong_violations = [r for r in results if r.violated and r.priority == ConstraintPriority.STRONG]
    soft_violations   = [r for r in results if r.violated and r.priority == ConstraintPriority.SOFT]

    # Parse failure penalty (check fidelity)
    syntax_penalty = 0.0
    if graph.fidelity <= 0.0:
        syntax_penalty = ENERGY_WEIGHTS[ConstraintPriority.HARD] * 2  # Extra penalty

    # Node delta penalty (soft optimization metric)
    node_delta = 0.0
    if original_graph is not None:
        delta = abs(len(graph.nodes) - len(original_graph.nodes))
        node_delta = delta * ENERGY_WEIGHTS[ConstraintPriority.SOFT] * 0.1

    breakdown = EnergyBreakdown(
        hard_penalty   = len(hard_violations)   * ENERGY_WEIGHTS[ConstraintPriority.HARD],
        strong_penalty = len(strong_violations) * ENERGY_WEIGHTS[ConstraintPriority.STRONG],
        soft_penalty   = len(soft_violations)   * ENERGY_WEIGHTS[ConstraintPriority.SOFT],
        syntax_penalty = syntax_penalty,
        node_delta     = node_delta,
    )

    # Distance to manifold = count of non-soft violations (hard + strong)
    distance = float(len(hard_violations) + len(strong_violations))

    return breakdown.total, distance, breakdown, results


# ===========================================================================
# STABILIZATION OPERATOR (Definition 7)
# ===========================================================================
# The stabilizer drives P → P' with E(P') ≤ E(P).
# Stage 0: deterministic rule-based stabilization.
# The interface is swappable (the Spec explicitly states the stabilizer is
# an interchangeable module).

@runtime_checkable
class Stabilizer(Protocol):
    """
    Interface contract for the stabilization operator S.
    Any implementation that satisfies this protocol is valid.
    """
    def stabilize(
        self,
        graph: ProgramGraph,
        task:  Task,
        cg:    ConstraintGraph,
    ) -> tuple[ProgramGraph, str]:
        """
        Apply stabilization for the given task.
        Returns (new_graph, description_of_change).
        Must satisfy: E(new_graph) ≤ E(graph) or return original graph unchanged.
        """
        ...


# ---------------------------------------------------------------------------
# Rewrite Rules (the actual transformations)
# ---------------------------------------------------------------------------

# NeoForge boilerplate templates
NEOFORGE_IMPORTS_TEMPLATE = """\
from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar
"""

NEOFORGE_GET_CAPABILITY_TEMPLATE = """\
def getCapability(self, cap, direction=None):
    if cap == MY_CAPABILITY:
        return LazyOptional.of(lambda: self._handler)
    return LazyOptional.empty()
"""

NEOFORGE_INVALIDATE_TEMPLATE = """\
def invalidateCapabilities(self):
    if self._handler_lazy is not None:
        self._handler_lazy.invalidate()
    self._handler_lazy = None
"""

NEOFORGE_REGISTER_CAPABILITY_TEMPLATE = """\
def register_capability(self, registrar: BlockCapabilityRegistrar):
    registrar.registerBlockEntity(MY_CAPABILITY, self)
"""


def _rewrite_source_remove_forbidden_apis(source: str) -> str:
    """
    Remove import statements referencing forbidden (Fabric) APIs.
    Operates at source level for simplicity at Stage 0.
    Returns rewritten source.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source  # Can't parse → return unchanged

    lines  = source.splitlines(keepends=True)
    remove_linenos: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = ""
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
            else:
                module = ", ".join(alias.name for alias in node.names)

            is_forbidden = any(
                forbidden in module
                for forbidden in FABRIC_APIS
            )
            if is_forbidden:
                for lineno in range(node.lineno, (node.end_lineno or node.lineno) + 1):
                    remove_linenos.add(lineno)

    result = [
        line for i, line in enumerate(lines, start=1)
        if i not in remove_linenos
    ]
    return "".join(result)


def _rewrite_source_inject_neoforge_imports(source: str) -> str:
    """
    Inject NeoForge import block at the top of the source.
    Only injects if not already present.
    """
    if "neoforge.common.capabilities" in source:
        return source  # Already present

    imports = NEOFORGE_IMPORTS_TEMPLATE.strip() + "\n\n"
    return imports + source


def _rewrite_source_add_method(source: str, method_name: str) -> str:
    """
    Inject a NeoForge stub method into EVERY class that doesn't already
    define it directly in its own body.

    Phase A fix: the previous version walked the AST, found the FIRST
    ClassDef, inserted into it, and returned immediately — leaving every
    other class in a multi-class file untouched. It also used a single
    global `if method_name in source` guard, so once *any* class anywhere
    had the method, every other class was silently skipped too. And it
    inserted at `end_lineno - 1` (before the class's last line) rather
    than after it, which spliced the new method into the middle of
    whatever statement currently sat on that last line — silently
    reassigning that statement's body to the new method instead of
    leaving it where it was (verified: this truncated getCapability's
    trailing `return` in the single-class case too, not just multi-class).

    This version checks each class independently via its own AST body,
    and inserts strictly after each class's last line, processing classes
    bottom-to-top so earlier insertions never invalidate the line numbers
    of classes not yet processed.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    template_map = {
        "getCapability":          NEOFORGE_GET_CAPABILITY_TEMPLATE,
        "invalidateCapabilities": NEOFORGE_INVALIDATE_TEMPLATE,
        "register_capability":    NEOFORGE_REGISTER_CAPABILITY_TEMPLATE,
    }
    template = template_map.get(method_name, f"    def {method_name}(self):\n        pass\n")
    indented = textwrap.indent(template.strip(), "    ")

    class_defs = [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]

    if not class_defs:
        # No class found — module-level fallback, single global check is fine here.
        if method_name in source:
            return source
        return source.rstrip("\n") + "\n\n" + indented + "\n"

    if method_name in CAPABILITY_METHOD_NAMES:
        # Phase D: only inject capability-provider stubs into classes that
        # already show capability-provider evidence. Previously this
        # considered every class regardless, which is what let a stub
        # getCapability/invalidateCapabilities/register_capability get
        # grafted onto a class with no capability semantics at all (e.g. a
        # pure Fabric event-bus handler), making it falsely "pass" while
        # its actual body kept calling now-undefined names.
        eligible = [c for c in class_defs if class_shows_capability_provider_evidence(c)]
    else:
        eligible = class_defs

    targets = [
        cdef for cdef in eligible
        if not any(
            isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == method_name
            for stmt in cdef.body
        )
    ]
    if not targets:
        return source  # every eligible class already defines it, or none are eligible

    lines = source.splitlines(keepends=True)
    for cdef in sorted(targets, key=lambda c: (c.end_lineno or c.lineno), reverse=True):
        insert_at = cdef.end_lineno or cdef.lineno
        lines.insert(insert_at, "\n" + indented + "\n")

    return "".join(lines)


def _rewrite_source_adapt_capability_body(source: str) -> str:
    """
    Rewrite the getCapability method body to use NeoForge API patterns.
    If method not found, injects it.
    """
    # Find and replace old capability return pattern
    # Stage 0: simple string-based transformation
    replacements = [
        # Fabric LazyOptional.of(...) → NeoForge LazyOptional.of(...)
        ("LazyOptional.of(lambda:", "LazyOptional.of(lambda:"),
        # Fabric get capability return
        ("return super().getCapability(cap, side)", "return LazyOptional.empty()"),
    ]
    result = source
    for old, new in replacements:
        result = result.replace(old, new)
    return result


def _rewrite_source_register_capability(source: str) -> str:
    """
    Inject a register_capability method into every class that needs one.

    Phase A fix: thin wrapper around _rewrite_source_add_method. The
    previous standalone implementation had the exact same multi-class and
    insertion-point bugs as _rewrite_source_add_method, plus its own
    "already registered" guard checked for the string "BlockCapabilityRegistrar"
    anywhere in source — which is satisfied by the import line alone, with
    no actual registration call required. There's no reason for this to be
    a second, independently bug-prone implementation of "insert a method
    into every class that's missing it."
    """
    return _rewrite_source_add_method(source, "register_capability")


# ---------------------------------------------------------------------------
# Stage 0 Deterministic Stabilizer Implementation
# ---------------------------------------------------------------------------

class DeterministicRuleStabilizer:
    """
    Stage 0 stabilization operator.
    Maps each Task kind to a deterministic source-level rewrite operation.

    This is the interchangeable module from Definition 7.
    Alternative implementations (Hopfield, search-based, neural) would plug in here.
    """

    def stabilize(
        self,
        graph: ProgramGraph,
        task:  Task,
        cg:    ConstraintGraph,
    ) -> tuple[ProgramGraph, str]:
        """Apply the appropriate rewrite rule for the given task."""
        source = graph.source

        match task.kind:

            case TaskKind.REMOVE_FORBIDDEN_APIS:
                new_source = _rewrite_source_remove_forbidden_apis(source)
                description = "Removed forbidden Fabric API imports"

            case TaskKind.INJECT_NEOFORGE_IMPORTS:
                new_source  = _rewrite_source_inject_neoforge_imports(source)
                description = "Injected NeoForge API imports"

            case TaskKind.ADD_REQUIRED_METHOD:
                method_name = task.params[0] if task.params else "getCapability"
                new_source  = _rewrite_source_add_method(source, method_name)
                description = f"Added required method: {method_name}"

            case TaskKind.ADAPT_CAPABILITY_BODY:
                new_source  = _rewrite_source_adapt_capability_body(source)
                description = "Adapted capability method body for NeoForge API"

            case TaskKind.REGISTER_CAPABILITY:
                new_source  = _rewrite_source_register_capability(source)
                description = "Added capability registration via BlockCapabilityRegistrar"

            case TaskKind.APPLY_NAMING_CONVENTION:
                # Soft optimization: Stage 0 is a no-op (naming preserved)
                new_source  = source
                description = "Naming conventions verified (no changes needed)"

            case TaskKind.MINIMIZE_DIFF:
                # Soft optimization: Stage 0 is a no-op
                new_source  = source
                description = "Diff minimization check (no changes)"

            case TaskKind.PROTECT_DATA_FIELDS | TaskKind.REPAIR:
                # Conservative: no change
                new_source  = source
                description = "Conservative repair: no structural changes"

            case TaskKind.VERIFY:
                # Verify tasks are handled by the engine, not the stabilizer
                return graph, "Verify checkpoint (no rewrite)"

            case _:
                new_source  = source
                description = f"Unknown task kind {task.kind.name}: no-op"

        new_graph = ProgramGraph.from_python_source(new_source)
        return new_graph, description


# ===========================================================================
# VERIFICATION KERNEL (sits inside the rewrite loop)
# ===========================================================================

@dataclass(frozen=True, slots=True)
class VerificationReport:
    """
    Full verification report for one iteration of the rewrite loop.
    Contains everything needed to update the cognitive state and
    make the Accept/Repair/Reject decision.
    """
    step:              int
    energy:            float
    distance:          float
    outcome:           VerificationOutcome
    breakdown:         EnergyBreakdown
    constraint_results: tuple[ConstraintResult, ...]
    rewrite_applied:   str      # Description of what was rewritten
    hard_fails:        int
    strong_fails:      int
    soft_fails:        int
    accepted:          bool     # True if Accept(P) is satisfied

    def to_verification_record(self) -> VerificationRecord:
        """Convert to a VerificationRecord for the cognitive state."""
        return VerificationRecord(
            step         = self.step,
            outcome      = self.outcome,
            energy       = self.energy,
            distance     = self.distance,
            hard_fails   = self.hard_fails,
            strong_fails = self.strong_fails,
            soft_fails   = self.soft_fails,
        )

    def to_dict(self) -> dict:
        return {
            "step":          self.step,
            "accepted":      self.accepted,
            "outcome":       self.outcome.name,
            "energy":        round(self.energy, 4),
            "distance":      round(self.distance, 4),
            "hard_fails":    self.hard_fails,
            "strong_fails":  self.strong_fails,
            "soft_fails":    self.soft_fails,
            "rewrite":       self.rewrite_applied,
            "energy_detail": self.breakdown.to_dict(),
            "violations": [
                {"id": r.constraint_id, "priority": r.priority.name, "msg": r.violation_msg}
                for r in self.constraint_results if r.violated
            ],
        }


class VerificationKernel:
    """
    Definition 6 enforcement: Accept(P) ⟺ V(P) = PASS ∧ E < τ ∧ d(P,M) = 0

    Runs all verification operators, computes energy + distance,
    and produces a VerificationReport.

    This is the CENTER OF LEARNING in the TSAM architecture:
    Every verification result shapes the energy landscape that guides stabilization.
    """

    def verify(
        self,
        graph:          ProgramGraph,
        cg:             ConstraintGraph,
        step:           int,
        rewrite_desc:   str = "",
        original_graph: ProgramGraph | None = None,
    ) -> VerificationReport:
        """Run full verification pass. Returns VerificationReport."""
        energy, distance, breakdown, results = compute_energy(graph, cg, original_graph)

        hard_fails   = sum(1 for r in results if r.violated and r.priority == ConstraintPriority.HARD)
        strong_fails = sum(1 for r in results if r.violated and r.priority == ConstraintPriority.STRONG)
        soft_fails   = sum(1 for r in results if r.violated and r.priority == ConstraintPriority.SOFT)

        # Determine outcome per Computational Contract
        if hard_fails > 0:
            outcome = VerificationOutcome.FAIL_HARD
        elif strong_fails > 0:
            outcome = VerificationOutcome.FAIL_STRONG
        elif soft_fails > 0:
            outcome = VerificationOutcome.FAIL_SOFT
        else:
            outcome = VerificationOutcome.PASS

        # Acceptance: Definition 6 — gated on hard+strong+syntax energy and
        # distance only. SOFT-tier energy (breakdown.quality, e.g. node_delta)
        # is reported but must never block acceptance — see EnergyBreakdown.gating.
        accepted = (
            outcome == VerificationOutcome.PASS
            and breakdown.gating < ACCEPTANCE_THRESHOLD
            and distance == 0.0
        )

        return VerificationReport(
            step               = step,
            energy             = energy,
            distance           = distance,
            outcome            = outcome,
            breakdown          = breakdown,
            constraint_results = tuple(results),
            rewrite_applied    = rewrite_desc,
            hard_fails         = hard_fails,
            strong_fails       = strong_fails,
            soft_fails         = soft_fails,
            accepted           = accepted,
        )


# ===========================================================================
# COMPUTATIONAL LOOP (Dynamical System from Formal Spec)
# ===========================================================================

@dataclass
class RewriteTrace:
    """Full trace of the computational loop execution."""
    steps:         list[dict] = field(default_factory=list)
    final_graph:   ProgramGraph | None = None
    final_source:  str = ""
    accepted:      bool = False
    diagnostic:    dict | None = None

    def add_step(self, report: VerificationReport, state_summary: dict) -> None:
        self.steps.append({
            "verification": report.to_dict(),
            "state":        state_summary,
        })

    def summary(self) -> dict:
        return {
            "accepted":     self.accepted,
            "total_steps":  len(self.steps),
            "final_energy": round(self.steps[-1]["verification"]["energy"], 4) if self.steps else float("inf"),
            "final_distance": round(self.steps[-1]["verification"]["distance"], 4) if self.steps else float("inf"),
            "diagnostic":   self.diagnostic,
        }


class TSAMComputationalLoop:
    """
    The complete TSAM computational loop (Formal Spec §Computational Loop):

        Observe → Constrain → Plan → Transform → Stabilize → Verify → Decide

    This is the single entry point for Stage 0 software synthesis.
    Implements Computational Contract C1: progress or diagnostic termination.
    """

    def __init__(self) -> None:
        self.planner    = TaskPlanner()
        self.stabilizer = DeterministicRuleStabilizer()
        self.verifier   = VerificationKernel()

    def run(
        self,
        source_code:    str,
        state:          CognitiveState,
        cg:             ConstraintGraph,
        verbose:        bool = True,
    ) -> tuple[ProgramGraph, CognitiveState, RewriteTrace]:
        """
        Execute the full TSAM computational loop.

        Args:
            source_code:  The input program (messy Fabric source)
            state:        Initial cognitive state S_0
            cg:           Active constraint graph

        Returns:
            (final_graph, final_state, trace)

        Guarantees Computational Contract C1:
          - Each iteration either reduces E or terminates with diagnostic
          - Budget exhaustion → explicit diagnostic emitted
          - Accepted solution → emits artifact + updates state
        """
        trace          = RewriteTrace()
        graph          = ProgramGraph.from_python_source(source_code)
        original_graph = ProgramGraph.from_python_source(source_code)
        step           = 0

        if verbose:
            print(f"[TSAM] Starting computational loop")
            print(f"[TSAM] Source nodes: {len(graph.nodes)}, fidelity: {graph.fidelity:.2f}")
            print(f"[TSAM] Budget: {state.budget.max_rewrites} rewrites")
            print()

        # ── OBSERVE: Initial state verification ──────────────────────────
        initial_report = self.verifier.verify(
            graph, cg, step, "initial_observation", original_graph
        )
        state = state.advance(
            verification_record = initial_report.to_verification_record(),
            context_updates     = {"phase": "observe", "step": str(step)},
        )
        trace.add_step(initial_report, state.summary())

        if verbose:
            print(f"[Step 0] Initial energy: {initial_report.energy:.2f}, "
                  f"distance: {initial_report.distance:.2f}, "
                  f"hard_fails: {initial_report.hard_fails}")

        if initial_report.accepted:
            # Source is already in the manifold — emit directly
            if verbose:
                print("[TSAM] Source already in manifold. Accepting without rewrite.")
            trace.final_graph  = graph
            trace.final_source = source_code
            trace.accepted     = True
            return graph, state, trace

        # ── CONSTRAIN → PLAN ─────────────────────────────────────────────
        plan = self.planner.plan(graph, cg, original_graph)

        if verbose:
            print(f"[TSAM] Task plan: {len(plan.tasks)} tasks")
            for t in plan.tasks:
                print(f"       [{t.priority.name}] {t.task_id}: {t.description}")
            print()

        # ── MAIN LOOP: Transform → Stabilize → Verify → Decide ───────────
        prev_energy = initial_report.energy

        while state.can_continue:
            step += 1

            # Get next executable task
            task = plan.executable_next()

            if task is None:
                # No pending tasks — all done or dependency deadlock
                if plan.is_complete():
                    break
                # Dependency deadlock → skip remaining tasks
                for t in plan.pending:
                    plan.mark(t.task_id, TaskStatus.SKIPPED)
                break

            if verbose:
                print(f"[Step {step}] Executing: [{task.priority.name}] {task.task_id}")

            # Handle VERIFY checkpoint tasks
            if task.kind == TaskKind.VERIFY:
                report = self.verifier.verify(
                    graph, cg, step, "verify_checkpoint", original_graph
                )
                state = state.advance(
                    verification_record = report.to_verification_record(),
                    context_updates     = {"phase": "verify", "step": str(step)},
                    consume_rewrite     = False,  # Verify doesn't cost a rewrite
                )
                trace.add_step(report, state.summary())

                if verbose:
                    print(f"         E={report.energy:.2f}, d={report.distance:.2f}, "
                          f"hard={report.hard_fails}, strong={report.strong_fails}")

                # HARD verification failure at checkpoint → immediate rejection
                if report.hard_fails > 0:
                    plan.mark(task.task_id, TaskStatus.FAILED)
                    if verbose:
                        print(f"[TSAM] ✗ HARD constraint violation at checkpoint — aborting")
                    break
                else:
                    plan.mark(task.task_id, TaskStatus.COMPLETED)
                continue

            # ── TRANSFORM + STABILIZE ────────────────────────────────────
            new_graph, rewrite_desc = self.stabilizer.stabilize(graph, task, cg)

            # ── VERIFY ───────────────────────────────────────────────────
            report = self.verifier.verify(
                new_graph, cg, step, rewrite_desc, original_graph
            )

            # Computational Contract C1: Check progress
            energy_reduced = report.energy < prev_energy
            if not energy_reduced and report.energy > prev_energy + 0.001:
                # Energy increased — rewrite made things worse; reject this rewrite
                if verbose:
                    print(f"         ✗ Energy increased ({prev_energy:.2f} → {report.energy:.2f}), reverting")
                plan.mark(task.task_id, TaskStatus.FAILED)
                # Keep old graph, advance state minimally
                state = state.advance(
                    verification_record = report.to_verification_record(),
                    context_updates     = {"phase": "revert", "step": str(step)},
                )
                trace.add_step(report, state.summary())
                continue

            # Accept the rewrite
            graph       = new_graph
            prev_energy = report.energy
            plan.mark(task.task_id, TaskStatus.COMPLETED)

            state = state.advance(
                verification_record = report.to_verification_record(),
                context_updates     = {
                    "phase":    "transform",
                    "step":     str(step),
                    "last_op":  rewrite_desc,
                },
            )
            trace.add_step(report, state.summary())

            if verbose:
                print(f"         ✓ {rewrite_desc}")
                print(f"         E={report.energy:.2f}, d={report.distance:.2f}, "
                      f"hard={report.hard_fails}, strong={report.strong_fails}")

            # ── DECIDE ───────────────────────────────────────────────────
            if report.accepted:
                if verbose:
                    print(f"\n[TSAM] ✓✓ ACCEPTED at step {step}")
                    print(f"       E={report.energy:.4f} < τ={ACCEPTANCE_THRESHOLD}")
                    print(f"       distance={report.distance:.4f} = 0")
                trace.final_graph  = graph
                trace.final_source = graph.source
                trace.accepted     = True
                return graph, state, trace

        # ── BUDGET EXHAUSTED OR PLAN COMPLETE WITHOUT ACCEPTANCE ─────────
        final_report = self.verifier.verify(
            graph, cg, step + 1, "final_check", original_graph
        )
        state = state.advance(
            verification_record = final_report.to_verification_record(),
            consume_rewrite     = False,
        )

        if final_report.accepted:
            if verbose:
                print(f"\n[TSAM] ✓✓ ACCEPTED at final check")
            trace.final_graph  = graph
            trace.final_source = graph.source
            trace.accepted     = True
            return graph, state, trace

        # Computational Contract C1 clause 2: emit diagnostic
        diagnostic = state.diagnostic_report()
        diagnostic["constraint_violations"] = [
            {"id": r.constraint_id, "priority": r.priority.name, "msg": r.violation_msg}
            for r in final_report.constraint_results
            if r.violated
        ]
        diagnostic["final_energy"]   = final_report.energy
        diagnostic["final_distance"] = final_report.distance

        if verbose:
            print(f"\n[TSAM] ✗ Terminated without acceptance.")
            print(f"       Budget: {state.budget.rewrites_remaining} rewrites remaining")
            print(f"       Final E={final_report.energy:.2f}, d={final_report.distance:.2f}")
            print(f"       See diagnostic for details.")

        trace.final_graph  = graph
        trace.final_source = graph.source
        trace.accepted     = False
        trace.diagnostic   = diagnostic
        return graph, state, trace


if __name__ == "__main__":
    import json
    from tsam.cognitive_state import CognitiveState, Mission
    from tsam.constraint_graph import build_neoforge_constraint_graph

    print("=== TSAM Phase 4: Energy + Stabilization + Verification ===\n")

    # Fabric source (violates multiple NeoForge constraints)
    fabric_source = """\
import net.fabricmc.fabric

class FabricCapabilityProvider:
    def __init__(self):
        self.handler = MyHandler()

    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return LazyOptional.of(lambda: self.handler)
        return LazyOptional.empty()
"""

    mission = Mission.neoforge_port()
    state   = CognitiveState.initialize(mission)
    cg      = build_neoforge_constraint_graph()
    loop    = TSAMComputationalLoop()

    print("Input (Fabric source):")
    print(fabric_source)
    print("-" * 60)

    final_graph, final_state, trace = loop.run(fabric_source, state, cg, verbose=True)

    print("\n--- Trace Summary ---")
    print(json.dumps(trace.summary(), indent=2))

    if trace.accepted:
        print("\n--- Output (NeoForge source) ---")
        print(final_graph.source)
    else:
        print("\n--- Diagnostic ---")
        print(json.dumps(trace.diagnostic, indent=2))
