# Linux packaging and deferred update plan (draft)

Status: planning only. No Linux release artifact or auto-update mechanism is implemented or verified by this document. Target workstation distribution, architecture, package policy and managed software tooling remain to be confirmed.

## Decision and rationale

The application is a PySide6 desktop GUI. Prefer a Linux-built PyInstaller **onedir** bundle for the first internal pilot on the organization's supported Linux workstation distribution and architecture, installed by a small, versioned deployment package or centrally managed software mechanism. Build and test on Linux, not on the Windows development machine. Keep the executable and bundled libraries in a versioned, read-only installation directory; keep queue, logs, user preferences, downloads and credentials outside it. The Linux source-tree suite has passed, but visible GUI operation, a live headless end-to-end run and a Linux release artifact are not yet established.

For a managed fleet, prefer a native `.deb` or `.rpm` wrapper only after the target distribution is known, or deploy the versioned onedir bundle with the organization's endpoint-management tooling. An AppImage is a possible portable pilot alternative if workstation policy permits it, but its portability, Qt/system-library integration, desktop integration and update behaviour require testing on each supported workstation image. Flatpak is an alternative only if permitted and if its sandbox can access the MinKNOW output paths, network and local report destinations. Do not introduce a second updater alongside an IT-managed package channel.

References: https://pyinstaller.org/en/stable/usage.html ; https://pyinstaller.org/en/stable/operating-mode.html ; https://briefcase.beeware.org/en/latest/reference/platforms/linux/flatpak.html ; https://docs.appimage.org/packaging-guide/index.html

## Phase 1: Linux readiness, before packaging

1. Confirm target Linux distribution/version, x86_64 versus ARM, display session, workstation permissions, network proxy, internal CA, access to MinKNOW output and desktop-management policy.
2. On a representative Linux workstation, create a clean Python environment; run the full unit suite and GUI from source; perform a real `--test-run` including upload, results, report creation, finalization and shutdown. Record run ID and source commit privately.
3. Audit Linux paths and application data: current `APPDATA` fallback to home is not a standard XDG layout. Choose XDG_CONFIG_HOME, XDG_DATA_HOME, XDG_STATE_HOME and XDG_CACHE_HOME where appropriate, and document migration from any existing paths. Ensure app logs and queue persist across upgrades.
4. Verify Qt platform plugins, fonts, image/PDF assets, browser pairing, certificates, filesystem watcher behaviour, external file opening, and GUI startup without a development checkout.
5. Configure trusted certificate validation for production. The current development configuration allows disabled verification and must not silently become a release default.

## Phase 2: reproducible Linux pilot artifact

1. Pin supported Python and runtime dependencies in a release lock or controlled build environment; use the project version as the sole release identifier and record source commit and build image.
2. Create a checked-in PyInstaller spec or equivalent reproducible build definition. Include PySide6 plugins and packaged report assets; exclude developer tests, test fixtures and optional Azure cloud-copy tooling from the operator build unless needed.
3. Build `onedir` on a Linux runner compatible with the oldest supported workstation image; produce one artifact per architecture. Add a `.desktop` launcher/icon and package metadata as required by workstation IT.
4. Publish internally with version, SHA-256 checksum, release notes and a rollback copy. Test installation, launch, normal operation and uninstall on a clean workstation without Python or a source checkout.
5. Run a pilot with a small number of operators. Test retained queue/settings/logs and safe restart when an upload is active. Record Linux-specific defects before wider rollout.

## Phase 3: updates, deferred until distribution is ready

**Decision to make with IT:** Prefer managed deployment updates (package repository, endpoint manager or internal software centre) for managed workstations. In this mode, the GUI can display its installed version and optionally show a read-only 'new version available' notification; it must not overwrite its own installed files. If self-service updates are required, design a separate, trusted launcher/updater rather than modifying a running executable.

Proposed update contract: publish a small HTTPS release manifest on an approved internal endpoint with version, supported OS/architecture, minimum compatible API version, artifact location, SHA-256 digest, release notes and a signature or other organization-approved provenance. Check at launch or on demand without blocking startup; handle offline and proxy failures quietly. Never log signed artifact URLs or credentials. Verify manifest trust and artifact integrity before staging. Do not activate an update during active uploads; request a restart and allow deferral. Install into a new versioned directory, switch the launcher only after successful validation, and retain the previous version for rollback. Never overwrite user data or require `--test-run` on operator machines.

Update acceptance tests: same-version/no-network; corrupt or tampered manifest/artifact; unsupported OS/architecture; proxy/CA failure; active-upload deferral; failed installation and rollback; migration of settings/queue; version display; release from old installed version to new installed version; compatibility with the FoodPort API. Document update ownership, cadence and support channel before enabling update checks. Until then, updates are manual/IT-managed and the GUI makes no update promise.

## Evidence and release gate

Windows evidence (user-reported, 29 September 2026): `python -m pytest tests/` passed 166 tests under Python 3.12.14 on Windows; the operator reported a successful full GUI end-to-end `--test-run` with intended results. Run ID, commit, logs, exact scenario and cleanup record were not provided, so the result is not independently auditable from this document. Linux source-tree tests were subsequently reported passing (181/181, Python 3.12.14). A completed live Linux GUI/headless run, packaged-app acceptance and update tests remain outstanding.

### Linux source-test update, 2 October 2026

The remote Linux source checkout passed `python -m pytest tests/` (181 tests, Python 3.12.14). The new `--headless-test-run` uses terminal pairing and approved local POD5 fixtures with Azure account credentials for cloud-copy (not local fixtures); it creates and finalizes a real test run. Its live end-to-end outcome has not been reported. Keep the visible workstation and clean installed-package tests in the release gate. The deferred-update decision above is unchanged.
