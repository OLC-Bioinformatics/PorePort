# PorePort

A local PySide6 application for laboratory operators. It pairs with FoodPort, creates or views Nanopore runs, watches MinKNOW output for stable `.pod5` files, uploads them directly to a short-lived Azure Blob SAS URL, confirms uploads with FoodPort, and displays run status and incremental results. Dorado and downstream analysis run in FoodPort, not on the workstation.

## Install and run from source (Windows, Git Bash)

From the repository root, with Python 3.10 or newer:

```bash
conda create -n poreport python=3.12
conda activate poreport
python -m pip install -r requirements.txt
python -m nanopore_gui
```

`requirements.txt` installs this package in editable mode, including its command-line entry point, and pins the workstation/test dependencies. To install from project metadata instead, use `python -m pip install -e ".[cloud-test,test]"`. The Azure SDK is needed for the optional cloud-copy test fixture, not for normal direct-to-SAS uploads. `matplotlib` and `reportlab` support report figures and PDF generation.

Run tests from the repository root:

```bash
python -m pytest tests/
```

## Configure the connection

`FOODPORT_API_BASE` overrides the development API URL defined in `nanopore_gui/main.py`. The current development default disables TLS certificate verification. **Do not use that default with production or sensitive data:** an unverified connection is vulnerable to interception. To verify against a public CA or a trusted internal CA bundle:

```bash
export FOODPORT_VERIFY_SSL=true
# For an internal CA, also set:
# export FOODPORT_CA_BUNDLE="C:/path/to/trusted-ca.pem"
python -m nanopore_gui
```

`FOODPORT_VERIFY_SSL=false` explicitly retains the insecure development behaviour. Set the CA bundle only to a certificate you trust. The setting applies to FoodPort API requests and direct SAS uploads made through the API client; the optional Azure SDK cloud-copy test fixture has its own SDK transport configuration.

The local queue and diagnostic log live under the application-data location selected by the current code (`APPDATA` on Windows, otherwise the home directory). The log is at `NanoporeCloudGUI/logs/nanopore-gui.log` and rotates at 5 MB with three backups. The Linux data-directory layout is still under review. Avoid sharing logs without reviewing them. Authentication tokens, pairing codes, passwords, and SAS query values must not be logged; API paths, status codes, and upload failures are recorded for troubleshooting.

## Operator workflow

1. Sign in to FoodPort in a browser, approve GUI pairing, and enter the one-time pairing code in the desktop app. The app exchanges that code for its own API token; it does not receive a FoodPort password or browser session cookie.
2. In Run setup, enter a run name, choose the laboratory and barcode kit, select the used barcodes, fill in SEQID and OLN ID for each selected barcode, and choose the POD5 directory. Complete the preflight checks and create the run.
3. Keep the GUI open as the scanner finds stable `.pod5` files. The default stability interval is 180 seconds and the default upload concurrency is three workers; both are adjustable. The local queue survives restarts, including interrupted uploads. Review and retry failed uploads before finalizing.
4. Monitor processing and live iteration results. Use the live report table to navigate iterations and filter rows. Generate the target report for the latest cached iteration when it is available; the app can open locally generated report documents and figures.
5. When sequencing is complete and uploads are resolved, use **Finalize run** to stop intake and request graceful shutdown. You can open a saved run for read-only report viewing with `--view-run RUN_ID`.

The run metadata includes `barcode_kit`, numeric `barcode_values`, sample `seqid`/`olnid` values, the selected laboratory, and reference-database choices. Processing and scheduler configuration remain on the backend. The GUI polls run status while a run is active and shows progress, reports, and available output links. The legacy scheduler CSV is not required for this API workflow.

## Test-run modes

Test runs use a preset run name and sample metadata; they are for development only. They can upload staged local POD5 fixtures or use the repository's Azure cloud-copy fixture. Do not use these options on a production run.

```bash
# Stage POD5 files from a local fixture directory:
python -m nanopore_gui --test-run --test-directory "C:\path\to\pod5-fixtures"

# Use the configured Azure cloud-copy fixture (requires Azure SDK and credentials):
python -m nanopore_gui --test-run

# Optional: set wave delay in seconds and leave the run open:
python -m nanopore_gui --test-run --test-directory "C:\path\to\pod5-fixtures" --test-wave-delay 5 --test-no-finalize

# Read-only viewing of a locally saved run:
python -m nanopore_gui --view-run 42
```

The default wave delay is 150 seconds. `--test-directory` is optional with `--test-run` or `--headless-test-run`. Cloud-copy test mode requires `AZURE_STORAGE_CONNECTION_STRING` with an account name and account key, plus access to the configured source fixture; no account key is bundled with the application.

## Remote Linux headless development test

This opt-in test creates and finalizes a **real FoodPort run**. Without `--test-directory`, it uses the same cloud-copy fixture and test metadata as interactive `--test-run`. Add `--test-directory` to use approved local POD5 fixtures instead. The approval URL appears in the SSH terminal; open it in an authorized browser, then enter the one-time code in the terminal. Cloud-copy mode requires an approved `AZURE_STORAGE_CONNECTION_STRING` on the controlled test host; local-fixture mode does not. Use a unique name and a trusted TLS configuration:

```bash
FOODPORT_VERIFY_SSL=false python -m nanopore_gui \
  --headless-test-run --headless-run-name UNIQUE_TEST_RUN
```

`--headless-timeout` defaults to 14400 seconds. Do not combine headless mode with `--test-run`, `--view-run`, or `--test-no-finalize`. A unit-test pass does not establish a live end-to-end pass; check remote run state after timeout or failure before retrying. This is a developer test mode, not a frontline workflow.

## Validation and Linux distribution status

On the Windows development workstation, the operator reported **166 passing tests** with `python -m pytest tests/` (Python 3.12.14) and a successful full GUI `--test-run`. This is a Windows end-to-end baseline, not evidence of every failure scenario. On a remote Linux source checkout (Python 3.12.14), the operator subsequently reported **181 passing tests**; a live Linux headless or visible-GUI run and an installed-package test have not been recorded. Run ID, source commit and redacted end-to-end evidence have not yet been recorded. See [testing and acceptance](docs/TESTING.md) and the [evidence ledger](docs/EVIDENCE_AND_OPEN_ITEMS.md).

Linux workstations are the intended distribution target. **No Linux installer or automatic update functionality is implemented or verified yet.** Before distribution, validate the GUI and tests on a representative Linux workstation, then build and test a Linux-native package. The [draft Linux packaging and deferred-update plan](docs/LINUX_PACKAGING_AND_UPDATES.md) proposes a Linux-built, one-directory PyInstaller pilot and prefers centrally managed updates where workstation policy permits. Update checks can remain offline until distribution is ready. Do not assume a Windows build can be distributed to Linux.

## Documentation

- [PorePort Nanopore documentation index](docs/README.md)
- [Desktop user guide](docs/USER_GUIDE.md)
- [Testing and acceptance](docs/TESTING.md)
- [Linux packaging and deferred updates](docs/LINUX_PACKAGING_AND_UPDATES.md)
- [Operator tools](docs/OPERATOR_TOOLS.md)

## Packaging and API notes

Install or build from the repository root. The `nanopore_gui/assets/` directory contains the branding images used by report generation and is included in the package. Project metadata reads its version from `nanopore_gui/version.py`; update that module when releasing a new version.

The desktop pairing flow uses `POST /auth/pairing/start/` and `POST /auth/pairing/exchange/`. Uploads use a FoodPort prepare call, direct HTTP PUT to the returned SAS URL, then a completion call. A native desktop client does not need browser CORS permissions for those requests. For endpoint details, refer to the project's API documentation and the implementation in `nanopore_gui/api.py`.
