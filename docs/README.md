# PorePort Nanopore documentation

Status: updated documentation snapshot, 2 October 2026. Earlier backend/image evidence remains historical; the Linux source-test result is recorded separately. Source: supplied GUI, FoodPort, Batch-service and VM-image code; deployment configuration; Packer logs; GPU acceptance JSON. This is documentation, not a deployment or code change.

## Start here

- [USER_GUIDE.md](USER_GUIDE.md): desktop installation, pairing, intake, finalization and reports.
- [nanopore-api.md](nanopore-api.md): API endpoints, authentication, upload protocol, result contract.
- [NANOPORE_TASK_WRAPPER_AND_STORAGE_PLAN.md](NANOPORE_TASK_WRAPPER_AND_STORAGE_PLAN.md): actual architecture, state machine, storage and publication.
- [OPERATIONS.md](OPERATIONS.md): operator checks, configuration, deployment and recovery.
- [NANOPORE_PACKER_IMAGE_BUILD.md](NANOPORE_PACKER_IMAGE_BUILD.md): image-build and GPU acceptance runbook.
- [TESTING.md](TESTING.md): layered strategy and reported Windows/Linux evidence.
- [OPERATOR_TOOLS.md](OPERATOR_TOOLS.md): read-only query, opt-in Draft reporting and controlled seeding.
- [LINUX_PACKAGING_AND_UPDATES.md](LINUX_PACKAGING_AND_UPDATES.md): Linux pilot and deferred updates.
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md): symptom-to-check diagnostic guide.
- [EVIDENCE_AND_OPEN_ITEMS.md](EVIDENCE_AND_OPEN_ITEMS.md): evidence ledger, known limitations and outstanding verification.

## Evidence conventions

**Confirmed by source** means a behavior exists in supplied code, not that every deployment executes it successfully. **Configured** refers to the environment values provided for the Batch service, not a guarantee that every historical task used them. **Observed** refers to supplied logs, the GPU acceptance result, or run-specific diagnostic outputs. **Not established** identifies missing evidence. Historical 0.0.11 manifest and 0.0.13 build guide are not evidence of 0.0.17.

## Version summary

The supplied Batch environment selects gallery image `development/nanopore/versions/0.0.17`, Ubuntu 24.04 Batch node agent, `Standard_NV18ads_A10_v5`, and the PoreSippR target FASTA `PoreSippR_DB_251110.fasta`. The wrapper reports 0.0.17. The 0.0.17 Packer log records successful image validation, 44 scheduler tests, and gallery publication. The supplied GPU acceptance result reports 23 POD5 files, five waves and 40/40 mapping assertions. GPU acceptance runs the scheduler directly; it does not prove the complete desktop-to-portal-to-Batch lifecycle. See TESTING.md.

## Placement

These Markdown files are in the repository `docs/` folder. This index is `docs/README.md`, separate from the root `README.md`. Keep signed URLs and private run evidence out of public documentation.

Operator/test-only scripts in the GUI repository are documented in [OPERATOR_TOOLS.md](OPERATOR_TOOLS.md). The tools and tests are maintained in the GUI repository.

## Linux distribution and updates

See `LINUX_PACKAGING_AND_UPDATES.md` for the proposed Linux pilot package and deferred update strategy. Linux packaging and auto-update are not yet implemented. The reported Windows and Linux source-test outcomes are recorded in `TESTING.md`; a live Linux end-to-end or packaged release is not yet established. Update the evidence ledger when run-specific evidence is available.
