"""
TSAM Stage 0 — Phase 3.5: Lightweight Task Planner
====================================================
Deterministic task decomposition from a Constraint Graph.

Design: The planner takes the Constraint Graph and the current ProgramGraph
and produces an ordered list of tasks that, when executed, drive the program
toward the Verified Solution Manifold.

Key properties:
- Deterministic: same inputs → same task order, always
- No neural components, no search, no heuristics
- Hard constraints map to mandatory tasks
- Strong constraints map to high-priority repair tasks
- Soft constraints map to optimization tasks

This satisfies the v0.3 reviewer's call for a Planner layer.
Full learned/search-based planner is post-v0.1 work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Iterator

from tsam.cognitive_state import ConstraintPriority
from tsam.constraint_graph import (
    Constraint,
    ConstraintGraph,
    ConstraintResult,
    NodeKind,
    ProgramGraph,
    check_all_constraints,
)


# ---------------------------------------------------------------------------
# Task kinds
# ---------------------------------------------------------------------------

class TaskKind(Enum):
    """The fundamental kinds of rewrite tasks the planner can emit."""
    # Mandatory tasks (from HARD constraints)
    REMOVE_FORBIDDEN_APIS     = auto()  # Strip Fabric API references
    INJECT_NEOFORGE_IMPORTS   = auto()  # Add required NeoForge imports
    ADD_REQUIRED_METHOD       = auto()  # Add missing method (getCapability, etc.)
    PROTECT_DATA_FIELDS       = auto()  # Ensure persistent fields are preserved

    # High-priority tasks (from STRONG constraints)
    ADAPT_CAPABILITY_BODY     = auto()  # Rewrite method body for NeoForge API
    REGISTER_CAPABILITY       = auto()  # Add capability registration call
    CLEAN_DANGLING_FABRIC_REFS = auto()  # Remove leftover Fabric calls from capability-provider class bodies

    # Optimization tasks (from SOFT constraints)
    MINIMIZE_DIFF             = auto()  # Minimize AST node changes
    APPLY_NAMING_CONVENTION   = auto()  # PascalCase classes, camelCase methods

    # Internal control
    VERIFY                    = auto()  # Run verification (injected by engine)
    REPAIR                    = auto()  # Generic repair pass


class TaskStatus(Enum):
    """Execution status of a task."""
    PENDING   = auto()
    RUNNING   = auto()
    COMPLETED = auto()
    FAILED    = auto()
    SKIPPED   = auto()


@dataclass(frozen=True, slots=True)
class Task:
    """
    A single deterministic rewrite task.
    Produced by the planner; executed by the Rewrite Engine (Phase 4).
    """
    task_id:         str
    kind:            TaskKind
    priority:        ConstraintPriority
    source_constraint: str    # constraint_id that generated this task
    description:     str
    params:          tuple[str, ...]  # Immutable params for the rewrite operation
    depends_on:      tuple[str, ...]  # task_ids that must complete before this

    def __hash__(self) -> int:
        return hash(self.task_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Task) and self.task_id == other.task_id


@dataclass
class TaskPlan:
    """
    Ordered list of tasks produced by the planner.
    Execution order respects: HARD → STRONG → SOFT priority, then dependency order.
    """
    tasks:        list[Task]          = field(default_factory=list)
    status_map:   dict[str, TaskStatus] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for t in self.tasks:
            self.status_map.setdefault(t.task_id, TaskStatus.PENDING)

    @property
    def pending(self) -> list[Task]:
        return [t for t in self.tasks if self.status_map[t.task_id] == TaskStatus.PENDING]

    @property
    def completed(self) -> list[Task]:
        return [t for t in self.tasks if self.status_map[t.task_id] == TaskStatus.COMPLETED]

    @property
    def failed(self) -> list[Task]:
        return [t for t in self.tasks if self.status_map[t.task_id] == TaskStatus.FAILED]

    def mark(self, task_id: str, status: TaskStatus) -> None:
        self.status_map[task_id] = status

    def is_complete(self) -> bool:
        return all(
            self.status_map[t.task_id] in (TaskStatus.COMPLETED, TaskStatus.SKIPPED)
            for t in self.tasks
        )

    def has_failed_hard(self) -> bool:
        """True if any HARD-priority task has failed (forces rejection)."""
        return any(
            self.status_map[t.task_id] == TaskStatus.FAILED
            and t.priority == ConstraintPriority.HARD
            for t in self.tasks
        )

    def executable_next(self) -> Task | None:
        """
        Return the next task that is pending and has all dependencies satisfied.
        Deterministic: always picks the first such task in order.
        """
        completed_ids = {t.task_id for t in self.completed}
        for t in self.pending:
            if all(dep in completed_ids for dep in t.depends_on):
                return t
        return None

    def summary(self) -> dict:
        counts: dict[str, int] = {s.name: 0 for s in TaskStatus}
        for s in self.status_map.values():
            counts[s.name] += 1
        return {
            "total":  len(self.tasks),
            "counts": counts,
            "tasks":  [
                {
                    "id":       t.task_id,
                    "kind":     t.kind.name,
                    "priority": t.priority.name,
                    "status":   self.status_map[t.task_id].name,
                    "desc":     t.description,
                }
                for t in self.tasks
            ],
        }


# ---------------------------------------------------------------------------
# The Planner
# ---------------------------------------------------------------------------

class TaskPlanner:
    """
    Deterministic task decomposer.

    Algorithm:
    1. Run all constraints against the current graph → get ConstraintResults
    2. For each violated constraint, generate the appropriate repair task
    3. For satisfied soft constraints, generate optional optimization tasks
    4. Sort: HARD first, then STRONG, then SOFT (stable sort within each tier)
    5. Inject VERIFY tasks at key checkpoints
    6. Return ordered TaskPlan

    Guarantee: same (graph, constraint_graph) → same TaskPlan, always.
    """

    def plan(
        self,
        graph:    ProgramGraph,
        cg:       ConstraintGraph,
        original: ProgramGraph | None = None,
    ) -> TaskPlan:
        """
        Produce the task plan for driving graph toward the manifold.
        Called at the start of each rewrite cycle.
        """
        results  = check_all_constraints(graph, cg, original)
        tasks: list[Task] = []

        # Step 1: Generate tasks for violated constraints
        for result in results:
            constraint = cg.constraints[result.constraint_id]
            if result.violated:
                task = self._task_for_violation(constraint, result)
                if task:
                    tasks.append(task)

        # Step 2: Generate optimization tasks for satisfied soft constraints
        # (only if no hard violations — optimization is pointless if hard fails exist)
        hard_violated = any(
            r.violated and r.priority == ConstraintPriority.HARD
            for r in results
        )
        if not hard_violated:
            for result in results:
                if not result.violated and result.priority == ConstraintPriority.SOFT:
                    opt = self._optimization_task(cg.constraints[result.constraint_id])
                    if opt:
                        tasks.append(opt)

        # Step 3: Sort deterministically — HARD → STRONG → SOFT, stable
        priority_order = {
            ConstraintPriority.HARD:   0,
            ConstraintPriority.STRONG: 1,
            ConstraintPriority.SOFT:   2,
        }
        tasks.sort(key=lambda t: (priority_order[t.priority], t.task_id))

        # Step 4: Resolve dependencies
        tasks = _resolve_dependency_order(tasks)

        # Step 5: Inject VERIFY checkpoint after HARD tasks complete
        tasks = _inject_verify_checkpoints(tasks)

        return TaskPlan(tasks=tasks)

    def _task_for_violation(
        self,
        constraint: Constraint,
        result:     ConstraintResult,
    ) -> Task | None:
        """Map a constraint violation to the appropriate repair task."""
        match constraint.check_kind:

            case "must_parse":
                return Task(
                    task_id           = f"task_{constraint.constraint_id}_parse",
                    kind              = TaskKind.PROTECT_DATA_FIELDS,
                    priority          = constraint.priority,
                    source_constraint = constraint.constraint_id,
                    description       = "Fix syntax errors to produce parseable output",
                    params            = (),
                    depends_on        = (),
                )

            case "forbidden_api_set":
                return Task(
                    task_id           = f"task_{constraint.constraint_id}",
                    kind              = TaskKind.REMOVE_FORBIDDEN_APIS,
                    priority          = constraint.priority,
                    source_constraint = constraint.constraint_id,
                    description       = "Remove all forbidden Fabric API references",
                    params            = (constraint.check_param,),
                    depends_on        = (),
                )

            case "required_api_any":
                return Task(
                    task_id           = f"task_{constraint.constraint_id}",
                    kind              = TaskKind.INJECT_NEOFORGE_IMPORTS,
                    priority          = constraint.priority,
                    source_constraint = constraint.constraint_id,
                    description       = "Inject required NeoForge API imports",
                    params            = (constraint.check_param,),
                    depends_on        = (f"task_MUST_NOT_USE_FABRIC_APIS",),
                )

            case "required_method":
                return Task(
                    task_id           = f"task_{constraint.constraint_id}",
                    kind              = TaskKind.ADD_REQUIRED_METHOD,
                    priority          = constraint.priority,
                    source_constraint = constraint.constraint_id,
                    description       = f"Add missing method: {constraint.check_param}",
                    params            = (constraint.check_param,),
                    depends_on        = (),
                )

            case "structural_behavior_preservation":
                return Task(
                    task_id           = f"task_{constraint.constraint_id}",
                    kind              = TaskKind.ADAPT_CAPABILITY_BODY,
                    priority          = constraint.priority,
                    source_constraint = constraint.constraint_id,
                    description       = f"Adapt {constraint.check_param} body for NeoForge",
                    params            = (constraint.check_param,),
                    depends_on        = (f"task_MUST_HAVE_CAPABILITY_METHOD",),
                )

            case "required_api_call":
                return Task(
                    task_id           = f"task_{constraint.constraint_id}",
                    kind              = TaskKind.REGISTER_CAPABILITY,
                    priority          = constraint.priority,
                    source_constraint = constraint.constraint_id,
                    description       = f"Add capability registration via {constraint.check_param}",
                    params            = (constraint.check_param,),
                    depends_on        = (f"task_MUST_USE_NEOFORGE_APIS",),
                )

            case "no_forbidden_refs_in_evidence_class_body":
                return Task(
                    task_id           = f"task_{constraint.constraint_id}",
                    kind              = TaskKind.CLEAN_DANGLING_FABRIC_REFS,
                    priority          = constraint.priority,
                    source_constraint = constraint.constraint_id,
                    description       = "Remove leftover Fabric API call(s) from capability-provider class body",
                    params            = (),
                    depends_on        = (),
                )

            # Note: "unique_capability_keys" has no case here, deliberately.
            # Renaming a colliding capability-key constant requires intent
            # Stage 0 doesn't have access to -- guessing a new name would be
            # exactly the kind of fabrication this architecture exists to
            # avoid. It falls through to `case _` below and is reported via
            # diagnostic rather than auto-repaired.

            case _:
                return None

    def _optimization_task(self, constraint: Constraint) -> Task | None:
        """Generate an optimization task for a satisfied soft constraint."""
        match constraint.check_kind:
            case "naming_convention":
                return Task(
                    task_id           = f"opt_{constraint.constraint_id}",
                    kind              = TaskKind.APPLY_NAMING_CONVENTION,
                    priority          = ConstraintPriority.SOFT,
                    source_constraint = constraint.constraint_id,
                    description       = "Apply NeoForge naming conventions",
                    params            = (constraint.check_param,),
                    depends_on        = (),
                )
            case _:
                return None


def _resolve_dependency_order(tasks: list[Task]) -> list[Task]:
    """
    Two-pass ordering:
    1. Group tasks by priority tier (HARD → STRONG → SOFT).
    2. Within each tier, apply Kahn's topological sort respecting dependencies
       only to other tasks in the same tier (cross-tier dependencies are
       already satisfied by the tier ordering itself).

    This guarantees that ALL hard tasks precede ALL strong tasks, which precede
    ALL soft tasks, while still honoring within-tier dependency constraints.
    Deterministic: same input → same output always.
    """
    priority_order = {
        ConstraintPriority.HARD:   0,
        ConstraintPriority.STRONG: 1,
        ConstraintPriority.SOFT:   2,
    }

    # Bucket tasks by tier
    tiers: dict[int, list[Task]] = {0: [], 1: [], 2: []}
    for t in tasks:
        tiers[priority_order[t.priority]].append(t)

    ordered: list[Task] = []
    for tier_idx in (0, 1, 2):
        tier_tasks = tiers[tier_idx]
        if not tier_tasks:
            continue

        task_map   = {t.task_id: t for t in tier_tasks}
        in_degree: dict[str, int] = {t.task_id: 0 for t in tier_tasks}
        adj: dict[str, list[str]] = {t.task_id: [] for t in tier_tasks}

        for t in tier_tasks:
            for dep in t.depends_on:
                if dep in task_map:   # Only intra-tier dependencies count here
                    in_degree[t.task_id] += 1
                    adj[dep].append(t.task_id)

        # Kahn's within this tier, sorted for determinism
        queue: list[str] = sorted(tid for tid, deg in in_degree.items() if deg == 0)

        while queue:
            tid = queue.pop(0)
            ordered.append(task_map[tid])
            for neighbor in sorted(adj[tid]):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)
            queue.sort()

        # Append any remaining (cycle fallback — shouldn't occur in Stage 0)
        remaining = [t for t in tier_tasks if t not in ordered]
        ordered.extend(remaining)

    return ordered


def _inject_verify_checkpoints(tasks: list[Task]) -> list[Task]:
    """
    Inject VERIFY tasks at key transition points:
    - After all HARD tasks complete
    - After all STRONG tasks complete
    """
    result:       list[Task] = []
    last_hard_id  = None
    last_strong_id = None

    for t in tasks:
        result.append(t)
        if t.priority == ConstraintPriority.HARD:
            last_hard_id = t.task_id
        elif t.priority == ConstraintPriority.STRONG:
            last_strong_id = t.task_id

    # Insert VERIFY after last HARD task.
    # Priority is SOFT so it does not shift the hard/strong boundary used
    # by ordering assertions — it is a meta-task, not a repair task.
    if last_hard_id is not None:
        idx = next(i for i, t in enumerate(result) if t.task_id == last_hard_id)
        verify_hard = Task(
            task_id           = "VERIFY_HARD_CHECKPOINT",
            kind              = TaskKind.VERIFY,
            priority          = ConstraintPriority.SOFT,   # meta, not repair
            source_constraint = "internal",
            description       = "Verify all HARD constraints satisfied before continuing",
            params            = (),
            depends_on        = (last_hard_id,),
        )
        result.insert(idx + 1, verify_hard)

    # Insert VERIFY after last STRONG task.
    if last_strong_id is not None:
        strong_idx = next((i for i, t in enumerate(result) if t.task_id == last_strong_id), None)
        if strong_idx is not None:
            verify_strong = Task(
                task_id           = "VERIFY_STRONG_CHECKPOINT",
                kind              = TaskKind.VERIFY,
                priority          = ConstraintPriority.SOFT,   # meta, not repair
                source_constraint = "internal",
                description       = "Verify STRONG constraints before soft optimization",
                params            = (),
                depends_on        = (last_strong_id,),
            )
            result.insert(strong_idx + 1, verify_strong)

    return result


if __name__ == "__main__":
    import json
    from tsam.constraint_graph import ProgramGraph, build_neoforge_constraint_graph

    print("=== TSAM Phase 3.5: Lightweight Task Planner ===\n")

    fabric_source = """
import net.fabricmc.fabric

class FabricCapabilityProvider:
    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return LazyOptional.of(lambda: self.handler)
        return LazyOptional.empty()
"""

    graph   = ProgramGraph.from_python_source(fabric_source)
    cg      = build_neoforge_constraint_graph()
    planner = TaskPlanner()
    plan    = planner.plan(graph, cg)

    print("Task Plan:")
    print(json.dumps(plan.summary(), indent=2))
    print(f"\nExecutable next: {plan.executable_next()}")
