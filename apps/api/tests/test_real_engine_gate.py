"""The real-engine gate itself: in CI the real-ClamAV tests can fail, never skip."""

import pytest

from tests.test_clamd_real import real_engine_required, skip_real_engine_tests

BOTH = {
    "REAL_CLAMD_ADDRESS": "tcp://127.0.0.1:3310",
    "REAL_CLAMD_TIGHT_ADDRESS": "tcp://127.0.0.1:3311",
}


@pytest.mark.parametrize(
    ("environ", "required", "skipped"),
    [
        ({}, False, True),  # a developer machine without an engine
        (BOTH, False, False),  # a developer machine with an engine
        ({"GITHUB_ACTIONS": "true"}, True, False),  # CI without REQUIRE_REAL_CLAMD
        ({"REQUIRE_REAL_CLAMD": "1"}, True, False),
        ({"GITHUB_ACTIONS": "true", "REQUIRE_REAL_CLAMD": "1", **BOTH}, True, False),
        ({"GITHUB_ACTIONS": "false"}, False, True),
        ({"REQUIRE_REAL_CLAMD": "0"}, False, True),
    ],
)
def test_the_real_engine_tests_skip_only_outside_ci_without_an_engine(
    environ: dict[str, str], required: bool, skipped: bool
) -> None:
    assert real_engine_required(environ) is required
    assert skip_real_engine_tests(environ) is skipped
