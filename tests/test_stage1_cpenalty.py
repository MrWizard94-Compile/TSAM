"""
TSAM Stage 1 — Tests for the Cross-Module Consistency penalty C
===============================================================
Tests against intended behaviour (CLAUDE.md §3). The two load-bearing
properties this slice must guarantee:

  - **Falsifiability of C.** Every unit of C_hard points at a specific defect.
    A consistent project yields C_hard = 0; an inconsistent one yields
    C_hard > 0 with named defects. If C could not tell them apart it would be
    unfalsifiable-by-construction and must not ship.
  - **Single-module invariance (no regression of the 0.6.0 fix).** On any
    single-module input C ≡ 0, the lexicographic key orders identically to the
    pre-Stage-1 3-tuple, ``node_delta`` stays out of the gate, and the
    acceptance decision is unchanged. The two acceptance anchors prove C lands
    in the correctness bucket, not the quality bucket: large-but-clean accepts,
    tiny-but-broken rejects.
"""

from __future__ import annotations

import unittest

from tsam.constraint_graph import ProgramGraph, build_neoforge_constraint_graph
from tsam.module_graph import ModuleGraph
from tsam.active_window import ActiveWindow
from tsam.rewrite_engine import EnergyBreakdown, compute_energy
from tsam.module_verifier import (
    compute_cross_module_penalty,
    verify_module_graph,
)
from validation.module_generators import (
    generate_consistent_project,
    generate_inconsistent_project,
    generate_reexport_chain_project,
)


def _penalty_for(source_map):
    graph = ModuleGraph.build(source_map)
    window = ActiveWindow.for_graph(graph)
    window.focus_on(graph.module_ids())
    for mid in graph.module_ids():
        window.resolve_module(mid)
    return graph, compute_cross_module_penalty(window)


# ---------------------------------------------------------------------------
# C penalty: falsifiability and classification
# ---------------------------------------------------------------------------

class TestCrossModulePenalty(unittest.TestCase):

    def test_consistent_project_has_zero_hard(self):
        _, c = _penalty_for(generate_consistent_project(0).source_map())
        self.assertEqual(c.hard_count, 0)
        self.assertTrue(c.is_consistent)
        self.assertEqual(c.hard_defects(), ())

    def test_inconsistent_project_has_hard_defects(self):
        _, c = _penalty_for(generate_inconsistent_project(0).source_map())
        kinds = {d.kind for d in c.hard_defects()}
        self.assertIn("DANGLING", kinds)
        self.assertIn("COLLISION", kinds)
        self.assertEqual(c.hard_count, 2)

    def test_consistent_and_inconsistent_are_distinguished_by_C(self):
        # The whole point: C_hard is the discriminator.
        _, c_ok = _penalty_for(generate_consistent_project(0).source_map())
        _, c_bad = _penalty_for(generate_inconsistent_project(0).source_map())
        self.assertEqual(c_ok.hard_count, 0)
        self.assertGreater(c_bad.hard_count, 0)

    def test_external_imports_do_not_count(self):
        # A provider importing only NeoForge/stdlib externals has no hard C.
        src = {
            "x.prov": (
                "from neoforge.common.capabilities import LazyOptional\n"
                "import os\n"
                "class P:\n"
                "    def getCapability(self, cap, side):\n"
                "        return LazyOptional.empty()\n"
            ),
        }
        _, c = _penalty_for(src)
        self.assertEqual(c.hard_count, 0)

    def test_dangling_counts_as_hard(self):
        src = {
            "y.core": "REAL = 1\n__all__ = ['REAL']\n",
            "y.user": "from y.core import GHOST\n",
        }
        _, c = _penalty_for(src)
        self.assertEqual(c.hard_count, 1)
        self.assertEqual(c.hard_defects()[0].kind, "DANGLING")

    def test_reexport_cycle_counts_as_hard(self):
        src = {
            "z.a": "from z.b import K\n__all__ = ['K']\n",
            "z.b": "from z.a import K\n__all__ = ['K']\n",
        }
        _, c = _penalty_for(src)
        self.assertTrue(any(d.kind == "CYCLE" for d in c.hard_defects()))

    def test_hop_limit_unused_is_soft(self):
        # A 4-deep chain whose consumer only imports (never uses) the symbol.
        _, c = _penalty_for(generate_reexport_chain_project(3))
        self.assertEqual(c.hard_count, 0)
        self.assertTrue(any(d.kind == "HOP_LIMIT" and d.severity == "soft"
                            for d in c.defects))

    def test_hop_limit_used_is_hard(self):
        # Same depth, but the consumer structurally uses the symbol in a
        # capability comparison, so it escalates to hard (D3).
        chain = generate_reexport_chain_project(3)
        chain["chain0.consumer"] = (
            "from chain0.m3 import CHAIN_KEY_0\n"
            "class C:\n"
            "    def getCapability(self, cap, side):\n"
            "        if cap == CHAIN_KEY_0:\n"
            "            return 1\n"
            "        return None\n"
        )
        _, c = _penalty_for(chain)
        self.assertTrue(any(d.kind == "HOP_LIMIT" and d.severity == "hard"
                            for d in c.hard_defects()))

    def test_penalty_is_deterministic(self):
        _, c1 = _penalty_for(generate_inconsistent_project(0).source_map())
        _, c2 = _penalty_for(generate_inconsistent_project(0).source_map())
        self.assertEqual(c1.to_dict(), c2.to_dict())


# ---------------------------------------------------------------------------
# Energy tuple: single-module invariance + correct C placement
# ---------------------------------------------------------------------------

class TestEnergyInvariance(unittest.TestCase):

    def setUp(self):
        self.cg = build_neoforge_constraint_graph()
        self.fabric = ProgramGraph.from_python_source(
            "import net.fabricmc.fabric\n"
            "class P:\n"
            "    def getCapability(self, cap, side):\n"
            "        return None\n"
        )

    def test_single_module_has_zero_cross_module_energy(self):
        _, _, bd, _ = compute_energy(self.fabric, self.cg)
        self.assertEqual(bd.cross_module_hard, 0.0)
        self.assertEqual(bd.cross_module_soft, 0.0)

    def test_lexicographic_key_is_four_tuple_with_zero_C_for_single_module(self):
        _, _, bd, _ = compute_energy(self.fabric, self.cg)
        key = bd.lexicographic_key
        self.assertEqual(len(key), 4)
        self.assertEqual(key[2], 0.0)  # C tier is zero on single-module input

    def test_gating_excludes_cross_module_and_quality(self):
        bd = EnergyBreakdown(
            hard_penalty=100.0, strong_penalty=10.0, soft_penalty=1.0,
            syntax_penalty=0.0, node_delta=5.0,
            cross_module_hard=10.0, cross_module_soft=2.0,
        )
        # gating is strictly hard+strong+syntax — no C, no node_delta.
        self.assertEqual(bd.gating, 110.0)
        # quality carries node_delta + soft + soft-C; never gates.
        self.assertEqual(bd.quality, 1.0 + 5.0 + 2.0)

    def test_C_hard_count_adds_to_distance(self):
        _, dist0, _, _ = compute_energy(self.fabric, self.cg)
        _, dist2, _, _ = compute_energy(
            self.fabric, self.cg,
            cross_module_hard=20.0, cross_module_hard_count=2,
        )
        self.assertEqual(dist2, dist0 + 2)

    def test_C_tier_dominates_quality_but_is_dominated_by_strong(self):
        # A single hard cross-module defect outranks any amount of quality...
        c_defect = EnergyBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, cross_module_hard=10.0)
        huge_quality = EnergyBreakdown(0.0, 0.0, 999.0, 0.0, 999.0)
        self.assertGreater(c_defect.lexicographic_key, huge_quality.lexicographic_key)
        # ...but a single strong violation outranks any amount of C.
        one_strong = EnergyBreakdown(0.0, 10.0, 0.0, 0.0, 0.0)
        huge_C = EnergyBreakdown(0.0, 0.0, 0.0, 0.0, 0.0, cross_module_hard=999.0)
        self.assertGreater(one_strong.lexicographic_key, huge_C.lexicographic_key)

    def test_node_delta_still_does_not_gate(self):
        # The 0.6.0 fix preserved: a large node_delta lands only in quality.
        bd = EnergyBreakdown(0.0, 0.0, 0.0, 0.0, node_delta=999.0)
        self.assertEqual(bd.gating, 0.0)
        self.assertEqual(bd.lexicographic_key[2], 0.0)  # not in the C tier either


# ---------------------------------------------------------------------------
# Multi-module verification: accept consistent, reject inconsistent
# ---------------------------------------------------------------------------

class TestModuleVerification(unittest.TestCase):

    def setUp(self):
        self.cg = build_neoforge_constraint_graph()

    def test_consistent_project_accepted(self):
        graph = ModuleGraph.build(generate_consistent_project(0).source_map())
        report = verify_module_graph(graph, self.cg)
        self.assertTrue(report.accepted)
        self.assertEqual(report.cross_module.hard_count, 0)
        self.assertEqual(report.distance, 0.0)
        self.assertIsNone(report.diagnostic)
        self.assertEqual(report.provider_count, 2)

    def test_inconsistent_project_rejected_with_named_defects(self):
        graph = ModuleGraph.build(generate_inconsistent_project(0).source_map())
        report = verify_module_graph(graph, self.cg)
        self.assertFalse(report.accepted)
        self.assertGreater(report.cross_module.hard_count, 0)
        self.assertEqual(report.distance, float(report.cross_module.hard_count))
        # The diagnostic names every cross-module defect.
        self.assertIsNotNone(report.diagnostic)
        kinds = {d["kind"] for d in report.diagnostic["cross_module_defects"]}
        self.assertIn("DANGLING", kinds)
        self.assertIn("COLLISION", kinds)

    def test_non_provider_modules_do_not_cause_failure(self):
        # The consistent project contains non-provider core/utility modules;
        # acceptance proves they are not held to provider constraints.
        graph = ModuleGraph.build(generate_consistent_project(0).source_map())
        report = verify_module_graph(graph, self.cg)
        self.assertTrue(report.accepted)
        # Provider modules were checked; non-providers contributed no violations.
        for mid, results in report.per_module_results.items():
            if not graph.descriptor(mid).capability_evidence:
                self.assertEqual([r for r in results if r.violated], [])

    def test_verification_is_deterministic(self):
        graph = ModuleGraph.build(generate_inconsistent_project(0).source_map())
        r1 = verify_module_graph(graph, self.cg)
        r2 = verify_module_graph(graph, self.cg)
        self.assertEqual(r1.accepted, r2.accepted)
        self.assertEqual(r1.breakdown.total, r2.breakdown.total)
        self.assertEqual(r1.cross_module.to_dict(), r2.cross_module.to_dict())


# ---------------------------------------------------------------------------
# Acceptance anchors: C is a correctness term, not a size penalty
# ---------------------------------------------------------------------------

class TestAcceptanceAnchors(unittest.TestCase):

    def setUp(self):
        self.cg = build_neoforge_constraint_graph()

    def test_large_but_clean_project_still_accepts(self):
        # Many providers + a large core: big project, but cross-module clean.
        # If C had smuggled a size-proportional term into the gate (the 0.6.0
        # failure mode), this would wrongly reject. It must accept.
        n = 8
        keys = [f"BIGKEY_{i}" for i in range(n)]
        core_assigns = "".join(f"{k} = object()\n" for k in keys)
        core_all = "__all__ = [" + ", ".join(repr(k) for k in keys) + "]\n"
        src = {"big.core": core_assigns + core_all}
        for i, k in enumerate(keys):
            src[f"big.prov{i}"] = (
                f"from big.core import {k}\n"
                "from neoforge.common.capabilities import ICapabilityProvider, LazyOptional\n"
                "from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar\n"
                f"class Prov{i}:\n"
                f"    def getCapability(self, cap, direction=None):\n"
                f"        if cap == {k}:\n"
                f"            return LazyOptional.of(lambda: self._h)\n"
                f"        return LazyOptional.empty()\n"
                f"    def invalidateCapabilities(self):\n"
                f"        self._lazy = None\n"
                f"    def register_capability(self, registrar: BlockCapabilityRegistrar):\n"
                f"        registrar.registerBlockEntity({k}, self)\n"
            )
        graph = ModuleGraph.build(src)
        report = verify_module_graph(graph, self.cg)
        self.assertTrue(report.accepted, report.diagnostic)
        self.assertEqual(report.cross_module.hard_count, 0)
        self.assertEqual(report.provider_count, n)

    def test_tiny_but_broken_project_rejects(self):
        # Smallest possible cross-module break: one dangling import.
        src = {
            "tiny.core": "REAL = 1\n__all__ = ['REAL']\n",
            "tiny.user": "from tiny.core import MISSING\n",
        }
        graph = ModuleGraph.build(src)
        report = verify_module_graph(graph, self.cg)
        self.assertFalse(report.accepted)
        self.assertGreaterEqual(report.cross_module.hard_count, 1)
        self.assertIn(
            "DANGLING",
            {d["kind"] for d in report.diagnostic["cross_module_defects"]},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
