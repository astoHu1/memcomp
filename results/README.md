# Comparison results

This directory holds the published part of the formal VM comparison
described in Section 3 of the whitepaper: 965 fresh guest boots, ten
backend groups, workloads W1 to W4. The whitepaper's figures and tables
are generated from the files in `formal-965/`.

## What ran

- Kernels: Linux 6.12.23 with the memcomp series at source tree
  `4a25cd2f2b176663e3b3323dbf307c8842ad1713`, and reference kernels with
  the same prerequisite layer and no series.
  `inputs/kernel-identities.json` gives each group's tree, `.config`
  hash and kernel image hash; the configurations are in
  `inputs/configs/`.
- Series v1 (tree `06b510f011f66cb14e8a5dd4d00d72a3a1a0a7a5`) differs from
  the measured tree only by the rename of the sysfs directory and by
  seven removed header-comment lines. The
  collection therefore read the earlier directory name, and the frozen
  tools and logs keep it. The copies under `bench/` read
  `/sys/kernel/mm/memcomp`.
- Host: one machine, WSL2 with nested KVM. Guests had two vCPUs, 1024 MiB
  of RAM and 2048 MiB of swap or zram backing. Collection ran in one
  segment, rounds 1 to 10, from 2026-11-11 03:39 UTC to 2026-11-13
  01:47 UTC.
- Input pages: anonymous memory captured from eight Android
  applications. The pages themselves are private and not included;
  commands name the corpus image only by a placeholder.

## Files

`formal-965/`

| File | Contents |
| --- | --- |
| `RESULT.json` | Overall state: 965 jobs, no extraction failure, environment check within noise. |
| `E4-decision-table.csv` | The paired decision for every compared metric, as produced by the rule. |
| `E4-presentation-table.csv` | The same rows with the presentation rule for counters memcomp accounts differently. The whitepaper quotes this table. |
| `paired-summary.csv`, `pairs.csv` | Candidate and reference medians per pair and per round. |
| `comparisons.json`, `slot-comparisons.json` | The list of comparisons and which slots feed each. |
| `w1-pageout.csv` | Per-boot W1 pageout timings, completion and memory observations. |
| `W4-source-split.*` | Per-round W4 read sources, read requests and cache hits. |
| `segments.json`, `segment-crosschecks.json` | The collection segment and the per-segment environment check. |

`inputs/` holds what the collection was started with: the matrix and
plan, the per-job identities, the build and freeze inputs, the launch
record, and the guest command of every job under `commands/`.

`w1-setup-memory/` measures the memory each backend allocates at setup,
before the W1 window starts, from the same guest logs. It is a later
derivation, not part of the frozen summary.

## What is not included

The raw guest records, 5796 files and 7.75 GB, are not in this
repository. `formal-965-files.sha256` lists each one with its SHA256 and
size, and `retained-large-files.json` gives the hash of that list and of
the three large intermediate CSVs (summary, per-guest and per-round).
They can be provided separately.

Earlier collections that used generated page contents, and smoke and
canary runs, are not comparison data and are not included.

## Paths

Local paths in these files are replaced by placeholders: `<repo>` for
the working repository, `<work>` for the scratch directory, `<corpus>`
for the corpus directory, `<plan>` for a planning file that is not
published and `<home>` for any other home-directory path. Only path
fields were changed; every value and hash is as recorded.
