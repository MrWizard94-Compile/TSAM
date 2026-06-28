"""
TSAM Stage 1 — Phase 1.1: Bounded Active Window W_t + Multi-Hop Resolver
=======================================================================
Slice 1 (``module_graph.py``) built the persistent multi-module program P_t.
This slice builds the *transient* executive view the planner, rewriter, and
verifier will actually operate over:

  - A **demand-driven, single-pass, bounded multi-hop resolver** that follows
    re-export chains from an import to the module that actually declares the
    symbol, terminating at a small constant hop ceiling (Stage 1 Spec §1.3).
  - The **Active Window W_t** (Spec §1.4, §2): a strictly bounded working set
    of modules, resolved cross-module edges, and resolved capability keys,
    with hard cardinality caps and LRU eviction (Spec §2.2, §2.3).

Scope boundary (what this slice does NOT do — that is slice 3):
  - It does not compute the Cross-module Consistency penalty C, nor the
    ``(H, S, C, Q)`` energy tuple, nor change any acceptance behaviour.
  - It is not yet wired into ``CognitiveState`` (S_t) or the rewrite loop.
    The window takes the current focus and recent-verification module sets as
    plain inputs, so it is fully testable in isolation; slice 3 derives those
    from ``Focus`` / ``V_t``. Stage 0 is untouched and the RVP is unchanged.

Invariants honoured (CLAUDE.md §5):
  - Bounded Executive State (5.1): three hard caps — ``MAX_ACTIVE_MODULES``,
    ``MAX_ACTIVE_EDGES``, ``MAX_RESOLVED_KEYS`` — are never exceeded; crossing
    one triggers immediate LRU eviction. Resolution explores at most
    ``MAX_HOPS`` modules per import, a constant independent of project size.
  - Determinism (5.2): LRU uses a logical clock (no wall-clock); every
    eviction victim and every accessor is chosen/ordered deterministically
    (ties broken by stable id). A re-export cycle is caught, not looped.
  - Clean Rejection (5.4): an import that does not resolve is recorded with a
    specific status (dangling / external / hop-limit / cycle), never silently
    dropped — this is the raw material the C penalty will score in slice 3.
  - Separation of Concerns (5.5): the window references P_t (the persistent
    ``ModuleGraph``) read-only and never mutates it; window state excludes the
    persistent artifact and excludes its own logical ticks from identity.

Stdlib only.

### A note on the hop-limit boundary (Spec §1.3 ambiguity, resolved here)
The spec says both "follow the chain only if hop_count < 3" and "any chain
reaching hop_count == 3 is terminated." Those can be read two ways. This
implementation takes the first as governing: a symbol resolves successfully
at ``hop_count`` up to and including ``MAX_HOPS`` (= 3), and a re-export that
would require advancing to a *fourth* module terminates as ``HOP_LIMIT``. The
boundary lives entirely in ``MAX_HOPS`` and the single ``hop >= max_hops``
check in :func:`resolve_symbol`; flip that to ``hop + 1 >= max_hops`` (or
lower ``MAX_HOPS``) if the intended reading is "resolution may only succeed at
hops 1–2." Flagged rather than silently chosen.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field, replace
from enum import Enum, auto

from tsam.constraint_graph import NodeKind
from tsam.module_graph import ModuleGraph, ModuleImport

__all__ = [
    "MAX_HOPS",
    "MAX_ACTIVE_MODULES",
    "MAX_ACTIVE_EDGES",
    "MAX_RESOLVED_KEYS",
    "VERIFICATION_PROTECTION_DEPTH",
    "ResolutionStatus",
    "ResolutionResult",
    "CrossModuleEdge",
    "ResolvedCapabilityKey",
    "resolve_symbol",
    "ActiveWindow",
]


# ---------------------------------------------------------------------------
# Hard bounds (Spec §1.3, §2.2). Caps are inviolable; crossing triggers
# immediate LRU eviction. MAX_HOPS bounds resolution cost per import.
# ---------------------------------------------------------------------------

MAX_HOPS: int = 3
MAX_ACTIVE_MODULES: int = 32
MAX_ACTIVE_EDGES: int = 128
MAX_RESOLVED_KEYS: int = 64

# How many recent verification records protect their modules from eviction
# (Spec §2.3: "the last 4 verification records").
VERIFICATION_PROTECTION_DEPTH: int = 4


# ---------------------------------------------------------------------------
# Resolution result types
# ---------------------------------------------------------------------------

class ResolutionStatus(Enum):
    """Outcome of resolving one imported symbol through the project."""
    RESOLVED             = auto()   # reached a module that locally declares it
    UNRESOLVED_DANGLING  = auto()   # reached an in-project module not exporting it
    UNRESOLVED_EXTERNAL  = auto()   # chain leaves the project (or target is external)
    UNRESOLVED_HOP_LIMIT = auto()   # a further re-export hop would exceed MAX_HOPS
    UNRESOLVED_CYCLE     = auto()   # re-export cycle detected


@dataclass(frozen=True, slots=True)
class ResolutionResult:
    """
    The outcome of :func:`resolve_symbol`.

    ``declaring_module_id`` is set iff ``status is RESOLVED`` — it is the
    module whose *local* definition the symbol resolves to. ``hop_count`` is
    the number of modules traversed (1 = resolved directly in the imported
    module). ``chain`` is the ordered list of module ids visited, for
    diagnostics.
    """
    status:              ResolutionStatus
    symbol:              str
    declaring_module_id: str | None
    hop_count:           int
    chain:               tuple[str, ...]

    @property
    def resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED

    def to_dict(self) -> dict:
        return {
            "status":              self.status.name,
            "symbol":              self.symbol,
            "declaring_module_id": self.declaring_module_id,
            "hop_count":           self.hop_count,
            "chain":               list(self.chain),
        }


@dataclass(frozen=True, slots=True)
class CrossModuleEdge:
    """
    A resolved cross-module edge (Spec §1.2):
    e = (source_module, target_module, symbol, hop_count, resolved_key),
    extended with the resolution ``status`` and the ``declaring_module_id``
    so an unresolved edge is a first-class, diagnosable object rather than an
    absence. ``resolved_key`` is the canonical capability-key name when the
    imported symbol is one (and resolved), else ``None``.
    """
    source_module_id:    str
    target_module_id:    str | None   # the directly-imported module (None if external import)
    symbol:              str
    hop_count:           int
    status:              ResolutionStatus
    declaring_module_id: str | None
    resolved_key:        str | None = None

    @property
    def resolved(self) -> bool:
        return self.status is ResolutionStatus.RESOLVED

    def edge_id(self) -> tuple[str, str, str]:
        return (self.source_module_id, self.target_module_id or "", self.symbol)

    def _sort_key(self) -> tuple[str, str, str]:
        return self.edge_id()

    def to_dict(self) -> dict:
        return {
            "source_module_id":    self.source_module_id,
            "target_module_id":    self.target_module_id,
            "symbol":              self.symbol,
            "hop_count":           self.hop_count,
            "status":              self.status.name,
            "declaring_module_id": self.declaring_module_id,
            "resolved_key":        self.resolved_key,
        }


@dataclass(frozen=True, slots=True)
class ResolvedCapabilityKey:
    """
    A resolved capability key (Spec §1.2):
    k = (declaring_module, key_name, referencing_classes).

    ``referencing_classes`` is the sorted set of ``"<module_id>:<ClassName>"``
    locations that reference this key (across all modules whose imports of it
    have been resolved into the window). A key referenced by more than one
    distinct class — especially across modules — is the cross-module
    registration-collision signal the C penalty will score in slice 3.
    """
    declaring_module_id: str
    key_name:            str
    referencing_classes: tuple[str, ...]

    def key_id(self) -> tuple[str, str]:
        return (self.declaring_module_id, self.key_name)

    def with_classes(self, classes: tuple[str, ...]) -> "ResolvedCapabilityKey":
        """Return a copy whose referencing classes are the sorted union with
        ``classes``."""
        merged = tuple(sorted(set(self.referencing_classes) | set(classes)))
        return replace(self, referencing_classes=merged)

    def _sort_key(self) -> tuple[str, str]:
        return self.key_id()

    def to_dict(self) -> dict:
        return {
            "declaring_module_id": self.declaring_module_id,
            "key_name":            self.key_name,
            "referencing_classes": list(self.referencing_classes),
        }


# ---------------------------------------------------------------------------
# Multi-hop resolver (Spec §1.3). Pure function over the persistent P_t.
# ---------------------------------------------------------------------------

def resolve_symbol(
    graph:            ModuleGraph,
    target_module_id: str,
    symbol:           str,
    max_hops:         int = MAX_HOPS,
) -> ResolutionResult:
    """
    Resolve ``symbol`` imported from ``target_module_id`` to the module that
    locally declares it, following re-export chains.

    Single-pass and demand-driven — no fixed-point iteration (Spec §1.3). At
    each module in the chain:
      - if the module locally defines the symbol (an export with no origin),
        it is RESOLVED there;
      - if the module re-exports it from another *in-project* module and the
        hop ceiling permits, advance one hop;
      - if it re-exports from an *external* module, the result is EXTERNAL;
      - if it does not export the symbol at all, the result is DANGLING;
      - if a further hop would exceed ``max_hops``, the result is HOP_LIMIT;
      - a re-export cycle yields CYCLE.

    The number of modules examined is bounded by ``max_hops`` regardless of
    project size. ``target_module_id`` must be an in-project module id.
    """
    chain: list[str] = []
    visited: set[str] = set()
    current = target_module_id
    hop = 0

    while True:
        hop += 1
        chain.append(current)
        if current in visited:
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_CYCLE, symbol, None, hop, tuple(chain),
            )
        visited.add(current)

        descriptor = graph.descriptor(current)
        export = next((e for e in descriptor.exports if e.symbol == symbol), None)

        if export is None:
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_DANGLING, symbol, None, hop, tuple(chain),
            )
        if export.origin_path is None:
            # Locally declared here.
            return ResolutionResult(
                ResolutionStatus.RESOLVED, symbol, current, hop, tuple(chain),
            )

        # Re-export: try to advance one hop toward the origin module.
        next_id = graph.resolve_path(export.origin_path)
        if next_id is None:
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_EXTERNAL, symbol, None, hop, tuple(chain),
            )
        if hop >= max_hops:
            # A fourth module would be required; terminate at the ceiling.
            return ResolutionResult(
                ResolutionStatus.UNRESOLVED_HOP_LIMIT, symbol, None, hop, tuple(chain),
            )
        current = next_id


# ---------------------------------------------------------------------------
# The bounded Active Window W_t (Spec §1.4, §2)
# ---------------------------------------------------------------------------

@dataclass
class ActiveWindow:
    """
    W_t ⊂ P_t: the transient, strictly bounded working set the executive
    operates over. Holds active module ids (with logical access times), the
    resolved cross-module edges among them, and the resolved capability keys,
    each under a hard cardinality cap enforced by LRU eviction.

    The window references the persistent :class:`ModuleGraph` read-only.
    Construct with :meth:`for_graph`; drive admission/resolution with
    :meth:`focus_on` / :meth:`resolve_module`; advance the eviction frontier
    with :meth:`after_pass`.
    """
    graph:               ModuleGraph
    max_modules:         int = MAX_ACTIVE_MODULES
    max_edges:           int = MAX_ACTIVE_EDGES
    max_keys:            int = MAX_RESOLVED_KEYS
    max_hops:            int = MAX_HOPS

    _clock:              int = 0
    _module_access:      dict[str, int]                       = field(default_factory=dict)
    _edges:              dict[tuple[str, str, str], CrossModuleEdge] = field(default_factory=dict)
    _edge_access:        dict[tuple[str, str, str], int]      = field(default_factory=dict)
    _keys:               dict[tuple[str, str], ResolvedCapabilityKey] = field(default_factory=dict)
    _key_access:         dict[tuple[str, str], int]           = field(default_factory=dict)
    _focus:              frozenset[str]                       = frozenset()
    _recent_verifications: deque                              = field(default_factory=deque)
    _resolution_count:   int = 0
    _eviction_count:     int = 0

    @classmethod
    def for_graph(cls, graph: ModuleGraph, **caps) -> "ActiveWindow":
        """Create an empty window over ``graph`` (optionally overriding caps,
        which is useful for tests that want to force eviction with small
        bounds)."""
        win = cls(graph=graph)
        for name, value in caps.items():
            if not hasattr(win, name):
                raise ValueError(f"unknown cap override: {name!r}")
            setattr(win, name, value)
        win._recent_verifications = deque(maxlen=VERIFICATION_PROTECTION_DEPTH)
        return win

    # -- logical clock -------------------------------------------------------

    def _tick(self) -> int:
        self._clock += 1
        return self._clock

    # -- protection set (Spec §2.3) -----------------------------------------

    def _protected_modules(self) -> set[str]:
        """Modules that may not be evicted: the current focus plus every
        module named in the last ``VERIFICATION_PROTECTION_DEPTH`` verification
        records."""
        protected: set[str] = set(self._focus)
        for record in self._recent_verifications:
            protected.update(record)
        return protected

    def set_focus(self, module_ids) -> None:
        """Set the current focus module set (these become eviction-protected)."""
        self._focus = frozenset(module_ids)

    def record_verification(self, module_ids) -> None:
        """Push a verification record (its modules become protected for the
        next ``VERIFICATION_PROTECTION_DEPTH`` passes). Bounded ring buffer."""
        self._recent_verifications.append(frozenset(module_ids))

    # -- admission (Spec §2.3) ----------------------------------------------

    def admit(self, module_id: str) -> None:
        """
        Bring ``module_id`` into the window (or refresh its access time), then
        admit its direct in-project dependencies (one level — "a dependency of
        a module already in the window"), then enforce the module cap by LRU
        eviction. Idempotent.
        """
        if module_id not in self.graph:
            raise KeyError(f"module {module_id!r} is not in the persistent graph")
        self._module_access[module_id] = self._tick()

        # Admit direct internal dependencies (one hop of the dependency graph).
        for imp in self.graph.descriptor(module_id).imports:
            dep = imp.target_module_id
            if dep is not None and dep not in self._module_access:
                self._module_access[dep] = self._tick()

        self._enforce_module_cap()

    def is_active(self, module_id: str) -> bool:
        return module_id in self._module_access

    def active_modules(self) -> list[str]:
        """Active module ids, sorted (deterministic)."""
        return sorted(self._module_access.keys())

    # -- demand-driven resolution -------------------------------------------

    def resolve_import(self, source_module_id: str, imp: ModuleImport) -> CrossModuleEdge:
        """
        Resolve one import of ``source_module_id`` into the window. Admits the
        modules along the resolution chain, records (and LRU-bounds) the
        resulting :class:`CrossModuleEdge`, and — if the symbol is a capability
        key the source's classes reference — records/merges the corresponding
        :class:`ResolvedCapabilityKey`. Star imports are not resolved to a
        single declaration (recorded as an external-style edge with the star
        symbol). Returns the edge.
        """
        self._resolution_count += 1

        if imp.target_module_id is None or imp.is_star:
            # External target, relative import, or star: no in-project single
            # declaration to resolve to.
            status = ResolutionStatus.UNRESOLVED_EXTERNAL
            edge = CrossModuleEdge(
                source_module_id    = source_module_id,
                target_module_id    = imp.target_module_id,
                symbol              = imp.symbol,
                hop_count           = 0,
                status              = status,
                declaring_module_id = None,
                resolved_key        = None,
            )
            self._record_edge(edge)
            return edge

        result = resolve_symbol(self.graph, imp.target_module_id, imp.symbol, self.max_hops)

        # Admit every module the chain touched (they are now in the working set).
        self.admit(source_module_id)
        for mid in result.chain:
            self._module_access[mid] = self._tick()
        self._enforce_module_cap()

        resolved_key_name: str | None = None
        if result.resolved:
            referencing = _referencing_classes(self.graph, source_module_id, imp.symbol)
            if referencing:
                resolved_key_name = imp.symbol
                self._record_key(ResolvedCapabilityKey(
                    declaring_module_id = result.declaring_module_id,  # type: ignore[arg-type]
                    key_name            = imp.symbol,
                    referencing_classes = tuple(referencing),
                ))

        edge = CrossModuleEdge(
            source_module_id    = source_module_id,
            target_module_id    = imp.target_module_id,
            symbol              = imp.symbol,
            hop_count           = result.hop_count,
            status              = result.status,
            declaring_module_id = result.declaring_module_id,
            resolved_key        = resolved_key_name,
        )
        self._record_edge(edge)
        return edge

    def resolve_module(self, source_module_id: str) -> list[CrossModuleEdge]:
        """
        Resolve every (non-star) import of ``source_module_id``, returning the
        resulting edges sorted. Re-resolution on a later call is safe and
        idempotent (Spec §2.3): it recomputes from the persistent graph and
        refreshes the window entries.
        """
        descriptor = self.graph.descriptor(source_module_id)
        edges = [self.resolve_import(source_module_id, imp) for imp in descriptor.imports]
        return sorted(edges, key=CrossModuleEdge._sort_key)

    def focus_on(self, module_ids) -> None:
        """
        Convenience driver: make ``module_ids`` the focus (protected), admit
        them, and resolve their imports into the window.
        """
        ids = list(module_ids)
        self.set_focus(ids)
        for mid in ids:
            self.admit(mid)
            self.resolve_module(mid)

    def after_pass(self) -> int:
        """
        Eviction frontier advance (Spec §2.3: "after each rewrite pass").
        Evicts every unprotected module that is over the cap by LRU, and trims
        edges/keys to their caps. Returns the number of modules evicted.
        Already enforced incrementally on admit/record; this is the explicit
        per-pass sweep and a hook slice 3 calls after each rewrite.
        """
        before = len(self._module_access)
        self._enforce_module_cap()
        self._enforce_edge_cap()
        self._enforce_key_cap()
        return before - len(self._module_access)

    # -- bookkeeping for edges / keys ---------------------------------------

    def _record_edge(self, edge: CrossModuleEdge) -> None:
        eid = edge.edge_id()
        self._edges[eid] = edge
        self._edge_access[eid] = self._tick()
        self._enforce_edge_cap()

    def _record_key(self, key: ResolvedCapabilityKey) -> None:
        kid = key.key_id()
        existing = self._keys.get(kid)
        if existing is not None:
            key = existing.with_classes(key.referencing_classes)
        self._keys[kid] = key
        self._key_access[kid] = self._tick()
        self._enforce_key_cap()

    # -- eviction (Spec §2.2, §2.3) -----------------------------------------

    def _enforce_module_cap(self) -> None:
        if len(self._module_access) <= self.max_modules:
            return
        protected = self._protected_modules()
        # Prefer evicting unprotected modules; fall back to protected only if
        # the protected set alone exceeds the cap (kept deterministic).
        while len(self._module_access) > self.max_modules:
            victim = self._lru_module(exclude=protected)
            if victim is None:
                victim = self._lru_module(exclude=set())
            if victim is None:
                break
            self._evict_module(victim)

    def _lru_module(self, exclude: set[str]) -> str | None:
        candidates = [m for m in self._module_access if m not in exclude]
        if not candidates:
            return None
        # Least-recently-used; ties broken by id for determinism.
        return min(candidates, key=lambda m: (self._module_access[m], m))

    def _evict_module(self, module_id: str) -> None:
        self._eviction_count += 1
        self._module_access.pop(module_id, None)
        # Drop edges that touch the evicted module (Spec §2.3: evicted modules
        # have their cross-module edges dropped; re-resolution on next access).
        for eid in [
            e for e, edge in self._edges.items()
            if edge.source_module_id == module_id or edge.target_module_id == module_id
        ]:
            self._edges.pop(eid, None)
            self._edge_access.pop(eid, None)
        # Drop / trim keys: a key declared by the evicted module goes entirely;
        # otherwise remove the evicted module's classes from its referencing set
        # and drop the key if nothing references it any more.
        for kid in list(self._keys.keys()):
            key = self._keys[kid]
            if key.declaring_module_id == module_id:
                self._keys.pop(kid, None)
                self._key_access.pop(kid, None)
                continue
            remaining = tuple(
                c for c in key.referencing_classes
                if not c.startswith(f"{module_id}:")
            )
            if not remaining:
                self._keys.pop(kid, None)
                self._key_access.pop(kid, None)
            elif remaining != key.referencing_classes:
                self._keys[kid] = replace(key, referencing_classes=remaining)

    def _enforce_edge_cap(self) -> None:
        while len(self._edges) > self.max_edges:
            victim = min(self._edge_access, key=lambda e: (self._edge_access[e], e))
            self._edges.pop(victim, None)
            self._edge_access.pop(victim, None)

    def _enforce_key_cap(self) -> None:
        while len(self._keys) > self.max_keys:
            victim = min(self._key_access, key=lambda k: (self._key_access[k], k))
            self._keys.pop(victim, None)
            self._key_access.pop(victim, None)

    # -- read-only views ----------------------------------------------------

    def edges(self) -> list[CrossModuleEdge]:
        """Resolved (and unresolved-but-recorded) edges, sorted."""
        return sorted(self._edges.values(), key=CrossModuleEdge._sort_key)

    def unresolved_edges(self) -> list[CrossModuleEdge]:
        """Every recorded edge that did not resolve to a local declaration —
        the set the C penalty will score in slice 3."""
        return [e for e in self.edges() if not e.resolved]

    def resolved_keys(self) -> list[ResolvedCapabilityKey]:
        """Resolved capability keys currently in the window, sorted."""
        return sorted(self._keys.values(), key=ResolvedCapabilityKey._sort_key)

    def colliding_resolved_keys(self) -> list[ResolvedCapabilityKey]:
        """Resolved keys referenced by more than one distinct class (a
        registration collision — typically cross-module)."""
        return [k for k in self.resolved_keys() if len(k.referencing_classes) > 1]

    def within_bounds(self) -> bool:
        """True iff all three hard caps are currently respected."""
        return (
            len(self._module_access) <= self.max_modules
            and len(self._edges) <= self.max_edges
            and len(self._keys) <= self.max_keys
        )

    def stats(self) -> dict:
        return {
            "active_modules":   len(self._module_access),
            "max_modules":      self.max_modules,
            "edges":            len(self._edges),
            "max_edges":        self.max_edges,
            "resolved_keys":    len(self._keys),
            "max_keys":         self.max_keys,
            "resolution_count": self._resolution_count,
            "eviction_count":   self._eviction_count,
            "within_bounds":    self.within_bounds(),
        }

    def structural_state_hash(self) -> str:
        """
        Deterministic hash of the window's *content* (active modules, edges,
        resolved keys), excluding logical ticks. Two windows driven through
        the same admission/resolution sequence hash identically.
        """
        import hashlib
        h = hashlib.sha256()
        for mid in self.active_modules():
            h.update(b"M"); h.update(mid.encode()); h.update(b"\x00")
        for e in self.edges():
            h.update(b"E")
            h.update(f"{e.source_module_id}|{e.target_module_id or ''}|{e.symbol}"
                     f"|{e.hop_count}|{e.status.name}|{e.declaring_module_id or ''}"
                     f"|{e.resolved_key or ''}".encode())
            h.update(b"\x00")
        for k in self.resolved_keys():
            h.update(b"K")
            h.update(f"{k.declaring_module_id}|{k.key_name}|"
                     f"{','.join(k.referencing_classes)}".encode())
            h.update(b"\x00")
        return h.hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _referencing_classes(graph: ModuleGraph, module_id: str, key_name: str) -> list[str]:
    """
    The sorted ``"<module_id>:<ClassName>"`` locations in ``module_id`` whose
    classes structurally reference ``key_name`` as a capability key (read from
    the per-class ``structural_capability_keys`` the Stage 0 parser computes).
    """
    pg = graph.program_graph(module_id)
    out: list[str] = []
    for node in pg.nodes.values():
        if node.kind is NodeKind.CLASS and key_name in node.structural_capability_keys:
            out.append(f"{module_id}:{node.name}")
    return sorted(out)


if __name__ == "__main__":
    import json
    from validation.module_generators import generate_unsolvable_project

    case = generate_unsolvable_project(0)
    graph = ModuleGraph.build(case.source_map())
    window = ActiveWindow.for_graph(graph)
    window.focus_on(graph.capability_modules())

    print("=== TSAM Stage 1.1: Active Window + Resolver ===\n")
    print(json.dumps(window.stats(), indent=2))
    print("\nResolved capability keys (collision => >1 referencing class):")
    for k in window.resolved_keys():
        marker = "  <-- COLLISION" if len(k.referencing_classes) > 1 else ""
        print(f"  {k.key_name}: {list(k.referencing_classes)}{marker}")
    print("\nUnresolved edges:")
    for e in window.unresolved_edges():
        print(f"  {e.source_module_id[:8]} -> {e.symbol}  [{e.status.name}]")
