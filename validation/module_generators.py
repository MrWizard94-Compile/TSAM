"""
TSAM Stage 1 — Multi-Module Test Fixtures
==========================================
Generators for heterogeneous, multi-module projects, the cross-module
analogue of ``validation/test_generators.py`` (which produces single-file,
homogeneous, N-copies-of-one-pattern cases). These fixtures deliberately mix
module archetypes within one project — a capability-key core, Fabric-style
providers needing a port, already-ported NeoForge providers, plain utility
modules with no capability semantics, and a registry that wires providers
together by importing them — so that cross-module structure (import/export
edges, capability evidence, dangling cross-module references, and
cross-module capability-key collisions) actually exists to be measured.

Determinism: every generator is a pure function of ``case_index``. No
randomness, no clock, no external state — ``case_index`` alone fixes the
entire project, byte for byte (consistent with the Determinism invariant and
with the single-file generators' design).

Scope: this slice provides representative inputs and their *expected
structural properties*. It does not run synthesis on them — the Stage 1
resolver, the C penalty, and acceptance behaviour are later slices. The
``expected_*`` fields encode intended structure so tests assert behaviour,
not implementation.
"""

from __future__ import annotations

import hashlib
import textwrap
from dataclasses import dataclass

__all__ = [
    "ExpectedEdge",
    "MultiModuleCase",
    "broken_module_source",
    "generate_solvable_project",
    "generate_unsolvable_project",
    "generate_reexport_chain_project",
    "generate_projects",
]


@dataclass(frozen=True, slots=True)
class ExpectedEdge:
    """An internal cross-module import edge the fixture intentionally creates:
    ``importer_path`` imports ``symbol`` from ``exporter_path`` (both module
    paths, pre-canonicalisation)."""
    importer_path: str
    exporter_path: str
    symbol:        str


@dataclass(frozen=True, slots=True)
class MultiModuleCase:
    """
    A single multi-module project fixture with its expected structure.

    ``modules`` is stored as an immutable tuple of ``(path, source)`` pairs;
    use :meth:`source_map` for the ``path -> source`` mapping that
    ``ModuleGraph.build`` consumes.
    """
    case_id:                     str
    solvable:                    bool
    modules:                     tuple[tuple[str, str], ...]
    description:                 str
    expected_module_count:       int
    expected_capability_modules:  int
    expected_dangling:           int
    expected_key_collisions:     int
    expected_edges:              tuple[ExpectedEdge, ...]

    def source_map(self) -> dict[str, str]:
        return {path: src for path, src in self.modules}


# ---------------------------------------------------------------------------
# Module archetype source builders
# ---------------------------------------------------------------------------

def _core_module(case_idx: int, key_names: tuple[str, ...]) -> str:
    """A pure capability-key core: module-level constants + ``__all__``. No
    classes, so it shows no capability-provider evidence itself; it only
    *supplies* the keys that providers import."""
    assignments = "\n".join(f"{k} = object()" for k in key_names)
    all_list = ", ".join(repr(k) for k in key_names)
    return f"{assignments}\n__all__ = [{all_list}]\n"


def _fabric_provider_module(case_idx: int, idx: int, core_path: str, key_name: str) -> str:
    """A Fabric-style capability provider that imports its key from the core
    module and must be ported. Shows capability evidence (LazyOptional +
    a ``cap``-parameter ``getCapability``)."""
    return textwrap.dedent(f"""\
        from {core_path} import {key_name}
        import net.fabricmc.fabric
        from net.fabricmc import FabricUtil{idx}

        class FabricProvider{idx}:
            def __init__(self):
                self.handler = MyHandler{idx}()
                self.lazy = LazyOptional.of(lambda: self.handler)

            def getCapability(self, cap, side):
                if cap == {key_name}:
                    return self.lazy
                return LazyOptional.empty()
    """)


def _neoforge_provider_module(case_idx: int, idx: int, core_path: str, key_name: str) -> str:
    """An already-ported, valid NeoForge capability provider importing its key
    from the core module. Shows capability evidence and is structurally
    complete (nothing to port)."""
    return textwrap.dedent(f"""\
        from {core_path} import {key_name}
        from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
        from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar

        class NeoProvider{idx}:
            def __init__(self):
                self._handler = MyHandler{idx}()
                self._handler_lazy = None

            def getCapability(self, cap, direction=None):
                if cap == {key_name}:
                    return LazyOptional.of(lambda: self._handler)
                return LazyOptional.empty()

            def invalidateCapabilities(self):
                if self._handler_lazy is not None:
                    self._handler_lazy.invalidate()
                self._handler_lazy = None

            def register_capability(self, registrar: BlockCapabilityRegistrar):
                registrar.registerBlockEntity({key_name}, self)
    """)


def _util_module(case_idx: int, idx: int) -> str:
    """A plain utility module with no capability semantics and no internal
    imports — exercises the 'leave untouched' archetype."""
    return textwrap.dedent(f"""\
        def add_{idx}(a, b):
            return a + b

        def scale_{idx}(values, factor):
            return [v * factor for v in values]

        _INTERNAL_CONSTANT = 7
    """)


def _registry_module(case_idx: int, provider_imports: tuple[tuple[str, str], ...]) -> str:
    """A registry that imports provider classes from other modules and wires
    them up. ``provider_imports`` is a tuple of ``(provider_path, ClassName)``.
    Has no capability evidence of its own (no provider markers, no
    ``cap``-parameter method)."""
    import_lines = "\n".join(
        f"from {path} import {cls}" for path, cls in provider_imports
    )
    register_lines = "\n".join(
        f"    registrar.register({cls}())" for _, cls in provider_imports
    )
    return (
        f"{import_lines}\n\n"
        f"def register_all(registrar):\n"
        f"{register_lines}\n"
    )


def _ghost_importer_module(case_idx: int, core_path: str, missing_symbol: str) -> str:
    """A module that imports a symbol the (in-project) core module does NOT
    export — a dangling cross-module reference. Parses fine; the breakage is
    structural, not syntactic."""
    return textwrap.dedent(f"""\
        from {core_path} import {missing_symbol}

        def use_it():
            return {missing_symbol}
    """)


def broken_module_source() -> str:
    """Syntactically invalid Python — for exercising ``parse_ok = False``
    handling. Intentionally not part of any project case (a project with an
    unparseable module is a malformed input, surfaced on its own)."""
    return "def broken(:\n    return 1\n"


# ---------------------------------------------------------------------------
# Project generators
# ---------------------------------------------------------------------------

def _case_id(kind: str, index: int) -> str:
    raw = f"tsam_stage1_{kind}_{index:03d}"
    digest = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"{raw}_{digest}"


def generate_solvable_project(case_index: int = 0) -> MultiModuleCase:
    """
    A well-formed heterogeneous project: a core supplying two distinct keys,
    one Fabric provider (needs porting), one already-ported NeoForge provider,
    a plain utility module, and a registry importing both providers. Every
    cross-module reference resolves; the two provider keys are distinct, so
    there is no capability-key collision and nothing dangles.
    """
    core_path     = f"proj{case_index}.core"
    fabric_path   = f"proj{case_index}.fabric_provider_0"
    neo_path      = f"proj{case_index}.neo_provider_1"
    util_path     = f"proj{case_index}.util_0"
    registry_path = f"proj{case_index}.registry"

    key0 = f"MY_CAPABILITY_{case_index}_0"
    key1 = f"MY_CAPABILITY_{case_index}_1"

    modules = (
        (core_path,     _core_module(case_index, (key0, key1))),
        (fabric_path,   _fabric_provider_module(case_index, 0, core_path, key0)),
        (neo_path,      _neoforge_provider_module(case_index, 1, core_path, key1)),
        (util_path,     _util_module(case_index, 0)),
        (registry_path, _registry_module(case_index, (
            (fabric_path, "FabricProvider0"),
            (neo_path,    "NeoProvider1"),
        ))),
    )

    expected_edges = (
        ExpectedEdge(fabric_path,   core_path,   key0),
        ExpectedEdge(neo_path,      core_path,   key1),
        ExpectedEdge(registry_path, fabric_path, "FabricProvider0"),
        ExpectedEdge(registry_path, neo_path,    "NeoProvider1"),
    )

    return MultiModuleCase(
        case_id   = _case_id("solvable", case_index),
        solvable  = True,
        modules   = modules,
        description = (
            "Solvable heterogeneous project: core + 1 Fabric provider (to port) "
            "+ 1 already-ported NeoForge provider + 1 utility + registry. "
            "All cross-module refs resolve; distinct keys; no collision; nothing dangles."
        ),
        expected_module_count       = 5,
        expected_capability_modules = 2,   # the two providers
        expected_dangling           = 0,
        expected_key_collisions     = 0,
        expected_edges              = expected_edges,
    )


def generate_unsolvable_project(case_index: int = 0) -> MultiModuleCase:
    """
    A project with two structural cross-module defects the Stage 1 C penalty
    is meant to catch:
      1. A capability-key collision: two Fabric providers import and register
         the *same* key from core (one shared registration slot, two owners).
      2. A dangling cross-module reference: a module imports a symbol the core
         module does not export.
    Both providers still parse and still show capability evidence; the project
    is 'unsolvable' because no per-module port can reconcile the shared-key
    collision or conjure the missing export.
    """
    core_path      = f"uproj{case_index}.core"
    fabric0_path   = f"uproj{case_index}.fabric_provider_0"
    fabric1_path   = f"uproj{case_index}.fabric_provider_1"
    ghost_path     = f"uproj{case_index}.ghost_importer"
    registry_path  = f"uproj{case_index}.registry"

    shared_key    = f"SHARED_CAPABILITY_{case_index}"
    missing_key   = f"NEVER_DEFINED_{case_index}"

    modules = (
        # Core defines and exports ONLY the shared key, not the missing one.
        (core_path,    _core_module(case_index, (shared_key,))),
        (fabric0_path, _fabric_provider_module(case_index, 0, core_path, shared_key)),
        (fabric1_path, _fabric_provider_module(case_index, 1, core_path, shared_key)),
        (ghost_path,   _ghost_importer_module(case_index, core_path, missing_key)),
        (registry_path, _registry_module(case_index, (
            (fabric0_path, "FabricProvider0"),
            (fabric1_path, "FabricProvider1"),
        ))),
    )

    expected_edges = (
        ExpectedEdge(fabric0_path,  core_path,    shared_key),
        ExpectedEdge(fabric1_path,  core_path,    shared_key),
        ExpectedEdge(ghost_path,    core_path,    missing_key),   # resolves to core, but dangling
        ExpectedEdge(registry_path, fabric0_path, "FabricProvider0"),
        ExpectedEdge(registry_path, fabric1_path, "FabricProvider1"),
    )

    return MultiModuleCase(
        case_id   = _case_id("unsolvable", case_index),
        solvable  = False,
        modules   = modules,
        description = (
            "Unsolvable project: two Fabric providers share one capability key "
            f"({shared_key}) — a cross-module registration collision — and a "
            f"module imports {missing_key}, which core never exports (dangling "
            "cross-module reference). Both are structural, not syntactic."
        ),
        expected_module_count       = 5,
        expected_capability_modules = 2,   # the two Fabric providers
        expected_dangling           = 1,   # ghost_importer -> core (missing_key)
        expected_key_collisions     = 1,   # the shared key, declared by 2 providers
        expected_edges              = expected_edges,
    )


def generate_reexport_chain_project(depth: int, case_index: int = 0) -> dict[str, str]:
    """
    A linear re-export chain for exercising the multi-hop resolver.

    Builds ``depth + 1`` modules: ``m0`` locally defines and exports ``KEY``;
    each ``m{i}`` for ``1 <= i <= depth`` re-exports ``KEY`` from ``m{i-1}``;
    and a ``consumer`` module imports ``KEY`` from ``m{depth}`` (the top of the
    chain). Resolving the consumer's import therefore traverses ``depth + 1``
    modules: ``depth = 0`` resolves in one hop, ``depth = 2`` in three, and
    ``depth = 3`` requires a fourth hop and must terminate at the ceiling.

    Returns a ``path -> source`` mapping (not a ``MultiModuleCase`` — these are
    resolver micro-fixtures, not full project cases).
    """
    if depth < 0:
        raise ValueError("depth must be >= 0")

    prefix = f"chain{case_index}"
    key = f"CHAIN_KEY_{case_index}"
    modules: dict[str, str] = {
        f"{prefix}.m0": f"{key} = object()\n__all__ = [{key!r}]\n",
    }
    for i in range(1, depth + 1):
        modules[f"{prefix}.m{i}"] = (
            f"from {prefix}.m{i - 1} import {key}\n__all__ = [{key!r}]\n"
        )
    modules[f"{prefix}.consumer"] = f"from {prefix}.m{depth} import {key}\n"
    return modules


def generate_projects(
    n_solvable: int = 3,
    n_unsolvable: int = 3,
) -> list[MultiModuleCase]:
    """Generate a deterministic suite of multi-module project fixtures."""
    cases: list[MultiModuleCase] = []
    for i in range(n_solvable):
        cases.append(generate_solvable_project(i))
    for i in range(n_unsolvable):
        cases.append(generate_unsolvable_project(i))
    return cases


if __name__ == "__main__":
    print("=== TSAM Stage 1 Multi-Module Fixture Preview ===\n")
    for case in generate_projects(n_solvable=1, n_unsolvable=1):
        kind = "SOLVABLE  " if case.solvable else "UNSOLVABLE"
        print(f"[{kind}] {case.case_id}  ({case.expected_module_count} modules, "
              f"{case.expected_capability_modules} providers, "
              f"{case.expected_dangling} dangling, "
              f"{case.expected_key_collisions} collisions)")
        for path, _ in case.modules:
            print(f"    - {path}")
        print()
