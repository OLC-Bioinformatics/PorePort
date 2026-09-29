# FoodPort Nanopore operations

## Deployment and configuration

Backend files use Python 3.5 and legacy Azure packages; Batch/Flask and VM files use their own newer environments. Do not port backend code to modern-only syntax while changing documentation. Confirm settings from the actual service environment, not only source defaults. Supplied Batch settings select `NANOPORE_IMAGE` gallery version 0.0.17, `NANOPORE_NODE_AGENT_SKU=batch.node.ubuntu 24.04`, `NANOPORE_BATCH_VM_SIZE=Standard_NV18ads_A10_v5`, `NANOPORE_SECURITY_TYPE=trustedLaunch`, Secure Boot and vTPM disabled, Micromamba root `/opt/micromamba/root`, and runtime binaries under the poresippr environment. The portal defaults include raw container `nanopore-runs`, results container `nanopore-results`, Dorado fast model and barcode kit SQK-RBK114-24. Inspect the running settings before declaring a deployment identical to these values.

To deploy a tested image: update the Batch service's `NANOPORE_IMAGE` to the accepted gallery version; recreate the affected services with the deployment's env file, for example:

```bash
docker compose --env-file env up --detach --force-recreate --no-deps web batch celeryworker celerybeat flower
docker compose --env-file env restart nginx
docker compose --env-file env ps
```

Recreation does not rebuild a Docker image unless `--build` is used; check whether source is bind-mounted or baked into the image. Avoid restarting active workers during critical submissions without an operational plan. Never put credentials in diagnostic output.

## Read-only run triage

From the portal host, use `docker compose --env-file env exec -T web python3 manage.py shell` and inspect a run by ID:

```python
from olc_webportalv2.cowbat.models import SequencingRun
run = SequencingRun.objects.get(pk=RUN_ID)
print(run.pk, run.run_name, run.workflow_state)
print([(b.generation, b.status, b.report_url, b.last_completed_at, b.error) for b in run.nanopore_processing_batches.order_by('generation')])
```

Check that `report_url` is a real blob and matches the run ID/name/generation before trusting it. `last_completed_at` and the latest pointer are useful but should be reconciled with the manifest. For run-level Batch details, check `azure_job_id`, `azure_task_id`, `azure_pool_id`, task exit code and error on each batch. If a new run name is needed, check all three namespaces before creation: raw `RUN_NAME/`, legacy results `RUN_NAME/`, and results `runs/RUN_NAME/`. Never reuse a name simply because its DB row was removed.

## Monitoring and recovery

`docker compose --env-file env logs --since=30m web celeryworker celerybeat batch` is a starting point; adjust service names to the actual compose file. Correlate timestamps, run ID, generation and request ID. Check the Batch job/task, VM wrapper logs, scheduler generation logs, raw input manifest, result manifest and latest pointer in that order. An HTTP 200 for status does not prove the result is published. An input batch status of `published` is not a published *result*. If a run is stopping, allow its final generation and monitor cleanup to finish; avoid manual task reruns or destructive Blob operations while a Batch task is active. An absent result manifest is not repaired by pointing the GUI at a CSV directly.

## Data and security

Treat POD5, FASTQ, BAM, CSV, reports, SAS URLs and pairing credentials as controlled research data. Preserve original logs but redact SAS query strings, authorization headers and sample-identifying data before sharing. Keep full Packer debug logs restricted: signed request URLs may appear. Legacy `curl --insecure` and `ssl_verify: false` in build scripts are security-review items; do not copy them into new automation without approval. The NVIDIA installer hash is printed but not compared against a pinned expected digest in the supplied script.

## GUI repository operator tools

The seeder in `tools/seed_blob_iterations.py` writes real runs and Azure blobs; the read-only API query tool in `tools/query_run_outputs.py` saves status and result manifests locally. Use them only with authorized test data and credentials. See OPERATOR_TOOLS.md for exact examples, limits, security and cleanup.
