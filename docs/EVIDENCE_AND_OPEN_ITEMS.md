# Evidence ledger and open items

Snapshot: 29 September 2026. This ledger distinguishes implementation, configured values and observed outcomes.

## Confirmed from supplied code

- GUI: file scanner, local queue, pairing client, SAS uploads, result CSV download, preview and user-triggered final report. Source: `nanopore_gui/api.py`, `ui.py`, `storage.py`, `scanner.py`, `uploader.py`, `reports.py`, `main.py`.
- Backend: API routes and views, pairing/token expiry, unique run name and Blob prefix guard, file completion scheduling, quiet-window collection, generation manifests, Batch submission, result reconciliation and monitor cleanup. Source: `api_urls.py`, `api_views.py`, `tasks.py`, models, serializers, pairing/auth modules.
- Batch: pool/job/task construction, gallery-image selection, 16-hour task limit, 24-hour input/output SAS, run-level stdout/stderr prefix. Source: `batch_api.py`, `azure_cli.py`, `batch_methods.py`.
- VM: 0.0.17 wrapper polls manifests and publishes results; incremental scheduler invokes Dorado, minimap2 and samtools. Source: wrapper and scheduler supplied.

## Configured and observed

- Operator-reported Windows verification (29 September 2026): Python 3.12.14, `python -m pytest tests/` completed with 166 passed; full GUI end-to-end `--test-run` completed and worked as intended. No run ID, commit, redacted logs or per-stage checklist was supplied; do not infer unreported details.

- Batch environment provided: gallery version 0.0.17; Ubuntu 24.04 node agent; Standard_NV18ads_A10_v5; GRID-related trustedLaunch flags; targets FASTA; collection window 60 seconds. This is configuration evidence, not a per-run image audit.
- Packer 0.0.17 build log: 44 tests passed, validator succeeded, gallery image version published, 19m13s. Supplied `packer-manifest.json` is 0.0.11.
- Supplied GPU acceptance JSON: accepted=true, 23 POD5, five waves, 40/40 mapping assertions, wrapper 0.0.17, commit 045baf1ba8372813e05bd981dd57886739e052df. Direct scheduler acceptance, not complete desktop/API/Batch acceptance.
- Prior observed portal runs and GUI reports demonstrate partial real integration, but do not substitute for a retained, repeatable release-level end-to-end acceptance record.

## Open verification and maintenance

- Linux packaging and future update strategy are documented in `LINUX_PACKAGING_AND_UPDATES.md`. Auto-update is deferred, not implemented.

1. Confirm actual image resource ID and source commit from a retained Batch task record for a 0.0.17 run.
2. Windows GUI end-to-end `--test-run`: operator reports successful completion on 29 September 2026. Retain run ID, source commit, timestamps, iterations, finalize, result downloads and pool/job teardown evidence before marking the formal release acceptance record complete. Repeat on Linux and on the installed Linux artifact.
3. Resolve the 16-hour Batch task and 24-hour SAS limits for runs that must stay open longer; no renewal mechanism established by supplied code.
4. Verify whether `NANOPORE_COLLECTION_RETRY_SECONDS` and `NANOPORE_MAX_FILES_PER_ITERATION` are loaded/enforced; do not claim 15-second retry or 100-file cap solely from environment text.
5. Review insecure build-download flags, disabled Micromamba TLS verification and unpinned NVIDIA installer digest with security owners.
6. Ensure source and docs use the same current versions and do not treat old 0.0.11 manifest or 0.0.13 build guide as current.
7. Validate the exact units of the GUI's `Coverage` field before interpreting its threshold 20 as 20x depth.
8. Keep backend Python 3.5 compatibility and deprecated Azure packages; Batch/Flask and VM runtimes are separate.

No source scripts, deployed resources or Blob data were modified to produce this documentation.
