"""The conformance suite must catch the adapter mistakes that cause routing bugs."""

from __future__ import annotations

import pytest

from conftest import Boom, FakeProvider, Throttled
from provider_router import (
    Attempt,
    BaseProvider,
    ContractViolation,
    Failure,
    FailureKind,
    Outcome,
    assert_provider_contract,
    check_provider_contract,
)


def test_a_well_behaved_adapter_passes():
    assert_provider_contract(
        FakeProvider("good"),
        sample_request="q",
        sample_result="result",
        expected_classifications={
            Throttled(retry_after=5): FailureKind.RATE_LIMITED,
            Boom(): FailureKind.TRANSIENT,
            ValueError("nope"): FailureKind.TERMINAL,
        },
    )


def test_catches_a_rate_limit_reported_as_terminal():
    """The nastiest adapter bug: the breaker never opens, so you hammer a throttled provider."""

    class Miscategorising(FakeProvider):
        def classify(self, exc: BaseException) -> Failure:
            from provider_router import terminal

            return terminal("everything is fatal")

    problems = check_provider_contract(
        Miscategorising("bad"),
        sample_request="q",
        expected_classifications={Throttled(): FailureKind.RATE_LIMITED},
    )
    assert any("expected rate_limited" in p for p in problems)


def test_catches_missing_classify():
    class Unimplemented(BaseProvider):
        name = "lazy"

    problems = check_provider_contract(Unimplemented(), sample_request="q")
    assert any("classify() is not implemented" in p for p in problems)


def test_catches_classify_that_raises():
    class Exploding(FakeProvider):
        def classify(self, exc: BaseException) -> Failure:
            raise RuntimeError("kaboom")

    problems = check_provider_contract(Exploding("bad"), sample_request="q")
    assert any("classify() raised RuntimeError" in p for p in problems)


def test_catches_classify_returning_the_wrong_type():
    class WrongType(FakeProvider):
        def classify(self, exc: BaseException):
            return "rate limited"

    problems = check_provider_contract(WrongType("bad"), sample_request="q")
    assert any("must return a Failure" in p for p in problems)


def test_catches_supports_that_raises():
    class Throwing(FakeProvider):
        def supports(self, request: object) -> bool:
            raise RuntimeError("cannot decide")

    problems = check_provider_contract(Throwing("bad"), sample_request="q")
    assert any("supports() raised RuntimeError" in p for p in problems)


def test_catches_supports_returning_a_non_bool():
    class Truthy(FakeProvider):
        def supports(self, request: object):
            return "yes"

    problems = check_provider_contract(Truthy("bad"), sample_request="q")
    assert any("supports() must return bool" in p for p in problems)


def test_catches_assess_that_raises():
    class BadAssess(FakeProvider):
        def assess(self, result: object, attempt: Attempt) -> Outcome:
            raise RuntimeError("no idea")

    problems = check_provider_contract(BadAssess("bad"), sample_request="q", sample_result="r")
    assert any("assess() raised RuntimeError" in p for p in problems)


def test_catches_assess_returning_the_wrong_type():
    class BadAssess(FakeProvider):
        def assess(self, result: object, attempt: Attempt):
            return "fine"

    problems = check_provider_contract(BadAssess("bad"), sample_request="q", sample_result="r")
    assert any("assess() must return an Outcome" in p for p in problems)


def test_catches_a_missing_name():
    nameless = FakeProvider("")
    problems = check_provider_contract(nameless, sample_request="q")
    assert any("non-empty string" in p for p in problems)


def test_assert_raises_with_every_problem_listed():
    class Awful(FakeProvider):
        def supports(self, request: object):
            raise RuntimeError("no")

        def classify(self, exc: BaseException):
            raise RuntimeError("also no")

    with pytest.raises(ContractViolation) as excinfo:
        assert_provider_contract(Awful("awful"), sample_request="q")

    message = str(excinfo.value)
    assert "supports() raised" in message
    assert "classify() raised" in message


def test_base_provider_defaults_are_usable():
    class Minimal(BaseProvider):
        name = "minimal"

        def classify(self, exc: BaseException) -> Failure:
            from provider_router import transient

            return transient(str(exc))

    provider = Minimal()
    assert provider.supports("anything") is True
    assert provider.assess("r", Attempt("minimal", 0.0)) is Outcome.OK
    assert provider.assess("r", Attempt("minimal", 0.0, notes=("partial",))) is Outcome.DEGRADED
