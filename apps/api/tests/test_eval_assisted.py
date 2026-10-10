"""The assisted evaluation's controls (AI1): replay, recording, spend cap, key file, freeze,
gates and the pre-registered decision. No network, no key, no sandbox."""

import json
import stat
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from api.extraction.assisted import CallRecord
from api.extraction.proposer import build_request, request_sha256, worst_case_cost
from scripts import eval_assisted
from scripts.eval_assisted import (
    BudgetedProposer,
    RecordingProposer,
    ReplayProposer,
    SpendLedger,
    call_summary,
    corpus_splits,
    decision,
    hard_failures,
    read_api_key,
    report_differences,
)
from tests.fake_proposer import FakeProposer, response

REQUEST = build_request("system", ["provider_name"], {1: "Supplier: Acme Ltd"})


def test_replay_serves_the_recorded_response_and_fails_closed_without_one(tmp_path: Path) -> None:
    replay = ReplayProposer(tmp_path)
    with pytest.raises(SystemExit, match="no recorded response"):
        replay.propose(REQUEST)
    recorder = RecordingProposer(FakeProposer(response('{"x": 1}')), tmp_path)
    recorded = recorder.propose(REQUEST)
    assert replay.propose(REQUEST) == recorded
    stored = json.loads((tmp_path / f"{request_sha256(REQUEST)}.json").read_text())
    assert set(stored) == {"request_sha256", "response"}
    assert "Supplier" not in json.dumps(stored)  # the request (document text) is not stored


def test_an_existing_recording_is_reused_never_overwritten_or_bought_again(
    tmp_path: Path,
) -> None:
    first = RecordingProposer(FakeProposer(response('{"x": 1}')), tmp_path).propose(REQUEST)
    path = tmp_path / f"{request_sha256(REQUEST)}.json"
    stored = path.read_bytes()
    inner = FakeProposer(response('{"x": 2}'))
    assert RecordingProposer(inner, tmp_path).propose(REQUEST) == first
    assert inner.requests == []
    assert path.read_bytes() == stored


def test_identical_requests_in_one_run_are_paid_and_recorded_once(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json", 1.0)
    inner = FakeProposer(response('{"x": 1}'), response('{"x": 2}'))
    recorder = RecordingProposer(BudgetedProposer(inner, ledger), tmp_path / "run-1")
    assert recorder.propose(REQUEST) == recorder.propose(dict(REQUEST))
    assert len(inner.requests) == 1
    assert len(ledger.calls) == 1
    assert len(json.loads((tmp_path / "ledger.json").read_text())["calls"]) == 1
    assert len(list((tmp_path / "run-1").glob("*.json"))) == 1


def test_the_cap_may_not_exceed_the_project_ceiling(tmp_path: Path) -> None:
    for cap in (0.0, -1.0, 5.01, 20.0):
        with pytest.raises(SystemExit, match="cap must be"):
            SpendLedger(tmp_path / "ledger.json", cap)
    assert SpendLedger(tmp_path / "ledger.json", 5.0).cap_usd == 5.0


def test_no_call_is_made_when_its_worst_case_would_pass_the_cap(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    worst = worst_case_cost(REQUEST)
    ledger = SpendLedger(path, cap_usd=worst * 1.5)
    inner = FakeProposer(response("{}", input_tokens=10_000, output_tokens=1_000))
    BudgetedProposer(inner, ledger).propose(REQUEST)  # spent 0.03 USD
    assert ledger.spent_usd == pytest.approx(0.03)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    reloaded = SpendLedger(path, cap_usd=worst * 1.5)
    assert reloaded.spent_usd == pytest.approx(0.03)  # survives a restart
    second = FakeProposer(response("{}"))
    with pytest.raises(SystemExit, match="spend cap"):
        BudgetedProposer(second, reloaded).propose(REQUEST)
    assert second.requests == []
    assert json.loads(path.read_text())["calls"][0]["request_sha256"] == request_sha256(REQUEST)


def test_the_cap_counts_the_worst_case_of_the_next_call(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    worst = worst_case_cost(REQUEST)
    cap = 1.0
    path.write_text(json.dumps({"calls": [{"cost_usd": cap - worst}]}))
    SpendLedger(path, cap).check(REQUEST)  # exactly at the cap: allowed
    path.write_text(json.dumps({"calls": [{"cost_usd": cap - worst / 2}]}))
    with pytest.raises(SystemExit, match="spend cap"):
        SpendLedger(path, cap).check(REQUEST)  # spent < cap, but not with this call


def test_a_failed_call_is_still_recorded_in_the_ledger(tmp_path: Path) -> None:
    ledger = SpendLedger(tmp_path / "ledger.json", 1.0)
    BudgetedProposer(FakeProposer(response(status="unavailable")), ledger).propose(REQUEST)
    assert [c["status"] for c in ledger.calls] == ["unavailable"]


def _env(tmp_path: Path, content: str, mode: int = 0o600) -> Path:
    path = tmp_path / "anthropic.env"
    path.write_text(content)
    path.chmod(mode)
    return path


def test_the_key_comes_only_from_a_private_env_file(tmp_path: Path) -> None:
    assert read_api_key(_env(tmp_path, "ANTHROPIC_API_KEY=sk-test-1\n")) == "sk-test-1"
    assert read_api_key(_env(tmp_path, "# x\nexport ANTHROPIC_API_KEY='sk-test-2'\n")) == (
        "sk-test-2"
    )
    for content, mode, match in (
        ("ANTHROPIC_API_KEY=sk-test-3\n", 0o644, "mode 600"),
        ("OTHER=sk-test-4\n", 0o600, "no ANTHROPIC_API_KEY"),
        ("ANTHROPIC_API_KEY=\n", 0o600, "no ANTHROPIC_API_KEY"),
    ):
        with pytest.raises(SystemExit, match=match) as raised:
            read_api_key(_env(tmp_path, content, mode))
        assert "sk-test" not in str(raised.value)
    with pytest.raises(SystemExit, match="missing"):
        read_api_key(tmp_path / "absent.env")


def _no_live_client(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise AssertionError("a live client was created")

    monkeypatch.setattr(eval_assisted, "AnthropicProposer", refuse)
    monkeypatch.setattr(eval_assisted, "read_api_key", refuse)
    monkeypatch.setenv("PARSER_IMAGE", "unused")


def _at(monkeypatch: pytest.MonkeyPatch, name: str, eval_dir: Path) -> eval_assisted.Measurement:
    """The named profile's measurement with its committed files under `eval_dir`."""
    measurement = replace(eval_assisted.MEASUREMENTS[name], eval_dir=eval_dir)
    monkeypatch.setitem(eval_assisted.MEASUREMENTS, name, measurement)
    return measurement


def _live(name: str, *cases: str) -> list[str]:
    argv = ["x", "--profile", name, "--live", "--run", "1", "--spend-cap", "1"]
    return argv + [arg for case in cases for arg in ("--case", case)]


def _cases(*splits: str) -> list[str]:
    return list(corpus_splits(splits))


def test_held_out_cases_are_never_called_before_the_freeze(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_live_client(monkeypatch)
    _at(monkeypatch, "assisted-2", tmp_path)
    for case in _cases("heldout", "heldout-2", "heldout-3"):
        assert eval_assisted.main(_live("assisted-2", case)) == 2


def test_assisted_2_never_sends_heldout_2_or_development_cases_once_frozen(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_live_client(monkeypatch)
    measurement = _at(monkeypatch, "assisted-2", tmp_path)
    measurement.frozen_config.write_text(
        json.dumps(eval_assisted.frozen_configuration(measurement))
    )
    assert eval_assisted.is_frozen(measurement)
    for case in _cases("heldout-2", "assist-dev"):
        assert eval_assisted.main(_live("assisted-2", case)) == 2


def test_assisted_1_is_never_called_again(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_live_client(monkeypatch)
    assert eval_assisted.MEASUREMENTS["assisted-1"].report.exists()
    assert eval_assisted.main(_live("assisted-1", _cases("dev")[0])) == 2


def test_live_needs_a_profile_a_run_number_and_a_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    _no_live_client(monkeypatch)
    assert eval_assisted.main(["x", "--live", "--run", "1", "--spend-cap", "1"]) == 2
    assert eval_assisted.main(["x", "--profile", "assisted-3", "--live", "--run", "1"]) == 2
    assert eval_assisted.main(["x", "--profile", "assisted-2", "--live", "--run", "1"]) == 2
    with pytest.raises(SystemExit, match="--run"):
        eval_assisted.main(["x", "--profile", "assisted-2", "--live", "--spend-cap", "1"])
    with pytest.raises(SystemExit, match="--run"):
        eval_assisted.main(
            ["x", "--profile", "assisted-2", "--live", "--run", "4", "--spend-cap", "1"]
        )


def test_a_changed_configuration_cannot_use_the_frozen_record(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_live_client(monkeypatch)
    measurement = _at(monkeypatch, "assisted-2", tmp_path)
    record = eval_assisted.frozen_configuration(measurement)
    assert record["verifier"] == "2" and record["extractor"].startswith("assisted-2+")
    measurement.frozen_config.write_text(json.dumps({**record, "prompt_sha256": "0" * 64}))
    with pytest.raises(SystemExit, match="differs from the frozen record"):
        eval_assisted.is_frozen(measurement)
    with pytest.raises(SystemExit, match="differs from the frozen record"):
        eval_assisted.main(_live("assisted-2", _cases("dev")[0]))
    measurement.frozen_config.write_text(json.dumps({**record, "verifier": "1"}))
    with pytest.raises(SystemExit, match="differs from the frozen record"):
        eval_assisted.is_frozen(measurement)


def test_the_committed_freeze_records_match_the_code() -> None:
    assisted_1 = eval_assisted.MEASUREMENTS["assisted-1"]
    assert eval_assisted.is_frozen(assisted_1)  # historical: must replay unchanged
    assert "verifier" not in eval_assisted.frozen_configuration(assisted_1)
    for measurement in eval_assisted.MEASUREMENTS.values():
        if measurement.frozen_config.exists():
            assert eval_assisted.is_frozen(measurement)


def test_assisted_2_is_frozen_exactly_as_verified() -> None:
    measurement = eval_assisted.MEASUREMENTS["assisted-2"]
    record = json.loads(measurement.frozen_config.read_text())
    assert record["extractor"] == "assisted-2+342453203207"
    assert record["verifier"] == "2" and record["max_tokens"] == 1024
    assert eval_assisted.is_frozen(measurement)


def test_a_frozen_profile_without_recorded_runs_is_not_replayed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _no_live_client(monkeypatch)
    _at(monkeypatch, "assisted-1", tmp_path / "one")  # not frozen there
    measurement = _at(monkeypatch, "assisted-2", tmp_path)
    measurement.frozen_config.write_text(
        json.dumps(eval_assisted.frozen_configuration(measurement))
    )
    assert eval_assisted.main(["x"]) == 2  # nothing replayed
    assert "assisted-2 is frozen; no recorded runs yet" in capsys.readouterr().out


def test_replay_without_a_frozen_configuration_reports_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _no_live_client(monkeypatch)
    _at(monkeypatch, "assisted-1", tmp_path)
    _at(monkeypatch, "assisted-2", tmp_path)
    assert eval_assisted.main(["x"]) == 2


def test_report_differences_allow_new_metrics_but_no_changed_value() -> None:
    committed = {"runs": [{"metrics": {"dev": {"field_accuracy": 1.0}}}], "decision": False}
    grown = {"runs": [{"metrics": {"dev": {"field_accuracy": 1.0, "new": 0}}}], "decision": False}
    assert report_differences(committed, grown) == []
    changed = {"runs": [{"metrics": {"dev": {"field_accuracy": 0.9}}}], "decision": False}
    assert report_differences(committed, changed) == ["report.runs[0].metrics.dev.field_accuracy"]
    assert report_differences(committed, {"runs": [], "decision": False}) == ["report.runs"]
    assert report_differences(committed, {"runs": committed["runs"]}) == [
        "report.decision (missing)"
    ]


def _call(
    latency: float | None, cost: float = 0.01, status: Any = "ok", entity_rejected: int = 0
) -> CallRecord:
    return CallRecord(
        status=status,
        requested=("provider_name",),
        latency_seconds=latency,
        cost_usd=cost,
        entity_rejected=entity_rejected,
    )


def test_call_summary_counts_and_nearest_rank_p95() -> None:
    calls = [_call(float(n)) for n in range(1, 21)] + [_call(None, 0.0, "not_needed")]
    summary = call_summary(calls)
    assert (summary["documents"], summary["calls"]) == (21, 20)
    assert summary["p95_latency_seconds"] == 19.0
    assert summary["mean_cost_per_call_usd"] == 0.01
    assert summary["statuses"] == {"not_needed": 1, "ok": 20}


def test_call_summary_counts_a_repeated_request_once_as_paid() -> None:
    first = replace(_call(1.0, 0.004), request_sha256="a" * 64)
    repeat = replace(_call(1.0, 0.004), request_sha256="a" * 64)  # identical text, replayed
    other = replace(_call(2.0, 0.005, entity_rejected=2), request_sha256="b" * 64)
    summary = call_summary([first, repeat, other])
    assert (summary["calls"], summary["cost_usd"]) == (3, 0.013)
    assert (summary["distinct_requests"], summary["distinct_cost_usd"]) == (2, 0.009)
    assert summary["entity_rejected_mentions"] == 2


def _metrics(**values: Any) -> dict[str, Any]:
    base = {
        "cases": 1,
        "failure_cases": 0,
        "outcome_accuracy": 1.0,
        "failure_code_accuracy": None,
        "schema_valid_rate": 1.0,
        "citation_resolution": 1.0,
        "unsupported_claims": 0,
        "field_accuracy": 1.0,
        "evidence_completeness": 1.0,
        "silent_conflicts": 0,
        "wrong_values": 0,
        "wrong_party_names": 0,
        "abstention_precision": 1.0,
        "abstention_recall": 1.0,
        "conflict_precision": 1.0,
        "conflict_recall": 1.0,
    }
    return {**base, **values}


ASSISTED_1 = eval_assisted.MEASUREMENTS["assisted-1"]
ASSISTED_2 = eval_assisted.MEASUREMENTS["assisted-2"]


def test_hard_gates_hold_heldout_2_to_integrity_only_and_pin_the_model() -> None:
    weak = _metrics(field_accuracy=0.1, abstention_precision=0.1, outcome_accuracy=0.1)
    model = ["claude-sonnet-5-5"]
    summary: dict[str, Any] = {"metrics": {"dev": _metrics(), "heldout-2": weak}, "models": model}
    assert hard_failures(summary, ASSISTED_1) == []
    summary["metrics"]["heldout-2"] = _metrics(unsupported_claims=1)
    assert hard_failures(summary, ASSISTED_1) == ["heldout-2: 1 unsupported claims (must be 0)"]
    summary = {"metrics": {"dev": _metrics(wrong_values=1)}, "models": ["claude-other"]}
    assert hard_failures(summary, ASSISTED_1) == [
        "dev: 1 wrong values (must be 0)",
        "model claude-other is not claude-sonnet-5-5",
    ]


def test_the_first_held_out_split_must_not_regress() -> None:
    summary = {"metrics": {"heldout": _metrics(field_accuracy=0.1)}, "models": []}
    assert any("heldout: field_accuracy" in f for f in hard_failures(summary, ASSISTED_1))
    assert any("heldout: field_accuracy" in f for f in hard_failures(summary, ASSISTED_2))


def test_assisted_2_gates_heldout_3_on_integrity_and_assist_dev_on_correctness() -> None:
    weak = _metrics(field_accuracy=0.1, abstention_precision=0.1)
    summary: dict[str, Any] = {
        "metrics": {"dev": _metrics(), "heldout-3": weak, "assist-dev": weak},
        "models": ["claude-sonnet-5-5"],
    }
    assert hard_failures(summary, ASSISTED_2) == []
    summary["metrics"]["assist-dev"] = _metrics(wrong_values=1, wrong_party_names=1)
    assert hard_failures(summary, ASSISTED_2) == [
        "assist-dev: 1 wrong_values (must be 0)",
        "assist-dev: 1 wrong_party_names (must be 0)",
    ]
    summary["metrics"]["assist-dev"] = _metrics(citation_resolution=0.9)
    assert hard_failures(summary, ASSISTED_2) == [
        "assist-dev: citation_resolution 0.9 (must be 1.0)"
    ]


def _run(field: float, evidence: float, split: str = "heldout-2", **values: Any) -> dict[str, Any]:
    latency = values.pop("latency", 5.0)
    cost = values.pop("cost", 0.01)
    failures = values.pop("failures", [])
    metrics = _metrics(
        field_accuracy=field,
        evidence_completeness=evidence,
        abstention_recall=values.pop("abstention_recall", 1.0),
        conflict_recall=values.pop("conflict_recall", 0.5),
        wrong_values=values.pop("wrong_values", 0),
        wrong_party_names=values.pop("wrong_party_names", 0),
        silent_conflicts=values.pop("silent_conflicts", 1),
    )
    calls = {"p95_latency_seconds": latency, "mean_cost_per_call_usd": cost}
    return {"metrics": {split: metrics}, "calls": {split: calls}, "hard_failures": failures}


def test_the_decision_follows_the_preregistration_on_the_worst_run() -> None:
    # rules-1 on heldout-2: field 0.6339, evidence 0.3538 (corpus/eval/heldout-2-baseline.json)
    good = [_run(0.80, 0.60), _run(0.79, 0.55), _run(0.7839, 0.5038)]
    assert decision(good, ASSISTED_1)["enable_assistance"] is True
    variants: list[tuple[int, dict[str, Any]]] = [
        (2, {"field": 0.7838}),  # one run below +0.15 field accuracy
        (1, {"evidence": 0.5037}),
        (0, {"abstention_recall": 0.9}),
        (0, {"conflict_recall": 0.0}),
        (2, {"wrong_values": 1}),
        (1, {"silent_conflicts": 2}),
        (0, {"latency": 20.5}),
        (1, {"cost": 0.051}),
        (2, {"failures": ["dev: 1 wrong values (must be 0)"]}),
    ]
    for index, change in variants:
        runs = list(good)
        field = change.pop("field", [0.80, 0.79, 0.7839][index])
        evidence = change.pop("evidence", [0.60, 0.55, 0.5038][index])
        runs[index] = _run(field, evidence, **change)
        assert decision(runs, ASSISTED_1)["enable_assistance"] is False, (index, field, evidence)
    assert decision(good[:2], ASSISTED_1) == {
        "runs_measured": 2,
        "rules_1": {},
        "worst": {},
        "checks": {},
        "enable_assistance": False,
    }


def test_assisted_2_decision_follows_the_heldout_3_preregistration() -> None:
    # rules-1 on heldout-3 (corpus/eval/heldout-3-baseline.json): field 0.7679, evidence
    # 0.4459, abstention recall 1.0, conflict recall 0.0, wrong values 0, silent conflicts 2.
    def h3(field: float = 0.95, evidence: float = 0.9, **values: Any) -> dict[str, Any]:
        values.setdefault("conflict_recall", 0.0)
        values.setdefault("silent_conflicts", 2)
        return _run(field, evidence, "heldout-3", **values)

    good = [h3(), h3(0.9179, 0.5959), h3()]
    result = decision(good, ASSISTED_2)
    assert result["enable_assistance"] is True
    assert result["checks"]["wrong_party_names"] is True
    for change in (
        {"field": 0.9178},
        {"evidence": 0.5958},
        {"wrong_values": 1},
        {"wrong_party_names": 1},
        {"silent_conflicts": 3},
        {"abstention_recall": 0.99},
        {"cost": 0.0501},
    ):
        runs = [*good[:2], h3(**change)]
        assert decision(runs, ASSISTED_2)["enable_assistance"] is False, change
