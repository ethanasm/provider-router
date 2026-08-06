"""A contract test any adapter can run against.

The router is only as good as its adapters' honesty. An adapter that returns
``None`` from ``classify``, raises out of ``supports``, or reports a rate limit
as terminal will produce routing bugs that look like provider bugs and are
miserable to trace back.

Point this at your adapter in your own test suite::

    from provider_router.conformance import assert_provider_contract

    def test_geocoder_contract():
        assert_provider_contract(
            NominatimGeocoder(),
            sample_request=Address("1600 Amphitheatre Pkwy"),
            sample_result=Coordinates(lat=37.42, lon=-122.08),
            expected_classifications={
                HTTPError(429): FailureKind.RATE_LIMITED,
                ConnectionResetError(): FailureKind.TRANSIENT,
                ValueError("unparseable address"): FailureKind.TERMINAL,
            },
        )

Adapters are *your* code — they encode your providers and your failure
dialects, so this library never ships them. What it ships is the contract they
implement and this test that holds them to it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .outcomes import Failure, FailureKind, Outcome
from .provider import Attempt, Provider

__all__ = ["ContractViolation", "assert_provider_contract", "check_provider_contract"]


class ContractViolation(AssertionError):
    """Raised when an adapter breaks the provider contract."""


def check_provider_contract(
    provider: Provider[Any, Any],
    *,
    sample_request: Any,
    sample_result: Any = None,
    expected_classifications: Mapping[BaseException, FailureKind] | None = None,
) -> list[str]:
    """Return a list of contract violations. Empty means the adapter is well-behaved."""
    problems: list[str] = []

    name = getattr(provider, "name", None)
    if not isinstance(name, str) or not name:
        problems.append("provider.name must be a non-empty string")

    problems += _check_supports(provider, sample_request)
    problems += _check_classify(provider, expected_classifications or {})
    if sample_result is not None:
        problems += _check_assess(provider, sample_result)

    return problems


def _check_supports(provider: Provider[Any, Any], request: Any) -> list[str]:
    try:
        supported = provider.supports(request)
    except Exception as exc:
        return [
            f"supports() raised {type(exc).__name__}: {exc}. It is called before any I/O "
            "and must answer, not throw — a provider that cannot decide is a provider "
            "the router cannot skip safely."
        ]
    if not isinstance(supported, bool):
        return [f"supports() must return bool, got {type(supported).__name__}"]
    return []


def _check_classify(
    provider: Provider[Any, Any], expected: Mapping[BaseException, FailureKind]
) -> list[str]:
    problems: list[str] = []

    # Every adapter must classify something it has never seen without exploding.
    probe = RuntimeError("unrecognized failure the adapter has never seen")
    try:
        result = provider.classify(probe)
    except NotImplementedError:
        problems.append(
            "classify() is not implemented. There is deliberately no default: guessing "
            "that an unknown error is transient turns a permanent misconfiguration into "
            "an endless failover loop."
        )
        return problems
    except Exception as exc:
        problems.append(f"classify() raised {type(exc).__name__} on an unknown error: {exc}")
        return problems

    if not isinstance(result, Failure):
        problems.append(f"classify() must return a Failure, got {type(result).__name__}")

    for sample, kind in expected.items():
        label = type(sample).__name__
        try:
            actual = provider.classify(sample)
        except Exception as raised:
            problems.append(f"classify({label}) raised {type(raised).__name__}: {raised}")
            continue
        if not isinstance(actual, Failure):
            problems.append(f"classify({label}) must return a Failure, got {type(actual).__name__}")
            continue
        if actual.kind is not kind:
            problems.append(
                f"classify({label}) returned {actual.kind.value}, expected {kind.value}"
            )
        if (
            actual.kind is FailureKind.RATE_LIMITED
            and actual.retry_after is not None
            and actual.retry_after < 0
        ):
            problems.append("rate-limited failures must not carry a negative retry_after")

    return problems


def _check_assess(provider: Provider[Any, Any], result: Any) -> list[str]:
    attempt = Attempt(provider=getattr(provider, "name", "?"), elapsed=0.01)
    try:
        outcome = provider.assess(result, attempt)
    except Exception as exc:
        return [f"assess() raised {type(exc).__name__}: {exc}; it must judge, not throw"]
    if not isinstance(outcome, Outcome):
        return [f"assess() must return an Outcome, got {type(outcome).__name__}"]
    return []


def assert_provider_contract(
    provider: Provider[Any, Any],
    *,
    sample_request: Any,
    sample_result: Any = None,
    expected_classifications: Mapping[BaseException, FailureKind] | None = None,
) -> None:
    """Raise :class:`ContractViolation` if the adapter misbehaves."""
    problems = check_provider_contract(
        provider,
        sample_request=sample_request,
        sample_result=sample_result,
        expected_classifications=expected_classifications,
    )
    if problems:
        name = getattr(provider, "name", type(provider).__name__)
        bullets = "\n".join(f"  - {p}" for p in problems)
        raise ContractViolation(f"{name} breaks the provider contract:\n{bullets}")
