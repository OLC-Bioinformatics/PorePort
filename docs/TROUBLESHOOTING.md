# Nanopore troubleshooting

## Desktop says no published results

Check that the GUI is viewing the intended run ID, not a previously restored active run. Inspect `GET nanopore/runs/RUN_ID/`, then `GET nanopore/runs/RUN_ID/results/GENERATION/` using authenticated tooling; do not paste a token into a URL. Check that the generation result manifest exists, its run ID/name/generation match, and each CSV output has `available=true` and a `download_url`. An input batch status `published` can precede result publication. If API 200 and URLs exist, check GUI logs and local report files. `reportlab` is needed for PDFs; `matplotlib` is needed for PNG graphs and summary images. A missing local report is not necessarily a failed VM task.

## Run name already taken

A deleted database record does not free Blob storage. Check raw `RUN_NAME/`, legacy results `RUN_NAME/`, and results `runs/RUN_NAME/` for leftovers. Use a new name; do not rename or delete existing blobs during an active investigation. Confirm manifest run IDs before any recovery.

## Iteration waiting or stopping

Check uploaded file records, quiet collection window, previous-generation blocker, input manifest, Batch job/task and wrapper logs. A result CSV alone is insufficient; require generation manifest and latest pointer. Finalize forces collection of already uploaded files but does not accept pending files. A run can remain stopping while the final generation drains and cleanup proceeds.

## Batch task failed or disappeared

Inspect backend and Batch service logs by run ID, generation and timestamp; inspect run-level `runs/RUN_NAME/logs/azure_stderr.txt` and `azure_stdout.txt`, then iteration scheduler logs. Legacy logs may be under `RUN_NAME/logs/`. Check 16-hour task limit and 24-hour SAS expiry for long runs. Preserve original evidence and check whether a task already exists before retrying submission; avoid duplicate jobs.

## Missing image or report

If iteration CSVs exist locally but no PNG, check that matplotlib is installed in the *GUI* environment; if no PDF, check reportlab. Live preview and the user-triggered PorePort Report are different. Use the GUI's regeneration control for cached CSVs if available; do not rerun cloud analysis to repair a local rendering dependency. For Windows Git Bash: `tail -n 80 "$(cygpath -u "$APPDATA")/NanoporeCloudGUI/logs/nanopore-gui.log"`.

## Evidence safety

Share status codes, redacted JSON keys, run/generation IDs and traceback lines. Strip SAS query strings, access tokens, pairing codes, private CSV contents and full Packer debug request URLs. Do not run cleanup, delete DB rows, or modify storage as a diagnostic first step.
