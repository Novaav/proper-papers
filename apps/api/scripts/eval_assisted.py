"""AI1 model-assisted extraction evaluation (make eval-assisted).

    make eval-assisted        # replay every frozen profile's committed runs (offline, no key):
                              # gates, decision, and equality with its committed report
    python -m scripts.eval_assisted --profile P --replay-local --run N [--case ID ...]
                              # re-score a local development run of profile P (offline)
    python -m scripts.eval_assisted --profile P --live --run N --spend-cap USD [--case ID ...]
                              # live calls (paid): explicit flags, the key file and the cap

Profiles (api.extraction.assisted): assisted-1 is measured and historical (its
runs only replay; never called again); assisted-2 is developed on dev and
assist-dev and, once frozen, measured on dev, heldout and heldout-3. heldout-2
is never sent for assisted-2.

Each case goes through the same pipeline as `make eval` (scripts/evaluate.py:
byte-checked PDF, parser sandbox, parser-drift check) and is scored with the
same scoring code; the extractor is the assisted one (rules first, verified
model mentions only for fields the rules left not_found).

Replay (the default, and CI): every call's response is committed under
corpus/eval/<profile>/run-<n>/<request sha256>.json and served by the exact
request's hash; a missing response stops the run (no fallback, no network).
The recomputed report must contain the committed report unchanged (new
metrics may be added; no committed value may differ).
A response holds the model's answer to a synthetic document, the status, the
tokens and the latency; never the API key, never the request.

Live: only with --live, the gitignored key file .local/anthropic.env (mode
600) and --spend-cap. Before each call the worst-case cost is checked against
the cap and the actual spend so far (the gitignored ledger
.local/anthropic-spend.json, every call recorded as it completes); the cap may
not exceed the project's USD 5 ceiling. An existing response is never
overwritten and never re-bought. Until a profile is frozen
(corpus/eval/<profile>-config.json, committed before its first held-out call),
its live calls are limited to its development splits and recorded under
.local/assisted-dev/; afterwards only its measured splits are called, recorded
under corpus/eval/. A profile with a committed report is never called again.

Gates, per run: the hard gates of `make eval` on dev and the first held-out
split (integrity, dev accuracy 1.0 and 0 wrong values, held-out no regression),
integrity on the profile's primary split, and on assist-dev integrity, 0 wrong
values and 0 wrong party names. Decision (the primary split's pre-registration):
its thresholds on the worst of the three runs against rules-1 and the budget. A
negative decision is a result, not a failure.
"""

import json
import math
import os
import stat
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from api.extraction.assisted import (
    ASSISTED_1,
    ASSISTED_2,
    AssistedExtractor,
    CallRecord,
    Profile,
    config_sha256,
    extractor_version,
    prompt_sha256,
)
from api.extraction.proposer import (
    MODEL,
    AnthropicProposer,
    Proposer,
    ProviderResponse,
    configuration,
    request_sha256,
    worst_case_cost,
)
from api.sandbox import SandboxSettings
from scripts.eval_scoring import CaseScore, gate, summarize
from scripts.evaluate import BASELINES, CORPUS, ROOT, SPLITS, run

RUNS = 3
LOCAL = ROOT / ".local"
KEY_FILE = LOCAL / "anthropic.env"
LEDGER = LOCAL / "anthropic-spend.json"
LOCAL_RECORDINGS = LOCAL / "assisted-dev"
PROJECT_CEILING_USD = 5.00


@dataclass(frozen=True)
class Measurement:
    """How a profile is developed and measured (its pre-registration)."""

    profile: Profile
    development: tuple[str, ...]  # live before the freeze, recorded locally
    measured: tuple[str, ...]  # the frozen runs
    primary: str  # the pre-registered held-out split
    eval_dir: Path = CORPUS / "eval"  # where its freeze record, runs and report are committed

    @property
    def recordings(self) -> Path:
        return self.eval_dir / self.profile.name

    @property
    def frozen_config(self) -> Path:
        return self.eval_dir / f"{self.profile.name}-config.json"

    @property
    def report(self) -> Path:
        return self.eval_dir / f"{self.profile.name}-report.json"

    @property
    def preregistration(self) -> Path:
        return CORPUS / "eval" / f"{self.primary}-preregistration.json"


MEASUREMENTS = {
    "assisted-1": Measurement(ASSISTED_1, ("dev",), ("dev", "heldout", "heldout-2"), "heldout-2"),
    "assisted-2": Measurement(
        ASSISTED_2, ("dev", "assist-dev"), ("dev", "heldout", "heldout-3"), "heldout-3"
    ),
}


def corpus_splits(splits: tuple[str, ...]) -> dict[str, str]:
    """Case id -> split for the given splits, in corpus order (`run` checks the manifest
    against the cases)."""
    cases = json.loads((CORPUS / "manifest.json").read_text())["cases"]
    return {case["id"]: case["split"] for case in cases if case["split"] in splits}


class ReplayProposer:
    """Serves recorded responses by the exact request's hash; never calls out."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def propose(self, request: dict[str, Any]) -> ProviderResponse:
        path = self.directory / f"{request_sha256(request)}.json"
        if not path.is_file():
            raise SystemExit(f"eval-assisted: no recorded response {_shown(path)}")
        return ProviderResponse(**json.loads(path.read_text())["response"])


class RecordingProposer:
    """Records every response of `inner`. A request already recorded in this run (two
    cases with identical text send identical requests) is answered from its recording:
    `inner` is not called, nothing is spent and the recording is not rewritten."""

    def __init__(self, inner: Proposer, directory: Path) -> None:
        self.inner = inner
        self.directory = directory

    def propose(self, request: dict[str, Any]) -> ProviderResponse:
        sha256 = request_sha256(request)
        path = self.directory / f"{sha256}.json"
        if path.exists():
            return ReplayProposer(self.directory).propose(request)
        response = self.inner.propose(request)
        self.directory.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"request_sha256": sha256, "response": asdict(response)}, indent=1) + "\n"
        )
        return response


class SpendLedger:
    """Actual spend of every live call (local, gitignored) and the pre-call cap check."""

    def __init__(self, path: Path, cap_usd: float) -> None:
        if not 0 < cap_usd <= PROJECT_CEILING_USD:
            raise SystemExit(f"eval-assisted: the cap must be in (0, {PROJECT_CEILING_USD}]")
        self.path = path
        self.cap_usd = cap_usd
        self.calls: list[dict[str, Any]] = (
            json.loads(path.read_text())["calls"] if path.exists() else []
        )

    @property
    def spent_usd(self) -> float:
        return sum(float(call["cost_usd"]) for call in self.calls)

    def check(self, request: dict[str, Any]) -> None:
        worst = worst_case_cost(request)
        if self.spent_usd + worst > self.cap_usd:
            raise SystemExit(
                f"eval-assisted: spend cap: spent {self.spent_usd:.4f} + worst case "
                f"{worst:.4f} > cap {self.cap_usd:.2f} USD; no call made"
            )

    def record(self, sha256: str, response: ProviderResponse) -> None:
        self.calls.append(
            {
                "at": datetime.now(UTC).isoformat(timespec="seconds"),
                "request_sha256": sha256,
                "model": response.model,
                "status": response.status,
                "input_tokens": response.input_tokens,
                "output_tokens": response.output_tokens,
                "cache_read_tokens": response.cache_read_tokens,
                "cache_write_tokens": response.cache_write_tokens,
                "cost_usd": round(response.cost_usd, 6),
            }
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"calls": self.calls}, indent=1) + "\n")
        temporary.chmod(0o600)
        temporary.replace(self.path)


class BudgetedProposer:
    """Checks the cap before, and records the actual cost after, every call of `inner`."""

    def __init__(self, inner: Proposer, ledger: SpendLedger) -> None:
        self.inner = inner
        self.ledger = ledger

    def propose(self, request: dict[str, Any]) -> ProviderResponse:
        self.ledger.check(request)
        response = self.inner.propose(request)
        self.ledger.record(request_sha256(request), response)
        return response


def read_api_key(path: Path) -> str:
    """ANTHROPIC_API_KEY from a mode-600 env file; errors never include its content."""
    if not path.is_file():
        raise SystemExit(f"eval-assisted: {_shown(path)} is missing")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise SystemExit(f"eval-assisted: {_shown(path)} must be mode 600")
    for line in path.read_text().splitlines():
        name, _, value = line.strip().removeprefix("export ").partition("=")
        if name.strip() == "ANTHROPIC_API_KEY" and value.strip().strip("'\""):
            return value.strip().strip("'\"")
    raise SystemExit(f"eval-assisted: no ANTHROPIC_API_KEY in {_shown(path)}")


def frozen_configuration(measurement: Measurement) -> dict[str, Any]:
    """The record committed at the freeze: prompt, hashes, configuration (and verifier)."""
    profile = measurement.profile
    record = {
        "extractor": extractor_version(profile),
        "prompt": profile.name,
        "prompt_sha256": prompt_sha256(profile),
        "config_sha256": config_sha256(profile),
        **configuration(),
    }
    if profile.verifier is not None:
        record["verifier"] = profile.verifier
    return record


def is_frozen(measurement: Measurement) -> bool:
    """True if the committed freeze record exists; it must match the current configuration."""
    path = measurement.frozen_config
    if not path.exists():
        return False
    if json.loads(path.read_text()) != frozen_configuration(measurement):
        raise SystemExit(
            "eval-assisted: the prompt or configuration differs from the frozen record "
            f"{_shown(path)}; it cannot be measured on held-out data again"
        )
    return True


type Row = tuple[CaseScore, CallRecord | None]


def evaluate_run(
    settings: SandboxSettings, proposer: Proposer, only: set[str], measurement: Measurement
) -> list[Row]:
    """Every selected case through the pipeline, one at a time, with its call record."""
    rows: list[Row] = []
    for case_id in corpus_splits(measurement.development + measurement.measured):
        if case_id not in only:
            continue
        extractor = AssistedExtractor(proposer, measurement.profile)
        scores, _ = run(settings, extractor, only={case_id})
        rows.append((scores[0], extractor.calls[0] if extractor.calls else None))
        if extractor.calls:
            call = extractor.calls[0]
            print(
                f"  {case_id}: {call.status}, {call.proposed} proposed, {call.rejected} "
                f"rejected, {call.input_tokens}/{call.output_tokens} tokens, "
                f"{call.cost_usd:.4f} USD, {call.latency_seconds} s"
            )
    return rows


def run_summary(rows: list[Row]) -> dict[str, Any]:
    scores = [score for score, _ in rows]
    metrics = {
        split: summarize([s for s in scores if s.split == split])
        for split in SPLITS
        if any(s.split == split for s in scores)
    }
    calls = {split: [c for s, c in rows if c is not None and s.split == split] for split in metrics}
    return {
        "metrics": metrics,
        "calls": {split: call_summary(found) for split, found in calls.items()},
        "models": sorted({c.model for _, c in rows if c is not None and c.model}),
        "problems": {s.case: s.problems for s in scores if s.problems},
    }


def call_summary(calls: list[CallRecord]) -> dict[str, Any]:
    made = [c for c in calls if c.latency_seconds is not None]
    latencies = sorted(c.latency_seconds for c in made if c.latency_seconds is not None)
    statuses: dict[str, int] = {}
    for call in calls:
        statuses[call.status] = statuses.get(call.status, 0) + 1
    # Two cases with identical text send one identical request, answered once: the
    # distinct requests are what was paid for.
    distinct = {c.request_sha256: c for c in made if c.request_sha256 is not None}
    return {
        "documents": len(calls),
        "calls": len(made),
        "statuses": dict(sorted(statuses.items())),
        "proposed_mentions": sum(c.proposed for c in calls),
        "rejected_mentions": sum(c.rejected for c in calls),
        "model_fields": sum(len(c.model_fields) for c in calls),
        "input_tokens": sum(c.input_tokens for c in calls),
        "output_tokens": sum(c.output_tokens for c in calls),
        "cost_usd": round(sum(c.cost_usd for c in calls), 6),
        "mean_cost_per_call_usd": round(sum(c.cost_usd for c in made) / len(made), 6)
        if made
        else None,
        "p95_latency_seconds": latencies[math.ceil(0.95 * len(latencies)) - 1]
        if latencies
        else None,
        "distinct_requests": len(distinct),
        "distinct_cost_usd": round(sum(c.cost_usd for c in distinct.values()), 6),
        "entity_rejected_mentions": sum(c.entity_rejected for c in calls),
    }


def hard_failures(summary: dict[str, Any], measurement: Measurement) -> list[str]:
    """`make eval`'s gates, except: the primary split is held to integrity only (its
    comparison with rules-1 is the pre-registered decision), and assist-dev to integrity,
    0 wrong values and 0 wrong party names."""
    metrics = summary["metrics"]
    baselines = {
        split: json.loads(path.read_text()) for split, path in BASELINES.items() if path.exists()
    }
    special = {measurement.primary, "assist-dev"}
    others = {split: values for split, values in metrics.items() if split not in special}
    failures = gate(others, {"heldout": baselines.get("heldout")})
    for split in sorted(special & metrics.keys()):
        failures += gate({split: metrics[split]}, {}, compare_baseline=False)
    if "assist-dev" in metrics:
        for name in ("wrong_values", "wrong_party_names"):
            if metrics["assist-dev"][name] != 0:
                failures.append(f"assist-dev: {metrics['assist-dev'][name]} {name} (must be 0)")
    failures += [f"model {model} is not {MODEL}" for model in summary["models"] if model != MODEL]
    return failures


def decision(runs: list[dict[str, Any]], measurement: Measurement) -> dict[str, Any]:
    """The pre-registered decision rule over the recorded runs."""
    split = measurement.primary
    prereg = json.loads(measurement.preregistration.read_text())
    primary = prereg["thresholds"][f"primary_{split.replace('-', '_')}_min_over_runs"]
    budget = (
        prereg["thresholds"].get("budget_per_call") or prereg["thresholds"]["budget_per_document"]
    )
    rules = json.loads(BASELINES[split].read_text())["metrics"]
    measured = [r["metrics"][split] for r in runs if split in r["metrics"]]
    checks: dict[str, bool] = {}
    worst: dict[str, Any] = {}
    if len(measured) == prereg["protocol"]["runs"]:
        for name, gain_key in (
            ("field_accuracy", "field_accuracy_gain_over_rules"),
            ("evidence_completeness", "evidence_completeness_gain_over_rules"),
        ):
            worst[name] = min(m[name] for m in measured)
            checks[gain_key] = round(worst[name] - rules[name], 4) >= primary[gain_key]
        for name in ("abstention_recall", "conflict_recall"):
            worst[name] = min(m[name] for m in measured)
            checks[f"{name}_not_below_rules"] = worst[name] >= rules[name]
        for name in ("wrong_values", "silent_conflicts"):
            worst[name] = max(m[name] for m in measured)
            checks[f"{name}_not_above_rules"] = worst[name] <= rules[name]
        if "wrong_party_names" in primary:
            worst["wrong_party_names"] = max(m["wrong_party_names"] for m in measured)
            checks["wrong_party_names"] = worst["wrong_party_names"] <= primary["wrong_party_names"]
        primary_calls = [r["calls"][split] for r in runs]
        worst["p95_latency_seconds"] = max(c["p95_latency_seconds"] or 0.0 for c in primary_calls)
        worst["mean_cost_per_call_usd"] = max(
            c["mean_cost_per_call_usd"] or 0.0 for c in primary_calls
        )
        checks["budget_p95_latency"] = worst["p95_latency_seconds"] <= budget["p95_latency_seconds"]
        checks["budget_mean_cost"] = worst["mean_cost_per_call_usd"] <= budget["mean_cost_usd"]
        checks["hard_gates_every_run"] = all(not r["hard_failures"] for r in runs)
    return {
        "runs_measured": len(measured),
        "rules_1": {name: rules[name] for name in worst if name in rules},
        "worst": worst,
        "checks": checks,
        "enable_assistance": bool(checks) and all(checks.values()),
    }


def report_differences(committed: Any, recomputed: Any, where: str = "report") -> list[str]:
    """Where the recomputed report does not contain the committed one unchanged."""
    if isinstance(committed, dict):
        if not isinstance(recomputed, dict):
            return [where]
        return [
            difference
            for key, value in committed.items()
            for difference in (
                report_differences(value, recomputed[key], f"{where}.{key}")
                if key in recomputed
                else [f"{where}.{key} (missing)"]
            )
        ]
    if isinstance(committed, list):
        if not isinstance(recomputed, list) or len(committed) != len(recomputed):
            return [where]
        return [
            difference
            for index, (a, b) in enumerate(zip(committed, recomputed, strict=True))
            for difference in report_differences(a, b, f"{where}[{index}]")
        ]
    return [] if committed == recomputed else [where]


def print_run(label: str, summary: dict[str, Any], measurement: Measurement) -> None:
    metrics = summary["metrics"]
    print(f"eval-assisted: {label}, extractor {extractor_version(measurement.profile)}")
    splits = list(metrics)
    print(f"  {'metric':24} " + " ".join(f"{split:>10}" for split in splits))
    for name in next(iter(metrics.values())):
        row = [metrics[split][name] for split in splits]
        print(f"  {name:24} " + " ".join(f"{'n/a' if v is None else v!s:>10}" for v in row))
    for split, calls in summary["calls"].items():
        print(f"  calls {split}: {json.dumps(calls)}")
    for case, problems in summary["problems"].items():
        for problem in problems:
            print(f"  {case}: {problem}")
    for failure in summary["hard_failures"]:
        print(f"  GATE FAILED {failure}", file=sys.stderr)


def main(argv: list[str]) -> int:
    image = os.environ.get("PARSER_IMAGE", "")
    if not image:
        print("eval-assisted: set PARSER_IMAGE (make parser-image)", file=sys.stderr)
        return 2
    settings = SandboxSettings(image=image, docker=os.environ.get("PARSER_DOCKER", "docker"))
    names = _option_values(argv, "--profile")
    if "--live" in argv or "--replay-local" in argv:
        if len(names) != 1 or names[0] not in MEASUREMENTS:
            print(f"eval-assisted: --profile must be one of {', '.join(MEASUREMENTS)}")
            return 2
        measurement = MEASUREMENTS[names[0]]
        if "--live" in argv:
            return live(settings, argv, measurement)
        return replay_local(settings, argv, measurement)
    selected = [MEASUREMENTS[n] for n in names if n in MEASUREMENTS] or list(MEASUREMENTS.values())
    status = 0
    replayed = 0
    for measurement in selected:
        if not is_frozen(measurement):
            print(f"eval-assisted: {measurement.profile.name} is not frozen; nothing to replay")
            continue
        if not measurement.recordings.exists():
            # Frozen, measurement not yet run: nothing recorded. Any recorded run must be
            # complete (a missing response stops the replay).
            print(f"eval-assisted: {measurement.profile.name} is frozen; no recorded runs yet")
            continue
        replayed += 1
        status = max(status, replay(settings, argv, measurement))
    if not replayed:
        print("eval-assisted: no frozen configuration and no recorded runs yet", file=sys.stderr)
        return 2
    return status


def replay(settings: SandboxSettings, argv: list[str], measurement: Measurement) -> int:
    runs: list[dict[str, Any]] = []
    cases = set(corpus_splits(measurement.measured))
    for number in range(1, RUNS + 1):
        proposer = ReplayProposer(measurement.recordings / f"run-{number}")
        summary = run_summary(evaluate_run(settings, proposer, cases, measurement))
        summary["hard_failures"] = hard_failures(summary, measurement)
        print_run(f"run {number} of {RUNS} (replay)", summary, measurement)
        runs.append(summary)
    result = decision(runs, measurement)
    print(f"eval-assisted: {measurement.profile.name} decision {json.dumps(result)}")
    report = {
        "configuration": frozen_configuration(measurement),
        "runs": runs,
        "decision": result,
    }
    failed = [f"run {i}: {f}" for i, r in enumerate(runs, 1) for f in r["hard_failures"]]
    if "--write-report" in argv:
        if measurement.report.exists():
            print(f"eval-assisted: {_shown(measurement.report)} exists; not rewritten")
            return 1
        measurement.report.write_text(json.dumps(report, indent=1) + "\n")
        print(f"eval-assisted: wrote {_shown(measurement.report)}")
    elif measurement.report.exists():
        committed = json.loads(measurement.report.read_text())
        differences = report_differences(committed, report)
        failed += [f"differs from the committed report at {d}" for d in differences]
    for failure in failed:
        print(f"  FAILED {failure}", file=sys.stderr)
    print("eval-assisted: all hard gates passed" if not failed else f"{len(failed)} failed")
    return 1 if failed else 0


def replay_local(settings: SandboxSettings, argv: list[str], measurement: Measurement) -> int:
    number = _run_number(argv)
    directory = LOCAL_RECORDINGS / extractor_version(measurement.profile) / f"run-{number}"
    development = corpus_splits(measurement.development)
    selected = set(_option_values(argv, "--case")) or set(development)
    if not selected <= set(development):
        print("eval-assisted: --replay-local scores development cases only", file=sys.stderr)
        return 2
    summary = run_summary(evaluate_run(settings, ReplayProposer(directory), selected, measurement))
    summary["hard_failures"] = hard_failures(summary, measurement)
    print_run(f"local development run {number} (replay)", summary, measurement)
    return 1 if summary["hard_failures"] else 0


def live(settings: SandboxSettings, argv: list[str], measurement: Measurement) -> int:
    number = _run_number(argv)
    cap = _option_values(argv, "--spend-cap")
    if len(cap) != 1:
        print("eval-assisted: --live needs --spend-cap USD", file=sys.stderr)
        return 2
    if measurement.report.exists():
        print(
            f"eval-assisted: {measurement.profile.name} is measured and reported; "
            "it is never called again",
            file=sys.stderr,
        )
        return 2
    if is_frozen(measurement):
        allowed = corpus_splits(measurement.measured)
        directory = measurement.recordings / f"run-{number}"
    else:
        allowed = corpus_splits(measurement.development)
        directory = LOCAL_RECORDINGS / extractor_version(measurement.profile) / f"run-{number}"
    selected = set(_option_values(argv, "--case")) or set(allowed)
    if not selected <= set(allowed):
        print(
            f"eval-assisted: {measurement.profile.name} may call only "
            f"{', '.join(sorted(set(allowed.values())))} now "
            f"(held-out cases need the frozen configuration {_shown(measurement.frozen_config)})",
            file=sys.stderr,
        )
        return 2
    ledger = SpendLedger(LEDGER, float(cap[0]))
    before = ledger.spent_usd
    proposer = RecordingProposer(
        BudgetedProposer(AnthropicProposer(read_api_key(KEY_FILE)), ledger), directory
    )
    summary = run_summary(evaluate_run(settings, proposer, selected, measurement))
    summary["hard_failures"] = hard_failures(summary, measurement)
    print_run(f"live run {number} -> {_shown(directory)}", summary, measurement)
    print(
        f"eval-assisted: this run {ledger.spent_usd - before:.4f} USD; "
        f"total {ledger.spent_usd:.4f} of cap {ledger.cap_usd:.2f} USD ({len(ledger.calls)} calls)"
    )
    return 1 if summary["hard_failures"] else 0


def _shown(path: Path) -> str:
    """A path relative to the repository when it is inside it (messages only)."""
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def _run_number(argv: list[str]) -> int:
    values = _option_values(argv, "--run")
    if len(values) != 1 or values[0] not in {str(n) for n in range(1, RUNS + 1)}:
        raise SystemExit(f"eval-assisted: --run must be one of 1..{RUNS}")
    return int(values[0])


def _option_values(argv: list[str], name: str) -> list[str]:
    """Every value of `--name value` or `--name=value`."""
    values: list[str] = []
    for index, arg in enumerate(argv):
        if arg == name and index + 1 < len(argv):
            values.append(argv[index + 1])
        elif arg.startswith(f"{name}="):
            values.append(arg.split("=", 1)[1])
    return values


if __name__ == "__main__":
    sys.exit(main(sys.argv))
