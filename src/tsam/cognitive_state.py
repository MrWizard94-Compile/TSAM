"""
TSAM Stage 0 — Phase 1: Cognitive State Engine
===============================================
Implements Definition 1 from TSAM Formal Specification v0.2:

    S_t = (M_t, C_t, F_t, V_t, B_t, R_t)

This is the executive scheduler — NOT a knowledge store.
Knowledge lives in manifolds and rewrite rules.

Design decisions (Python 3.12 best practices):
- frozen=True  → immutable state snapshots (no mutable state bugs)
- slots=True   → memory-efficient, faster attribute access
- Protocols    → interface contracts without inheritance coupling
- All state vectors have FIXED size (constant memory invariant)
"""

from __future__ import annotations

import sys
import time
import tracemalloc
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Priority levels from Definition 2 (Constraint Graph)
# ---------------------------------------------------------------------------

class ConstraintPriority(Enum):
    """
    Three-tier priority from Formal Spec Definition 2.
    HARD   → immediate rejection on violation
    STRONG → large energy penalty + justification required
    SOFT   → tie-breaking and optimization only
    """
    HARD   = auto()
    STRONG = auto()
    SOFT   = auto()


class VerificationOutcome(Enum):
    """Outcome of a single verification pass."""
    PASS        = auto()  # All checks satisfied
    FAIL_HARD   = auto()  # Hard constraint violated → reject immediately
    FAIL_STRONG = auto()  # Strong constraint violated → repair attempt
    FAIL_SOFT   = auto()  # Soft constraint violated → continue with penalty
    PENDING     = auto()  # Not yet verified


# ---------------------------------------------------------------------------
# Fixed-capacity ring buffer for Recent Verification Results (V_t)
# Constant size — never grows regardless of iteration count
# ---------------------------------------------------------------------------

MAX_RECENT_VERIFICATIONS: int = 8   # Fixed ring buffer size

@dataclass(frozen=True, slots=True)
class VerificationRecord:
    """
    One entry in the recent verification ring buffer.
    All fields fixed-width types → constant memory.
    """
    step:        int
    outcome:     VerificationOutcome
    energy:      float            # E at this step (scalar for Stage 0)
    distance:    float            # d(P, M) at this step
    hard_fails:  int              # count of hard constraint violations
    strong_fails: int             # count of strong constraint violations
    soft_fails:  int              # count of soft constraint violations
    timestamp:   float = field(default_factory=time.monotonic)

    def improved_over(self, other: "VerificationRecord") -> bool:
        """True if this record shows strict improvement over other."""
        return self.energy < other.energy and self.distance <= other.distance


@dataclass(frozen=True, slots=True)
class VerificationSummary:
    """
    V_t: Compact, fixed-size record of recent verification outcomes.
    Ring buffer of MAX_RECENT_VERIFICATIONS entries — NEVER grows.
    """
    records:        tuple[VerificationRecord, ...]  # Ring buffer (max 8)
    best_energy:    float                           # Best E seen so far
    best_distance:  float                           # Best d seen so far
    total_steps:    int                             # Total steps since init
    consecutive_fails: int                          # Consecutive fail count

    @classmethod
    def empty(cls) -> "VerificationSummary":
        return cls(
            records=(),
            best_energy=float("inf"),
            best_distance=float("inf"),
            total_steps=0,
            consecutive_fails=0,
        )

    def update(self, record: VerificationRecord) -> "VerificationSummary":
        """Add a new record; evict oldest if ring buffer full."""
        new_records = self.records[-MAX_RECENT_VERIFICATIONS + 1:] + (record,)
        new_best_e  = min(self.best_energy, record.energy)
        new_best_d  = min(self.best_distance, record.distance)
        new_fails   = (
            0 if record.outcome == VerificationOutcome.PASS
            else self.consecutive_fails + 1
        )
        return VerificationSummary(
            records=new_records,
            best_energy=new_best_e,
            best_distance=new_best_d,
            total_steps=self.total_steps + 1,
            consecutive_fails=new_fails,
        )

    @property
    def last(self) -> VerificationRecord | None:
        return self.records[-1] if self.records else None

    @property
    def is_improving(self) -> bool:
        """True if last two records show strict energy improvement."""
        if len(self.records) < 2:
            return True
        return self.records[-1].energy < self.records[-2].energy


# ---------------------------------------------------------------------------
# Resource Monitor (R_t)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResourceSnapshot:
    """
    R_t: Monitored resource consumption at a point in time.
    All values are scalars — constant memory.
    """
    peak_memory_bytes:    int    # Peak RSS since start (from tracemalloc)
    current_memory_bytes: int    # Current RSS
    elapsed_seconds:      float  # Wall-clock seconds since loop start
    rewrite_ops_done:     int    # Rewrite operations completed

    @classmethod
    def capture(cls, rewrite_ops_done: int = 0) -> "ResourceSnapshot":
        """Capture current resource state."""
        current, peak = tracemalloc.get_traced_memory()
        return cls(
            peak_memory_bytes    = peak,
            current_memory_bytes = current,
            elapsed_seconds      = time.monotonic(),
            rewrite_ops_done     = rewrite_ops_done,
        )


# ---------------------------------------------------------------------------
# Execution Budget (B_t)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ExecutionBudget:
    """
    B_t: Remaining execution budget.
    When exhausted, the Computational Contract C1 mandates termination + diagnostic.
    """
    max_rewrites:        int    # Total allowed rewrite attempts
    rewrites_remaining:  int    # Remaining rewrite attempts
    max_repair_passes:   int    # Total allowed repair passes per cycle
    repairs_remaining:   int    # Remaining repair passes this cycle
    exhausted:          bool = False

    @classmethod
    def default(cls, max_rewrites: int = 50, max_repair_passes: int = 5) -> "ExecutionBudget":
        return cls(
            max_rewrites       = max_rewrites,
            rewrites_remaining = max_rewrites,
            max_repair_passes  = max_repair_passes,
            repairs_remaining  = max_repair_passes,
        )

    def consume_rewrite(self) -> "ExecutionBudget":
        """Deduct one rewrite attempt; mark exhausted if depleted."""
        remaining = self.rewrites_remaining - 1
        return replace(
            self,
            rewrites_remaining = max(0, remaining),
            exhausted          = remaining <= 0,
        )

    def consume_repair(self) -> "ExecutionBudget":
        """Deduct one repair pass."""
        remaining = self.repairs_remaining - 1
        return replace(
            self,
            repairs_remaining = max(0, remaining),
        )

    def reset_repairs(self) -> "ExecutionBudget":
        """Reset repair counter for a new rewrite cycle."""
        return replace(self, repairs_remaining=self.max_repair_passes)

    @property
    def has_rewrites(self) -> bool:
        return self.rewrites_remaining > 0 and not self.exhausted

    @property
    def has_repairs(self) -> bool:
        return self.repairs_remaining > 0


# ---------------------------------------------------------------------------
# Mission (M_t): Persistent high-level intent
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Mission:
    """
    M_t: Persistent high-level intent or constraint set.
    Stable across multiple tasks in a session.
    """
    description:          str
    source_framework:     str        # e.g. "Fabric", "Forge 1.12"
    target_framework:     str        # e.g. "NeoForge 1.20.1"
    hard_constraint_ids:  tuple[str, ...]  # IDs of HARD constraints
    strong_constraint_ids: tuple[str, ...] # IDs of STRONG constraints
    soft_constraint_ids:  tuple[str, ...]  # IDs of SOFT constraints

    @classmethod
    def neoforge_port(
        cls,
        source: str = "Fabric 1.19",
        target: str = "NeoForge 1.20.1",
    ) -> "Mission":
        """Factory for the Stage 0 benchmark mission."""
        return cls(
            description          = f"Port {source} capability provider to {target}",
            source_framework     = source,
            target_framework     = target,
            hard_constraint_ids  = (
                "MUST_COMPILE",
                "MUST_USE_NEOFORGE_APIS",
                "MUST_PRESERVE_SAVES",
                "MUST_NOT_USE_FABRIC_APIS",
            ),
            strong_constraint_ids = (
                "MUST_PRESERVE_BEHAVIOR",
                "MUST_REGISTER_CAPABILITY",
            ),
            soft_constraint_ids   = (
                "MINIMIZE_DIFF_SIZE",
                "FOLLOW_NEOFORGE_NAMING",
            ),
        )


# ---------------------------------------------------------------------------
# Context (C_t): Bounded relevant history
# ---------------------------------------------------------------------------

MAX_CONTEXT_ITEMS: int = 16   # Fixed bound — constant memory

@dataclass(frozen=True, slots=True)
class ContextEntry:
    """Single bounded context item."""
    key:   str
    value: str    # Always a string for constant-size guarantee
    step:  int


@dataclass(frozen=True, slots=True)
class BoundedContext:
    """
    C_t: Bounded relevant history and environmental facts.
    Hard cap of MAX_CONTEXT_ITEMS — evicts oldest on overflow.
    """
    entries: tuple[ContextEntry, ...]

    @classmethod
    def empty(cls) -> "BoundedContext":
        return cls(entries=())

    def set(self, key: str, value: str, step: int) -> "BoundedContext":
        """Add/update entry; evict oldest if at capacity."""
        # Remove existing entry with same key (update semantics)
        filtered = tuple(e for e in self.entries if e.key != key)
        new_entry = ContextEntry(key=key, value=value, step=step)
        combined  = filtered + (new_entry,)
        # Evict oldest if overflow
        if len(combined) > MAX_CONTEXT_ITEMS:
            combined = combined[-MAX_CONTEXT_ITEMS:]
        return BoundedContext(entries=combined)

    def get(self, key: str) -> str | None:
        for entry in reversed(self.entries):
            if entry.key == key:
                return entry.value
        return None

    def __len__(self) -> int:
        return len(self.entries)


# ---------------------------------------------------------------------------
# Focus (F_t): Current region under active transformation
# ---------------------------------------------------------------------------

class FocusLevel(Enum):
    """Hierarchy level currently under focus."""
    WORKSPACE  = auto()
    PROJECT    = auto()
    MODULE     = auto()
    CLASS      = auto()
    METHOD     = auto()
    STATEMENT  = auto()


@dataclass(frozen=True, slots=True)
class Focus:
    """
    F_t: The current region of the graph under active transformation.
    Single scalar focus point — constant memory.
    """
    level:          FocusLevel
    element_id:     str         # Unique ID of element under focus
    element_type:   str         # e.g. "capability_provider", "event_handler"
    confidence:     float       # 0.0–1.0: how well intent maps to focus region

    @classmethod
    def top_level(cls) -> "Focus":
        return cls(
            level        = FocusLevel.MODULE,
            element_id   = "root",
            element_type = "program_graph",
            confidence   = 1.0,
        )

    def refine(
        self,
        level: FocusLevel,
        element_id: str,
        element_type: str,
        confidence: float,
    ) -> "Focus":
        return Focus(
            level        = level,
            element_id   = element_id,
            element_type = element_type,
            confidence   = max(0.0, min(1.0, confidence)),
        )


# ---------------------------------------------------------------------------
# COGNITIVE STATE  S_t = (M_t, C_t, F_t, V_t, B_t, R_t)
# This is the root object of Phase 1.
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CognitiveState:
    """
    Definition 1 (Formal Spec v0.2):

        S_t = (M_t, C_t, F_t, V_t, B_t, R_t)

    Implements the executive scheduler for the TSAM computational loop.
    Immutable value object — each transition creates a new instance via replace().

    CONSTANT MEMORY GUARANTEE:
      Every field has a fixed maximum size. The object size in memory is
      strictly bounded regardless of iteration count.
    """
    mission:     Mission           # M_t — persistent intent
    context:     BoundedContext    # C_t — bounded history
    focus:       Focus             # F_t — current transformation region
    verification: VerificationSummary  # V_t — recent verification outcomes
    budget:      ExecutionBudget   # B_t — remaining execution budget
    resources:   ResourceSnapshot  # R_t — monitored resource consumption
    step:        int               # Current loop step t

    @classmethod
    def initialize(
        cls,
        mission: Mission,
        max_rewrites: int = 50,
        max_repair_passes: int = 5,
    ) -> "CognitiveState":
        """Create initial cognitive state S_0."""
        tracemalloc.start()
        return cls(
            mission      = mission,
            context      = BoundedContext.empty(),
            focus        = Focus.top_level(),
            verification = VerificationSummary.empty(),
            budget       = ExecutionBudget.default(max_rewrites, max_repair_passes),
            resources    = ResourceSnapshot.capture(0),
            step         = 0,
        )

    def advance(
        self,
        verification_record: VerificationRecord | None = None,
        new_focus: Focus | None = None,
        context_updates: dict[str, str] | None = None,
        consume_rewrite: bool = True,
    ) -> "CognitiveState":
        """
        State transition: S_t → S_{t+1}
        Implements the evolution governed by transition function F.
        """
        new_step = self.step + 1
        new_budget = self.budget.consume_rewrite() if consume_rewrite else self.budget
        new_verification = (
            self.verification.update(verification_record)
            if verification_record else self.verification
        )
        new_context = self.context
        if context_updates:
            for k, v in context_updates.items():
                new_context = new_context.set(k, v, new_step)
        new_resources = ResourceSnapshot.capture(
            self.resources.rewrite_ops_done + (1 if consume_rewrite else 0)
        )
        return CognitiveState(
            mission      = self.mission,
            context      = new_context,
            focus        = new_focus if new_focus else self.focus,
            verification = new_verification,
            budget       = new_budget,
            resources    = new_resources,
            step         = new_step,
        )

    # ------------------------------------------------------------------
    # Decision properties (drive the Computational Loop)
    # ------------------------------------------------------------------

    @property
    def can_continue(self) -> bool:
        """True if the budget allows another rewrite attempt."""
        return self.budget.has_rewrites

    @property
    def must_terminate(self) -> bool:
        """True if budget is exhausted (Computational Contract C1 clause 2)."""
        return not self.budget.has_rewrites

    @property
    def last_verification(self) -> VerificationRecord | None:
        return self.verification.last

    @property
    def last_energy(self) -> float:
        v = self.last_verification
        return v.energy if v else float("inf")

    @property
    def last_distance(self) -> float:
        v = self.last_verification
        return v.distance if v else float("inf")

    @property
    def is_improving(self) -> bool:
        return self.verification.is_improving

    def diagnostic_report(self) -> dict:
        """
        Machine-readable diagnostic per Computational Contract C1 clause 2.
        Emitted on termination without solution.
        """
        return {
            "tsam_diagnostic": True,
            "step":            self.step,
            "budget_exhausted": self.budget.exhausted,
            "rewrites_used":   self.budget.max_rewrites - self.budget.rewrites_remaining,
            "rewrites_remaining": self.budget.rewrites_remaining,
            "last_energy":     self.last_energy,
            "last_distance":   self.last_distance,
            "best_energy":     self.verification.best_energy,
            "best_distance":   self.verification.best_distance,
            "total_verify_steps": self.verification.total_steps,
            "consecutive_fails": self.verification.consecutive_fails,
            "focus_element":   self.focus.element_id,
            "focus_level":     self.focus.level.name,
            "memory_peak_bytes": self.resources.peak_memory_bytes,
            "reason":          (
                "Budget exhausted without reaching verified manifold. "
                "See energy/distance trajectory above."
            ),
        }

    def summary(self) -> dict:
        """Human-readable state summary for tracing."""
        return {
            "step":          self.step,
            "energy":        round(self.last_energy, 4),
            "distance":      round(self.last_distance, 4),
            "rewrites_left": self.budget.rewrites_remaining,
            "improving":     self.is_improving,
            "focus":         f"{self.focus.level.name}/{self.focus.element_id}",
            "memory_kb":     self.resources.current_memory_bytes // 1024,
        }


# ---------------------------------------------------------------------------
# Constant-Memory Proof Harness
# ---------------------------------------------------------------------------

def prove_constant_memory(
    iterations: int = 100,
    mission: Mission | None = None,
) -> dict:
    """
    Empirically verify that CognitiveState memory usage is bounded.
    
    Creates a state and advances it `iterations` times, recording memory at each
    step. If memory stays within the allowed drift window, the proof passes.
    
    Returns a report dict including pass/fail and memory trajectory.
    """
    if mission is None:
        mission = Mission.neoforge_port()

    tracemalloc.start()
    state   = CognitiveState.initialize(mission)
    samples = []

    for i in range(iterations):
        # Simulate a verification step
        rec = VerificationRecord(
            step         = i,
            outcome      = VerificationOutcome.PASS if i % 3 != 0 else VerificationOutcome.FAIL_STRONG,
            energy       = max(0.0, 10.0 - i * 0.1),
            distance     = max(0.0, 5.0 - i * 0.05),
            hard_fails   = 0,
            strong_fails = 0 if i % 3 != 0 else 1,
            soft_fails   = 0,
        )
        state = state.advance(
            verification_record = rec,
            context_updates     = {"last_op": f"rewrite_{i}"},
        )
        _, peak = tracemalloc.get_traced_memory()
        samples.append(peak)

    tracemalloc.stop()

    # Analyze: after initial warm-up (first 10 steps), peak should plateau
    warmup_cutoff = min(10, len(samples) // 5)
    plateau       = samples[warmup_cutoff:]
    initial_peak  = samples[0]
    max_peak      = max(plateau)
    drift_bytes   = max_peak - initial_peak
    drift_kb      = drift_bytes / 1024

    # Allow up to 64 KB drift (ring buffers, tuple allocation overhead)
    DRIFT_LIMIT_KB = 64.0
    passed         = drift_kb <= DRIFT_LIMIT_KB

    return {
        "test":           "constant_memory_proof",
        "passed":         passed,
        "iterations":     iterations,
        "initial_peak_kb": round(initial_peak / 1024, 2),
        "max_peak_kb":    round(max_peak / 1024, 2),
        "drift_kb":       round(drift_kb, 2),
        "drift_limit_kb": DRIFT_LIMIT_KB,
        "verdict":        "PASS: Memory is bounded" if passed else f"FAIL: Drift {drift_kb:.1f} KB exceeds limit",
    }


# ---------------------------------------------------------------------------
# Protocol: anything that can receive a CognitiveState update
# ---------------------------------------------------------------------------

@runtime_checkable
class CognitiveStateObserver(Protocol):
    """Observer contract for components that react to state transitions."""
    def on_state_transition(
        self,
        previous: CognitiveState,
        current:  CognitiveState,
    ) -> None: ...


if __name__ == "__main__":
    import json
    print("=== TSAM Phase 1: Cognitive State Engine ===\n")

    mission = Mission.neoforge_port()
    state   = CognitiveState.initialize(mission)

    print("Initial state:")
    print(json.dumps(state.summary(), indent=2))
    print()

    # Simulate a few steps
    for i in range(5):
        rec = VerificationRecord(
            step         = i,
            outcome      = VerificationOutcome.PASS if i < 3 else VerificationOutcome.FAIL_STRONG,
            energy       = max(0.0, 5.0 - i),
            distance     = max(0.0, 2.5 - i * 0.5),
            hard_fails   = 0,
            strong_fails = 0 if i < 3 else 1,
            soft_fails   = 0,
        )
        state = state.advance(verification_record=rec)

    print("After 5 steps:")
    print(json.dumps(state.summary(), indent=2))
    print()

    print("Running constant-memory proof (100 iterations)...")
    proof = prove_constant_memory(iterations=100)
    print(json.dumps(proof, indent=2))
