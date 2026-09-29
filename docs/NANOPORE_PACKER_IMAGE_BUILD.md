# Nanopore VM image build and GPU acceptance

## Scope and evidence

The supplied Packer template builds the gallery image; `development.pkrvars.hcl` and `foodport-image.json` identify version 0.0.17 and PoreSippR commit `045baf1ba8372813e05bd981dd57886739e052df`. The 0.0.17 Packer log records 44 passing scheduler tests, successful image validation, and publication to the gallery in 19 minutes 13 seconds. A separately supplied GPU acceptance result reports `accepted: true`. The older `packer-manifest.json` is for 0.0.11, not 0.0.17. Keep historical 0.0.8/0.0.11/0.0.13 guidance as archival provenance, not the active runbook.

## Release procedure

1. In PoreSippR-GUI, commit wrapper/scheduler/test changes; run `python -m pytest -q tests/test_foodport_nanopore_task.py` and any changed scheduler tests; push and record `git rev-parse HEAD`.
2. In the portal repo, set the new `image_version` and `poresippr_repository_commit` in `development.pkrvars.hcl`; set the same version and repository commit in `foodport-image.json`. Update default version/commit in `launch-gpu-acceptance.sh` and commit in `run-gpu-acceptance.sh`. The Packer template need not change for a version bump.
3. Run `./infrastructure/nanopore-image/scripts/build-image.sh` from the portal repo. Retain the version-specific Packer log, generated manifest, image resource ID and source commit. The script creates a target SAS, runs Packer and publishes a gallery image. Confirm final validation and gallery publication; a started build is not a successful build.
4. Launch GPU acceptance explicitly: `IMAGE_VERSION=VERSION PORESIPPR_REPOSITORY_COMMIT="$PORESIPPR_COMMIT" ./infrastructure/nanopore-image/acceptance/launch-gpu-acceptance.sh`. It creates a temporary A10 VM, stages the allowlisted dataset, runs scheduler acceptance and removes temporary resources afterward. Retain `acceptance-result.json`, Markdown summary, assertions and evidence archive under `infrastructure/nanopore-image/docs/acceptance-results/VERSION/`. Check actual output directory with `ls` and `git status --ignored --short` if an editor hides it. Do not rerun just because a folder is hidden.
5. Only after acceptance passes, change the deployed Batch `NANOPORE_IMAGE` resource ID and restart relevant services. Verify running configuration and an actual Batch task image before announcing rollout.

## Provisioning order and installed runtime

Packer uploads image metadata and environment YAML, then runs base provisioning, NVIDIA GRID installation, Dorado installation, Micromamba installation, poresippr environment creation, targets download, pinned repository checkout, image validation and Azure deprovision. The supplied environment specifies Python 3.12, minimap2, samtools, pod5, pysam, pandas, pyyaml, requests, Azure Blob SDK and pytest. Dorado 2.1.2 and `dna_r10.4.1_e8.2_400bps_fast@v5.2.0` are installed; the targets FASTA is checked against a pinned SHA-256 and 6,663-sequence expectation. `validate-image.sh` checks binaries, manifests, wrapper CLI and tests on the build VM. GPU behavior is checked separately on the acceptance VM.

## 0.0.17 acceptance record

Reported GPU: NVIDIA A10-12Q, 12,288 MiB, driver 570.237. Dataset `poresippr-dataset2/20260421_MIN`: 23 POD5 files, 9,565,788,680 bytes; waves 3,7,2,5,6. Scheduler completed with `once-complete`, five batches, 23 processed POD5, duplicate processing prevented, 36 retained FASTQ fragments, 58 result CSVs, 15 BAM and 15 BAI files. Mapping recorded 3,632 result rows, 101,987 mapped reads and 40/40 per-iteration assertions passed. Wrapper version 0.0.17, repository commit and target hash match the release inputs. This is direct scheduler GPU acceptance, not a full GUI/API/Batch integration test.

## Risks and hygiene

The task definition has a 16-hour wall clock limit and 24-hour SAS expiry; test long-running behavior separately. Packer debug logs may contain signed Azure request URLs; keep originals restricted and redact before distribution. Review `curl --insecure`, Micromamba `ssl_verify: false`, and the NVIDIA installer digest check as security/reproducibility work. This documentation does not change the scripts or relax validation.
