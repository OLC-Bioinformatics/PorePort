# Nanopore architecture, wrapper and storage

## Current flow (implementation snapshot)

Desktop scanner -> local queue -> FoodPort prepare -> direct SAS upload to raw Blob -> complete -> Celery collection -> numbered input manifest -> Batch service pool/job/task -> VM wrapper polling -> one-shot incremental Dorado scheduler -> generation result manifest and latest pointer -> FoodPort monitor reconciliation -> GUI result download and local report generation. File completion can trigger collection before finalize. Finalize closes intake and requests the remaining drain; it is not the sole trigger for Batch submission.

FoodPort persists `SequencingRun`, `NanoporeRun`, `NanoporeFile`, `NanoporeManifest`, and `NanoporeProcessingBatch`. Uploaded unclaimed files are assigned to a generation and a batch. A 60-second quiet window is configured in the supplied environment; forced collection on finalize bypasses that wait. A previous generation without a published result blocks the next collection. The code supplied does not demonstrate effective use of environment keys `NANOPORE_COLLECTION_RETRY_SECONDS=15` or `NANOPORE_MAX_FILES_PER_ITERATION=100`; do not advertise them as enforced limits without a separate code check.

## Storage namespaces

`nanopore-runs/RUN_NAME/` is the raw container namespace for POD5 input, input CSVs, control and generation manifests. `nanopore-results/runs/RUN_NAME/iterations/iteration-000001/` holds generation outputs: scheduler FASTQ fragments, mapping BAM/BAI, CSV results, scheduler state/status/logs and an immutable result manifest. `nanopore-results/runs/RUN_NAME/manifests/latest.json` points to the latest published generation. Run-level Batch stdout/stderr are requested under `nanopore-results/runs/RUN_NAME/logs/`. The legacy `nanopore-results/RUN_NAME/logs/` path may still contain old logs; do not delete or silently move them without verifying references and run identity. Azure container names are not repeated in blob names.

An input manifest identifies run ID/name, generation, POD5 blob paths, expected sizes and scheduler configuration. The VM wrapper downloads and checks each manifest, invokes the scheduler with `--once`, captures scheduler logs and exit status, inventories outputs, publishes the generation result manifest and publication state, and advances the latest pointer. The scheduler uses Dorado basecalling/demux, keeps barcode FASTQ fragments, maps cumulative reads with minimap2/samtools, and records processed-POD5 state to avoid duplicate work. Publication and backend reconciliation are separate operations; database `report_url` alone does not prove a blob exists.

## Runtime constraints and shutdown

The Batch task definition in the supplied service sets a 16-hour max wall-clock time; the generated input/output SAS URLs expire after 24 hours. The wrapper polls for future generations but no credential renewal or automatic continuation beyond those limits was established. These limits are a production design constraint, not merely a testing limit. FoodPort's monitor reconciles result manifests and handles stopping and resource cleanup. Verify the actual job/task/pool states before claiming teardown; never delete a pool or job based solely on a GUI message. Preserve logs, manifests and run IDs for investigations.

## Image inputs and identity

Supplied Batch configuration points at gallery `development/nanopore/versions/0.0.17`, Ubuntu 24.04 node agent and `Standard_NV18ads_A10_v5`. The wrapper identifies as 0.0.17. The pinned repository commit is `045baf1ba8372813e05bd981dd57886739e052df`; the installed reference FASTA is `/opt/foodport/poresippr-data/PoreSippR_DB_251110.fasta` (6,663 sequences). The desktop's historical local FASTA asset has a different filename and must not be equated with the VM reference. Image build and GPU acceptance are documented separately.
