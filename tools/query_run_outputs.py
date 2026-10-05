#!/usr/bin/env python3
"""Query FoodPort run status and published Nanopore iteration outputs."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
import webbrowser
from pathlib import Path
from datetime import datetime, timezone

if __package__:
    from ..nanopore_gui.api import FoodPortClient, FoodPortError
else:
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    from nanopore_gui.api import FoodPortClient, FoodPortError


from nanopore_gui.reports import build_iteration_report, generate_target_report, load_report
from nanopore_gui.version import __version__

TERMINAL_WORKFLOW_STATES = {"complete", "error"}


def parse_args(argv=None):
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Query published FoodPort Nanopore outputs."
    )
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument(
        "--api-base",
        default=os.getenv(
            "FOODPORT_API_BASE",
            "https://foodport-dev.cloud-nuage.inspection.gc.ca/en-ca/api",
        ),
    )
    parser.add_argument(
        "--token",
        default=os.getenv("FOODPORT_TOKEN"),
    )
    parser.add_argument("--pairing", action="store_true")
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Poll until the run reaches complete or error",
    )
    parser.add_argument(
        "--stop-on-processing-failure",
        action="store_true",
        help=(
            "Stop local polling when processing reports a failed generation; "
            "this does not stop remote cleanup"
        ),
    )
    parser.add_argument("--poll-seconds", type=float, default=15)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("run-reports"),
    )
    parser.add_argument(
        "--generate-reports", action="store_true",
        help="Download published CSVs and build local iteration tables, figures and previews",
    )
    parser.add_argument(
        "--final-report", action="store_true",
        help="Also generate an OLC Draft PDF/HTML from the latest ready iteration",
    )
    parser.add_argument(
        "--regenerate", action="store_true",
        help="Re-download CSVs and rebuild existing local previews and final report",
    )
    return parser.parse_args(argv)


def authenticate(client, args):
    """Authenticate using a supplied token or interactive browser pairing."""
    if args.token:
        client.set_token(args.token)
        return

    if not args.pairing:
        raise RuntimeError(
            "Provide FOODPORT_TOKEN/--token or use --pairing."
        )

    pairing = client.start_pairing()
    print(f"Approve this URL in your browser:\n{pairing['approval_url']}")
    webbrowser.open(pairing["approval_url"])
    code = getpass.getpass("One-time pairing code: ")
    client.exchange_pairing(pairing["pairing_id"], code)


def _redact_urls(value):
    """Recursively redact signed URLs before writing reports to disk."""
    if isinstance(value, dict):
        return {
            key: "<redacted>"
            if key.lower().endswith("url")
            else _redact_urls(item)
            for key, item in value.items()
        }

    if isinstance(value, list):
        return [_redact_urls(item) for item in value]

    return value


def _integer(value):
    """Return an integer where possible, otherwise None."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _published_iterations(processing):
    """Return sorted unique published iteration numbers."""
    published = set()

    for item in processing.get("reports", []):
        if not isinstance(item, dict):
            continue

        iteration = item.get("iteration")
        if iteration is None:
            iteration = item.get("generation")

        iteration = _integer(iteration)
        if iteration is not None:
            published.add(iteration)

    return sorted(published)


def _write_json(path, data):
    """Write one redacted JSON report."""
    path.write_text(
        json.dumps(_redact_urls(data), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _print_iteration_outputs(iteration, manifest):
    """Print a compact inventory from one downloaded iteration manifest."""
    outputs = manifest.get("outputs", []) if isinstance(manifest, dict) else []
    recognized = [item for item in outputs if isinstance(item, dict)]

    if not recognized:
        print(
            f"\nIteration {iteration} manifest published with no "
            "recognized outputs."
        )
        return

    print(f"\nIteration {iteration} outputs ({len(recognized)}):")
    for output in recognized:
        print(
            "  {0} ({1} bytes)".format(
                output.get("path", "<unnamed>"),
                output.get("size_bytes", "?"),
            )
        )


def _report_context(status):
    """Use non-approving, generic document fields for headless reports."""
    metadata = status.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "run_name": str(status.get("run_name") or "nanopore-run-{}".format(status.get("run_id", "unknown"))),
        "lab_name": "OLC",
        "report_state": "Draft",
        "reviewer_name": "",
        "rdims_document_id": "",
        "approval_timestamp": "",
        "digital_signature": "",
        "sample_metadata": metadata.get("sample_metadata") or status.get("sample_metadata") or {},
        "reference_database": metadata.get("reference_database") or {},
        "version": "PorePort {}".format(__version__),
        "analysis_generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _cached_iteration(directory, iteration):
    path = directory / "report.json"
    if not path.is_file():
        return None
    try:
        report = load_report(path)
        if (int(report["iterations"][0]["iteration"]) != iteration
                or not report.get("csv_files")
                or not all(Path(item).is_file() for item in report["csv_files"])
                or not all(Path(item).is_file() for item in report.get("artifacts", []))):
            return None
        return report
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        return None


def _download_new_iterations(client, args, processing, seen, status=None):
    """Fetch published results; optionally build reports from downloaded CSVs."""
    published = _published_iterations(processing)
    want_reports = getattr(args, "generate_reports", False) or getattr(args, "final_report", False)
    run_dir = args.output_dir / "run-{}".format(args.run_id)
    for iteration in published:
        if iteration in seen:
            continue
        destination = run_dir / "iteration-{:06d}".format(iteration)
        if want_reports and not getattr(args, "regenerate", False) and _cached_iteration(destination, iteration):
            seen.add(iteration)
            continue
        try:
            result = client.iteration_result(args.run_id, iteration)
        except FoodPortError as error:
            if error.status == 404 and (
                "not published yet" in error.detail.lower()
                or "results are not published" in error.detail.lower()
            ):
                print("Iteration {} results are pending publication.".format(iteration))
                continue
            raise
        manifest = result.get("result_manifest") or result.get("report") or result
        _write_json(args.output_dir / "iteration-{:06d}.json".format(iteration), manifest)
        if want_reports:
            destination.mkdir(parents=True, exist_ok=True)
            csv_files = client.download_iteration_csvs(result, destination)
            if not csv_files:
                raise RuntimeError("Iteration {} has no downloadable scheduler CSVs".format(iteration))
            report = build_iteration_report(
                iteration, csv_files, destination, context=_report_context(status or {}),
            )
            print("Iteration {} preview: {}".format(iteration, report["summary_image"]))
        seen.add(iteration)
        _print_iteration_outputs(iteration, manifest)
    return published


def _generate_latest_final(args, status, seen):
    """Generate the final draft only from the latest published, locally ready iteration."""
    if not getattr(args, "final_report", False):
        return
    published = _published_iterations((status.get("processing") or {}))
    if not published or published[-1] not in seen:
        print("Final report deferred: latest advertised iteration is not ready.")
        return
    iteration = published[-1]
    run_dir = args.output_dir / "run-{}".format(args.run_id)
    cached = _cached_iteration(run_dir / "iteration-{:06d}".format(iteration), iteration)
    if cached is None:
        print("Final report deferred: latest iteration preview is incomplete.")
        return
    marker_path = run_dir / "generated-target-report.json"
    if marker_path.is_file() and not getattr(args, "regenerate", False):
        try:
            marker = load_report(marker_path)
            if (marker.get("iteration") == iteration
                    and all(Path(marker[key]).is_file() for key in ("pdf", "html"))):
                return
        except (OSError, ValueError, KeyError, TypeError):
            pass
    if getattr(args, "_final_generated_iteration", None) == iteration:
        return
    marker = generate_target_report(
        iteration, [Path(item) for item in cached["csv_files"]],
        run_dir, context=_report_context(status),
    )
    args._final_generated_iteration = iteration
    print("OLC Draft report: {}".format(marker["pdf"]))


def _format_batch_counts(value):
    """Render batch counts in stable status order."""
    if not isinstance(value, dict) or not value:
        return "{}"

    status_order = (
        "pending",
        "submitting",
        "submitted",
        "running",
        "published",
        "complete",
        "failed",
    )
    parts = []
    visited = set()

    for key in status_order:
        if key in value:
            parts.append(f"{key}:{value[key]}")
            visited.add(key)

    for key in sorted(value):
        if key not in visited:
            parts.append(f"{key}:{value[key]}")

    return "{" + ", ".join(parts) + "}"


def _print_status(status, processing, published):
    """Print collection, processing, and publication state."""
    workflow = status.get("workflow_state")
    processing_status = processing.get("status")
    waiting = processing.get("waiting_file_count")
    active = processing.get("active_generation")
    failed = processing.get("failed_generation")
    processed = processing.get("processed_pod5_count")
    total = processing.get("pod5_count")
    counts = _format_batch_counts(processing.get("batch_counts"))
    message = processing.get("message") or processing.get("error")

    print(
        "Run {run_id}: workflow={workflow} processing={processing_status} "
        "waiting={waiting} processed={processed}/{total} "
        "active_iteration={active} failed_iteration={failed} "
        "batches={counts} published={published}".format(
            run_id=status.get("run_id"),
            workflow=workflow,
            processing_status=processing_status,
            waiting=waiting if waiting is not None else "?",
            processed=processed if processed is not None else "?",
            total=total if total is not None else "?",
            active=active if active is not None else "-",
            failed=failed if failed is not None else "-",
            counts=counts,
            published=published,
        )
    )

    if message and processing_status == "failed":
        print(f"  Processing failure: {message}")


def _query_once(client, args, seen):
    """Query status once, save it, and download new manifests."""
    status = client.status(args.run_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    _write_json(args.output_dir / "status.json", status)

    processing = status.get("processing") or {}
    if not isinstance(processing, dict):
        processing = {}

    published = _download_new_iterations(
        client,
        args,
        processing,
        seen,
        status,
    )
    _generate_latest_final(args, status, seen)
    _print_status(status, processing, published)
    return status, processing


def main(argv=None):
    """Run a one-time status query or poll until a terminal state."""
    args = parse_args(argv)

    if args.poll_seconds <= 0:
        raise ValueError("--poll-seconds must be positive")

    client = FoodPortClient(
        args.api_base,
        verify=(
            os.getenv("FOODPORT_VERIFY_SSL", "false").lower()
            == "true"
        ),
    )

    try:
        authenticate(client, args)
        seen = set()

        while True:
            status, processing = _query_once(client, args, seen)
            workflow = status.get("workflow_state")
            processing_status = processing.get("status")

            if not args.watch:
                break

            if workflow in TERMINAL_WORKFLOW_STATES:
                print(f"Run reached terminal workflow state: {workflow}")
                break

            if (
                args.stop_on_processing_failure
                and processing_status == "failed"
            ):
                print(
                    "Stopping local polling because processing failed. "
                    "FoodPort cleanup may still be running remotely."
                )
                break

            time.sleep(args.poll_seconds)
    finally:
        client.close()


if __name__ == "__main__":
    main()
