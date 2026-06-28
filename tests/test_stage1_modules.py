"""
TSAM Stage 1 — Tests for the Multi-Module Representation
========================================================
Tests are written against intended behaviour (CLAUDE.md §3), and explicitly
exercise the Stage 0 invariants the representation must respect:
  - Determinism (same input -> identical structural hash; input ordering and
    process restarts do not matter; the logical clock never moves backwards).
  - Bounded Executive State (a descriptor's size scales with its capped
    public surface, not with the module's source length).
  - Clean Rejection (unparseable modules and ambiguous projects are surfaced
    loudly, not silently absorbed; truncated public surfaces are flagged).
  - Separation of Concerns (the heavy parsed artifact lives apart from the
    bounded descriptor; window state is excluded from artifact identity).
"""

from __future__ import annotations

import sys
import unittest

from tsam.module_graph import (
    MAX_EXPORTS_PER_MODULE,
    MAX_IMPORTS_PER_MODULE,
    ModuleDescriptor,
    ModuleGraph,
    canonicalize_module_path,
    extract_exports,
    extract_imports,
    module_id_for_path,
)
from validation.module_generators import (
    broken_module_source,
    generate_projects,
    generate_solvable_project,
    generate_unsolvable_project,
)

import ast


# ---------------------------------------------------------------------------
# Module identity & path canonicalisation
# ---------------------------------------------------------------------------

class TestModuleIdentity(unittest.TestCase):

    def test_canonicalisation_unifies_separators_and_suffix(self):
        for variant in ("mod/core.py", "mod\\core.py", "mod.core", " mod.core ", "mod..core"):
            self.assertEqual(canonicalize_module_path(variant), "mod.core")

    def test_empty_path_rejected(self):
        for bad in ("", "   ", ".", "..", "/"):
            with self.assertRaises(ValueError):
                canonicalize_module_path(bad)

    def test_id_is_deterministic_sha256_of_canonical_path(self):
        a = module_id_for_path("mod/core.py")
        b = module_id_for_path("mod.core")
        self.assertEqual(a, b)
        self.assertEqual(len(a), 64)          # full SHA-256 hex
        self.assertTrue(all(c in "0123456789abcdef" for c in a))

    def test_distinct_paths_get_distinct_ids(self):
        self.assertNotEqual(module_id_for_path("a.b"), module_id_for_path("a.c"))


# ---------------------------------------------------------------------------
# Export extraction
# ---------------------------------------------------------------------------

class TestExportExtraction(unittest.TestCase):

    def _exports(self, src: str) -> dict[str, str | None]:
        tree = ast.parse(src)
        return {e.symbol: e.origin_path for e in extract_exports(tree)}

    def test_public_defs_and_constants_exported_private_excluded(self):
        src = (
            "X = 1\n"
            "_hidden = 2\n"
            "def public_fn():\n    return 1\n"
            "def _private_fn():\n    return 2\n"
            "class PublicClass:\n    pass\n"
            "class _PrivateClass:\n    pass\n"
        )
        exports = self._exports(src)
        self.assertEqual(set(exports), {"X", "public_fn", "PublicClass"})
        self.assertTrue(all(origin is None for origin in exports.values()))

    def test_dunder_all_overrides_default_visibility(self):
        # __all__ can both restrict (drop a public name) and include a "private" one.
        src = (
            "A = 1\n"
            "B = 2\n"
            "_C = 3\n"
            "__all__ = ['A', '_C']\n"
        )
        exports = self._exports(src)
        self.assertEqual(set(exports), {"A", "_C"})

    def test_reexport_provenance_recorded(self):
        src = (
            "from other.mod import Thing\n"
            "__all__ = ['Thing']\n"
        )
        exports = self._exports(src)
        self.assertEqual(exports, {"Thing": "other.mod"})

    def test_local_definition_beats_import_for_provenance(self):
        # A name both imported and locally defined is a local definition.
        src = (
            "from other.mod import Thing\n"
            "def Thing():\n    return 1\n"
            "__all__ = ['Thing']\n"
        )
        exports = self._exports(src)
        self.assertEqual(exports, {"Thing": None})

    def test_tuple_unpacking_assignment_targets_exported(self):
        exports = self._exports("A, B = 1, 2\n")
        self.assertEqual(set(exports), {"A", "B"})


# ---------------------------------------------------------------------------
# Import extraction & resolution
# ---------------------------------------------------------------------------

class TestImportExtraction(unittest.TestCase):

    def _imports(self, src: str):
        return extract_imports(ast.parse(src))

    def test_from_import_symbol_is_source_name_not_alias(self):
        imps = self._imports("from a.b import X as Y\n")
        self.assertEqual(len(imps), 1)
        self.assertEqual((imps[0].target_path, imps[0].symbol), ("a.b", "X"))

    def test_plain_import_records_full_path(self):
        imps = self._imports("import a.b.c\n")
        self.assertEqual((imps[0].target_path, imps[0].symbol), ("a.b.c", "a.b.c"))

    def test_star_import_flagged(self):
        imps = self._imports("from a.b import *\n")
        self.assertTrue(imps[0].is_star)
        self.assertEqual(imps[0].symbol, "*")

    def test_relative_import_flagged_and_not_resolved_internally(self):
        imps = self._imports("from . import sibling\n")
        self.assertTrue(imps[0].is_relative)

    def test_nested_imports_inside_functions_ignored(self):
        src = "def f():\n    import os\n    return os\n"
        self.assertEqual(self._imports(src), [])

    def test_resolution_distinguishes_internal_from_external(self):
        graph = ModuleGraph.build({
            "pkg.a": "from pkg.b import Thing\nimport os\n",
            "pkg.b": "Thing = 1\n__all__ = ['Thing']\n",
        })
        a = graph.descriptor(graph.resolve_path("pkg.a"))
        by_target = {(i.target_path, i.symbol): i for i in a.imports}
        self.assertIsNotNone(by_target[("pkg.b", "Thing")].target_module_id)
        self.assertIsNone(by_target[("os", "os")].target_module_id)


# ---------------------------------------------------------------------------
# Capability evidence
# ---------------------------------------------------------------------------

class TestCapabilityEvidence(unittest.TestCase):

    def test_provider_module_shows_evidence_plain_module_does_not(self):
        graph = ModuleGraph.build({
            "p.provider": (
                "from neoforge.common.capabilities import LazyOptional\n"
                "class P:\n"
                "    def getCapability(self, cap, side):\n"
                "        return LazyOptional.empty()\n"
            ),
            "p.util": "def add(a, b):\n    return a + b\n",
            "p.core": "KEY = object()\n__all__ = ['KEY']\n",
        })
        self.assertTrue(graph.descriptor(graph.resolve_path("p.provider")).capability_evidence)
        self.assertFalse(graph.descriptor(graph.resolve_path("p.util")).capability_evidence)
        self.assertFalse(graph.descriptor(graph.resolve_path("p.core")).capability_evidence)


# ---------------------------------------------------------------------------
# Clean rejection: unparseable modules, ambiguous projects, truncation
# ---------------------------------------------------------------------------

class TestCleanRejection(unittest.TestCase):

    def test_unparseable_module_is_flagged_not_silently_empty(self):
        graph = ModuleGraph.build({"m.broken": broken_module_source()})
        d = graph.descriptor(graph.resolve_path("m.broken"))
        self.assertFalse(d.parse_ok)
        self.assertEqual(d.exports, ())
        self.assertEqual(d.imports, ())
        self.assertFalse(d.capability_evidence)
        # The artifact is still present (empty) so the module is accounted for.
        self.assertIn(d.module_id, graph.program_graphs)

    def test_duplicate_module_identity_rejected_loudly(self):
        with self.assertRaises(ValueError):
            ModuleGraph.build({"mod/core.py": "X = 1\n", "mod.core": "X = 2\n"})

    def test_export_cap_truncates_and_flags(self):
        n = MAX_EXPORTS_PER_MODULE + 10
        src = "".join(f"S{i} = {i}\n" for i in range(n))
        graph = ModuleGraph.build({"big.mod": src})
        d = graph.descriptor(graph.resolve_path("big.mod"))
        self.assertTrue(d.exports_truncated)
        self.assertEqual(len(d.exports), MAX_EXPORTS_PER_MODULE)

    def test_import_cap_truncates_and_flags(self):
        n = MAX_IMPORTS_PER_MODULE + 10
        src = "".join(f"import dep{i}\n" for i in range(n))
        graph = ModuleGraph.build({"big.importer": src})
        d = graph.descriptor(graph.resolve_path("big.importer"))
        self.assertTrue(d.imports_truncated)
        self.assertEqual(len(d.imports), MAX_IMPORTS_PER_MODULE)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestDeterminism(unittest.TestCase):

    def test_structural_hash_stable_across_builds(self):
        case = generate_solvable_project(0)
        h1 = ModuleGraph.build(case.source_map()).structural_hash()
        h2 = ModuleGraph.build(case.source_map()).structural_hash()
        self.assertEqual(h1, h2)

    def test_structural_hash_independent_of_input_ordering(self):
        case = generate_solvable_project(0)
        forward = dict(case.modules)
        reversed_items = dict(reversed(case.modules))
        self.assertEqual(
            ModuleGraph.build(forward).structural_hash(),
            ModuleGraph.build(reversed_items).structural_hash(),
        )

    def test_structural_hash_excludes_last_accessed(self):
        # Window state (last_accessed) must not change artifact identity.
        case = generate_solvable_project(0)
        graph = ModuleGraph.build(case.source_map())
        before = graph.structural_hash()
        mid = graph.module_ids()[0]
        graph.descriptors[mid] = graph.descriptor(mid).touched(99)
        self.assertEqual(graph.structural_hash(), before)

    def test_accessors_return_sorted_order(self):
        case = generate_unsolvable_project(0)
        graph = ModuleGraph.build(case.source_map())
        self.assertEqual(graph.module_ids(), sorted(graph.module_ids()))
        edges = graph.cross_module_imports()
        self.assertEqual(edges, sorted(edges, key=lambda e: e._sort_key()))


# ---------------------------------------------------------------------------
# Bounded descriptor (separation of concerns / Invariant 5.1)
# ---------------------------------------------------------------------------

class TestBoundedDescriptor(unittest.TestCase):

    def test_descriptor_size_independent_of_source_length(self):
        # Two modules with identical public surface (2 exports, 0 imports) but
        # vastly different body sizes must yield descriptors of equal shallow
        # size — the descriptor is a bounded view, the body lives in the
        # separate program-graph artifact.
        small_body = "A = 1\nB = 2\n"
        huge_body = "A = 1\nB = 2\n" + "".join(
            f"def _f{i}():\n    return {i}\n" for i in range(2000)
        )
        graph = ModuleGraph.build({"m.small": small_body, "m.huge": huge_body})
        d_small = graph.descriptor(graph.resolve_path("m.small"))
        d_huge = graph.descriptor(graph.resolve_path("m.huge"))
        # Same public surface (private _f* are not exported).
        self.assertEqual(d_small.exported_symbols(), d_huge.exported_symbols())
        self.assertEqual(sys.getsizeof(d_small), sys.getsizeof(d_huge))
        # The artifact, by contrast, is allowed to grow with the body.
        self.assertGreater(
            len(graph.program_graph(d_huge.module_id).nodes),
            len(graph.program_graph(d_small.module_id).nodes),
        )

    def test_touched_rejects_backwards_clock(self):
        graph = ModuleGraph.build({"m.x": "A = 1\n"})
        d = graph.descriptor(graph.resolve_path("m.x")).touched(5)
        with self.assertRaises(ValueError):
            d.touched(4)
        self.assertEqual(d.touched(5).last_accessed, 5)  # equal tick is allowed (idempotent)


# ---------------------------------------------------------------------------
# Cross-module structure: edges, dangling, collisions, importers
# ---------------------------------------------------------------------------

class TestCrossModuleStructure(unittest.TestCase):

    def test_dangling_internal_import_detected(self):
        graph = ModuleGraph.build({
            "p.core": "REAL = 1\n__all__ = ['REAL']\n",
            "p.user": "from p.core import GHOST\n",
        })
        dangling = graph.dangling_internal_imports()
        self.assertEqual(len(dangling), 1)
        self.assertEqual(dangling[0].symbol, "GHOST")
        self.assertEqual(dangling[0].target_module_id, graph.resolve_path("p.core"))

    def test_external_import_is_not_dangling(self):
        graph = ModuleGraph.build({"p.user": "from os.path import join\n"})
        self.assertEqual(graph.dangling_internal_imports(), [])

    def test_capability_key_collision_surfaced(self):
        graph = ModuleGraph.build(generate_unsolvable_project(0).source_map())
        collisions = graph.colliding_capability_keys()
        self.assertEqual(len(collisions), 1)
        (key, locations), = collisions.items()
        self.assertEqual(len(locations), 2)  # two providers declare the same key

    def test_importers_of_is_correct_and_sorted(self):
        graph = ModuleGraph.build({
            "p.core": "K = 1\n__all__ = ['K']\n",
            "p.a": "from p.core import K\n",
            "p.b": "from p.core import K\n",
        })
        core_id = graph.resolve_path("p.core")
        importers = graph.importers_of(core_id)
        self.assertEqual(importers, sorted([graph.resolve_path("p.a"), graph.resolve_path("p.b")]))


# ---------------------------------------------------------------------------
# Fixtures: built graphs match declared expected structure
# ---------------------------------------------------------------------------

class TestFixtures(unittest.TestCase):

    def _assert_case(self, case):
        graph = ModuleGraph.build(case.source_map())

        # Every fixture module parses (the defects are structural, not syntactic).
        for d in graph.descriptors.values():
            self.assertTrue(d.parse_ok, f"{d.canonical_path} failed to parse")

        self.assertEqual(len(graph), case.expected_module_count)
        self.assertEqual(len(graph.capability_modules()), case.expected_capability_modules)
        self.assertEqual(len(graph.dangling_internal_imports()), case.expected_dangling)
        self.assertEqual(len(graph.colliding_capability_keys()), case.expected_key_collisions)

        # Every intentionally-created internal edge is present and resolved.
        actual = {
            (e.source_module_id, e.target_module_id, e.symbol)
            for e in graph.cross_module_imports()
        }
        for exp in case.expected_edges:
            src = graph.resolve_path(exp.importer_path)
            dst = graph.resolve_path(exp.exporter_path)
            self.assertIsNotNone(src, f"importer {exp.importer_path} missing")
            self.assertIsNotNone(dst, f"exporter {exp.exporter_path} missing")
            self.assertIn((src, dst, exp.symbol), actual,
                          f"expected edge {exp.importer_path}->{exp.exporter_path} "
                          f"({exp.symbol}) not found")

    def test_solvable_projects_match_expected(self):
        for i in range(3):
            with self.subTest(case=i):
                self._assert_case(generate_solvable_project(i))

    def test_unsolvable_projects_match_expected(self):
        for i in range(3):
            with self.subTest(case=i):
                self._assert_case(generate_unsolvable_project(i))

    def test_generated_suite_is_deterministic(self):
        a = [ModuleGraph.build(c.source_map()).structural_hash() for c in generate_projects()]
        b = [ModuleGraph.build(c.source_map()).structural_hash() for c in generate_projects()]
        self.assertEqual(a, b)

    def test_solvable_and_unsolvable_are_structurally_distinct(self):
        s = ModuleGraph.build(generate_solvable_project(0).source_map())
        u = ModuleGraph.build(generate_unsolvable_project(0).source_map())
        self.assertEqual(s.summary()["dangling_internal"], 0)
        self.assertGreater(u.summary()["dangling_internal"], 0)
        self.assertEqual(s.summary()["capability_key_collisions"], 0)
        self.assertGreater(u.summary()["capability_key_collisions"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
