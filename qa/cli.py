"""Command-line entry point for the permanent developer-only QA harness."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .evidence import (
    EvidenceStore,
    compare_audit_content_manifests,
    validate_evidence_file,
    write_audit_content_manifest,
)
from .models import (
    EXIT_FAIL,
    EXIT_HARNESS_ERROR,
    EXIT_INCOMPLETE,
    EXIT_PASS,
    CampaignIdentity,
    Verdict,
)
from .output import safe_json_text
from .overlay import verify_overlay_source_pins, verify_reviewed_input_head
from .redaction import redact_text
from .replay import replay_file, write_replay_report
from .review.adjudicator import adjudicate_files, write_adjudication
from .review.reviewer import build_review_packets
from .runner import AssuranceRunner, LoopbackHttpExecutor
from .scenario_loader import load_suite, validate_suite


def _emit(document: dict[str, Any], *, canaries: Iterable[str] = ()) -> None:
    sys.stdout.write(safe_json_text(document, canaries=canaries, append_newline=True))


def _sha256_anchor(value: str) -> str:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("anchor must be a lowercase SHA-256 digest")
    return value


def _commit_sha(value: str) -> str:
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise argparse.ArgumentTypeError("commit must be a full lowercase SHA")
    return value


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
        expected_context_digests=(args.context_anchor_1, args.context_anchor_2),
        expected_review_digests=(args.review_anchor_1, args.review_anchor_2),
        expected_manifest_sha256=args.expected_manifest_sha256,
    )
    default_name = f"{result.reviews[0].packet.case_id}.adjudication.json"
    output = args.output or args.review_1.parent / default_name
    write_adjudication(
        output,
        result,
        expected_context_digests=(args.context_anchor_1, args.context_anchor_2),
        expected_review_digests=(args.review_anchor_1, args.review_anchor_2),
    )
    _emit(
        {
            "command": "adjudicate",
            "ok": True,
            "verdict": result.verdict.value,
            "independence_verified": result.independence_verified,
            "review_anchors_verified": result.review_anchors_verified,
            "independence_level": (
                result.independence_level.value if result.independence_level else None
            ),
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


def _cmd_validate_overlay_sources(args: argparse.Namespace) -> int:
    result = verify_overlay_source_pins(
        args.repository_root,
        args.overlay,
        args.expected_source_commit,
        args.git_executable,
    )
    _emit(
        {
            "command": "validate-overlay-sources",
            "ok": result.ok,
            "source_pins": len(result.pins),
            "source_pins_matched": result.matched,
            "task_mappings": result.task_mappings,
            "task_files_matched": result.task_files_matched,
            "errors": list(result.errors),
        }
    )
    return EXIT_PASS if result.ok else EXIT_FAIL


def _cmd_verify_reviewed_input(args: argparse.Namespace) -> int:
    result = verify_reviewed_input_head(
        args.repository_root,
        args.reviewed_input_commit,
        args.git_executable,
    )
    _emit(
        {
            "command": "verify-reviewed-input",
            "ok": result.ok,
            "expected": result.expected,
            "actual": result.actual,
            "errors": list(result.errors),
        }
    )
    return EXIT_PASS if result.ok else EXIT_FAIL


def _cmd_audit_manifest_create(args: argparse.Namespace) -> int:
    artifact = write_audit_content_manifest(
        args.repository_root,
        args.backup_root,
        args.output_name,
        args.git_executable,
    )
    _emit(
        {
            "command": "audit-manifest-create",
            "ok": True,
            "manifest": artifact.path.name,
            "manifest_sha256": artifact.sha256,
            "entries": artifact.entry_count,
        }
    )
    return EXIT_PASS


def _cmd_audit_manifest_compare(args: argparse.Namespace) -> int:
    comparison = compare_audit_content_manifests(
        args.repository_root,
        args.backup_root,
        args.before_name,
        args.after_name,
        args.git_executable,
        expected_before_sha256=args.expected_before_sha256,
        expected_after_sha256=args.expected_after_sha256,
        result_name=args.result_name,
    )
    _emit(
        {
            "command": "audit-manifest-compare",
            "ok": comparison.ok,
            "before_sha256": comparison.before_sha256,
            "after_sha256": comparison.after_sha256,
            "difference_count": len(comparison.differences),
            "difference_codes": sorted(
                {str(item["code"]) for item in comparison.differences}
            ),
            "result": comparison.result.path.name,
            "result_sha256": comparison.result.sha256,
        }
    )
    return EXIT_PASS if comparison.ok else EXIT_FAIL


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
    adjudicate_parser.add_argument("--context-anchor-1", required=True, type=_sha256_anchor)
    adjudicate_parser.add_argument("--context-anchor-2", required=True, type=_sha256_anchor)
    adjudicate_parser.add_argument("--review-anchor-1", required=True, type=_sha256_anchor)
    adjudicate_parser.add_argument("--review-anchor-2", required=True, type=_sha256_anchor)
    adjudicate_parser.add_argument("--expected-manifest-sha256")
    adjudicate_parser.add_argument("--output", type=Path)
    adjudicate_parser.set_defaults(handler=_cmd_adjudicate)

    run_parser = subparsers.add_parser("run-suite")
    run_parser.add_argument("suite", type=Path)
    run_parser.add_argument("--output-root", type=Path, required=True)
    run_parser.add_argument("--base-url")
    run_parser.add_argument("--campaign-prefix", default="jarvis-assurance")
    run_parser.set_defaults(handler=_cmd_run_suite)

    overlay_parser = subparsers.add_parser("validate-overlay-sources")
    overlay_parser.add_argument("--repository-root", type=Path, required=True)
    overlay_parser.add_argument("--overlay", required=True)
    overlay_parser.add_argument(
        "--expected-source-commit",
        required=True,
        type=_commit_sha,
    )
    overlay_parser.add_argument("--git-executable", type=Path, required=True)
    overlay_parser.set_defaults(handler=_cmd_validate_overlay_sources)

    reviewed_input_parser = subparsers.add_parser("verify-reviewed-input")
    reviewed_input_parser.add_argument("--repository-root", type=Path, required=True)
    reviewed_input_parser.add_argument(
        "--reviewed-input-commit",
        required=True,
        type=_commit_sha,
    )
    reviewed_input_parser.add_argument("--git-executable", type=Path, required=True)
    reviewed_input_parser.set_defaults(handler=_cmd_verify_reviewed_input)

    audit_create_parser = subparsers.add_parser("audit-manifest-create")
    audit_create_parser.add_argument("--repository-root", type=Path, required=True)
    audit_create_parser.add_argument("--backup-root", type=Path, required=True)
    audit_create_parser.add_argument("--output-name", required=True)
    audit_create_parser.add_argument("--git-executable", type=Path, required=True)
    audit_create_parser.set_defaults(handler=_cmd_audit_manifest_create)

    audit_compare_parser = subparsers.add_parser("audit-manifest-compare")
    audit_compare_parser.add_argument("--repository-root", type=Path, required=True)
    audit_compare_parser.add_argument("--backup-root", type=Path, required=True)
    audit_compare_parser.add_argument("--before-name", required=True)
    audit_compare_parser.add_argument("--after-name", required=True)
    audit_compare_parser.add_argument(
        "--expected-before-sha256",
        required=True,
        type=_sha256_anchor,
    )
    audit_compare_parser.add_argument(
        "--expected-after-sha256",
        required=True,
        type=_sha256_anchor,
    )
    audit_compare_parser.add_argument("--result-name", required=True)
    audit_compare_parser.add_argument("--git-executable", type=Path, required=True)
    audit_compare_parser.set_defaults(handler=_cmd_audit_manifest_compare)
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
