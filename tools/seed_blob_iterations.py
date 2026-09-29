#!/usr/bin/env python3
"""Seed FoodPort Nanopore uploads from existing Azure POD5 blobs.

Files are released in configurable waves. FoodPort, rather than this client,
creates immutable processing iterations after its server-side collection
window becomes quiet. A release wave therefore describes upload timing, not a
client-controlled processing batch.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

if __package__:
    from ..nanopore_gui.api import FoodPortClient
else:
    sys.path.insert(
        0,
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    )
    from nanopore_gui.api import FoodPortClient


DEFAULT_FILES = tuple(
    "FBF69780_2a40bb31_a027dece_{0}.pod5".format(index)
    for index in range(23)
)
DEFAULT_BARCODE_VALUES = (3, 4)
DEFAULT_RELEASE_SIZES = (3, 7, 2, 5, 6)
DEFAULT_COLLECTION_WINDOW_SECONDS = 60.0
DEFAULT_WAIT_SECONDS = 150.0

DEFAULT_SAMPLE_METADATA = {
    3: {"seqid": "2026-MIN-0105", "olnid": "BDS-VTEC099"},
    4: {"seqid": "2026-MIN-0106", "olnid": "BDS-VTEC100"},
}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description=(
            "Copy existing Azure POD5 blobs into FoodPort in configurable "
            "upload release waves."
        )
    )
    parser.add_argument(
        "--run-name",
        required=True,
        help="FoodPort run name, for example 260924-nanopore",
    )
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
        help="FoodPort token; otherwise use --pairing",
    )
    parser.add_argument(
        "--pairing",
        action="store_true",
        help="Open browser pairing approval and prompt for the one-time code",
    )
    parser.add_argument("--source-container", default="poresippr-dataset2")
    parser.add_argument("--source-prefix", default="20260421_MIN")
    parser.add_argument("--target-prefix", default="pass")
    parser.add_argument(
        "--file",
        dest="files",
        action="append",
        help="Source POD5 filename; repeat to replace the 23 default files",
    )
    parser.add_argument(
        "--barcode",
        type=int,
        action="append",
        dest="barcodes",
        help=(
            "Restrict the run to this barcode; repeat for multiple barcodes "
            "(default: 3 and 4)"
        ),
    )
    parser.add_argument(
        "--seqid",
        help="Override the configured SEQID for one --barcode",
    )
    parser.add_argument(
        "--olnid",
        help="Override the configured OLNID for one --barcode",
    )
    parser.add_argument(
        "--release-size",
        type=int,
        action="append",
        dest="release_sizes",
        help=(
            "Number of files completed in one upload release wave; repeat "
            "to replace the default 3,7,2,5,6 plan"
        ),
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        action="append",
        dest="legacy_batch_sizes",
        help=(
            "Deprecated alias for --release-size; supplied values replace "
            "the complete default release plan"
        ),
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        action="append",
        dest="wait_seconds",
        help=(
            "Delay between completed upload release waves. Supply once to "
            "use the same delay for every gap, or repeat once for each gap "
            "between release waves. Default: 150 seconds for every gap."
        ),
    )
    parser.add_argument(
        "--expected-collection-window-seconds",
        type=float,
        default=DEFAULT_COLLECTION_WINDOW_SECONDS,
        help=(
            "Expected FoodPort quiet collection window, used only to warn "
            "about release waves that may be merged"
        ),
    )
    parser.add_argument("--copy-timeout-seconds", type=float, default=900)
    parser.add_argument("--copy-poll-seconds", type=float, default=5)
    parser.add_argument(
        "--no-finalize",
        action="store_true",
        help="Leave the run open for additional uploads",
    )
    return parser.parse_args(argv)


def storage_client():
    """Create an Azure Blob client from the configured connection string."""
    try:
        from azure.storage.blob import BlobServiceClient
    except ImportError as error:
        raise RuntimeError(
            "Install the test-tool dependency with: "
            "python -m pip install azure-storage-blob "
            "(the legacy conda package azure_storage is different)"
        ) from error

    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not connection_string:
        raise RuntimeError(
            "Set AZURE_STORAGE_CONNECTION_STRING for the server-side copy."
        )

    values = {
        part.split("=", 1)[0]: part.split("=", 1)[1]
        for part in connection_string.split(";")
        if "=" in part
    }
    account = values.get("AccountName")
    key = values.get("AccountKey")
    if not account or not key:
        raise RuntimeError(
            "The connection string must contain AccountName and AccountKey."
        )

    service = BlobServiceClient.from_connection_string(connection_string)
    return service, account, key


def source_url(account: str, key: str, container: str, name: str) -> str:
    """Create a short-lived read URL for one source blob."""
    from azure.storage.blob import BlobSasPermissions, generate_blob_sas

    expiry = datetime.now(timezone.utc) + timedelta(hours=2)
    sas = generate_blob_sas(
        account_name=account,
        container_name=container,
        blob_name=name,
        account_key=key,
        permission=BlobSasPermissions(read=True),
        expiry=expiry,
    )
    return (
        f"https://{account}.blob.core.windows.net/"
        f"{container}/{name}?{sas}"
    )


def authenticate(client: FoodPortClient, args: argparse.Namespace) -> None:
    """Authenticate using a supplied token or interactive browser pairing."""
    if args.token:
        client.set_token(args.token)
        return
    if not args.pairing:
        raise RuntimeError(
            "Provide --token/FOODPORT_TOKEN or use --pairing for manual "
            "browser login."
        )

    pairing = client.start_pairing()
    print(f"Approve this URL in your browser:\n{pairing['approval_url']}")
    webbrowser.open(pairing["approval_url"])
    code = getpass.getpass("One-time pairing code: ")
    client.exchange_pairing(pairing["pairing_id"], code)


def normalize_wait_plan(
    configured_waits: list[float] | None,
    release_count: int,
) -> tuple[float, ...]:
    """Return one delay for every gap between release waves."""
    gap_count = max(release_count - 1, 0)
    waits = tuple(configured_waits or (DEFAULT_WAIT_SECONDS,))

    if any(wait < 0 for wait in waits):
        raise ValueError("--wait-seconds values must be non-negative")
    if gap_count == 0:
        return ()
    if len(waits) == 1:
        return waits * gap_count
    if len(waits) == gap_count:
        return waits

    raise ValueError(
        "Configure either one --wait-seconds value or exactly {0} values "
        "for the {1} gaps between {2} release waves; received {3}.".format(
            gap_count,
            gap_count,
            release_count,
            len(waits),
        )
    )


def validate_arguments(args: argparse.Namespace):
    """Validate and normalize seeder inputs before creating a run."""
    files = tuple(args.files or DEFAULT_FILES)
    barcode_values = tuple(args.barcodes or DEFAULT_BARCODE_VALUES)

    if args.release_sizes and args.legacy_batch_sizes:
        raise ValueError("Use --release-size or --batch-size, not both.")

    release_sizes = tuple(
        args.release_sizes
        or args.legacy_batch_sizes
        or DEFAULT_RELEASE_SIZES
    )
    wait_seconds = normalize_wait_plan(
        args.wait_seconds,
        len(release_sizes),
    )

    if not files:
        raise ValueError("At least one POD5 file is required")
    if len(files) != len(set(files)):
        raise ValueError("POD5 filenames must be unique")

    for filename in files:
        if (
            not filename
            or PurePosixPath(filename).name != filename
            or not filename.lower().endswith(".pod5")
        ):
            raise ValueError(
                "Every --file value must be a basename ending in .pod5: "
                f"{filename!r}"
            )

    if any(barcode < 1 for barcode in barcode_values):
        raise ValueError("--barcode values must be positive")
    if len(barcode_values) != len(set(barcode_values)):
        raise ValueError("--barcode values must be unique")

    metadata_override = args.seqid is not None or args.olnid is not None
    if metadata_override and len(barcode_values) != 1:
        raise ValueError("--seqid and --olnid require exactly one --barcode")
    if (args.seqid is None) != (args.olnid is None):
        raise ValueError("--seqid and --olnid must be supplied together")

    if not release_sizes or any(size < 1 for size in release_sizes):
        raise ValueError("Release sizes must be positive integers")
    if sum(release_sizes) != len(files):
        raise ValueError(
            "Release sizes total {0}, but {1} files are configured".format(
                sum(release_sizes),
                len(files),
            )
        )
    if args.expected_collection_window_seconds < 0:
        raise ValueError(
            "--expected-collection-window-seconds must be non-negative"
        )
    if args.copy_timeout_seconds <= 0:
        raise ValueError("--copy-timeout-seconds must be positive")
    if args.copy_poll_seconds <= 0:
        raise ValueError("--copy-poll-seconds must be positive")

    return files, barcode_values, release_sizes, wait_seconds


def build_samples(
    barcode_values: tuple[int, ...],
    args: argparse.Namespace,
) -> list[dict[str, Any]]:
    """Build FoodPort sample metadata for the configured barcodes."""
    samples = []
    for barcode in barcode_values:
        sample = DEFAULT_SAMPLE_METADATA.get(barcode)
        if sample is None and (args.seqid is None or args.olnid is None):
            raise ValueError(
                f"No default sample metadata exists for barcode {barcode}; "
                "provide both --seqid and --olnid."
            )

        if len(barcode_values) == 1 and args.seqid:
            seqid = args.seqid
            olnid = args.olnid
        else:
            seqid = sample["seqid"]
            olnid = sample["olnid"]

        samples.append(
            {"barcode": barcode, "seqid": seqid, "olnid": olnid}
        )
    return samples


def copy_file(
    client: FoodPortClient,
    service,
    account: str,
    key: str,
    args: argparse.Namespace,
    run_id: int,
    filename: str,
    wave_number: int,
) -> dict[str, Any]:
    """Copy one source POD5 into the FoodPort raw-input location."""
    from azure.core.exceptions import ResourceNotFoundError
    from azure.storage.blob import BlobClient

    source_name = str(PurePosixPath(args.source_prefix) / filename)
    source = service.get_blob_client(args.source_container, source_name)
    source_properties = source.get_blob_properties()
    relative_path = str(
        PurePosixPath(args.target_prefix)
        / f"release-wave-{wave_number:06d}"
        / filename
    )
    prepared = client.prepare_file(
        run_id,
        relative_path,
        source_properties.size,
    )
    destination_url = urlsplit(prepared["upload_url"])
    destination_host = (destination_url.hostname or "").lower()
    source_host = f"{account}.blob.core.windows.net".lower()

    if destination_host != source_host:
        raise RuntimeError(
            "FoodPort prepared a destination in a different storage account "
            f"({destination_host or 'unknown'} vs {source_host}). The test "
            "seeder cannot poll that asynchronous copy using the source "
            "connection string."
        )

    path_parts = [
        unquote(part)
        for part in destination_url.path.split("/")
        if part
    ]
    if len(path_parts) < 2:
        raise RuntimeError("FoodPort returned an invalid destination upload URL")

    destination_container = path_parts[0]
    destination_name = "/".join(path_parts[1:])
    destination = service.get_blob_client(
        destination_container,
        destination_name,
    )
    destination_upload = BlobClient.from_blob_url(prepared["upload_url"])
    destination_upload.start_copy_from_url(
        source_url(
            account,
            key,
            args.source_container,
            source_name,
        )
    )

    deadline = time.monotonic() + args.copy_timeout_seconds
    while True:
        try:
            properties = destination.get_blob_properties()
        except ResourceNotFoundError:
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "Azure created the copy request, but the destination "
                    "blob did not become visible: "
                    f"{destination_container}/{destination_name}"
                ) from None
            time.sleep(args.copy_poll_seconds)
            continue

        copy_status = properties.copy.status if properties.copy else "success"
        if copy_status == "success":
            break
        if copy_status in {"failed", "aborted"}:
            detail = (
                properties.copy.status_description
                if properties.copy
                else "unknown copy error"
            )
            raise RuntimeError(f"Azure Blob copy failed: {detail}")
        if time.monotonic() >= deadline:
            raise TimeoutError(
                "Azure Blob copy did not finish within "
                f"{args.copy_timeout_seconds} seconds"
            )
        time.sleep(args.copy_poll_seconds)

    print(f"Copied wave {wave_number}: {filename} -> {relative_path}")
    return prepared


def partition_files(
    files: tuple[str, ...],
    release_sizes: tuple[int, ...],
):
    """Yield numbered release waves in configured file order."""
    offset = 0
    for wave_number, release_size in enumerate(release_sizes, start=1):
        wave_files = files[offset: offset + release_size]
        offset += release_size
        yield wave_number, wave_files


def print_configuration(
    args: argparse.Namespace,
    files: tuple[str, ...],
    barcode_values: tuple[int, ...],
    release_sizes: tuple[int, ...],
    wait_seconds: tuple[float, ...],
) -> None:
    """Print the effective run configuration without revealing secrets."""
    print("FoodPort Nanopore upload seeder configuration:")
    print(f"  API base:             {args.api_base}")
    print(f"  Run name:             {args.run_name}")
    print(f"  Source container:     {args.source_container}")
    print(f"  Source prefix:        {args.source_prefix}")
    print(f"  POD5 files:           {len(files)}")
    print(
        "  Release-wave plan:   "
        + ",".join(str(size) for size in release_sizes)
    )
    print(
        "  Barcode values:      "
        + ",".join(str(value) for value in barcode_values)
    )
    print(
        "  Wave-delay plan:     "
        + (
            ",".join(str(value) for value in wait_seconds) + " seconds"
            if wait_seconds
            else "none"
        )
    )
    print(
        "  Expected quiet time: "
        f"{args.expected_collection_window_seconds} seconds"
    )
    print(f"  Finalize:             {not args.no_finalize}")

    for gap_number, delay in enumerate(wait_seconds, start=1):
        if delay <= args.expected_collection_window_seconds:
            print(
                "WARNING: The delay between release waves {0} and {1} is "
                "{2} seconds, which is not longer than the expected "
                "{3}-second FoodPort collection window. These waves may be "
                "combined into one processing iteration.".format(
                    gap_number,
                    gap_number + 1,
                    delay,
                    args.expected_collection_window_seconds,
                ),
                file=sys.stderr,
            )


def main(argv: list[str] | None = None) -> None:
    """Create and populate a FoodPort Nanopore run."""
    args = parse_args(argv)
    (
        files,
        barcode_values,
        release_sizes,
        wait_seconds,
    ) = validate_arguments(args)
    samples = build_samples(barcode_values, args)
    print_configuration(
        args,
        files,
        barcode_values,
        release_sizes,
        wait_seconds,
    )

    service, account, key = storage_client()
    client = FoodPortClient(
        args.api_base,
        verify=(
            os.getenv("FOODPORT_VERIFY_SSL", "false").lower()
            == "true"
        ),
    )

    try:
        authenticate(client, args)
        run = client.create_run(
            args.run_name,
            {
                "barcode_kit": "SQK-RBK114-24",
                "barcode_values": list(barcode_values),
                "sample_metadata": {"samples": samples},
            },
        )
        run_id = int(run["run_id"])
        print(f"Created run {run_id}: {args.run_name}")

        release_plan = tuple(partition_files(files, release_sizes))
        for wave_index, (wave_number, wave_files) in enumerate(
            release_plan,
            start=1,
        ):
            prepared_wave = []
            print(
                f"Preparing release wave {wave_number}/"
                f"{len(release_plan)} with {len(wave_files)} POD5 files"
            )

            for filename in wave_files:
                prepared = copy_file(
                    client,
                    service,
                    account,
                    key,
                    args,
                    run_id,
                    filename,
                    wave_number,
                )
                prepared_wave.append((filename, prepared))

            print(
                f"Completing release wave {wave_number} with "
                f"{len(prepared_wave)} copied files"
            )
            for filename, prepared in prepared_wave:
                completed = client.complete_file(
                    run_id,
                    int(prepared["file_id"]),
                )
                print(
                    f"Completed upload: {filename}; "
                    f"file_id={prepared['file_id']}; "
                    f"batch_id={completed.get('batch_id')}"
                )

            if wave_index < len(release_plan):
                delay = wait_seconds[wave_index - 1]
                print(
                    f"Waiting {delay} seconds before release wave "
                    f"{wave_index + 1}"
                )
                if delay > 0:
                    time.sleep(delay)

        if not args.no_finalize:
            result = client.finalize(run_id)
            print(
                f"Finalized run {run_id}; "
                f"workflow={result.get('workflow_state', 'unknown')}"
            )
        else:
            print(f"Run {run_id} remains open for additional uploads")

        print(f"Monitor run {run_id} with tools/query_run_outputs.py.")
    finally:
        client.close()


if __name__ == "__main__":
    main()
