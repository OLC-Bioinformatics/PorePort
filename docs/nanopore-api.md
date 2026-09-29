# FoodPort Nanopore API contract

Source of truth: supplied `api_urls.py`, `api_views.py`, `serializers.py`, `pairing.py`, `entra.py`, `authentication.py`, and GUI `api.py`. Route examples below are relative to the GUI's configured API base URL. The portal may prepend a locale and `/api/`; inspect the deployment's route prefix instead of hard-coding it. Authenticate Nanopore operations with `Authorization: Token <token>`. Never log or paste SAS URLs.

## Authentication

`POST auth/pairing/start/` starts browser pairing; the portal's approval URL is opened by the user; `POST auth/pairing/exchange/` exchanges `pairing_id` and code for a desktop token. Pairing approval requires a signed-in portal user; code is stored hashed, expires and is consumed once using a transaction/row lock. Desktop token authentication checks expiry. `POST auth/entra/logout/` revokes the current token; legacy `POST auth/token/` also exists. An Entra desktop exchange route is present, but browser pairing is the GUI's documented sign-in path. Pairing exchange currently masks internal exceptions as an authentication failure; inspect server logs when correct codes fail unexpectedly.

## Run lifecycle

- `POST nanopore/runs/`: create a run with `run_name` and accepted serializer fields such as instrument metadata, Dorado model, barcode kit/values and sample metadata. Check the serializer for exact fields; do not assume `lab_name` or `reference_database` are accepted top-level API fields. A taken name yields HTTP 409. The backend checks the database and raw/results Blob prefixes; storage-check failure should fail closed rather than permit reuse.
- `GET nanopore/runs/{run_id}/`: run, file and processing status; a 200 response may still contain a processing failure. Read the nested processing status and error fields, not just the HTTP status.
- `POST nanopore/runs/{run_id}/files/prepare/`: JSON `relative_path`, `size_bytes`; returns file ID and short-lived upload URL. Upload the bytes directly to the returned SAS URL, then call `POST nanopore/runs/{run_id}/files/{file_id}/complete/`. An uploaded blob alone is not a completed file record. File preparation can return 409 when intake is closed or state conflicts.
- `POST nanopore/runs/{run_id}/finalize/`: closes intake and forces remaining uploaded files into collection. It returns 400 if no files were registered and 409 if any registered file is not uploaded. Acceptance is not proof of immediate Batch teardown; poll status.
- `GET nanopore/runs/{run_id}/results/latest/` and `GET nanopore/runs/{run_id}/results/{generation}/`: latest pointer and a specific published generation. The iteration result includes `report.outputs`; an output's relative `path` is not itself a download URL. When the blob exists, the view enriches outputs with `available`, `storage_blob_name` and `download_url` (a read-only SAS URL). The GUI downloads scheduler result CSVs from these URLs and creates local report artifacts.

## Result safety and failure handling

The backend checks result manifest run ID, run name and generation against the database record. It must not accept a stale manifest from an older database row sharing a run name. The output resolver checks the verified iteration directory, e.g. `runs/RUN_NAME/iterations/iteration-000003/scheduler/results/NAME.csv`, rather than assuming a CSV lives directly beneath the run root. A generation marked `published` in the database can still lack a result manifest: distinguish *input published* from *results published*. Do not claim completion merely because the Batch task exited 0; verify the expected result manifest and identity.

Errors use HTTP status and JSON error text where available. The GUI raises `FoodPortError` on non-2xx responses; a status response with 200 must be interpreted separately. Correlate request IDs in server logs. Never include full SAS query strings, authorization headers or pairing codes in diagnostic bundles. These are behavioral summaries, not a generated OpenAPI schema; inspect `api_urls.py` and serializer code when implementing another client.
