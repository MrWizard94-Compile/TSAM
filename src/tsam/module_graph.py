"""
TSAM Stage 1 — Phase 1.0: Multi-Module Program Representation
============================================================
Stage 0 operated on a flat, single-file ``ProgramGraph``. Real software is
organized across modules with import/export relationships. This module
introduces the *persistent* multi-module representation P_t that Stage 1's
bounded active window W_t, demand-driven resolver, and Cross-module
Consistency penalty C will operate over (those are subsequent slices; this
slice provides only the representation and the static structure it derives).

What this slice deliberately does NOT do (later Stage 1 slices):
  - It does not build the bounded Active Window W_t or its LRU eviction.
  - It does not perform multi-hop / transitive import resolution.
  - It does not compute the Cross-module Consistency penalty C or change
    any acceptance behaviour.
It produces the inputs all of those will consume, and nothing more.

Design decisions tied to the Stage 0 invariants (see CLAUDE.md §5):
  - Bounded Executive State (Inv. 5.1): a ``ModuleDescriptor`` is a small,
    fixed-shape view. Its size scales only with the (capped) export/import
    counts, never with the module's source length. The heavy per-module
    artifact (the parsed ``ProgramGraph``) is stored separately in
    ``ModuleGraph.program_graphs`` — that is allowed to grow with project
    size (it is artifact memory, not executive state).
  - Determinism (Inv. 5.2): module identity is a content-free SHA-256 of the
    canonical path; every collection that is hashed or serialised is sorted
    first (Python's per-process string-hash randomisation makes raw set/dict
    iteration order non-deterministic). ``last_accessed`` is a *logical*
    tick, not a wall-clock timestamp, so LRU ordering is reproducible.
  - Clean Rejection (Inv. 5.4): an unparseable module is recorded with
    ``parse_ok = False`` and empty exports/imports rather than being
    silently treated as an empty-but-valid module; a cap that truncates a
    module's exports/imports sets a ``*_truncated`` flag so downstream
    resolution can treat the module conservatively instead of mis-resolving.
  - Separation of Concerns (Inv. 5.5): descriptors (the bounded view) and
    program graphs (the artifact) are stored in distinct maps, and
    ``last_accessed`` is excluded from the structural hash because it is
    window/executive state, not part of the artifact's identity.

No attention, no neural components, stdlib only — consistent with Stage 0.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace

from tsam.constraint_graph import (
    NodeKind,
    ProgramGraph,
    class_shows_capability_provider_evidence,
)

__all__ = [
    "MAX_EXPORTS_PER_MODULE",
    "MAX_IMPORTS_PER_MODULE",
    "canonicalize_module_path",
    "module_id_for_path",
    "ModuleImport",
    "ModuleExport",
    "ModuleDescriptor",
    "CrossModuleImport",
    "DanglingImport",
    "ModuleGraph",
]


# ---------------------------------------------------------------------------
# Bounds (Invariant 5.1 guards). Hard caps, not soft targets: a module whose
# public surface exceeds these is truncated deterministically and flagged.
# Sized generously relative to the Stage 1 active-window limits (Active
# Modules = 32) so that a normal module never approaches them; they exist to
# bound worst-case descriptor size, not to shape ordinary inputs.
# ---------------------------------------------------------------------------

MAX_EXPORTS_PER_MODULE: int = 256
MAX_IMPORTS_PER_MODULE: int = 256


# ---------------------------------------------------------------------------
# Module identity
# ---------------------------------------------------------------------------

def canonicalize_module_path(path: str) -> str:
    """
    Normalise a module path to its canonical form for identity + resolution.

    Modules are identified by a dotted logical path (e.g. ``"mod.core"``).
    Normalisation strips surrounding whitespace, converts any path
    separators to ``.``, collapses repeated separators, and strips a
    trailing ``.py``/``.pyi`` suffix if present, so that a module written as
    ``"mod/core.py"`` and one written as ``"mod.core"`` resolve to the same
    identity. The result is deterministic and idempotent.
    """
    p = path.strip()
    if not p:
        raise ValueError("module path must be a non-empty string")
    # Unify separators to '.'
    for sep in ("\\", "/"):
        p = p.replace(sep, ".")
    # Strip a single recognised source suffix.
    for suffix in (".py", ".pyi"):
        if p.endswith(suffix):
            p = p[: -len(suffix)]
            break
    # Collapse repeated dots and strip leading/trailing dots.
    parts = [seg for seg in p.split(".") if seg]
    if not parts:
        raise ValueError(f"module path {path!r} canonicalises to empty")
    return ".".join(parts)


def module_id_for_path(path: str) -> str:
    """
    Deterministic module id: full SHA-256 hex digest of the canonical path
    (Stage 1 Spec §1.2). Content-free — depends only on identity, so two
    builds of the same project produce identical ids.
    """
    canonical = canonicalize_module_path(path)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Import / export records
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ModuleImport:
    """
    One module-level import binding.

    For ``from a.b import X``      → target_path="a.b",  symbol="X".
    For ``from a.b import X as Y`` → target_path="a.b",  symbol="X" (the
        *source* symbol, which is what resolution follows; the local alias
        ``Y`` is irrelevant to cross-module identity).
    For ``import a.b``             → target_path="a.b",  symbol="a.b".
    For ``import a.b as c``        → target_path="a.b",  symbol="a.b".
    For ``from a.b import *``      → target_path="a.b",  symbol="*", is_star=True.

    ``target_module_id`` is the id of the in-project module that
    ``target_path`` resolves to, or ``None`` if the target is external to the
    project (stdlib / third-party / simply absent). Resolution is by exact
    canonical-path match and is filled in by :meth:`ModuleGraph.build`.
    """
    target_path:      str
    symbol:           str
    target_module_id: str | None = None
    is_star:          bool = False
    is_relative:      bool = False

    @property
    def is_internal(self) -> bool:
        """True if this import resolves to a module within the project."""
        return self.target_module_id is not None

    def _sort_key(self) -> tuple[str, str, str]:
        return (self.target_path, self.symbol, self.target_module_id or "")

    def to_dict(self) -> dict:
        return {
            "target_path":      self.target_path,
            "symbol":           self.symbol,
            "target_module_id": self.target_module_id,
            "is_star":          self.is_star,
            "is_relative":      self.is_relative,
        }


@dataclass(frozen=True, slots=True)
class ModuleExport:
    """
    One publicly exported symbol.

    ``origin_path`` is ``None`` for a locally-defined export (class, function,
    or module-level constant defined in this module) and is set to the source
    module's path for a *re-export* (a name made public here but bound by an
    import from another module, e.g. ``__all__ = ["X"]`` where ``X`` came from
    ``from other import X``). Re-export provenance is what the later
    transitive resolver needs to follow ``B re-exports s from C`` chains.
    """
    symbol:      str
    origin_path: str | None = None

    @property
    def is_reexport(self) -> bool:
        return self.origin_path is not None

    def _sort_key(self) -> tuple[str, str]:
        return (self.symbol, self.origin_path or "")

    def to_dict(self) -> dict:
        return {"symbol": self.symbol, "origin_path": self.origin_path}


# ---------------------------------------------------------------------------
# Module descriptor (Stage 1 Spec §1.2)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ModuleDescriptor:
    """
    m = (id, canonical_path, exports, imports, capability_evidence,
         last_accessed)  — plus parse/truncation status for Clean Rejection.

    Immutable. ``last_accessed`` is a *logical* tick set by the working-set
    manager (Stage 1 slice 2), not a wall-clock timestamp — see module
    docstring (Determinism invariant). It is intentionally excluded from
    :meth:`structural_signature`.
    """
    module_id:           str
    canonical_path:      str
    exports:             tuple[ModuleExport, ...]   # sorted, capped
    imports:             tuple[ModuleImport, ...]   # sorted, capped
    capability_evidence: bool
    parse_ok:            bool
    exports_truncated:   bool = False
    imports_truncated:   bool = False
    last_accessed:       int = 0

    # -- derived views -------------------------------------------------------

    def exported_symbols(self) -> frozenset[str]:
        """The set of symbol names other modules can import from this one."""
        return frozenset(e.symbol for e in self.exports)

    def exports_symbol(self, symbol: str) -> bool:
        """True if ``symbol`` is exported (``*`` star imports always 'match')."""
        return symbol == "*" or symbol in self.exported_symbols()

    def reexports(self) -> tuple[ModuleExport, ...]:
        """The re-exported subset of exports (those with an origin module)."""
        return tuple(e for e in self.exports if e.is_reexport)

    def internal_imports(self) -> tuple[ModuleImport, ...]:
        """Imports that resolved to an in-project module."""
        return tuple(i for i in self.imports if i.is_internal)

    # -- working-set support (deterministic; used by slice 2) ---------------

    def touched(self, tick: int) -> "ModuleDescriptor":
        """
        Return a copy with ``last_accessed`` advanced to ``tick``.

        The caller (the working-set manager) owns the logical clock; this is
        the only sanctioned way the access time changes, keeping LRU ordering
        a pure function of the deterministic admission sequence.
        """
        if tick < self.last_accessed:
            raise ValueError(
                f"logical clock must not move backwards: "
                f"{tick} < {self.last_accessed}"
            )
        return replace(self, last_accessed=tick)

    # -- identity / serialisation -------------------------------------------

    def structural_signature(self) -> str:
        """
        A deterministic content signature for this descriptor, excluding
        ``last_accessed`` (window state, not artifact identity). Used by
        :meth:`ModuleGraph.structural_hash`.
        """
        parts: list[str] = [
            f"id={self.module_id}",
            f"path={self.canonical_path}",
            f"cap={int(self.capability_evidence)}",
            f"parse_ok={int(self.parse_ok)}",
            f"exp_trunc={int(self.exports_truncated)}",
            f"imp_trunc={int(self.imports_truncated)}",
        ]
        for e in self.exports:  # already sorted at construction
            parts.append(f"E:{e.symbol}<-{e.origin_path or ''}")
        for i in self.imports:  # already sorted at construction
            parts.append(
                f"I:{i.target_path}.{i.symbol}->{i.target_module_id or ''}"
                f"{'*' if i.is_star else ''}{'~' if i.is_relative else ''}"
            )
        return "|".join(parts)

    def to_dict(self) -> dict:
        return {
            "module_id":           self.module_id,
            "canonical_path":      self.canonical_path,
            "capability_evidence": self.capability_evidence,
            "parse_ok":            self.parse_ok,
            "exports_truncated":   self.exports_truncated,
            "imports_truncated":   self.imports_truncated,
            "last_accessed":       self.last_accessed,
            "exports":             [e.to_dict() for e in self.exports],
            "imports":             [i.to_dict() for i in self.imports],
        }


# ---------------------------------------------------------------------------
# Cross-module relationship records (resolved, project-level)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class CrossModuleImport:
    """A resolved internal import edge: ``source`` imports ``symbol`` from
    ``target`` (both module ids). Star imports are represented with
    ``symbol == '*'``."""
    source_module_id: str
    target_module_id: str
    symbol:           str

    def _sort_key(self) -> tuple[str, str, str]:
        return (self.source_module_id, self.target_module_id, self.symbol)

    def to_dict(self) -> dict:
        return {
            "source_module_id": self.source_module_id,
            "target_module_id": self.target_module_id,
            "symbol":           self.symbol,
        }


@dataclass(frozen=True, slots=True)
class DanglingImport:
    """
    A *dangling internal import*: an import that resolves to an in-project
    module which does not export the requested symbol. This is the
    cross-module analogue of a dangling reference — the unresolved-reference
    signal the Stage 1 Cross-module Consistency penalty C will be built on.
    Reported, not raised: detecting it is this representation's job; deciding
    what it costs is the verifier's (a later slice).
    """
    source_module_id: str
    target_module_id: str
    symbol:           str

    def _sort_key(self) -> tuple[str, str, str]:
        return (self.source_module_id, self.target_module_id, self.symbol)

    def to_dict(self) -> dict:
        return {
            "source_module_id": self.source_module_id,
            "target_module_id": self.target_module_id,
            "symbol":           self.symbol,
        }


# ---------------------------------------------------------------------------
# Static extraction from a parsed module
# ---------------------------------------------------------------------------

def _top_level_bound_names(tree: ast.Module) -> dict[str, str]:
    """
    Map each name bound at module top level to how it was bound:
    ``"def"`` (class/function/async-function), ``"assign"`` (module-level
    constant), or ``"import"`` (bound by an import). When a name is bound more
    than once, a definition or assignment takes precedence over an import
    (so a name that is both imported and locally redefined is treated as
    locally defined, not a re-export).
    """
    binding: dict[str, str] = {}

    def _prefer(name: str, kind: str) -> None:
        # Precedence: def/assign override import; first def/assign wins.
        existing = binding.get(name)
        if existing is None or (existing == "import" and kind != "import"):
            binding[name] = kind

    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            _prefer(stmt.name, "def")
        elif isinstance(stmt, ast.Assign):
            for target in stmt.targets:
                for name in _names_from_target(target):
                    _prefer(name, "assign")
        elif isinstance(stmt, ast.AnnAssign):
            if isinstance(stmt.target, ast.Name):
                _prefer(stmt.target.id, "assign")
        elif isinstance(stmt, (ast.Import, ast.ImportFrom)):
            for alias in stmt.names:
                if alias.name == "*":
                    continue  # star import binds no enumerable names
                local = alias.asname or alias.name.split(".")[0]
                _prefer(local, "import")

    return binding


def _names_from_target(target: ast.expr) -> list[str]:
    """Bound names from an assignment target, including tuple/list unpacking."""
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, (ast.Tuple, ast.List)):
        names: list[str] = []
        for elt in target.elts:
            names.extend(_names_from_target(elt))
        return names
    return []


def _extract_dunder_all(tree: ast.Module) -> frozenset[str] | None:
    """
    Return the set of names in a module-level ``__all__`` if present and
    expressed as a literal list/tuple of string constants, else ``None``.
    A non-literal ``__all__`` is treated as absent (conservative: we do not
    attempt to evaluate it).
    """
    for stmt in tree.body:
        targets: list[ast.expr] = []
        if isinstance(stmt, ast.Assign):
            targets = stmt.targets
        elif isinstance(stmt, ast.AnnAssign):
            targets = [stmt.target]
        else:
            continue
        if not any(isinstance(t, ast.Name) and t.id == "__all__" for t in targets):
            continue
        value = stmt.value
        if isinstance(value, (ast.List, ast.Tuple)):
            names: set[str] = set()
            for elt in value.elts:
                if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                    names.add(elt.value)
            return frozenset(names)
    return None


def _import_origin_paths(tree: ast.Module) -> dict[str, str]:
    """
    Map each top-level name bound by a ``from X import Y[/as Z]`` to the
    source module path ``X`` (used to attribute re-export provenance).
    Plain ``import`` bindings are not re-export sources in the usual sense and
    are omitted.
    """
    origins: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            module = stmt.module or ""
            if stmt.level and stmt.level > 0:
                module = ("." * stmt.level) + module
            for alias in stmt.names:
                if alias.name == "*":
                    continue
                local = alias.asname or alias.name
                origins[local] = module
    return origins


def extract_exports(tree: ast.Module) -> list[ModuleExport]:
    """
    Determine the module's public export surface.

    A name is exported if it is bound at module top level and either:
      - it appears in a literal ``__all__``, or
      - ``__all__`` is absent and the name does not begin with ``"_"``.

    Each export records re-export provenance: if the only top-level binding
    for an exported name is an import, ``origin_path`` is that import's source
    module; otherwise ``origin_path`` is ``None`` (locally defined).
    """
    bound = _top_level_bound_names(tree)
    dunder_all = _extract_dunder_all(tree)
    origins = _import_origin_paths(tree)

    if dunder_all is not None:
        public = {name for name in bound if name in dunder_all}
        # __all__ may legitimately name something not otherwise detectable as
        # a simple top-level binding (rare); include those too, as locally
        # defined, so the declared surface is never under-reported.
        public |= {name for name in dunder_all if name not in bound}
    else:
        public = {name for name in bound if not name.startswith("_")}

    exports: list[ModuleExport] = []
    for name in public:
        kind = bound.get(name)
        origin = origins.get(name) if kind == "import" else None
        exports.append(ModuleExport(symbol=name, origin_path=origin))
    exports.sort(key=ModuleExport._sort_key)
    return exports


def extract_imports(tree: ast.Module) -> list[ModuleImport]:
    """
    Extract module-level imports (top-level statements only — imports nested
    inside functions/classes are local concerns, not part of the module's
    cross-module interface at this stage).
    """
    imports: list[ModuleImport] = []
    for stmt in tree.body:
        if isinstance(stmt, ast.ImportFrom):
            module = stmt.module or ""
            is_relative = bool(stmt.level and stmt.level > 0)
            target_path = (("." * stmt.level) + module) if is_relative else module
            for alias in stmt.names:
                if alias.name == "*":
                    imports.append(ModuleImport(
                        target_path=target_path, symbol="*",
                        is_star=True, is_relative=is_relative,
                    ))
                else:
                    imports.append(ModuleImport(
                        target_path=target_path, symbol=alias.name,
                        is_relative=is_relative,
                    ))
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                imports.append(ModuleImport(
                    target_path=alias.name, symbol=alias.name,
                ))
    imports.sort(key=ModuleImport._sort_key)
    return imports


def _module_has_capability_evidence(tree: ast.Module) -> bool:
    """True if any top-level class in the module is a plausible capability
    provider (reuses the Stage 0 structural heuristic, unchanged)."""
    return any(
        isinstance(stmt, ast.ClassDef)
        and class_shows_capability_provider_evidence(stmt)
        for stmt in tree.body
    )


# ---------------------------------------------------------------------------
# The multi-module graph (persistent P_t)
# ---------------------------------------------------------------------------

@dataclass
class ModuleGraph:
    """
    The persistent multi-module program P_t: every module's descriptor (the
    bounded view) plus its parsed ``ProgramGraph`` (the artifact), keyed by
    module id, together with the resolved cross-module import structure.

    Build with :meth:`build`. All accessors return results in a deterministic
    (sorted) order so that any hash or report derived from a graph is stable
    across runs and across input ordering.
    """
    descriptors:    dict[str, ModuleDescriptor] = field(default_factory=dict)
    program_graphs: dict[str, ProgramGraph]     = field(default_factory=dict)
    _id_by_path:    dict[str, str]              = field(default_factory=dict)

    # -- construction --------------------------------------------------------

    @classmethod
    def build(cls, sources: Mapping[str, str]) -> "ModuleGraph":
        """
        Build a ``ModuleGraph`` from a mapping of ``path -> source``.

        Two passes for determinism:
          1. Assign every module its canonical path and id, parse it, and
             extract its (unresolved) exports/imports + capability evidence.
          2. Resolve each import's ``target_module_id`` by exact canonical-path
             match against the now-complete id table.

        Raises ``ValueError`` if two input paths canonicalise to the same
        identity (an ambiguous project is rejected loudly, not silently
        collapsed — Clean Rejection).
        """
        graph = cls()

        # Pass 1: identity, parse, raw extraction. Iterate inputs in sorted
        # path order so construction order is itself deterministic.
        pre_imports: dict[str, list[ModuleImport]] = {}
        for raw_path in sorted(sources.keys()):
            canonical = canonicalize_module_path(raw_path)
            module_id = module_id_for_path(canonical)
            if module_id in graph.descriptors:
                clash = graph.descriptors[module_id].canonical_path
                raise ValueError(
                    f"duplicate module identity: {raw_path!r} canonicalises to "
                    f"{canonical!r}, already provided as {clash!r}"
                )
            graph._id_by_path[canonical] = module_id

            source = sources[raw_path]
            program_graph = ProgramGraph.from_python_source(source)
            graph.program_graphs[module_id] = program_graph

            parse_ok = True
            try:
                tree = ast.parse(source)
            except SyntaxError:
                parse_ok = False
                tree = None

            if tree is not None:
                exports = extract_exports(tree)
                imports = extract_imports(tree)
                cap_evidence = _module_has_capability_evidence(tree)
            else:
                exports = []
                imports = []
                cap_evidence = False

            exports, exports_truncated = _cap(exports, MAX_EXPORTS_PER_MODULE)
            imports, imports_truncated = _cap(imports, MAX_IMPORTS_PER_MODULE)
            pre_imports[module_id] = imports

            graph.descriptors[module_id] = ModuleDescriptor(
                module_id           = module_id,
                canonical_path      = canonical,
                exports             = tuple(exports),
                imports             = tuple(imports),  # target ids filled in pass 2
                capability_evidence = cap_evidence,
                parse_ok            = parse_ok,
                exports_truncated   = exports_truncated,
                imports_truncated   = imports_truncated,
            )

        # Pass 2: resolve import targets.
        for module_id, imports in pre_imports.items():
            resolved: list[ModuleImport] = []
            for imp in imports:
                target_id: str | None = None
                if not imp.is_relative:
                    try:
                        target_canonical = canonicalize_module_path(imp.target_path)
                    except ValueError:
                        target_canonical = ""
                    target_id = graph._id_by_path.get(target_canonical)
                resolved.append(replace(imp, target_module_id=target_id))
            resolved.sort(key=ModuleImport._sort_key)
            old = graph.descriptors[module_id]
            graph.descriptors[module_id] = replace(old, imports=tuple(resolved))

        return graph

    # -- lookups -------------------------------------------------------------

    def module_ids(self) -> list[str]:
        """All module ids, sorted (deterministic iteration order)."""
        return sorted(self.descriptors.keys())

    def resolve_path(self, path: str) -> str | None:
        """Resolve a (possibly non-canonical) path to a module id, or None."""
        try:
            canonical = canonicalize_module_path(path)
        except ValueError:
            return None
        return self._id_by_path.get(canonical)

    def descriptor(self, module_id: str) -> ModuleDescriptor:
        """Return the descriptor for ``module_id`` (KeyError if absent)."""
        return self.descriptors[module_id]

    def program_graph(self, module_id: str) -> ProgramGraph:
        """Return the parsed artifact for ``module_id`` (KeyError if absent)."""
        return self.program_graphs[module_id]

    def __len__(self) -> int:
        return len(self.descriptors)

    def __contains__(self, module_id: object) -> bool:
        return module_id in self.descriptors

    # -- derived cross-module structure (all deterministic) -----------------

    def importers_of(self, module_id: str) -> list[str]:
        """Sorted ids of modules that import anything from ``module_id``."""
        return sorted(
            mid for mid, d in self.descriptors.items()
            if any(i.target_module_id == module_id for i in d.imports)
        )

    def cross_module_imports(self) -> list[CrossModuleImport]:
        """
        Every resolved internal import edge, sorted. (One entry per imported
        symbol; star imports appear with ``symbol == '*'``.)
        """
        edges: list[CrossModuleImport] = []
        for mid in self.module_ids():
            for imp in self.descriptors[mid].imports:
                if imp.target_module_id is not None:
                    edges.append(CrossModuleImport(
                        source_module_id=mid,
                        target_module_id=imp.target_module_id,
                        symbol=imp.symbol,
                    ))
        edges.sort(key=CrossModuleImport._sort_key)
        return edges

    def dangling_internal_imports(self) -> list[DanglingImport]:
        """
        Every import that resolves to an in-project module which does not
        export the requested symbol. Star imports are skipped (a star import
        cannot be statically known to be dangling). This is the
        cross-module unresolved-reference signal for the future C penalty.
        """
        dangling: list[DanglingImport] = []
        for mid in self.module_ids():
            for imp in self.descriptors[mid].imports:
                if imp.target_module_id is None or imp.is_star:
                    continue
                target = self.descriptors[imp.target_module_id]
                if not target.exports_symbol(imp.symbol):
                    dangling.append(DanglingImport(
                        source_module_id=mid,
                        target_module_id=imp.target_module_id,
                        symbol=imp.symbol,
                    ))
        dangling.sort(key=DanglingImport._sort_key)
        return dangling

    def capability_modules(self) -> list[str]:
        """Sorted ids of modules that show capability-provider evidence."""
        return sorted(
            mid for mid, d in self.descriptors.items()
            if d.capability_evidence
        )

    def capability_key_index(self) -> dict[str, list[str]]:
        """
        Map each structural capability key to the sorted list of
        ``"<module_id>:<ClassName>"`` locations that declare it, read from the
        per-class ``structural_capability_keys`` the Stage 0 parser already
        computes. A key appearing under more than one location is a candidate
        cross-class / cross-module registration collision — the raw material
        the Stage 1 C penalty will score (this slice only surfaces it).
        """
        index: dict[str, set[str]] = {}
        for mid in self.module_ids():
            pg = self.program_graphs[mid]
            for node in pg.nodes.values():
                if node.kind is not NodeKind.CLASS:
                    continue
                for key in node.structural_capability_keys:
                    index.setdefault(key, set()).add(f"{mid}:{node.name}")
        return {key: sorted(locs) for key, locs in sorted(index.items())}

    def colliding_capability_keys(self) -> dict[str, list[str]]:
        """The subset of :meth:`capability_key_index` with >1 declaring location."""
        return {
            key: locs
            for key, locs in self.capability_key_index().items()
            if len(locs) > 1
        }

    # -- identity / reporting ------------------------------------------------

    def structural_hash(self) -> str:
        """
        Deterministic SHA-256 over the whole project's structure: the sorted
        module ids and each descriptor's :meth:`structural_signature`
        (excluding ``last_accessed``). Independent of input ordering — the
        project-level analogue of ``ProgramGraph.structural_hash`` and the
        basis for an H2-style determinism check at the module layer.
        """
        h = hashlib.sha256()
        for mid in self.module_ids():
            h.update(mid.encode("utf-8"))
            h.update(b"\x00")
            h.update(self.descriptors[mid].structural_signature().encode("utf-8"))
            h.update(b"\x01")
        return h.hexdigest()

    def summary(self) -> dict:
        """Human-readable, deterministic summary of the project structure."""
        cross = self.cross_module_imports()
        dangling = self.dangling_internal_imports()
        collisions = self.colliding_capability_keys()
        return {
            "module_count":          len(self.descriptors),
            "capability_modules":    len(self.capability_modules()),
            "cross_module_edges":    len(cross),
            "dangling_internal":     len(dangling),
            "unparseable_modules":   sum(
                1 for d in self.descriptors.values() if not d.parse_ok
            ),
            "capability_key_collisions": len(collisions),
            "structural_hash":       self.structural_hash(),
        }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _cap(items: list, limit: int) -> tuple[list, bool]:
    """
    Deterministically cap a sorted list to ``limit`` items. Returns
    ``(possibly-truncated list, truncated?)``. Caller passes an already-sorted
    list so truncation keeps a stable, reproducible prefix.
    """
    if len(items) <= limit:
        return items, False
    return items[:limit], True


if __name__ == "__main__":
    import json

    demo = {
        "app.core": (
            "MY_CAPABILITY = object()\n"
            "__all__ = ['MY_CAPABILITY']\n"
        ),
        "app.provider": (
            "from app.core import MY_CAPABILITY\n"
            "from neoforge.common.capabilities import LazyOptional\n"
            "\n"
            "class Provider:\n"
            "    def getCapability(self, cap, direction=None):\n"
            "        if cap == MY_CAPABILITY:\n"
            "            return LazyOptional.of(lambda: self._h)\n"
            "        return LazyOptional.empty()\n"
        ),
        "app.util": (
            "def add(a, b):\n"
            "    return a + b\n"
        ),
    }
    g = ModuleGraph.build(demo)
    print("=== TSAM Stage 1.0: Multi-Module Representation ===\n")
    print(json.dumps(g.summary(), indent=2))
    print("\nCross-module edges:")
    for e in g.cross_module_imports():
        print(f"  {e.source_module_id[:8]} -> {e.target_module_id[:8]}  ({e.symbol})")
    print("\nCapability key index:")
    print(json.dumps(g.capability_key_index(), indent=2))
