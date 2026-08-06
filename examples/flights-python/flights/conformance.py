"""Conformance checks specific to flight adapters.

Layered on top of the core :mod:`provider_router.conformance` suite, which
checks the generic provider contract. This one checks the thing that goes wrong
in *this* domain: capability declarations that describe the adapter rather than
the provider.
"""

from __future__ import annotations

from dataclasses import fields

from provider_router.conformance import ContractViolation

from .query import FlightCapabilities

__all__ = ["assert_capability_evidence", "check_capability_evidence"]

_BOOLEAN_CAPABILITIES = ("cabin", "airlines", "paginates", "max_stops")


def check_capability_evidence(caps: FlightCapabilities, provider: str) -> list[str]:
    """Require a citation for anything declared unsupported.

    Saying a provider *can* do something is self-correcting — the call fails
    loudly if you're wrong. Saying it *cannot* is not: the router simply stops
    asking, and nothing ever contradicts you. So the burden of proof sits on
    the negative claim.

    Returns a list of problems; empty means the declaration is reviewable.
    """
    problems: list[str] = []

    declared_false = [
        f.name
        for f in fields(caps)
        if f.name in _BOOLEAN_CAPABILITIES and getattr(caps, f.name) is False
    ]
    if declared_false and not (caps.evidence or "").strip():
        problems.append(
            f"{provider} declares {', '.join(sorted(declared_false))} unsupported but gives no "
            "`evidence`. Cite the provider's own schema (e.g. its MCP `tools/list` output or "
            "documented parameter set) — an unsupported capability that is actually supported "
            "fails silently: the router stops asking and every search takes the provider's "
            "default instead of what the caller requested."
        )
    return problems


def assert_capability_evidence(caps: FlightCapabilities, provider: str) -> None:
    """Raise :class:`ContractViolation` if an unsupported claim is unevidenced."""
    problems = check_capability_evidence(caps, provider)
    if problems:
        raise ContractViolation("\n".join(f"  - {p}" for p in problems))
