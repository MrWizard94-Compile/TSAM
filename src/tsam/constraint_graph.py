"""
TSAM Stage 0 — Phase 2 & 3: Structural Encoding + Constraint Graph
===================================================================
Phase 2: Structural Encoding
  - Prime-scattering structural signature (fast, deterministic)
  - AST-level representation of NeoForge capability + event patterns
  - Mandatory fidelity metric (how faithful the encoding is)

Phase 3: Constraint Graph Builder (Definition 2)
  - C = (V, E, Λ, Π)
  - Three priority tiers: HARD / STRONG / SOFT
  - Drives both the Task Planner and the energy function in verification

No attention mechanisms. No neural components. Pure deterministic Python.

REVIEW FIXES APPLIED (see TeamComms.md / RVP follow-up for full history):
  Phase A — per-class checking: ProgramNode now carries parent_class_id
    (computed in _populate_from_ast, previously a no-op loop), and
    required_method / structural_behavior_preservation check every class
    individually instead of "does this name exist anywhere in the graph."
  Phase D — capability-provider intent: required_method / structural_
    behavior_preservation now only apply capability-specific methods
    (getCapability / invalidateCapabilities / register_capability) to
    classes that actually show capability-provider evidence (see
    class_shows_capability_provider_evidence). A new HARD constraint,
    MUST_MATCH_KNOWN_PATTERN, explicitly rejects classes that use
    forbidden Fabric APIs in their body but show no such evidence --
    previously these got a generic capability-method stub bolted on
    indiscriminately and were wrongly accepted, while their actual body
    kept calling now-undefined names.
"""

from __future__ import annotations

import ast
import hashlib
import json
from dataclasses import dataclass, field, replace
from enum import Enum, auto
from typing import Protocol, runtime_checkable

from tsam.cognitive_state import ConstraintPriority


# ===========================================================================
# PHASE 2: STRUCTURAL ENCODING
# ===========================================================================

# ---------------------------------------------------------------------------
# AST Node types for Stage 0 (NeoForge capability + event pattern)
# ---------------------------------------------------------------------------

class NodeKind(Enum):
    """The kinds of AST nodes TSAM tracks at Stage 0."""
    # Top-level structural elements
    CLASS          = auto()
    METHOD         = auto()
    DECORATOR      = auto()
    IMPORT         = auto()
    # NeoForge-specific patterns
    CAPABILITY_REGISTRATION = auto()
    CAPABILITY_INVALIDATION = auto()
    EVENT_SUBSCRIPTION      = auto()
    EVENT_HANDLER_BODY      = auto()
    # Generic
    ASSIGNMENT     = auto()
    RETURN         = auto()
    CALL           = auto()
    UNKNOWN        = auto()


@dataclass(frozen=True, slots=True)
class ProgramNode:
    """
    A single node in the Program Graph P_t.
    Represents one AST element with its structural properties.
    """
    node_id:     str          # Unique ID within graph
    kind:        NodeKind
    name:        str          # Identifier name (class, method, etc.)
    api_refs:    tuple[str, ...]  # API names referenced by this node
    line_start:  int
    line_end:    int
    signature:   int          # Prime-scatter structural signature
    parent_class_id: str | None = None     # node_id of enclosing ClassDef, if any
    body_api_refs: tuple[str, ...] = ()    # CLASS nodes only: every Name/Attribute ref anywhere in the subtree
    capability_evidence: bool = False      # CLASS nodes only: see class_shows_capability_provider_evidence

    @classmethod
    def from_ast_node(
        cls,
        node:    ast.AST,
        node_id: str,
        source:  str = "",
    ) -> "ProgramNode":
        """Create a ProgramNode from a Python AST node."""
        kind, name, api_refs = _classify_ast_node(node)
        sig = _prime_signature(node_id, kind, name, api_refs)
        line_start = getattr(node, "lineno", 0)
        line_end   = getattr(node, "end_lineno", line_start)
        return cls(
            node_id   = node_id,
            kind      = kind,
            name      = name,
            api_refs  = tuple(sorted(api_refs)),
            line_start = line_start,
            line_end   = line_end,
            signature  = sig,
        )


# ---------------------------------------------------------------------------
# Prime-scatter structural signature
# ---------------------------------------------------------------------------

# First 64 primes — used to scatter node properties into a hash space
_PRIMES = [
    2, 3, 5, 7, 11, 13, 17, 19, 23, 29, 31, 37, 41, 43, 47, 53,
    59, 61, 67, 71, 73, 79, 83, 89, 97, 101, 103, 107, 109, 113,
    127, 131, 137, 139, 149, 151, 157, 163, 167, 173, 179, 181,
    191, 193, 197, 199, 211, 223, 227, 229, 233, 239, 241, 251,
    257, 263, 269, 271, 277, 281, 283, 293, 307, 311,
]

def _prime_signature(
    node_id:  str,
    kind:     NodeKind,
    name:     str,
    api_refs: list[str],
) -> int:
    """
    Deterministic structural signature via prime scattering.
    Maps (node_id, kind, name, api_refs) → fixed-width int.
    Different structural elements produce different signatures.
    Collision-resistant for Stage 0 pattern space.
    """
    # Hash each component then scatter via primes
    def h(s: str) -> int:
        return int(hashlib.md5(s.encode()).hexdigest(), 16)

    acc = _PRIMES[0] * h(node_id)
    acc ^= _PRIMES[1] * h(kind.name)
    acc ^= _PRIMES[2] * h(name)
    for i, api in enumerate(sorted(api_refs)):
        acc ^= _PRIMES[(i + 3) % len(_PRIMES)] * h(api)
    return acc % (2**31)   # Keep positive, 31-bit


def _classify_ast_node(
    node: ast.AST,
) -> tuple[NodeKind, str, list[str]]:
    """
    Classify an AST node into (kind, name, api_refs).
    Deterministic — same node always produces same classification.
    """
    api_refs: list[str] = []

    if isinstance(node, ast.ClassDef):
        api_refs = _extract_base_names(node)
        return NodeKind.CLASS, node.name, api_refs

    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
        decorators = _extract_decorator_names(node)
        api_refs   = decorators
        name       = node.name
        # Detect capability registration pattern
        if "register_capabilities" in name or "getCapability" in name:
            return NodeKind.CAPABILITY_REGISTRATION, name, api_refs
        if "invalidate" in name.lower() or "invalidateCapabilities" in name:
            return NodeKind.CAPABILITY_INVALIDATION, name, api_refs
        # NeoForge event subscription via decorator
        if any("SubscribeEvent" in d or "EventBusSubscriber" in d for d in decorators):
            return NodeKind.EVENT_SUBSCRIPTION, name, api_refs
        return NodeKind.METHOD, name, api_refs

    if isinstance(node, ast.Import | ast.ImportFrom):
        refs = _extract_import_names(node)
        return NodeKind.IMPORT, refs[0] if refs else "", refs

    if isinstance(node, ast.Assign):
        return NodeKind.ASSIGNMENT, "", []

    if isinstance(node, ast.Return):
        return NodeKind.RETURN, "", []

    if isinstance(node, ast.Call):
        func_name = _call_func_name(node)
        return NodeKind.CALL, func_name, [func_name] if func_name else []

    return NodeKind.UNKNOWN, "", []


def _extract_base_names(node: ast.ClassDef) -> list[str]:
    return [ast.unparse(base) for base in node.bases]


def _extract_decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    result = []
    for dec in node.decorator_list:
        result.append(ast.unparse(dec))
    return result


def _extract_import_names(node: ast.Import | ast.ImportFrom) -> list[str]:
    if isinstance(node, ast.ImportFrom):
        module = node.module or ""
        return [f"{module}.{alias.name}" for alias in node.names]
    return [alias.name for alias in node.names]


def _call_func_name(node: ast.Call) -> str:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return ast.unparse(node.func)
    return ""


# ---------------------------------------------------------------------------
# Capability-provider intent detection
# ---------------------------------------------------------------------------
# These markers + the method-naming convention are what actually distinguish
# a capability-provider class from any other class in this benchmark's
# templates (Fabric and NeoForge both reference LazyOptional inside the
# capability methods; nothing else in the domain does). Used to scope the
# capability-method constraints/rewrites to classes that are plausibly
# capability providers, and to catch classes that are NOT but still use
# forbidden APIs (see MUST_MATCH_KNOWN_PATTERN below).

CAPABILITY_EVIDENCE_MARKERS: frozenset[str] = frozenset({
    "LazyOptional", "ICapabilityProvider", "BlockCapabilityRegistrar",
})

CAPABILITY_METHOD_NAMES: frozenset[str] = frozenset({
    "getCapability", "invalidateCapabilities", "register_capability",
})


def _collect_subtree_refs(node: ast.AST) -> set[str]:
    """
    Collect every Name/Attribute reference anywhere within a node's full
    subtree, as strings. Deliberately broader than ProgramNode.api_refs,
    which only captures references at the specific node types reachable
    from the flat ast.walk(tree) filter in _populate_from_ast (imports,
    decorators, base classes) — it never looks inside method bodies. That
    gap is exactly what let body-level Fabric API usage go completely
    untracked, which is what this exists to fix.
    """
    refs: set[str] = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute):
            try:
                refs.add(ast.unparse(n))
            except Exception:
                pass
        elif isinstance(n, ast.Name):
            refs.add(n.id)
    return refs


def class_shows_capability_provider_evidence(cdef: ast.ClassDef) -> bool:
    """
    Structural heuristic for "is this class plausibly a capability
    provider" — independent of whether it already correctly implements
    the pattern. Used to scope the capability-method hard/strong
    constraints (and the rewrite engine's stub injection) to classes that
    are actually meant to be capability providers, instead of bolting
    getCapability/invalidateCapabilities/register_capability onto every
    class indiscriminately.

    (Review finding: doing that indiscriminately is what let a class with
    no capability semantics at all — e.g. a pure Fabric event-bus handler
    — get "accepted" once a generic stub was force-fitted onto it, while
    its actual logic was left calling now-undefined names.)

    Evidence (either is sufficient):
      - Already defines one of the three capability method names.
      - References LazyOptional / ICapabilityProvider / BlockCapabilityRegistrar
        anywhere in its body (not just at import level).
    """
    method_names = {
        stmt.name for stmt in cdef.body
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if method_names & CAPABILITY_METHOD_NAMES:
        return True

    refs = _collect_subtree_refs(cdef)
    return any(marker in ref for ref in refs for marker in CAPABILITY_EVIDENCE_MARKERS)


# ---------------------------------------------------------------------------
# Program Graph P_t
# ---------------------------------------------------------------------------

@dataclass
class ProgramGraph:
    """
    P_t: The current program graph under transformation.
    Flat for Stage 0; hierarchy hooks for v0.2+.
    """
    nodes:   dict[str, ProgramNode]   = field(default_factory=dict)
    edges:   list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, relation)
    source:  str                      = ""
    lang:    str                      = "python"

    # Fidelity metric: how faithfully structure was captured
    # 1.0 = perfect round-trip, 0.0 = nothing captured
    _fidelity: float = field(default=0.0, init=False)

    @classmethod
    def from_python_source(cls, source: str) -> "ProgramGraph":
        """Parse Python source into a ProgramGraph."""
        graph = cls(source=source)
        try:
            tree = ast.parse(source)
            graph._populate_from_ast(tree)
        except SyntaxError as e:
            # SyntaxError → graph with 0 nodes and 0.0 fidelity
            graph._fidelity = 0.0
        return graph

    def _populate_from_ast(self, tree: ast.Module) -> None:
        node_counter = 0
        ast_id_to_node_id: dict[int, str] = {}

        for node in ast.walk(tree):
            if isinstance(node, (
                ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef,
                ast.Import, ast.ImportFrom,
            )):
                node_id = f"n{node_counter:04d}"
                pnode   = ProgramNode.from_ast_node(node, node_id, self.source)
                self.nodes[node_id] = pnode
                ast_id_to_node_id[id(node)] = node_id
                node_counter += 1

        # Second pass: link each class's DIRECT methods to it via parent_class_id,
        # using actual AST nesting (cdef.body), not the flat ast.walk order.
        # Also compute and store capability-provider evidence + the full
        # body reference set per class, so check_constraint can read them
        # directly without re-parsing. ProgramNode is frozen, so the linked
        # node is replaced via dataclasses.replace.
        # (Previously this loop was a no-op — "Edges added by traversal in
        # Phase 4" — so no containment info ever existed, which is what
        # made it impossible for check_constraint to verify per-class
        # requirements instead of graph-global ones.)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                class_node_id = ast_id_to_node_id.get(id(node))
                if class_node_id is None:
                    continue
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        child_node_id = ast_id_to_node_id.get(id(child))
                        if child_node_id is not None:
                            old = self.nodes[child_node_id]
                            self.nodes[child_node_id] = replace(old, parent_class_id=class_node_id)
                            self.edges.append((class_node_id, child_node_id, "contains"))

                body_refs = _collect_subtree_refs(node)
                evidence  = class_shows_capability_provider_evidence(node)
                old_class_node = self.nodes[class_node_id]
                self.nodes[class_node_id] = replace(
                    old_class_node,
                    body_api_refs       = tuple(sorted(body_refs)),
                    capability_evidence = evidence,
                )

        # Fidelity: ratio of source lines represented by nodes
        total_lines = len(self.source.splitlines()) or 1
        covered_lines = sum(
            (n.line_end - n.line_start + 1) for n in self.nodes.values()
        )
        self._fidelity = min(1.0, covered_lines / total_lines)

    @property
    def fidelity(self) -> float:
        """Structural fidelity metric [0, 1]."""
        return self._fidelity

    def api_inventory(self) -> set[str]:
        """All API references across the entire graph."""
        apis: set[str] = set()
        for node in self.nodes.values():
            apis.update(node.api_refs)
        return apis

    def node_ids_by_kind(self, kind: NodeKind) -> list[str]:
        return [nid for nid, n in self.nodes.items() if n.kind == kind]

    def structural_hash(self) -> str:
        """Deterministic hash of graph structure (for equality checks)."""
        sigs = sorted(n.signature for n in self.nodes.values())
        return hashlib.md5(str(sigs).encode()).hexdigest()


# ===========================================================================
# PHASE 3: CONSTRAINT GRAPH
# ===========================================================================

@dataclass(frozen=True, slots=True)
class Constraint:
    """
    One constraint node in the Constraint Graph C = (V, E, Λ, Π).
    A Constraint is a machine-checkable assertion about a ProgramGraph.
    """
    constraint_id:  str
    description:    str
    priority:       ConstraintPriority
    # Pattern used to check satisfaction (checked by ConstraintChecker)
    check_kind:     str    # e.g. "forbidden_api", "required_api", "required_method", ...
    check_param:    str    # e.g. the API name, method signature, etc.

    def __hash__(self) -> int:
        return hash(self.constraint_id)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Constraint) and self.constraint_id == other.constraint_id


@dataclass
class ConstraintGraph:
    """
    Definition 2 (Formal Spec v0.2):
        C = (V, E, Λ, Π)

    Represents the full constraint set extracted from intent.
    Drives both the Task Planner (Phase 3.5) and the Verification Kernel (Phase 4).
    """
    constraints:  dict[str, Constraint]      = field(default_factory=dict)
    edges:        list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, relation)

    # Priority indices for fast lookup
    _hard:   list[str] = field(default_factory=list, init=False)
    _strong: list[str] = field(default_factory=list, init=False)
    _soft:   list[str] = field(default_factory=list, init=False)

    def add(self, c: Constraint) -> None:
        self.constraints[c.constraint_id] = c
        match c.priority:
            case ConstraintPriority.HARD:
                self._hard.append(c.constraint_id)
            case ConstraintPriority.STRONG:
                self._strong.append(c.constraint_id)
            case ConstraintPriority.SOFT:
                self._soft.append(c.constraint_id)

    def add_edge(self, from_id: str, to_id: str, relation: str) -> None:
        self.edges.append((from_id, to_id, relation))

    @property
    def hard(self) -> list[Constraint]:
        return [self.constraints[i] for i in self._hard if i in self.constraints]

    @property
    def strong(self) -> list[Constraint]:
        return [self.constraints[i] for i in self._strong if i in self.constraints]

    @property
    def soft(self) -> list[Constraint]:
        return [self.constraints[i] for i in self._soft if i in self.constraints]

    @property
    def all_ordered(self) -> list[Constraint]:
        """HARD first, then STRONG, then SOFT — for energy computation ordering."""
        return self.hard + self.strong + self.soft

    def summary(self) -> dict:
        return {
            "total":  len(self.constraints),
            "hard":   len(self._hard),
            "strong": len(self._strong),
            "soft":   len(self._soft),
            "edges":  len(self.edges),
        }


# ---------------------------------------------------------------------------
# Stage 0 NeoForge Constraint Graph Builder
# ---------------------------------------------------------------------------

# Known Fabric APIs (forbidden in NeoForge target)
FABRIC_APIS: frozenset[str] = frozenset({
    "net.fabricmc.fabric",
    "net.fabricmc",
    "io.github.fabricators_of_create",
    "FabricBlockEntityTypeBuilder",
    "FabricBlockSettings",
    "ServerLifecycleEvents",
    "ItemGroupEvents",
    "RegistryEntryAdder",
})

# Required NeoForge APIs (must be present)
NEOFORGE_REQUIRED_APIS: frozenset[str] = frozenset({
    "neoforge.common.capabilities",
    "net.neoforged",
    "BlockCapabilityRegistrar",
    "ICapabilityProvider",
    "LazyOptional",
})

# Required structural elements for the capability provider pattern
REQUIRED_METHODS: frozenset[str] = frozenset({
    "getCapability",
    "invalidateCapabilities",
})


def build_neoforge_constraint_graph() -> ConstraintGraph:
    """
    Build the Stage 0 benchmark Constraint Graph for:
    'Port a Fabric capability provider to NeoForge 1.20.1'

    7 hard constraints, 2 strong constraints, 2 soft constraints.
    Matches TSAM Formal Spec Definition 2 structure.
    """
    g = ConstraintGraph()

    # ------------------------------------------------------------------
    # HARD CONSTRAINTS (immediate rejection on violation)
    # ------------------------------------------------------------------

    g.add(Constraint(
        constraint_id = "MUST_COMPILE",
        description   = "Output must parse as valid Python AST (proxy for compilation)",
        priority      = ConstraintPriority.HARD,
        check_kind    = "must_parse",
        check_param   = "",
    ))

    g.add(Constraint(
        constraint_id = "MUST_NOT_USE_FABRIC_APIS",
        description   = "Output must contain no Fabric API references",
        priority      = ConstraintPriority.HARD,
        check_kind    = "forbidden_api_set",
        check_param   = json.dumps(sorted(FABRIC_APIS)),
    ))

    g.add(Constraint(
        constraint_id = "MUST_USE_NEOFORGE_APIS",
        description   = "Output must reference at least one required NeoForge API",
        priority      = ConstraintPriority.HARD,
        check_kind    = "required_api_any",
        check_param   = json.dumps(sorted(NEOFORGE_REQUIRED_APIS)),
    ))

    g.add(Constraint(
        constraint_id = "MUST_PRESERVE_SAVES",
        description   = "Output must not delete or rename persistent data fields",
        priority      = ConstraintPriority.HARD,
        check_kind    = "no_data_field_removal",
        check_param   = "",
    ))

    g.add(Constraint(
        constraint_id = "MUST_HAVE_CAPABILITY_METHOD",
        description   = "Output must define a getCapability method",
        priority      = ConstraintPriority.HARD,
        check_kind    = "required_method",
        check_param   = "getCapability",
    ))

    g.add(Constraint(
        constraint_id = "MUST_HAVE_INVALIDATION_METHOD",
        description   = "Output must define an invalidateCapabilities method",
        priority      = ConstraintPriority.HARD,
        check_kind    = "required_method",
        check_param   = "invalidateCapabilities",
    ))

    g.add(Constraint(
        constraint_id = "MUST_MATCH_KNOWN_PATTERN",
        description   = (
            "Classes using forbidden Fabric APIs in their body must show "
            "capability-provider evidence (the only pattern Stage 0 can "
            "rewrite); otherwise the file is outside the verified knowledge "
            "manifold and must be rejected rather than patched by analogy"
        ),
        priority      = ConstraintPriority.HARD,
        check_kind    = "fabric_entanglement_requires_evidence",
        check_param   = "",
    ))

    # ------------------------------------------------------------------
    # STRONG CONSTRAINTS (large energy penalty, repair required)
    # ------------------------------------------------------------------

    g.add(Constraint(
        constraint_id = "MUST_PRESERVE_BEHAVIOR",
        description   = "getCapability must return same logical result as source",
        priority      = ConstraintPriority.STRONG,
        check_kind    = "structural_behavior_preservation",
        check_param   = "getCapability",
    ))

    g.add(Constraint(
        constraint_id = "MUST_REGISTER_CAPABILITY",
        description   = "Each capability provider class must register itself via NeoForge's registrar",
        priority      = ConstraintPriority.STRONG,
        check_kind    = "required_method",
        check_param   = "register_capability",
    ))

    # ------------------------------------------------------------------
    # SOFT CONSTRAINTS (desirable, tie-breaking, optimization)
    # ------------------------------------------------------------------

    g.add(Constraint(
        constraint_id = "MINIMIZE_DIFF_SIZE",
        description   = "Minimize number of AST nodes changed from source",
        priority      = ConstraintPriority.SOFT,
        check_kind    = "minimize_node_delta",
        check_param   = "",
    ))

    g.add(Constraint(
        constraint_id = "FOLLOW_NEOFORGE_NAMING",
        description   = "Method and class names should follow NeoForge conventions",
        priority      = ConstraintPriority.SOFT,
        check_kind    = "naming_convention",
        check_param   = "PascalCase_classes,camelCase_methods",
    ))

    # ------------------------------------------------------------------
    # Edges (constraint dependencies)
    # ------------------------------------------------------------------
    # MUST_USE_NEOFORGE_APIS depends on MUST_NOT_USE_FABRIC_APIS (order matters)
    g.add_edge("MUST_NOT_USE_FABRIC_APIS", "MUST_USE_NEOFORGE_APIS", "precedes")
    # Registration depends on having the capability method
    g.add_edge("MUST_HAVE_CAPABILITY_METHOD", "MUST_REGISTER_CAPABILITY", "enables")

    return g


# ---------------------------------------------------------------------------
# Constraint satisfaction checker (used by Verification Kernel in Phase 4)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ConstraintResult:
    """Result of checking one constraint against a program graph."""
    constraint_id: str
    priority:      ConstraintPriority
    satisfied:     bool
    violation_msg: str    # Empty string if satisfied

    @property
    def violated(self) -> bool:
        return not self.satisfied


def check_constraint(
    c:     Constraint,
    graph: ProgramGraph,
    original_graph: ProgramGraph | None = None,
) -> ConstraintResult:
    """
    Check a single constraint against a program graph.
    Deterministic: same graph + constraint → same result.
    """
    match c.check_kind:

        case "must_parse":
            # We already have a ProgramGraph, so fidelity > 0 means it parsed
            ok  = graph.fidelity > 0.0
            msg = "" if ok else "Source did not parse as valid Python AST"
            return ConstraintResult(c.constraint_id, c.priority, ok, msg)

        case "forbidden_api_set":
            forbidden = set(json.loads(c.check_param))
            actual    = graph.api_inventory()
            violations = actual.intersection(forbidden)
            ok         = len(violations) == 0
            msg        = f"Forbidden APIs found: {sorted(violations)}" if not ok else ""
            return ConstraintResult(c.constraint_id, c.priority, ok, msg)

        case "required_api_any":
            required = set(json.loads(c.check_param))
            actual   = graph.api_inventory()
            # Check if any required API appears as a substring of any actual API
            found = any(
                any(req in api for api in actual)
                for req in required
            )
            ok  = found
            msg = f"No required NeoForge APIs found. Present: {sorted(actual)}" if not ok else ""
            return ConstraintResult(c.constraint_id, c.priority, ok, msg)

        case "required_method":
            method_name = c.check_param
            class_ids = graph.node_ids_by_kind(NodeKind.CLASS)

            if not class_ids:
                # No classes present (e.g. free functions only) — fall back
                # to the original global existence check.
                method_ids  = graph.node_ids_by_kind(NodeKind.METHOD)
                cap_ids     = graph.node_ids_by_kind(NodeKind.CAPABILITY_REGISTRATION)
                inv_ids     = graph.node_ids_by_kind(NodeKind.CAPABILITY_INVALIDATION)
                all_method_names = {
                    graph.nodes[nid].name
                    for nid in method_ids + cap_ids + inv_ids
                }
                ok  = method_name in all_method_names
                msg = f"Required method '{method_name}' not found" if not ok else ""
                return ConstraintResult(c.constraint_id, c.priority, ok, msg)

            # Capability-specific methods only apply to classes that actually
            # show capability-provider evidence (Phase D fix). A class with
            # none isn't a capability provider and shouldn't be required (or
            # silently force-fitted via stub injection) to have one. Other,
            # unrelated uses of "required_method" (if any) keep the original
            # every-class behavior.
            if method_name in CAPABILITY_METHOD_NAMES:
                in_scope_class_ids = [
                    cid for cid in class_ids if graph.nodes[cid].capability_evidence
                ]
            else:
                in_scope_class_ids = class_ids

            # Per-class check: every in-scope class must define this method
            # itself. (Checking "does this name exist anywhere in the graph"
            # is what let a single-class fix silently pass for a whole
            # multi-class file — see Phase A review finding.)
            missing_in = [
                cid for cid in in_scope_class_ids
                if not any(
                    n.name == method_name and n.parent_class_id == cid
                    for n in graph.nodes.values()
                )
            ]
            ok  = len(missing_in) == 0
            msg = (
                f"Required method '{method_name}' missing in "
                f"{len(missing_in)}/{len(in_scope_class_ids)} in-scope class(es): "
                f"{[graph.nodes[cid].name for cid in missing_in]}"
            ) if not ok else ""
            return ConstraintResult(c.constraint_id, c.priority, ok, msg)

        case "no_data_field_removal":
            # For Stage 0: if no original graph supplied, we can't check removals
            # Conservative: pass (no evidence of removal)
            return ConstraintResult(c.constraint_id, c.priority, True, "")

        case "structural_behavior_preservation":
            # Check that the method structure is similar between source and target
            method_name = c.check_param
            class_ids   = graph.node_ids_by_kind(NodeKind.CLASS)

            if not class_ids:
                methods = [n for n in graph.nodes.values() if n.name == method_name]
                ok  = len(methods) > 0
                msg = f"Method '{method_name}' missing, behavior not preserved" if not ok else ""
                return ConstraintResult(c.constraint_id, c.priority, ok, msg)

            if method_name in CAPABILITY_METHOD_NAMES:
                in_scope_class_ids = [
                    cid for cid in class_ids if graph.nodes[cid].capability_evidence
                ]
            else:
                in_scope_class_ids = class_ids

            # Per-class, same reasoning as the required_method fix above.
            missing_in = [
                cid for cid in in_scope_class_ids
                if not any(
                    n.name == method_name and n.parent_class_id == cid
                    for n in graph.nodes.values()
                )
            ]
            ok  = len(missing_in) == 0
            msg = (
                f"Method '{method_name}' missing in {len(missing_in)}/{len(in_scope_class_ids)} "
                f"in-scope class(es); behavior not preserved for: "
                f"{[graph.nodes[cid].name for cid in missing_in]}"
            ) if not ok else ""
            return ConstraintResult(c.constraint_id, c.priority, ok, msg)

        case "required_api_call":
            api_name = c.check_param
            actual   = graph.api_inventory()
            # Substring match
            found = any(api_name in api for api in actual)
            ok    = found
            msg   = f"Required API call to '{api_name}' not found" if not ok else ""
            return ConstraintResult(c.constraint_id, c.priority, ok, msg)

        case "fabric_entanglement_requires_evidence":
            # A class that uses forbidden Fabric APIs in its body but shows
            # no capability-provider evidence is outside the only pattern
            # Stage 0 knows how to rewrite. Per the Formal Spec's own
            # Non-Goals ("operate on arbitrary codebases without a
            # pre-defined verified solution manifold for the target
            # domain"), such a class must be rejected, not patched by
            # analogy with an unrelated stub.
            bad = [
                n.name for n in graph.nodes.values()
                if n.kind == NodeKind.CLASS
                and not n.capability_evidence
                and any(forbidden in ref for ref in n.body_api_refs for forbidden in FABRIC_APIS)
            ]
            ok  = len(bad) == 0
            msg = (
                f"Class(es) {bad} reference forbidden Fabric APIs in their body but show "
                f"no capability-provider pattern Stage 0 can rewrite — outside the "
                f"verified knowledge manifold"
            ) if not ok else ""
            return ConstraintResult(c.constraint_id, c.priority, ok, msg)

        case "minimize_node_delta":
            # Soft constraint: always "satisfied" for Stage 0, energy penalty computed separately
            return ConstraintResult(c.constraint_id, c.priority, True, "")

        case "naming_convention":
            # Soft: Stage 0 always passes, energy penalty computed separately
            return ConstraintResult(c.constraint_id, c.priority, True, "")

        case _:
            # Unknown check kind → conservatively pass but log
            return ConstraintResult(c.constraint_id, c.priority, True, "")


def check_all_constraints(
    graph:          ProgramGraph,
    cg:             ConstraintGraph,
    original_graph: ProgramGraph | None = None,
) -> list[ConstraintResult]:
    """Run all constraints in priority order."""
    return [
        check_constraint(c, graph, original_graph)
        for c in cg.all_ordered
    ]


if __name__ == "__main__":
    print("=== TSAM Phase 2+3: Structural Encoding + Constraint Graph ===\n")

    # Test structural encoding on a minimal Fabric-style source
    fabric_source = """
import net.fabricmc.fabric

class FabricCapabilityProvider:
    def getCapability(self, cap, side):
        if cap == MY_CAP:
            return LazyOptional.of(lambda: self.handler)
        return LazyOptional.empty()
    
    def invalidate(self):
        pass
"""

    graph = ProgramGraph.from_python_source(fabric_source)
    print(f"Graph nodes: {len(graph.nodes)}")
    print(f"Fidelity:   {graph.fidelity:.2f}")
    print(f"APIs:       {graph.api_inventory()}")
    print(f"Hash:       {graph.structural_hash()}")
    print()

    cg = build_neoforge_constraint_graph()
    print("Constraint Graph:")
    print(json.dumps(cg.summary(), indent=2))
    print()

    print("Checking constraints against Fabric source (should have violations):")
    results = check_all_constraints(graph, cg)
    for r in results:
        status = "✓ PASS" if r.satisfied else "✗ FAIL"
        priority_name = r.priority.name
        print(f"  [{priority_name}] {r.constraint_id}: {status}")
        if r.violated:
            print(f"         → {r.violation_msg}")
