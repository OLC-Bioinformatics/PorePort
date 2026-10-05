# PorePort desktop user guide

## Purpose and prerequisites

PorePort watches a local POD5 directory, registers stable files with FoodPort, uploads them directly to Azure Blob Storage using short-lived SAS URLs, and shows iteration results while sequencing continues. The desktop does not stop a sequencer. Use an approved FoodPort account, a supported GUI Python environment (the supplied desktop code uses Python 3.12), access to the portal and Blob endpoint, and a directory with completed POD5 files. The GUI's local queue and reports are kept under the operating system's application-data directory, commonly `%APPDATA%/NanoporeCloudGUI` on Windows. Never copy API tokens or SAS query strings into tickets.

Run from the GUI repository with `python -m nanopore_gui.main`. Use `python -m nanopore_gui.main --help` for the authoritative options. `--view-run 1835` opens an existing locally recorded run for viewing without starting intake; `--test-run` runs the test workflow, and `--test-no-finalize` keeps a test run open. Do not combine view-run and test-run. For Linux, install Qt system libraries and run from a desktop session; test packaging on the target Linux distribution.

## Authenticate and create

1. Start the GUI and use browser pairing. Approve the displayed pairing request in the signed-in FoodPort portal, then exchange its short-lived code for a desktop token. Do not share the code or token.
2. Select the POD5 directory, provide a new run name, select barcode kit and sample metadata, and verify the sample-to-barcode mapping. Review the chosen reference and Dorado model. The GUI reference asset and VM reference are separate: a desktop `PoreSippRDB_240509.fasta` asset must not be assumed to equal the VM's `PoreSippR_DB_251110.fasta`.
3. Create the run once. A name already present in the database or any of the three run-related Blob prefixes is rejected. Do not delete a database row and reuse its name to retry; old manifests and results can belong to a different run ID.

## Upload and monitor

The scanner waits for local file stability before releasing a POD5 file. The uploader prepares a file record, uploads through its SAS URL, then calls complete. Check Pending, Failed and Uploaded counts; retry failures using the GUI. Backend processing starts after uploaded files are collected into a generation, normally following the configured quiet collection window. A run can produce multiple numbered iterations before finalization. The Live results section can show Summary, Coverage, Targets and Trends for available iterations; the latest iteration is not necessarily the only one with a published result. A report may be temporarily unavailable while publication or reconciliation is pending.

The summary-table image, charts, downloaded CSVs, and generated PDF are **desktop-side artifacts**. A published scheduler CSV does not mean the desktop has generated a PDF. The report code uses a numeric coverage-display cutoff of 20 in source metric units; do not interpret that unconditionally as 20-fold depth. In the interactive GUI, a PorePort Report PDF/HTML is created only after locking report fields and pressing **Generate report**. The separate opt-in query tool can generate a local, unapproved Draft PDF/HTML without a GUI button. Review laboratory selection and address before locking. For existing completed runs, use `--view-run RUN_ID` if that run is in the local queue. The GUI's ordinary startup may restore a different active run.

## Finalize and stop intake

Finalize is an irreversible intake decision for this cloud run. The GUI checks files it has discovered and asks for confirmation; it has no rule that rejects finalization merely because a file was seen recently. The server rejects runs with zero registered files (400) or any registered file not uploaded (409). On acceptance it marks the run stopping and forces collection of remaining uploaded files; monitor processing and resource cleanup until completion. Sequencing can continue locally, but newly produced POD5 files will not be picked up by the finalized GUI run. Wait for expected files and Pending/Failed = 0 if all files must be included. Pause intake instead if you only want a temporary stop.

## Local outputs and diagnostics

The GUI writes rotating diagnostic logs under `%APPDATA%/NanoporeCloudGUI/logs` on Windows. Clicking the Diagnostic log link opens the local file. Generated reports are under `%APPDATA%/NanoporeCloudGUI/reports/run-RUN_ID/iteration-00000N/`; run-level final PDFs may be in `run-RUN_ID/`. On Git Bash: `log_dir="$(cygpath -u "$APPDATA")/NanoporeCloudGUI/logs"; tail -n 80 "$log_dir/nanopore-gui.log"`. Check files without opening private results using `find "$(cygpath -u "$APPDATA")/NanoporeCloudGUI/reports/run-RUN_ID" -type f`. A missing optional matplotlib installation can skip PNG graphs; a missing reportlab installation prevents PDF generation. Install both in the GUI environment, not the backend container.

## Developer-only Linux headless test

The opt-in `--headless-test-run` uses terminal pairing and the cloud-copy fixture by default (local POD5 fixtures require `--test-directory`) without visible GUI interaction. It creates and finalizes a real FoodPort test run. Use approved fixtures and a unique name; this is not a frontline workflow:

```bash
FOODPORT_VERIFY_SSL=false python -m nanopore_gui \
  --headless-test-run --headless-run-name UNIQUE_TEST_RUN
```

The default `--headless-timeout` is 14400 seconds. Cloud-copy mode requires `AZURE_STORAGE_CONNECTION_STRING` on the controlled test host; local-fixture mode does not. The 181 Linux source tests passed, but no live headless end-to-end success has been reported. See [testing](TESTING.md).
