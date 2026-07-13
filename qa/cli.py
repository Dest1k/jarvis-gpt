"""Command-line entry point for the permanent developer-only QA harness."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .evidence import EvidenceStore, validate_evidence_file
from .models import (
    EXIT_FAIL,
    EXIT_HARNESS_ERROR,
    EXIT_INCOMPLETE,
    EXIT_PASS,
    CampaignIdentity,
    Verdict,
)
from .output import safe_json_text
from .redaction import redact_text
from .replay import replay_file, write_replay_report
from .review.adjudicator import adjudicate_files, write_adjudication
from .review.reviewer import build_review_packets
from .runner import AssuranceRunner, LoopbackHttpExecutor
from .scenario_loader import load_suite, validate_suite


def _emit(document: dict[str, Any], *, canaries: Iterable[str] = ()) -> None:
    sys.stdout.write(
        safe_json_text(document, canaries=canaries, append_newline=True)
    )


def _cmd_validate_suite(args: argparse.Namespace) -> int:
    scenarios, errors = validate_suite(args.suite)
    _emit(
        {
            "command": "validate-suite",
            "ok": not errors,
            "scenarios": len(scenarios),
            "errors": errors,
        }
    )
    return EXIT_PASS if not errors else EXIT_FAIL


def _cmd_validate_evidence(args: argparse.Namespace) -> int:
    records, errors = validate_evidence_file(
        args.evidence,
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    _emit(
        {
            "command": "validate-evidence",
            "ok": not errors,
            "records": len(records),
            "errors": errors,
        }
    )
    return EXIT_PASS if not errors else EXIT_FAIL


def _cmd_replay(args: argparse.Namespace) -> int:
    summary = replay_file(
        args.evidence,
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    output = None
    if args.output is not None:
        output = str(write_replay_report(args.output, summary))
    _emit(
        {
            "command": "replay",
            "ok": summary.exit_code == EXIT_PASS,
            "cases": len(summary.cases),
            "counts": summary.counts,
            "mismatches": list(summary.mismatches),
            "errors": list(summary.errors),
            "evidence_sha256": summary.evidence_sha256,
            "manifest_sha256": summary.manifest_sha256,
            "replay_digest": summary.replay_digest,
            "output": output,
        }
    )
    return summary.exit_code


def _cmd_build_review_packets(args: argparse.Namespace) -> int:
    output = args.output_dir or args.evidence.with_suffix(".review-packets")
    paths = build_review_packets(
        args.evidence,
        output,
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    _emit(
        {
            "command": "build-review-packets",
            "ok": True,
            "packets": len(paths),
            "output_dir": str(output),
        }
    )
    return EXIT_PASS


def _cmd_adjudicate(args: argparse.Namespace) -> int:
    result = adjudicate_files(
        args.review_1,
        args.review_2,
        replay_path=args.replay,
        evidence_path=args.evidence,
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    default_name = f"{result.reviews[0].packet.case_id}.adjudication.json"
    output = args.output or args.review_1.parent / default_name
    write_adjudication(output, result)
    _emit(
        {
            "command": "adjudicate",
            "ok": True,
            "verdict": result.verdict.value,
            "output": str(output),
        }
    )
    if result.verdict is Verdict.FAIL:
        return EXIT_FAIL
    if result.verdict is Verdict.INCONCLUSIVE:
        return EXIT_INCOMPLETE
    return EXIT_PASS


def _cmd_run_suite(args: argparse.Namespace) -> int:
    scenarios = load_suite(args.suite)
    identity = CampaignIdentity.create(args.campaign_prefix)
    store = EvidenceStore(args.output_root, identity)
    http = LoopbackHttpExecutor(args.base_url) if args.base_url else None
    runner = AssuranceRunner(identity, store, http_executor=http)
    try:
        summary = runner.run_suite(scenarios)
    finally:
        runner.close()
    _emit(
        {
            "command": "run-suite",
            "campaign_id": identity.campaign_id,
            "namespace": identity.namespace,
            "counts": summary.counts,
            "exit_code": summary.exit_code,
            "evidence": str(store.path),
            "evidence_sha256": store.anchor.evidence_sha256 if store.anchor else None,
            "manifest_sha256": store.anchor.manifest_sha256 if store.anchor else None,
        }
    )
    return summary.exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_suite_parser = subparsers.add_parser("validate-suite")
    validate_suite_parser.add_argument("suite", type=Path)
    validate_suite_parser.set_defaults(handler=_cmd_validate_suite)

    validate_evidence_parser = subparsers.add_parser("validate-evidence")
    validate_evidence_parser.add_argument("evidence", type=Path)
    validate_evidence_parser.add_argument("--expected-manifest-sha256")
    validate_evidence_parser.set_defaults(handler=_cmd_validate_evidence)

    replay_parser = subparsers.add_parser("replay")
    replay_parser.add_argument("evidence", type=Path)
    replay_parser.add_argument("--expected-manifest-sha256")
    replay_parser.add_argument("--output", type=Path)
    replay_parser.set_defaults(handler=_cmd_replay)

    packets_parser = subparsers.add_parser("build-review-packets")
    packets_parser.add_argument("evidence", type=Path)
    packets_parser.add_argument("--output-dir", type=Path)
    packets_parser.add_argument("--expected-manifest-sha256")
    packets_parser.set_defaults(handler=_cmd_build_review_packets)

    adjudicate_parser = subparsers.add_parser("adjudicate")
    adjudicate_parser.add_argument("review_1", type=Path)
    adjudicate_parser.add_argument("review_2", type=Path)
    adjudicate_parser.add_argument("--replay", type=Path, required=True)
    adjudicate_parser.add_argument("--evidence", type=Path, required=True)
    adjudicate_parser.add_argument("--expected-manifest-sha256")
    adjudicate_parser.add_argument("--output", type=Path)
    adjudicate_parser.set_defaults(handler=_cmd_adjudicate)

    run_parser = subparsers.add_parser("run-suite")
    run_parser.add_argument("suite", type=Path)
    run_parser.add_argument("--output-root", type=Path, required=True)
    run_parser.add_argument("--base-url")
    run_parser.add_argument("--campaign-prefix", default="jarvis-assurance")
    run_parser.set_defaults(handler=_cmd_run_suite)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except Exception as exc:  # CLI boundary, with redacted bounded diagnostics
        message = redact_text(f"{type(exc).__name__}: {exc}").value[:2000]
        _emit({"command": args.command, "ok": False, "harness_error": message})
        return EXIT_HARNESS_ERROR


if __name__ == "__main__":
    sys.exit(main())
