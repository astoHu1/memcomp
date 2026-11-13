# Data to collect on the phone

This is the checklist for the A/B comparison described in
[README.md](README.md). It lists what to record, not how to parse it.
The phone procedure is separate from the VM comparison in the
whitepaper and does not reuse its schedule, disks or pressure setup.

## Builds, boots and conditions

- Arm A is ACK with the memcomp series and `MEMCOMP=y`. Arm B is the same
  ACK base without the series, using stock zram. Both use the same
  toolchain, the same local boot changes and the same unrelated
  configuration. Record both source heads, image hashes, configurations,
  module manifests and the toolchain. [BUILD.md](BUILD.md) lists the
  intended differences.
- Boot six times, in the order **ABBAAB**: A in slots 1, 4 and 5, B in
  slots 2, 3 and 6. Every boot is fresh. Record the slot, the arm and the
  governor regime with every sample. The scripts do not boot the phone
  or check that a boot is fresh.
- Measure only after at least 180 seconds of uptime, with the screen on,
  unlocked and out of Doze, and with the relevant temperature sensors
  **below 45 °C**. Record the sensor names, units and values. If a
  condition fails, keep the sample and the reason; do not pick a
  replacement silently.
- Measure each boot twice. First set both CPU policies to performance
  and wait until `cpuinfo_cur_freq` equals `scaling_max_freq`. Then
  restore the original governors and measure again. Save the original
  governors and check that they were restored. Report the two regimes
  separately. The tested phone has policy0 and policy6; check the
  layout on any other phone.
- Use CPU **2**, `NPAGES=32768 SINGLE=10000 ROUNDS=9` and the same corpus
  on both arms. mcbench assumes 4 KiB pages. Keep all nine rounds; round
  0 is a warmup and is left out of the summaries. Inner rounds are not
  independent boots: report per-boot medians and the spread between
  boots, with counts, and do not pool all BENCH rows.

## Microbenchmark and applications

- Keep every mcbench **BENCH** line, the requested, swapped and present
  page counts, the pageout, sequential, random and single-page timings,
  and every warning and FAIL line. Keep the exit status of mcbench apart
  from that of the privacy filter; a missing status means unknown.
- mcbench can print `RESULT: FAIL` and still exit 0, and it does not
  check every system call. Exit 0 is therefore not a pass, and a
  returned PAGEOUT call does not show that pages went cold. If no page
  was seen leaving memory, cold-page latency is N/A. Do not filter
  same-filled pages or otherwise edit the corpus.
- Run `refault-perf -p PID -P` on **Chrome and QQ in both regimes**. Keep
  the full stdout and stderr, the exit status and the target package and
  PID. Without `-P` it reads only resident pages.
- Launch **50 real applications** with the same list, order and seed on
  both arms, using the existing launch and relaunch workflow. Keep the
  `APP` and `RE` rows: LaunchState, TotalTime, the original and current
  PID, same_pid, the alive, dead and missing counts, and MemAvailable at
  the end. Keep failed launches. The synthetic app_switch workers are
  not a substitute for these applications.
- Analyze launches on the host with
  `device/launchpair.py observations.csv --apps apps.txt --draws 4000 --seed 1`.
  [launchpair.md](launchpair.md) defines the CSV header and the mapping
  from `APP` and `RE` rows. Give the complete roster of expected
  applications, not only the ones that launched. Declare the boot pairs,
  arms, regimes and raw sources, and keep missing boots, applications
  and attempts as rows. Never fill a missing TotalTime from WaitTime or
  zero. Confidence intervals resample **boot pairs**, not applications or
  launches. With fewer than three usable pairs the estimate is N/A and
  the counts are still reported.
- Report **pgmajfault**, memory PSI some and full, and the final
  MemAvailable with the launch and survival results. System and kernel
  CPU time are descriptive only. Do not claim a speedup from one boot,
  one inner round or an interval that hides lost pairs.

## Counters and their units

Before and after each run, keep `/proc/{uptime,meminfo,vmstat,swaps,stat,diskstats}`,
memory, CPU and I/O PSI, the governor with current and maximum
frequency, and `/sys/kernel/mm/memcomp/{stat,compressor,events}` where
readable. Keep stdout, stderr and the exit status of each read. A file
that cannot be read is N/A, not zero. The snapshots are taken one file
after another and cover the whole system.

| Quantity | Definition and limits |
| --- | --- |
| pgmajfault, allocstall | vmstat names and deltas as printed; do not add a total to its per-zone parts. |
| PSI | some and full avg10/60/300 and the cumulative total in microseconds; give the delta, the duration and whether it is system-wide or per cgroup. |
| Swap and disk writes | pswpin and pswpout count native pages. Diskstats sectors are 512 bytes. Name the device, avoid counting stacked devices twice, and note that background writes are not all backend payload. |
| L | First memcomp `stat` field: logical bytes stored, including entries on swap. |
| C | Second field: compressed payload bytes, including entries on swap. It is not pool RAM. |
| P | Third field: allocator pool bytes in RAM. It excludes metadata and is not the RAM saved. |
| Ratios | Keep L, C, P and the compressor. L/C is the payload ratio and L/P the logical-to-pool ratio; a zero denominator is N/A. Neither is a whole-system saving. |
| Metadata | The exact total is N/A. Slab and SUnreclaim are not backend metadata. |
| Writeback CPU | Writeback CPU inside shared kworkers is N/A; label any CPU figure by its scope and units. |
| events | Report the fields that exist, with their definitions; absent fields are N/A. A reset, a reboot or a negative delta makes the pair invalid; do not clamp it to zero. |

The wrapper's snapshots bracket the whole invocation, not only the timed
reads. For application runs, take your own snapshots around them. Do not
attribute whole-run counters to a single read or launch.

## Privacy and tracing

The wrapper does not collect the full kernel command line. Only
`memcomp.*` arguments go into the metadata. Serial numbers, IMEI, ADB
identifiers, `ro.boot.*` properties and logcat are never collected. The
wrapper drops mcbench's command-line line before anything is stored.
Review logs and paths before publishing, and de-identify any command
line you collect yourself.

Do not read DDR `clock_measure` and do not trace all `sched_switch`
events. If you add kprobes, use your own group names, record them and
remove only those; never clear `kprobe_events`, remove vendor probes or
toggle the global `tracing_on`. Restore only the governor, screen and
probe settings you changed; the wrapper changes none of them.

## Still to confirm before measuring

- both image manifests and the shared local changes;
- the installed paths and hashes of mcbench, the corpus and refault-perf;
- that CPU 2 is online and the page size is 4 KiB;
- the clock and thermal sensors to read;
- the 50-application list and the launch and relaunch inputs;
- what the existing helper scripts trace and clean up. The launch helper
  starts kswapd-perf and changes the screen timeout, and the larger
  profiling suites are not drop-in replacements for these steps.

Do not mix in latency or energy results from other kernels. The host
builds are described in [BUILD.md](BUILD.md). No phone result is claimed
here.
