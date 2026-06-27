"""
TSAM Research Validation Phase — Test Case Generators
======================================================
Generates synthetic test cases at each complexity level L1–L5.
Each generator produces both solvable and structurally-unsolvable cases.

Solvable:   Fabric-style source that TSAM can transform into the NeoForge manifold.
Unsolvable: Source with structural contradictions that no finite rewrite budget can resolve
            (e.g., contradictory API usage + missing required structural pattern).

Design principle: generators are deterministic and parameterized.
seed + complexity_level + case_index → identical source every time.
No random state, no datetime, no external dependencies.
"""

from __future__ import annotations

import hashlib
import textwrap
from dataclasses import dataclass
from enum import Enum, auto


class ComplexityLevel(Enum):
    L1 = 1   # 1 class,  2 methods,  6H+2S constraints
    L2 = 2   # 3 classes, 8 methods,  6H+2S+2soft
    L3 = 3   # 5 classes, 20 methods, 8H+4S+3soft
    L4 = 4   # 10 classes, 50 methods, 10H+6S+4soft
    L5 = 5   # 20 classes, 100 methods, 12H+8S+5soft


@dataclass(frozen=True, slots=True)
class ComplexityProfile:
    """Defines the structural shape of a test case at a given level."""
    level:              ComplexityLevel
    n_classes:          int
    n_methods_per_class: int
    n_hard_constraints: int
    n_strong_constraints: int
    n_soft_constraints: int
    expected_rewrites_min: int
    expected_rewrites_max: int


COMPLEXITY_PROFILES: dict[ComplexityLevel, ComplexityProfile] = {
    ComplexityLevel.L1: ComplexityProfile(ComplexityLevel.L1,  1,   2,  6,  2, 0,  3,   5),
    ComplexityLevel.L2: ComplexityProfile(ComplexityLevel.L2,  3,   3,  6,  2, 2,  5,  10),
    ComplexityLevel.L3: ComplexityProfile(ComplexityLevel.L3,  5,   4,  8,  4, 3, 10,  20),
    ComplexityLevel.L4: ComplexityProfile(ComplexityLevel.L4, 10,   5, 10,  6, 4, 20,  40),
    ComplexityLevel.L5: ComplexityProfile(ComplexityLevel.L5, 20,   5, 12,  8, 5, 40,  80),
}


@dataclass(frozen=True, slots=True)
class TestCase:
    """A single generated test case."""
    case_id:       str
    level:         ComplexityLevel
    solvable:      bool
    source:        str
    description:   str
    expected_hard_violations: int   # In the raw source before transformation
    class_count:   int
    method_count:  int


# ---------------------------------------------------------------------------
# Source generators
# ---------------------------------------------------------------------------

def _fabric_capability_block(class_idx: int, n_extra_methods: int) -> str:
    """
    Generate one Fabric-style capability provider class.
    Contains getCapability (present but Fabric-style) and
    n_extra_methods helper methods at class body level.
    """
    extras_lines: list[str] = []
    for i in range(n_extra_methods):
        extras_lines.append(f"    def helper_{class_idx}_{i}(self):")
        extras_lines.append(f"        return self.data_{class_idx}_{i}")
        extras_lines.append("")
    extras = "\n".join(extras_lines)

    block = (
        f"import net.fabricmc.fabric\n"
        f"from net.fabricmc import FabricUtil{class_idx}\n"
        f"\n"
        f"class FabricProvider{class_idx}:\n"
        f"    def __init__(self):\n"
        f"        self.handler_{class_idx} = MyHandler{class_idx}()\n"
        f"        self.lazy_{class_idx} = LazyOptional.of(lambda: self.handler_{class_idx})\n"
        f"\n"
        + extras
        + f"    def getCapability(self, cap, side):\n"
        f"        if cap == MY_CAP_{class_idx}:\n"
        f"            return self.lazy_{class_idx}\n"
        f"        return LazyOptional.empty()\n"
    )
    return block


def _neoforge_capability_block(class_idx: int, n_extra_methods: int) -> str:
    """
    Generate one valid NeoForge capability provider class.
    Used for the 'already valid' path and for building expected outputs.
    """
    extras = "\n".join(
        f"    def helper_{class_idx}_{i}(self):\n        return self.data_{class_idx}_{i}"
        for i in range(n_extra_methods)
    )
    return textwrap.dedent(f"""\
        from neoforge.common.capabilities import ICapabilityProvider, LazyOptional
        from net.neoforged.neoforge.capabilities import BlockCapabilityRegistrar

        class NeoProvider{class_idx}:
            def __init__(self):
                self._handler_{class_idx} = MyHandler{class_idx}()
                self._handler_lazy_{class_idx} = None
        {textwrap.indent(extras, '    ')}
            def getCapability(self, cap, direction=None):
                if cap == MY_CAPABILITY_{class_idx}:
                    return LazyOptional.of(lambda: self._handler_{class_idx})
                return LazyOptional.empty()

            def invalidateCapabilities(self):
                if self._handler_lazy_{class_idx} is not None:
                    self._handler_lazy_{class_idx}.invalidate()
                self._handler_lazy_{class_idx} = None

            def register_capability(self, registrar: BlockCapabilityRegistrar):
                registrar.registerBlockEntity(MY_CAPABILITY_{class_idx}, self)
    """)


def _unsolvable_block(class_idx: int) -> str:
    """
    Generate a source block that cannot be transformed to NeoForge.
    Contains contradictory API usage: uses Fabric APIs but also
    explicitly re-imports them in a way that will survive the
    forbidden-API rewrite (cyclic dependency pattern).
    Also missing the structural patterns required.
    """
    return textwrap.dedent(f"""\
        import net.fabricmc.fabric
        from io.github.fabricators_of_create import CreateFabricThing{class_idx}
        from net.fabricmc import ServerLifecycleEvents

        # This class deliberately uses Fabric event bus, not capability pattern.
        # It has no getCapability or invalidateCapabilities.
        # It cannot be ported by the current rewrite rules.
        class FabricEventOnlyClass{class_idx}:
            def __init__(self):
                ServerLifecycleEvents.SERVER_STARTED.register(self.on_start_{class_idx})

            def on_start_{class_idx}(self, server):
                CreateFabricThing{class_idx}().do_fabric_thing()
                return net.fabricmc.fabric.FABRIC_CONSTANT_{class_idx}
    """)


# ---------------------------------------------------------------------------
# Level-specific generators
# ---------------------------------------------------------------------------

def generate_solvable(level: ComplexityLevel, case_index: int = 0) -> TestCase:
    """
    Generate a solvable test case at the given complexity level.
    Source is Fabric-style; TSAM should transform it to NeoForge successfully.
    """
    profile = COMPLEXITY_PROFILES[level]
    n_extra = max(0, profile.n_methods_per_class - 2)  # -2 for getCapability + __init__

    blocks = []
    for i in range(profile.n_classes):
        blocks.append(_fabric_capability_block(
            class_idx     = i + (case_index * 100),
            n_extra_methods = n_extra,
        ))

    source = "\n\n".join(blocks)
    case_id = _case_id("solvable", level, case_index)

    return TestCase(
        case_id    = case_id,
        level      = level,
        solvable   = True,
        source     = source,
        description = (
            f"L{level.value} solvable: {profile.n_classes} Fabric provider class(es), "
            f"{profile.n_methods_per_class} methods each. "
            f"Should port to NeoForge within budget."
        ),
        expected_hard_violations = 3,   # FABRIC_APIs + no_neoforge + no_invalidate
        class_count  = profile.n_classes,
        method_count = profile.n_classes * profile.n_methods_per_class,
    )


def generate_unsolvable(level: ComplexityLevel, case_index: int = 0) -> TestCase:
    """
    Generate a structurally unsolvable test case.
    Source uses Fabric event bus pattern with no capability structure —
    cannot be transformed to NeoForge capability pattern with current rules.
    System must reject with a clean diagnostic.
    """
    profile = COMPLEXITY_PROFILES[level]

    blocks = []
    for i in range(profile.n_classes):
        blocks.append(_unsolvable_block(class_idx=i + (case_index * 100)))

    source = "\n\n".join(blocks)
    case_id = _case_id("unsolvable", level, case_index)

    return TestCase(
        case_id    = case_id,
        level      = level,
        solvable   = False,
        source     = source,
        description = (
            f"L{level.value} unsolvable: {profile.n_classes} Fabric event-bus class(es). "
            f"No capability pattern present. Should reject with diagnostic."
        ),
        expected_hard_violations = 5,   # fabric + no neoforge + no methods
        class_count  = profile.n_classes,
        method_count = profile.n_classes * 2,  # __init__ + on_start
    )


def generate_level(
    level:         ComplexityLevel,
    n_solvable:    int = 5,
    n_unsolvable:  int = 5,
) -> list[TestCase]:
    """
    Generate a full set of test cases for one complexity level.
    Returns n_solvable solvable cases + n_unsolvable unsolvable cases.
    """
    cases: list[TestCase] = []
    for i in range(n_solvable):
        cases.append(generate_solvable(level, case_index=i))
    for i in range(n_unsolvable):
        cases.append(generate_unsolvable(level, case_index=i))
    return cases


def generate_all_levels(n_per_level: int = 5) -> dict[ComplexityLevel, list[TestCase]]:
    """Generate the full RVP test suite across all 5 complexity levels."""
    return {
        level: generate_level(level, n_solvable=n_per_level, n_unsolvable=n_per_level)
        for level in ComplexityLevel
    }


def _case_id(kind: str, level: ComplexityLevel, index: int) -> str:
    """Deterministic case ID."""
    raw = f"tsam_rvp_{kind}_L{level.value}_{index:03d}"
    digest = hashlib.md5(raw.encode()).hexdigest()[:8]
    return f"{raw}_{digest}"


# ---------------------------------------------------------------------------
# Quick preview
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=== RVP Test Case Generator Preview ===\n")
    for level in ComplexityLevel:
        cases = generate_level(level, n_solvable=2, n_unsolvable=1)
        profile = COMPLEXITY_PROFILES[level]
        print(f"Level {level.name}  ({profile.n_classes} classes, {profile.n_methods_per_class} methods/class):")
        for c in cases:
            solv = "SOLVABLE  " if c.solvable else "UNSOLVABLE"
            src_lines = len(c.source.splitlines())
            print(f"  [{solv}] {c.case_id}  source_lines={src_lines}  methods={c.method_count}")
        print()
