"""
Cost-awareness contract. Every module in this pipeline that touches an
external API declares a MODULE_COST_PROFILE at module level using this
dataclass - no exceptions, including modules that are free (they declare
requires_paid_api=False explicitly, so "did someone forget to check" is
never ambiguous).

This is deliberately a plain module-level constant, not a decorator or
metaclass trick - explicit interfaces over implicit behavior, per the
architecture's own style constraints. Anyone reading a module file sees
its cost profile in the first 10 lines, no need to trace call graphs.
"""

from dataclasses import dataclass, field


@dataclass
class CostProfile:
    module_name: str
    requires_paid_api: bool = False
    # Rough per-call cost in USD. 0.0 for free modules. For paid modules,
    # this should match the real, documented pricing (see README cost
    # section) - not a made-up placeholder number.
    estimated_cost_per_call_usd: float = 0.0
    # What this module does INSTEAD when the paid API is disabled/unavailable
    # - every paid module must have a real fallback, not just "returns nothing."
    free_fallback_strategy: str = "N/A - this module has no paid dependency."
    notes: str = ""


# Central registry - modules register themselves here at import time via
# register(), so main.py can print one aggregate cost report at startup
# and after each scan cycle rather than each module silently doing its
# own thing.
_REGISTRY: dict = {}


def register(profile: CostProfile) -> CostProfile:
    _REGISTRY[profile.module_name] = profile
    return profile


def all_profiles() -> dict:
    return dict(_REGISTRY)


def paid_modules() -> list:
    return [p for p in _REGISTRY.values() if p.requires_paid_api]


def print_startup_report():
    """Called once at process start so cost exposure is visible immediately,
    not discovered after the first bill."""
    paid = paid_modules()
    print("=" * 60)
    print("COST-AWARENESS REPORT (printed at startup)")
    print("=" * 60)
    print(f"Total modules registered: {len(_REGISTRY)}")
    print(f"Modules requiring a paid API: {len(paid)}")
    if not paid:
        print("None currently active. Entire pipeline is running on free, "
              "deterministic public APIs.")
    else:
        for p in paid:
            print(f"  - {p.module_name}: ~${p.estimated_cost_per_call_usd:.3f}/call "
                  f"| fallback: {p.free_fallback_strategy}")
    print("=" * 60)
