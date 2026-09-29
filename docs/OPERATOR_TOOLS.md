# GUI repository operator tools

These are test/diagnostic utilities, not normal GUI intake. Run from the PoreSippR-GUI repository root with its Python 3.12 environment and installed dependencies (`requests`, `azure-storage-blob` for the seeder). Run `python tools/query_run_outputs.py --help` and `python tools/seed_blob_iterations.py --help` for exact current arguments.

## Read-only API query

`tools/query_run_outputs.py` calls FoodPort status and per-generation result endpoints. It saves `status.json` and `iteration-000001.json` etc. under `--output-dir`, recursively redacting JSON fields whose names end in `url`. `--watch` polls until workflow `complete` or `error`; `--stop-on-processing-failure` exits local polling on a failed generation but does not stop remote cleanup. With the included fix, an advertised iteration whose result manifest returns the specific *results not published yet* 404 remains pending and is retried next poll. Other 404s and HTTP errors still fail visibly. A terminal workflow state does not cause indefinite polling for pending manifests; inspect missing generations manually. Treat report JSON as sensitive even after URLs are redacted, since identifiers and sample metadata may remain.

```bash
export FOODPORT_API_BASE='https://your-authorized-portal.example/en-ca/api'
export FOODPORT_VERIFY_SSL=true
# Supply FOODPORT_TOKEN through an approved secret mechanism or use --pairing.
python tools/query_run_outputs.py --run-id 1234 --pairing --watch --poll-seconds 15 --output-dir run-reports/1234
```

## Seeder: writes real data

`tools/seed_blob_iterations.py` creates a real FoodPort run, prepares uploads, starts server-side Azure Blob copies from an existing dataset, waits for each copy, completes a release wave, waits between waves, and finalizes by default. Its default 23-file source dataset, barcodes 3 and 4, release sizes 3,7,2,5,6 and 150-second gaps are **test defaults**, not generic production defaults. The FoodPort quiet collection window decides actual generation boundaries; five waves do not guarantee five generations. `--no-finalize` leaves intake open. The script requires `AZURE_STORAGE_CONNECTION_STRING` with source account name/key and requires the prepared destination to be in the same account for polling. Confirm source container/prefix and run name before execution. Use only approved test storage; do not use a production dataset by accident. A failed copy leaves a prepared file record and may require operator review before finalization.

```bash
export FOODPORT_API_BASE='https://your-authorized-portal.example/en-ca/api'
export FOODPORT_VERIFY_SSL=true
# Set AZURE_STORAGE_CONNECTION_STRING via an approved secret store; never echo it.
python tools/seed_blob_iterations.py --run-name UNIQUE_TEST_RUN --pairing --no-finalize
```

The original tools default `FOODPORT_VERIFY_SSL` to false; set it to `true` where the certificate chain is trusted. Neither tool's JSON redaction makes terminal output, environment variables, or original Azure logs safe to share. Keep run IDs, storage copies and local output folders in the test cleanup record.

## Installation and tests

The companion code bundle contains a complete replacement `tools/query_run_outputs.py` and two new tests under `tests/`. `tools/seed_blob_iterations.py` is unchanged. From the GUI repository root, after installing the project's dependencies, run:

```bash
python -m pytest -q tests/test_query_run_outputs.py tests/test_seed_blob_iterations.py
```

These tests mock the client and Blob interactions; they do not perform a GPU or FoodPort end-to-end acceptance test.
