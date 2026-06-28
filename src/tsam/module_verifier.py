"""
TSAM Stage 1 — Phase 1.2: Cross-Module Consistency Penalty C + Multi-Module Verifier
====================================================================================
Slices 1–2 built the persistent multi-module program P_t and the bounded
active window W_t with its demand-driven resolver. This slice is the first
that *changes acceptance behaviour*: it turns the window's resolved structure
into the Cross-module Consistency penalty **C** and folds C into the energy
model's ``(H, S, C, Q)`` lexicographic tuple and the acceptance gate, then
exposes a project-level verifier that accepts a consistent multi-module
project and cleanly rejects an inconsistent one with a machine-readable
diagnostic.

Where C sits (the anti-cliff design — see CLAUDE.md history of the 0.6.0 fix):
  The 0.6.0 acceptance cliff was caused by a *soft, size-proportional,
  correctness-irrelevant* term (``node_delta``) sitting in the acceptance
  gate. C is the opposite kind of quantity: a dangling cross-module import is
  *broken code* (the program names a symbol that does not exist), the
  cross-module twin of the within-file dangling-reference bug 0.6.0 treated
  as Severe. So C's hard part lives in the **correctness bucket alongside
  hard/strong**, never in the quality bucket where ``node_delta`` lived. It
  enters the energy tuple as its own tier between Strong and Quality, and the
  acceptance gate via ``distance`` (which already means "count of
  correctness violations"). ``node_delta`` does not move.

What counts toward C (falsifiable by construction — every unit points at a
specific defect a reader can name):
  - C_hard (gates acceptance):
      * DANGLING  — an import resolves to an in-project module that does not
        export the symbol (an immediate ImportError in real Python).
      * CYCLE     — a re-export cycle: the symbol is never actually defined.
      * COLLISION — two or more classes register the same capability key.
  - C_soft (ranks only, never gates):
      * HOP_LIMIT — the bounded resolver gave up before confirming
        resolution. This is a resolver *limit*, not proven breakage, so it is
        only escalated to C_hard when the unresolved symbol is structurally
        used by the source module (the D3 calibration decision); otherwise it
        is a soft coupling smell.
  - UNRESOLVED_EXTERNAL never counts: stdlib / third-party imports are
    expected, not defects.

Per-module Stage-0 scoping (mirrors the per-class capability-evidence gating
in constraint_graph): the Stage-0 NeoForge constraint set assumes its target
*is* a capability provider, so a core/utility/registry module would spuriously
"fail" it. A module is therefore held to the provider constraints only if it
shows capability-provider evidence; a non-provider module is required only to
parse. This is the module-level analogue of Phase D's per-class intent gating.

Invariants (CLAUDE.md §5):
  - Determinism (5.2): C is computed from the deterministic window; the
    tuple comparison and the diagnostic ordering are deterministic.
  - Clean Rejection (5.4): an inconsistent project is rejected with a
    diagnostic that names every offending edge and key collision, never
    accepted as plausible-but-broken.
  - Single-module invariance: on any single-module project C ≡ 0 and the
    acceptance decision is byte-identical to v0.8.0 (verified by test).
  - Separation of Concerns (5.5): W_t is owned by the verifier (loop-owned
    working state), not embedded in the frozen CognitiveState; only a bounded
    summary is projected into the report/diagnostic.

Stdlib only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tsam.cognitive_state import ConstraintPriority, VerificationOutcome
from tsam.constraint_graph import (
    ConstraintGraph,
    ConstraintResult,
    NodeKind,
    ProgramGraph,
    check_all_constraints,
)
from tsam.module_graph import ModuleGraph
from tsam.active_window import (
    ActiveWindow,
    CrossModuleEdge,
    ResolutionStatus,
    ResolvedCapabilityKey,
)
from tsam.rewrite_engine import (
    ACCEPTANCE_THRESHOLD,
    CROSS_MODULE_HARD_WEIGHT,
    CROSS_MODULE_SOFT_WEIGHT,
    EnergyBreakdown,
    compute_energy,
)

__all__ = [
    "CrossModuleDefect",
    "CrossModulePenalty",
    "compute_cross_module_penalty",
    "ModuleVerificationReport",
    "verify_module_graph",
]


# ---------------------------------------------------------------------------
# Cross-module penalty
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CrossModuleDefect:
    """One named, pointable-at cross-module defect (the falsifiability unit)."""
    kind:             str        # "DANGLING" | "CYCLE" | "HOP_LIMIT" | "COLLISION"
    severity:         str        # "hard" | "soft"
    source_module_id: str | None
    target_module_id: str | None
    symbol:           str | None
    detail:           str

    def to_dict(self) -> dict:
        return {
            "kind":             self.kind,
            "severity":         self.severity,
            "source_module_id": self.source_module_id,
            "target_module_id": self.target_module_id,
            "symbol":           self.symbol,
            "detail":           self.detail,
        }


@dataclass(frozen=True, slots=True)
class CrossModulePenalty:
    """
    The Cross-module Consistency penalty C, decomposed into a gating hard part
    and a non-gating soft part, plus the list of specific defects that produced
    it (so a rejection diagnostic can name them).
    """
    hard_count:   int
    soft_count:   int
    defects:      tuple[CrossModuleDefect, ...]

    @property
    def hard_penalty(self) -> float:
        return self.hard_count * CROSS_MODULE_HARD_WEIGHT

    @property
    def soft_penalty(self) -> float:
        return self.soft_count * CROSS_MODULE_SOFT_WEIGHT

    @property
    def is_consistent(self) -> bool:
        """True iff there is no hard cross-module defect."""
        return self.hard_count == 0

    def hard_defects(self) -> tuple[CrossModuleDefect, ...]:
        return tuple(d for d in self.defects if d.severity == "hard")

    def to_dict(self) -> dict:
        return {
            "hard_count":   self.hard_count,
            "soft_count":   self.soft_count,
            "hard_penalty": round(self.hard_penalty, 4),
            "soft_penalty": round(self.soft_penalty, 4),
            "defects":      [d.to_dict() for d in self.defects],
        }


def _symbol_structurally_used(pg: ProgramGraph, symbol: str) -> bool:
    """
    True if ``symbol`` is referenced by the module beyond its own import
    statement: in any non-import node's API references, or in a class body's
    reference set, or as a structural capability key. Robust for class-based
    usage (capability keys); conservative (may under-detect) for plain
    top-level-function bodies, which is the safe direction — it only ever
    *downgrades* a HOP_LIMIT to soft, never fabricates a hard defect.
    """
    for node in pg.nodes.values():
        if node.kind is NodeKind.IMPORT:
            continue
        if symbol in node.api_refs:
            return True
        if symbol in node.body_api_refs:
            return True
        if symbol in node.structural_capability_keys:
            return True
    return False


def compute_cross_module_penalty(window: ActiveWindow) -> CrossModulePenalty:
    """
    Derive C from a resolved :class:`ActiveWindow` (pure; does not mutate the
    window or the graph).

    Classification (see module docstring):
      DANGLING / CYCLE        -> hard (genuine import-time breakage)
      COLLISION               -> hard (registration conflict)
      HOP_LIMIT               -> hard iff the symbol is structurally used by
                                 the source module, else soft (D3)
      UNRESOLVED_EXTERNAL     -> ignored
    Defects are emitted in a deterministic order.
    """
    graph = window.graph
    hard_defects: list[CrossModuleDefect] = []
    soft_defects: list[CrossModuleDefect] = []

    for edge in window.unresolved_edges():  # already sorted
        if edge.status is ResolutionStatus.UNRESOLVED_EXTERNAL:
            continue
        if edge.status is ResolutionStatus.UNRESOLVED_DANGLING:
            hard_defects.append(CrossModuleDefect(
                kind="DANGLING", severity="hard",
                source_module_id=edge.source_module_id,
                target_module_id=edge.target_module_id,
                symbol=edge.symbol,
                detail=(f"module imports {edge.symbol!r} from an in-project "
                        f"module that does not export it"),
            ))
        elif edge.status is ResolutionStatus.UNRESOLVED_CYCLE:
            hard_defects.append(CrossModuleDefect(
                kind="CYCLE", severity="hard",
                source_module_id=edge.source_module_id,
                target_module_id=edge.target_module_id,
                symbol=edge.symbol,
                detail=(f"re-export cycle: {edge.symbol!r} is never locally "
                        f"defined anywhere in the chain"),
            ))
        elif edge.status is ResolutionStatus.UNRESOLVED_HOP_LIMIT:
            pg = graph.program_graph(edge.source_module_id)
            used = _symbol_structurally_used(pg, edge.symbol)
            severity = "hard" if used else "soft"
            defect = CrossModuleDefect(
                kind="HOP_LIMIT", severity=severity,
                source_module_id=edge.source_module_id,
                target_module_id=edge.target_module_id,
                symbol=edge.symbol,
                detail=(f"resolution of {edge.symbol!r} exceeded the hop ceiling"
                        + (" and the symbol is used here" if used
                           else " (symbol not structurally used here; treated as smell)")),
            )
            (hard_defects if used else soft_defects).append(defect)

    for key in window.colliding_resolved_keys():  # already sorted
        hard_defects.append(CrossModuleDefect(
            kind="COLLISION", severity="hard",
            source_module_id=None,
            target_module_id=key.declaring_module_id,
            symbol=key.key_name,
            detail=(f"capability key {key.key_name!r} (declared in "
                    f"{key.declaring_module_id[:8]}) is registered by "
                    f"{len(key.referencing_classes)} classes: "
                    f"{list(key.referencing_classes)}"),
        ))

    defects = tuple(hard_defects + soft_defects)
    return CrossModulePenalty(
        hard_count=len(hard_defects),
        soft_count=len(soft_defects),
        defects=defects,
    )


# ---------------------------------------------------------------------------
# Multi-module verification
# ---------------------------------------------------------------------------

@dataclass
class ModuleVerificationReport:
    """
    Project-level verification result. ``accepted`` is true iff every
    in-scope module satisfies its Stage-0 obligations AND there is no hard
    cross-module defect (Accept = Stage-0 PASS ∧ gating < τ ∧ distance == 0,
    with distance including the C_hard count).
    """
    accepted:           bool
    outcome:            VerificationOutcome
    breakdown:          EnergyBreakdown
    distance:           float
    cross_module:       CrossModulePenalty
    module_count:       int
    provider_count:     int
    per_module_results: dict                       # module_id -> list[ConstraintResult]
    window_stats:       dict
    diagnostic:         dict | None = field(default=None)

    @property
    def energy(self) -> float:
        return self.breakdown.total

    def to_dict(self) -> dict:
        return {
            "accepted":       self.accepted,
            "outcome":        self.outcome.name,
            "energy":         round(self.breakdown.total, 4),
            "distance":       round(self.distance, 4),
            "breakdown":      self.breakdown.to_dict(),
            "cross_module":   self.cross_module.to_dict(),
            "module_count":   self.module_count,
            "provider_count": self.provider_count,
            "window_stats":   self.window_stats,
            "diagnostic":     self.diagnostic,
        }


def _build_resolved_window(graph: ModuleGraph, focus_modules) -> ActiveWindow:
    """Create a window over ``graph`` and resolve every in-scope module's
    imports into it. ``focus_modules`` (ids) defaults to all modules."""
    window = ActiveWindow.for_graph(graph)
    ids = list(focus_modules) if focus_modules is not None else graph.module_ids()
    window.set_focus(ids)
    for mid in ids:
        window.admit(mid)
        window.resolve_module(mid)
    return window


def verify_module_graph(
    graph:         ModuleGraph,
    cg:            ConstraintGraph,
    focus_modules=None,
) -> ModuleVerificationReport:
    """
    Verify a whole multi-module project against the Stage-0 constraint set
    (provider-scoped) plus the cross-module consistency penalty C.

    Returns a :class:`ModuleVerificationReport`. ``focus_modules`` restricts
    the scope to a set of module ids (default: all modules). For projects that
    fit within the active-window caps (true for all current fixtures) the
    window holds the whole project and C is global; larger projects would
    require iterating the window across the project, which is out of scope for
    this slice and would be flagged by ``window_stats['within_bounds']``.
    """
    ids = list(focus_modules) if focus_modules is not None else graph.module_ids()

    # 1. Cross-module penalty from the resolved window.
    window = _build_resolved_window(graph, ids)
    cross = compute_cross_module_penalty(window)

    # 2. Per-module Stage-0 verification (provider-scoped).
    per_module_results: dict = {}
    agg_hard = agg_strong = agg_soft = agg_syntax = 0.0
    hard_fails = strong_fails = soft_fails = 0
    provider_count = 0

    for mid in ids:
        descriptor = graph.descriptor(mid)
        pg = graph.program_graph(mid)

        if descriptor.capability_evidence:
            provider_count += 1
            _, dist, bd, results = compute_energy(pg, cg)
            per_module_results[mid] = results
            agg_hard   += bd.hard_penalty
            agg_strong += bd.strong_penalty
            agg_soft   += bd.soft_penalty
            agg_syntax += bd.syntax_penalty
            hard_fails   += sum(1 for r in results
                                if r.violated and r.priority is ConstraintPriority.HARD)
            strong_fails += sum(1 for r in results
                                if r.violated and r.priority is ConstraintPriority.STRONG)
            soft_fails   += sum(1 for r in results
                                if r.violated and r.priority is ConstraintPriority.SOFT)
        else:
            # Non-provider module: only required to parse. A parse failure is
            # a hard, syntax-level defect (Clean Rejection).
            if not descriptor.parse_ok:
                agg_syntax += CROSS_MODULE_HARD_WEIGHT  # any positive hard weight
                hard_fails += 1
                per_module_results[mid] = [ConstraintResult(
                    constraint_id="MUST_PARSE_NON_PROVIDER",
                    priority=ConstraintPriority.HARD,
                    satisfied=False,
                    violation_msg=f"non-provider module {descriptor.canonical_path} did not parse",
                )]
            else:
                per_module_results[mid] = []

    # 3. Project-level energy breakdown: aggregated per-module penalties plus
    #    the project cross-module penalty. node_delta = 0 (static verification,
    #    no "original" project to diff against).
    breakdown = EnergyBreakdown(
        hard_penalty       = agg_hard,
        strong_penalty     = agg_strong,
        soft_penalty       = agg_soft,
        syntax_penalty     = agg_syntax,
        node_delta         = 0.0,
        cross_module_hard  = cross.hard_penalty,
        cross_module_soft  = cross.soft_penalty,
    )

    # 4. Distance includes the C_hard count, so distance == 0 requires no hard
    #    cross-module defect (this is how C gates acceptance).
    distance = float(hard_fails + strong_fails + cross.hard_count)

    # 5. Outcome + acceptance, mirroring the single-file Accept() exactly.
    if hard_fails > 0:
        outcome = VerificationOutcome.FAIL_HARD
    elif strong_fails > 0:
        outcome = VerificationOutcome.FAIL_STRONG
    elif soft_fails > 0:
        outcome = VerificationOutcome.FAIL_SOFT
    else:
        outcome = VerificationOutcome.PASS

    accepted = (
        outcome is VerificationOutcome.PASS
        and breakdown.gating < ACCEPTANCE_THRESHOLD
        and distance == 0.0
    )

    diagnostic = None
    if not accepted:
        diagnostic = _build_diagnostic(
            graph, ids, per_module_results, cross, breakdown, distance, outcome,
        )

    return ModuleVerificationReport(
        accepted           = accepted,
        outcome            = outcome,
        breakdown          = breakdown,
        distance           = distance,
        cross_module       = cross,
        module_count       = len(ids),
        provider_count     = provider_count,
        per_module_results = per_module_results,
        window_stats       = window.stats(),
        diagnostic         = diagnostic,
    )


def _build_diagnostic(
    graph, ids, per_module_results, cross, breakdown, distance, outcome,
) -> dict:
    """Machine-readable rejection diagnostic (Verification Contract): names
    every per-module constraint violation and every cross-module defect."""
    module_violations = []
    for mid in ids:
        for r in per_module_results.get(mid, []):
            if r.violated:
                module_violations.append({
                    "module_id":     mid,
                    "module_path":   graph.descriptor(mid).canonical_path,
                    "constraint_id": r.constraint_id,
                    "priority":      r.priority.name,
                    "msg":           r.violation_msg,
                })
    return {
        "tsam_module_diagnostic": True,
        "outcome":                outcome.name,
        "final_energy":           round(breakdown.total, 4),
        "final_distance":         round(distance, 4),
        "gating_energy":          round(breakdown.gating, 4),
        "cross_module_hard":      cross.hard_count,
        "cross_module_soft":      cross.soft_count,
        "module_violations":      module_violations,
        "cross_module_defects":   [d.to_dict() for d in cross.defects],
        "reason": (
            "Rejected: hard cross-module inconsistency (see cross_module_defects)."
            if cross.hard_count > 0 and len(module_violations) == 0 else
            "Rejected: per-module constraint violations and/or cross-module inconsistency."
        ),
    }


if __name__ == "__main__":
    import json
    from validation.module_generators import (
        generate_consistent_project,
        generate_inconsistent_project,
    )
    from tsam.constraint_graph import build_neoforge_constraint_graph

    cg = build_neoforge_constraint_graph()

    print("=== TSAM Stage 1.2: Cross-Module Consistency (C) ===\n")
    for label, gen in (("CONSISTENT", generate_consistent_project),
                       ("INCONSISTENT", generate_inconsistent_project)):
        graph = ModuleGraph.build(gen(0).source_map())
        report = verify_module_graph(graph, cg)
        print(f"[{label}] accepted={report.accepted}  "
              f"C_hard={report.cross_module.hard_count}  "
              f"distance={report.distance}")
        if report.diagnostic:
            for d in report.diagnostic["cross_module_defects"]:
                print(f"    - {d['kind']}({d['severity']}): {d['detail']}")
        print()
