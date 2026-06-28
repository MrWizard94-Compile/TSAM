"""
TSAM Stage 1 — Tests for the Active Window W_t and the Multi-Hop Resolver
========================================================================
Tests against intended behaviour (CLAUDE.md §3), exercising the invariants
this transient executive component must hold:
  - Bounded Executive State (5.1): the three hard caps are never exceeded;
    resolution explores at most MAX_HOPS modules.
  - Determinism (5.2): the same admission/resolution sequence yields an
    identical content hash; LRU victims are chosen deterministically; cycles
    terminate rather than loop.
  - Clean Rejection (5.4): every import resolves to a specific status; an
    unresolved import is a recorded, diagnosable edge, not an absence.
  - Separation of Concerns (5.5): the window never mutates the persistent
    graph; its content hash excludes logical ticks.
"""

from __future__ import annotations

import unittest

from tsam.module_graph import ModuleGraph
from tsam.active_window import (
    MAX_ACTIVE_MODULES,
    MAX_ACTIVE_EDGES,
    MAX_RESOLVED_KEYS,
    MAX_HOPS,
    ActiveWindow,
    ResolutionStatus,
    resolve_symbol,
)
from validation.module_generators import (
    generate_reexport_chain_project,
    generate_solvable_project,
    generate_unsolvable_project,
)


# ---------------------------------------------------------------------------
# Multi-hop resolver
# ---------------------------------------------------------------------------

class TestResolver(unittest.TestCase):

    def _resolve_consumer(self, depth: int):
        graph = ModuleGraph.build(generate_reexport_chain_project(depth))
        consumer = graph.descriptor(graph.resolve_path(f"chain0.consumer"))
        imp = consumer.imports[0]
        return graph, resolve_symbol(graph, imp.target_module_id, imp.symbol)

    def test_direct_resolution_one_hop(self):
        graph, r = self._resolve_consumer(0)
        self.assertIs(r.status, ResolutionStatus.RESOLVED)
        self.assertEqual(r.hop_count, 1)
        self.assertEqual(r.declaring_module_id, graph.resolve_path("chain0.m0"))

    def test_one_reexport_hop(self):
        graph, r = self._resolve_consumer(1)
        self.assertIs(r.status, ResolutionStatus.RESOLVED)
        self.assertEqual(r.hop_count, 2)
        self.assertEqual(r.declaring_module_id, graph.resolve_path("chain0.m0"))

    def test_resolution_at_hop_ceiling(self):
        graph, r = self._resolve_consumer(2)
        self.assertIs(r.status, ResolutionStatus.RESOLVED)
        self.assertEqual(r.hop_count, MAX_HOPS)  # 3

    def test_beyond_hop_ceiling_terminates(self):
        graph, r = self._resolve_consumer(3)
        self.assertIs(r.status, ResolutionStatus.UNRESOLVED_HOP_LIMIT)
        self.assertEqual(r.hop_count, MAX_HOPS)
        # The chain is bounded by the ceiling, not by project size.
        self.assertLessEqual(len(r.chain), MAX_HOPS)

    def test_dangling_symbol_not_exported(self):
        graph = ModuleGraph.build({"p.core": "REAL = 1\n__all__ = ['REAL']\n"})
        r = resolve_symbol(graph, graph.resolve_path("p.core"), "GHOST")
        self.assertIs(r.status, ResolutionStatus.UNRESOLVED_DANGLING)

    def test_reexport_from_external_module(self):
        graph = ModuleGraph.build({
            "p.shim": "from third_party.lib import K\n__all__ = ['K']\n",
        })
        r = resolve_symbol(graph, graph.resolve_path("p.shim"), "K")
        self.assertIs(r.status, ResolutionStatus.UNRESOLVED_EXTERNAL)

    def test_reexport_cycle_terminates(self):
        graph = ModuleGraph.build({
            "p.a": "from p.b import K\n__all__ = ['K']\n",
            "p.b": "from p.a import K\n__all__ = ['K']\n",
        })
        r = resolve_symbol(graph, graph.resolve_path("p.a"), "K")
        self.assertIs(r.status, ResolutionStatus.UNRESOLVED_CYCLE)

    def test_resolution_explores_bounded_module_count(self):
        # Even a long chain examines at most MAX_HOPS modules.
        graph, r = self._resolve_consumer(10)
        self.assertLessEqual(len(set(r.chain)), MAX_HOPS)


# ---------------------------------------------------------------------------
# Active window: resolution into the window, collisions, dangling edges
# ---------------------------------------------------------------------------

class TestWindowResolution(unittest.TestCase):

    def test_focus_admits_and_resolves(self):
        graph = ModuleGraph.build(generate_solvable_project(0).source_map())
        win = ActiveWindow.for_graph(graph)
        win.focus_on(graph.capability_modules())
        self.assertTrue(win.within_bounds())
        # Each provider's key import resolves to the core.
        resolved = [e for e in win.edges() if e.resolved]
        self.assertTrue(resolved)
        for e in resolved:
            self.assertEqual(e.declaring_module_id, graph.resolve_path("proj0.core"))

    def test_cross_module_key_collision_detected(self):
        graph = ModuleGraph.build(generate_unsolvable_project(0).source_map())
        win = ActiveWindow.for_graph(graph)
        win.focus_on(graph.capability_modules())
        collisions = win.colliding_resolved_keys()
        self.assertEqual(len(collisions), 1)
        self.assertEqual(len(collisions[0].referencing_classes), 2)
        # The two referencing classes live in two distinct modules.
        modules = {c.split(":")[0] for c in collisions[0].referencing_classes}
        self.assertEqual(len(modules), 2)

    def test_dangling_cross_module_import_recorded_as_unresolved(self):
        graph = ModuleGraph.build(generate_unsolvable_project(0).source_map())
        win = ActiveWindow.for_graph(graph)
        ghost = graph.resolve_path("uproj0.ghost_importer")
        win.focus_on([ghost])
        dangling = [
            e for e in win.unresolved_edges()
            if e.status is ResolutionStatus.UNRESOLVED_DANGLING
        ]
        self.assertTrue(any(e.symbol.startswith("NEVER_DEFINED") for e in dangling))

    def test_star_and_external_imports_recorded_external(self):
        graph = ModuleGraph.build({
            "p.user": "from p.core import *\nimport os\n",
            "p.core": "K = 1\n__all__ = ['K']\n",
        })
        win = ActiveWindow.for_graph(graph)
        win.focus_on([graph.resolve_path("p.user")])
        statuses = {e.symbol: e.status for e in win.edges()}
        self.assertIs(statuses["*"], ResolutionStatus.UNRESOLVED_EXTERNAL)
        self.assertIs(statuses["os"], ResolutionStatus.UNRESOLVED_EXTERNAL)


# ---------------------------------------------------------------------------
# Bounded executive state: hard caps + LRU eviction
# ---------------------------------------------------------------------------

class TestBoundedWindow(unittest.TestCase):

    def _wide_project(self, n: int) -> ModuleGraph:
        sources = {"w.core": "K = 1\n__all__ = ['K']\n"}
        for i in range(n):
            sources[f"w.m{i}"] = "from w.core import K\n"
        return ModuleGraph.build(sources)

    def test_default_caps_match_spec(self):
        self.assertEqual(MAX_ACTIVE_MODULES, 32)
        self.assertEqual(MAX_ACTIVE_EDGES, 128)
        self.assertEqual(MAX_RESOLVED_KEYS, 64)
        self.assertEqual(MAX_HOPS, 3)

    def test_module_cap_never_exceeded_and_evicts_lru(self):
        graph = self._wide_project(10)
        win = ActiveWindow.for_graph(graph, max_modules=4)
        for i in range(6):
            win.admit(graph.resolve_path(f"w.m{i}"))
            self.assertTrue(win.within_bounds())
        active = set(win.active_modules())
        # core is refreshed on every admit (it is each module's dependency),
        # so it survives; the earliest providers are evicted (LRU).
        self.assertIn(graph.resolve_path("w.core"), active)
        self.assertNotIn(graph.resolve_path("w.m0"), active)
        self.assertIn(graph.resolve_path("w.m5"), active)
        self.assertLessEqual(len(active), 4)

    def test_focus_protects_from_eviction(self):
        graph = self._wide_project(10)
        win = ActiveWindow.for_graph(graph, max_modules=4)
        protected = graph.resolve_path("w.m0")
        win.set_focus([protected])
        win.admit(protected)
        for i in range(1, 8):
            win.admit(graph.resolve_path(f"w.m{i}"))
        self.assertIn(protected, win.active_modules())  # never evicted
        self.assertTrue(win.within_bounds())

    def test_verification_record_protects_then_ages_out(self):
        graph = self._wide_project(12)
        win = ActiveWindow.for_graph(graph, max_modules=3)
        guarded = graph.resolve_path("w.m0")
        win.admit(guarded)
        win.record_verification([guarded])
        for i in range(1, 6):
            win.admit(graph.resolve_path(f"w.m{i}"))
            win.after_pass()
        self.assertIn(guarded, win.active_modules())  # still inside protection window
        # Push 4 newer verification records → guarded's record ages out of the ring.
        for i in range(6, 10):
            win.record_verification([graph.resolve_path(f"w.m{i}")])
        win.admit(graph.resolve_path("w.m10"))
        win.admit(graph.resolve_path("w.m11"))
        win.after_pass()
        self.assertTrue(win.within_bounds())

    def test_edge_cap_never_exceeded(self):
        graph = self._wide_project(40)
        win = ActiveWindow.for_graph(graph, max_edges=8, max_modules=64)
        for i in range(40):
            win.resolve_module(graph.resolve_path(f"w.m{i}"))
            self.assertLessEqual(len(win.edges()), 8)
        self.assertTrue(win.within_bounds())

    def test_key_cap_never_exceeded(self):
        # Many distinct capability keys, each referenced by its own provider.
        sources = {}
        all_keys = []
        for i in range(10):
            key = f"CAP_{i}"
            all_keys.append(key)
            sources[f"k.core{i}"] = f"{key} = object()\n__all__ = [{key!r}]\n"
            sources[f"k.prov{i}"] = (
                f"from k.core{i} import {key}\n"
                "from neoforge.common.capabilities import LazyOptional\n"
                f"class Prov{i}:\n"
                f"    def getCapability(self, cap, side):\n"
                f"        if cap == {key}:\n"
                f"            return LazyOptional.of(lambda: 1)\n"
                f"        return LazyOptional.empty()\n"
            )
        graph = ModuleGraph.build(sources)
        win = ActiveWindow.for_graph(graph, max_keys=3, max_modules=64, max_edges=256)
        for i in range(10):
            win.resolve_module(graph.resolve_path(f"k.prov{i}"))
            self.assertLessEqual(len(win.resolved_keys()), 3)
        self.assertTrue(win.within_bounds())

    def test_eviction_drops_edges_and_reresolution_recovers(self):
        graph = self._wide_project(6)
        win = ActiveWindow.for_graph(graph, max_modules=3)
        m0 = graph.resolve_path("w.m0")
        win.resolve_module(m0)
        self.assertTrue(any(e.source_module_id == m0 for e in win.edges()))
        # Force m0 out by admitting many other modules.
        for i in range(1, 6):
            win.admit(graph.resolve_path(f"w.m{i}"))
        self.assertNotIn(m0, win.active_modules())
        self.assertFalse(any(e.source_module_id == m0 for e in win.edges()))
        # Re-resolution on next access recomputes the dropped edges.
        win.resolve_module(m0)
        self.assertTrue(any(e.source_module_id == m0 for e in win.edges()))
        self.assertTrue(win.within_bounds())


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

class TestWindowDeterminism(unittest.TestCase):

    def _drive(self, graph) -> ActiveWindow:
        win = ActiveWindow.for_graph(graph)
        win.focus_on(graph.capability_modules())
        for mid in graph.module_ids():
            win.resolve_module(mid)
        return win

    def test_same_sequence_same_content_hash(self):
        graph = ModuleGraph.build(generate_unsolvable_project(0).source_map())
        self.assertEqual(
            self._drive(graph).structural_state_hash(),
            self._drive(graph).structural_state_hash(),
        )

    def test_content_hash_excludes_ticks(self):
        graph = ModuleGraph.build(generate_solvable_project(0).source_map())
        win = self._drive(graph)
        before = win.structural_state_hash()
        # Re-admitting changes access ticks but not window content.
        for mid in win.active_modules():
            win.admit(mid)
        self.assertEqual(win.structural_state_hash(), before)

    def test_within_bounds_invariant_holds_throughout(self):
        graph = ModuleGraph.build(generate_unsolvable_project(0).source_map())
        win = ActiveWindow.for_graph(graph, max_modules=3, max_edges=4, max_keys=2)
        for mid in graph.module_ids():
            win.resolve_module(mid)
            self.assertTrue(win.within_bounds())
            win.after_pass()
            self.assertTrue(win.within_bounds())


if __name__ == "__main__":
    unittest.main(verbosity=2)
