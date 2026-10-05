# Nanopore test and acceptance plan

## Test layers

1. **Desktop unit tests**: run `python -m pytest tests` in the GUI environment. Include API errors, queue persistence, scanner stability, retries, report generation and Qt UI tests. Use an offscreen Qt test setup only when supported by the host. An isolated mocked pass is not a real GUI integration pass.
2. **Backend unit/API tests**: validate run-name collisions across DB and all three Blob prefixes, pairing expiry/one-time exchange, file prepare/complete, finalize guards, generation claiming, result identity checks, missing-manifest behavior, and idempotent monitoring. Run in the backend's Python 3.5-compatible environment; do not infer tests passed without execution output.
3. **Batch service tests**: validate configured gallery image, VM size/node agent, pool/job/task submission, SAS generation, task constraints, run-level log prefix, task exit and cleanup. A successful HTTP response alone does not prove the Batch task ran.
4. **Image build validation**: `validate-image.sh` checks installed dependencies and scheduler tests on the Packer build VM. For 0.0.17, supplied build log reports 44 passing tests, validation success and gallery publication.
5. **GPU acceptance**: `launch-gpu-acceptance.sh` provisions a temporary GPU VM; `run-gpu-acceptance.sh` exercises 23 POD5 files in five waves and validates 40 mapping assertions. The supplied 0.0.17 result reports accepted=true and 40/40 passes. It tests the scheduler directly, not all FoodPort services.
6. **End-to-end release smoke test**: use a new unique run name; pair GUI; upload at least one POD5; observe input manifest, Batch task, published generation manifest, backend reconciliation, GUI CSV download and summary PNG; finalize; verify final generation and job/pool cleanup. Record image ID, run ID, timestamps and redacted evidence. The supplied evidence does not establish a comprehensive automated end-to-end acceptance pass.

## Acceptance checklist

- Confirm run ID/name in every input and output manifest; reject stale results from a deleted/reused name.
- Verify each input generation is claimed once and later generations do not overtake unpublished earlier results.
- Verify result manifest exists before declaring iteration complete; check `latest.json` points to the expected generation.
- Verify scheduler CSV `download_url` points under the correct iteration directory, without printing the SAS token.
- For the interactive GUI, verify the report appears only after pressing Generate report and that laboratory and fields match. Validate the separate opt-in query-tool Draft report independently.
- Finalize with pending files to verify 409; finalize with zero files to verify 400; finalize a complete intake and observe stopping-to-complete and resource cleanup.
- Exercise network interruption, expired SAS, retry, long-lived run exceeding task/SAS limits, and a failed Batch task. Document observed outcomes; do not assume success from a test fixture.

## Evidence retention

Retain the 0.0.17 Packer build log and GPU acceptance JSON/Markdown with restricted access. The provided `packer-manifest.json` belongs to 0.0.11. Keep a redacted deployment record with source commit, gallery version, acceptance outcome and actual Batch task image. Do not publish signed URLs or sample-level data in a public test report.

## GUI repository operator tools

Run `python -m pytest -q tests/test_query_run_outputs.py tests/test_seed_blob_iterations.py` in the GUI environment. These are mocked tests; they must not access FoodPort or Azure. Verify query retries only the not-yet-published 404, preserves unrelated errors, and redacts signed URLs. Verify seeder validation, failed copy handling and the copy-before-complete boundary. See OPERATOR_TOOLS.md for live-use cautions.

## Operator-reported Windows verification, 29 September 2026

On a Windows workstation with Python 3.12.14, the operator reported `python -m pytest tests/`: **166 passed in 9.04s**. The operator also reported a successful full GUI end-to-end `--test-run` with intended results. These statements document reported outcomes, not a retained release sign-off: run ID, commit, detailed stage checklist and redacted evidence have not been supplied. Do not infer that each negative-path item above was exercised. Before Linux distribution, repeat the full suite and end-to-end scenario from source and from the installed Linux package. See `LINUX_PACKAGING_AND_UPDATES.md` for the packaging and deferred update acceptance criteria.

### Linux source-tree result, operator-reported 2 October 2026

`python -m pytest tests/` on remote Linux with Python 3.12.14: **181 passed**. This includes mocked headless-startup and query-report tests. This is not a live headless run, a visible desktop test, or an installed Linux package test. Source commit, distribution version and live run ID were not supplied. The historical Windows 166-test pass and interactive `--test-run` remain separate evidence.

### Live Linux headless acceptance still outstanding

With an approved local fixture and unique run name, use terminal pairing, then verify uploads, result publication, locally generated report, finalization and remote cleanup. Record run ID, source commit, host, image version, redacted logs and exit status. The mode creates a real run and finalizes it; investigate remote state before retrying after failure or timeout. A complete workflow without a locally generated report must not count as a successful headless test.

```bash
FOODPORT_VERIFY_SSL=false python -m nanopore_gui \
  --headless-test-run --headless-run-name UNIQUE_TEST_RUN
```
