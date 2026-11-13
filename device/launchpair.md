# Per-app launch pairing and bootpair confidence intervals

Host-only, standard-library Python 3.8+ analysis. No Android execution, ADB,
boot control, tracing, builds or test runner. This implementation was written
without running it or tests. It does not change the device launcher or existing
analysis script. Output is CSV on stdout and method/error information on stderr.

## Provenance and inspected real format

The requested historical `mcbench/device/launchpair.py` was absent locally;
the actual existing method was in `mcbench/launchpair.py`, SHA-256
`eb5b9e4d5dc7797883ca12ba22799730a95271f3f02a668c14a0f0a0131b866e`.
That source pools per-app arm medians, reports a cross-app geometric mean ratio,
and bootstraps **apps**. Its aggregate interval is not a per-app interval or a
boot-level interval. This release extension retains median timings/geometric
ratios, but uses explicit fixed **bootpairs** as the resampling unit, for both
per-app estimates and the distinct overall summary. The resulting estimates
are not numerically identical to the old pooled-arm summary; scopes are explicit.

Only existing samples were read for format inspection, not analyzed as new
results: `logs/mthp/mr-{on1,off1}.alive`, `.relaunch`, and an existing `.snap`.
Real rows have these shapes (grammar, not fabricated result records):

```text
APP <ordinal> <package> pid=<pid-or-none> am_ms=<elapsed> LaunchState:<state> TotalTime:<ms> WaitTime:<ms> ...
RE <package> same_pid=<0-or-1> LaunchState:<state> TotalTime:<ms> WaitTime:<ms>
```

Some actual inspected rows instead have `LaunchState:UNKNOWN` and `WaitTime`
**without TotalTime**. Never substitute WaitTime/am_ms for the missing TotalTime,
fill zeroes, or silently omit these attempts. The old workflow only relaunched
part of the app list; a non-relaunched app has no hot measurement. Existing snap
headers have uptime/kernel labels but no sufficient fixed-pair/fresh-boot and
regime evidence. Old on/off labels also describe a different historical experiment;
they must not become release A/B evidence by renaming tags.

No automatic legacy parser/import is supplied. The following small normalized
CSV can map actual source rows, explicit failures and missing scheduled attempts.
No new receipt, metadata generator or parser-selftest framework is introduced.

## Invocation and one observation schema

The human/main may later run on the host (not run for this handoff):

```sh
python3 device/launchpair.py observations.csv --apps apps.txt \
  --draws 4000 --seed 1 > launchpair.csv 2> launchpair.stderr
```

`apps.txt` is the **expected complete roster**, one package per line, unique,
no header/comments; do not derive it solely from successful launches. The tool
reports all roster apps, including absent ones. It does not assume there are 50;
retain/report the actual expected count and explain any departure from the
planned 50-app workflow. Input apps outside the roster are errors.

CSV header, exact order:

```csv
pair_id,boot_id,arm,regime,app,phase,attempt,launch_state,total_ms,status,metadata_status,boot_metadata,source_ref
```

| Field | Definition / mapping |
| --- | --- |
| pair_id | Preassigned fixed A/B bootpair identifier. Never choose a partner after seeing app timings/failures. Empty means unpaired historical input, excluded from estimates. |
| boot_id | Stable local alias for ONE real/planned boot, not a serial/IMEI/device ID. Multiple launches are not different boots. A never-started slot must remain unverified/missing, not become a documented fresh boot. |
| arm | Explicit A or B. For current method F, A is the series and B the matched stock baseline. Do not infer this from arbitrary historical names. |
| regime | Separate governor condition, normally fixed or default. The same physical boot may occur once per regime but must retain the same arm/pair identity. No mixing regimes. |
| app | Exact package, from APP/RE and the expected roster. |
| phase | alive for APP/initial launch; relaunch for RE. |
| attempt | Positive integer identifying the scheduled attempt within boot/regime/app/phase. Duplicate attempts are errors, not extra samples. |
| launch_state | Exact observed LaunchState, normally COLD/HOT/WARM/UNKNOWN; empty only if unavailable. Do not promote WARM/UNKNOWN to HOT. |
| total_ms | Actual integer TotalTime milliseconds, never am_ms/WaitTime. Empty if unavailable. A true observed 0 may be retained but cannot enter a log-ratio; no epsilon is added. |
| status | ok = observed launch timing with known outcome; failed = launch/tool failure; unavailable = missing or unusable measurement (including UNKNOWN without TotalTime); missing = planned attempt with no observation. A missing placeholder has empty state/time/source_ref. |
| metadata_status | documented only when the operator has existing boot metadata establishing this slot's identity, arm/pair and regime/conditions. Otherwise unverified; never infer documented from a filename/version or successful process exit. |
| boot_metadata | Existing boot metadata/manifest file reference, relative to observations.csv or absolute. Required/readable for documented. May be empty for unverified. It must actually describe that boot, not merely be a random existing file. |
| source_ref | Traceable raw log file and line/record reference for each observed attempt, unique across input. Empty for missing placeholders. This is a provenance label, not code to execute; the tool does not invent/parse source records. |

Within a boot/regime, arm/pair/metadata declarations must agree across all rows.
The same boot cannot be reused in another pair/arm. A pair/regime has at most
one boot on each arm. Source-row reuse, malformed rows, inconsistent roles,
unknown statuses, unavailable documented metadata and duplicate attempts cause
exit 2, not silent row loss. Raw files/metadata remain external read-only inputs.
The script only checks the metadata reference exists/is readable and that the
CSV declarations agree; it **does not attest its contents**. Human correlation
with actual boot metadata is mandatory. `documented` is not a convenience flag
for making old data produce an interval.

### Fixed ABBAAB map and missingness

Before examining outcomes, method F's six fresh boot slots can be paired as:

| Pair | A slot | B slot |
| --- | --- | --- |
| 1 | 1 | 2 |
| 2 | 4 | 3 |
| 3 | 5 | 6 |

Use these same pairs for every app/category/regime. CSV pairing is explicit;
the script does not prove chronology or manufacture slots from on/off tags.
Metadata must establish genuinely different fresh boots and the matched-image,
180-second, screen/unlock/non-Doze, temperature and governor conditions. Keep
that evidence; a version string or a later label alone is insufficient.

Include planned missing boots/attempts as `missing` rows, unverified where no
boot occurred. Retain failed/unavailable source attempts with their real status.
Include each planned boot/app/phase even if a whole file is absent. Otherwise a
missing entire bootpair cannot be discovered from observation rows alone. The
roster detects apps missing on both arms of known pairs, but cannot invent a
never-declared bootpair. A partially observed pair never borrows another boot.

Historical rows without boot metadata remain `unverified`; leave pair_id empty
if no authentic pair assignment survives. They contribute diagnostics/counts,
not a formal point estimate/CI. Merely adding an old `.snap` path and relabeling
it documented is not valid provenance. With fewer than three actually documented
usable bootpairs, **ratio and both CI bounds are N/A** (counts remain visible).
An undocumented planned pair remains in planned/unverified counts, never replaced.

## Estimator and interval (unit = bootpair)

For app i, category c and bootpair p, take the median of successful, strictly
positive TotalTime values on **each individual boot**, then

```text
d[i,c,p] = log(median_A[i,c,p]) - log(median_B[i,c,p])
per_app_ratio = exp(mean_p(d[i,c,p]))
```

Each pair has equal weight, regardless of launch count. Failure/unavailability
is retained in counts; the conditional timing estimate does not imply success
or survival of every attempt. A cell with no successful category-appropriate
positive timing is unavailable for that pair. `ok_attempts` includes observed
ok rows even if their state differs or their time is 0; those are separately
counted, not silently eligible for logarithms.

Categories match the original intended distinctions:

- cold: alive / COLD;
- hot: relaunch / HOT;
- warm: relaunch / WARM;
- hot_warm: relaunch / HOT or WARM, **explicitly mixed**, not a hot-only substitute.

If A is HOT but B is WARM, the hot-only pair is unavailable, even if hot_warm is
estimable. A cold relaunch does not get moved into an initial cold-launch cell.
Statistics use >=3 documented, distinct bootpairs with values on both arms.

Bootstrap draws n bootpairs **with replacement**, n = usable pairs for that
estimate, and recompute the mean log-ratio. The output exponentiates that mean
and the 2.5/97.5 percentiles of 4000 bootstrap mean log-ratios (default seed 1).
Percentiles use linear interpolation at `(B-1)*q`. Sorting is deterministic;
the RNG is reset to the chosen seed for each estimate. Apps/launches/switches
are never the bootstrap population. Three pairs provide a coarse interval;
this is not proof of a meaningful speedup, normality or safety. Identical pair
ratios can legitimately yield a degenerate interval; more resampling draws do
not create more independent boots. Ratio <1 means lower conditional TotalTime
for A, not a survivability or universal performance claim.

### Overall summary is not per-app evidence

`scope=overall` uses a **fixed complete app panel**: apps with a valid pair
value in every metadata-eligible pair for that regime/category. It computes the
mean app log-ratio within each pair, then averages those pair means. Bootstrap
resamples whole pair means, keeping all apps in each pair together. It is the
geometric mean of the panel's paired ratios, not the old pooled-arm medians and
not an interval obtained by treating 50 apps as 50 independent boots.

All roster apps still get per-app rows and diagnostics. `apps_expected` vs
`apps_used` exposes the panel restriction; the overall failure/missing counters
cover the **whole roster**, not only the selected panel. An empty panel yields
N/A. Do not quote an aggregate interval as any individual app's interval, infer
that unpaired apps improved, or call a restricted panel a complete 50-app result.

## Output and error semantics

One CSV row per regime/app/category plus a distinctly labeled overall row:

- planned_pairs, metadata_eligible_pairs, metadata_unverified_pairs: declared
  pair counts; eligible requires both arms documented, not merely a valid time.
- usable_pairs: independent documented pairs contributing timings (or panel pair
  means for overall). apps_expected/apps_used expose app participation.
- ok_attempts, failed_attempts, unavailable_attempts, missing_rows: raw status
  counts within the category's source phase; same-phase counts repeat across
  category rows and must not be summed across cold/hot/warm/hot_warm.
- state_mismatch and zero_time_attempts: additional overlapping diagnostics.
- absent_boot_cells: no row at all for an expected arm/app/phase in a declared
  pair; explicit missing placeholders are missing_rows, not absent cells.
- missing_a_cells, missing_b_cells, missing_both_cells: pair/app cells without a
  usable time, including failures, missing rows and inappropriate states. These
  counts overlap. Overall sums app-cell diagnostics across the whole roster;
  do not confuse app-cells with the independent usable_pairs sample size.
- unpaired_rows: phase rows without pair_id; retained but never resampled.
- ratio_a_over_b, ci95_low/high: all N/A below three usable documented pairs.
  estimate_status=estimated is an analysis result, **not** a correctness PASS,
  device attestation, complete-roster claim or acceptance of a historical series.

Exit 0 means analysis/output completed, including a report that is entirely N/A.
Read counts and stderr; it does not mean measurements passed. Invalid input or
I/O errors return 2. A partial stdout after an error is not a finished report.
No file is created by the analyzer itself; choose new stdout/stderr destinations.

## Additional collection needed before a formal F5 result

The inspected history cannot by itself establish the release cold/hot-per-app
bootpairs. Main/operator needs the expected 50-app roster, a complete relaunch
schedule (not just the old subset), genuine fresh-boot aliases/slots/fixed pairs,
A/B manifests and matched local changes, governor regime and environment checks,
source log references, explicit failed/missing launch outcomes, and valid
LaunchState/TotalTime for each scheduled phase. Preserve launch exit/errors and
survival separately; a PID still existing is not proof a timed launch succeeded.
No timing, boot metadata or CI is synthesized to fill those gaps.

Completed in this handoff: read-only source/real-row format inspection, AST
syntax parsing without importing/executing the analyzer, ownership/permission
readback, and SHA-256 confirmation that the original script is unchanged.
Pending: main's CSV-to-raw/provenance audit, then separately authorized runtime
checks of missing/failed/state-mismatch/zero-time handling, repeated-boot
rejection, <=2-pair N/A and deterministic bootpair bootstrap arithmetic.
**No analyzer execution, test, build, device action or commit was performed.**
