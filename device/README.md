# Phone test package

This package compares memcomp with stock zram on an arm64 phone running
ACK android16-6.12. It is separate from the VM comparison in the
whitepaper and uses its own schedule. It contains instructions, a
wrapper script, an analysis script and AArch64 tools built on the host;
it contains no phone results. The variables on a phone could not be
controlled, so the whitepaper reports KVM results only.

Everything here was built on the host (see [BUILD.md](BUILD.md) and the
`BUILD-*.txt` records). Every phone action is for the person
running the test: the scripts never switch to root, install or boot an
image, or change system settings.

[data-needed.md](data-needed.md) lists everything to record.

## Images and boot order

- **Arm A:** the ACK series with `MEMCOMP=y` and zram disabled.
- **Arm B:** the same ACK base without the series, with stock zram built
  in. The existing userspace setup configures zram. Its default
  compressor is lzo-rle, so select zstd explicitly and check that the
  selection took effect.
- Both arms share the ACK base, the toolchain, the local boot changes
  and all unrelated configuration. Keep the source heads, image hashes,
  configurations, toolchain identity and module manifests of both. A
  version string alone does not show where an image came from.
- Boot six times in the order **ABBAAB**: A in slots 1, 4 and 5, B in
  slots 2, 3 and 6. Every boot is fresh. The scripts do not reboot the
  phone or check that a boot is fresh.
- Wait for at least **180 seconds** of uptime. Keep the screen on,
  unlocked and out of Doze. Before each measurement, check the relevant
  temperature sensors: **below 45 °C**. Record the sensor names, units
  and values. If a condition fails, keep the sample marked invalid with
  the reason instead of replacing it.
- On each boot, measure once with both CPU policies at **performance**
  and once with the **original governors**. Restore exactly the saved
  governors. Report the two regimes separately.

`run.sh --confirm-env` records that the operator has checked these
conditions and the matched builds; it does not check them itself. The
script checks the uptime and that the slot and arm agree with ABBAAB. It
cannot see whether the boot is fresh, the screen state, the temperature
or which image is installed, so record those in the notes. The
benchmark runs on CPU 2. Check that CPU 2 is online and allowed; do not
substitute another CPU.

## Programs and where they come from

The table names existing programs and scripts that the procedure reuses.
Entries marked *maintainer-local* are the maintainer's own phone scripts.
They are not part of this package and are named so the procedure can be
followed.

| Program | How it is used |
| --- | --- |
| `src/bench_init.c`, built as static `mcbench` | Run as an ordinary program with the same corpus on both arms. `run.sh --run-w1` runs it. |
| maintainer-local `ab.sh - 9` | Source of the invocation `CPU=2 NPAGES=32768 SINGLE=10000 ROUNDS=9 ./mcbench "$CORPUS"`. Use the invocation, not the script's own governor writes or its one-second settling. |
| maintainer-local `rfab.sh` | Source of the `refault-perf -p PID -P` calls for Chrome and QQ. Do not use its filtered summary or its extra CPU 7 and profiling runs. Confirm the installed path of refault-perf. |
| maintainer-local `alivefix.sh <seed> <tag>` | The existing 50-application launch and survival workflow. It needs the package list and `kswapd-perf`, changes the screen timeout and does not clean up on every error, so review it and restore the settings it changes. |
| maintainer-local `mthprun.sh <tag> <seed>` | Source of the alive and relaunch log format only. Do not run its 600-second mTHP, zscan and profiling suite. |
| `device/launchpair.py` | Host-side analysis of launch times by boot pair; see [launchpair.md](launchpair.md). An older maintainer-local `launchpair.py` pools samples differently and is not used. |
| `../bench/compare/{refault.c,shmem.c,app_switch.c,common.h}` | Optional extra tools from the VM comparison. Their synthetic workers do not replace the 50 real applications. |
| `tools/testing/selftests/mm/memcomp.c` in the ACK series | The correctness selftest, built as `memcomp_selftest`. This package never runs it or its swapoff test. |

Keep `bench/compare/` next to `device/` if you build the optional tools;
SHA256SUMS includes their sources, header and Makefile. Running the
supplied binaries needs no compiler, source or Python on the phone. Never
run `guest` or mcbench as PID 1.

### Building the tools on the host

From the repository root, with the host's `sh`:

```sh
sh device/build.sh --static --arch aarch64 --cc aarch64-linux-gnu-gcc
```

```sh
sh device/build.sh --static --arch aarch64 --target compare
```

The first command builds mcbench from `device/src/bench_init.c` into
`device/bin/`. A different source can be given with `--mcbench-source`;
its file name and hash, not its directory, go into BUILD-mcbench.txt.
The second builds refault, shmem and app_switch from `../bench/compare`.
Both are serial static AArch64 builds; neither builds a kernel or runs
anything. BUILD-mcbench.txt and BUILD-compare.txt record the input
hashes, compiler, options and output hashes. Refresh SHA256SUMS after
any rebuild. mcbench assumes 4 KiB pages; check that the phone and the
corpus agree. Static linking does not by itself show that a binary runs
under Android's permissions and SELinux policy.

The selftest is built from the kernel tree, against installed ACK
headers:

```sh
make -C "$KERNEL/tools/testing/selftests/mm" OUTPUT="$SELFTEST_OUT" \
  ARCH=arm64 CC=aarch64-linux-gnu-gcc KHDR_INCLUDES="-isystem $HEADERS/include" \
  EXTRA_CFLAGS="-Werror -g0" LDFLAGS=-static "$SELFTEST_OUT/memcomp_selftest"
```

`KERNEL`, `HEADERS` and `SELFTEST_OUT` are absolute paths of your
choice; `SELFTEST_OUT` is a separate output directory.

## Microbenchmark

`run.sh` without arguments prints its help. `--collect --out /absolute/NEW-dir`
only reads system state. The parent directory must exist; the script
never reuses, deletes or overwrites an existing output. It needs only
`/system/bin/sh`, the usual Toybox utilities and `taskset`.

For each boot, set `ARM`, `SLOT`, `SOURCE_COMMIT`, `IMAGE_SHA256`,
`CORPUS`, `MCBENCH` and two new output directories, `OUT_FIXED` and
`OUT_DEFAULT`. `MCBENCH` defaults to `/data/local/tmp/mcbench`; the
binary in `device/bin/` can be used instead. The profile is **CPU=2,
NPAGES=32768, SINGLE=10000, ROUNDS=9**, which is 128 MiB with 4 KiB
pages. Round 0 is a warmup: its BENCH row is kept but left out of the
summaries. The following is run by hand:

```sh
# Screen, Doze, temperature, image manifests, corpus and CPU 2 checked.
# The tested phone has policy0 and policy6; check the layout on others.
(
  P=/sys/devices/system/cpu/cpufreq
  g0=$(cat "$P/policy0/scaling_governor") || exit 1
  g6=$(cat "$P/policy6/scaling_governor") || exit 1
  restore() {
    printf '%s\n' "$g0" > "$P/policy0/scaling_governor"
    printf '%s\n' "$g6" > "$P/policy6/scaling_governor"
  }
  trap restore 0
  trap 'exit 130' 2
  trap 'exit 143' 15
  printf '%s\n' performance > "$P/policy0/scaling_governor" || exit 1
  printf '%s\n' performance > "$P/policy6/scaling_governor" || exit 1
  for n in 0 6; do
    i=0
    while [ "$(cat "$P/policy$n/cpuinfo_cur_freq")" != "$(cat "$P/policy$n/scaling_max_freq")" ]; do
      [ "$i" -lt 30 ] || exit 1
      sleep 1; i=$((i + 1))
    done
  done
  /system/bin/sh ./run.sh --run-w1 --regime fixed --out "$OUT_FIXED" \
    --arm "$ARM" --boot-slot "$SLOT" --source-commit "$SOURCE_COMMIT" \
    --image-sha256 "$IMAGE_SHA256" --confirm-env --corpus "$CORPUS" --mcbench "$MCBENCH"
)
# Check that the governors were restored and recheck the conditions, then:
/system/bin/sh ./run.sh --run-w1 --regime default --out "$OUT_DEFAULT" \
  --arm "$ARM" --boot-slot "$SLOT" --source-commit "$SOURCE_COMMIT" \
  --image-sha256 "$IMAGE_SHA256" --confirm-env --corpus "$CORPUS" --mcbench "$MCBENCH"
```

Write down the original governors outside the subshell as well. If the
shell is lost or killed, the trap does not run; restore the governors by
hand. The wrapper itself only reads policy0 and policy6. In fixed mode it
requires the performance governor and `cpuinfo_cur_freq` equal to
`scaling_max_freq`; in default mode it accepts whatever governor is set
and changes nothing. Missing clocks or a failed CPU affinity are
reported as unavailable. Recheck the temperature before the application
runs as well.

## Real applications and the 50-application run

Run the installed `refault-perf` with **`-P`** on Chrome and QQ, once in
each governor regime, and keep the full stdout, stderr and exit status.
With `$APP_OUT` a new directory and the current PIDs checked by hand:

```sh
"$REFPERF" -p "$CHROME_PID" -P > "$APP_OUT/chrome.stdout" 2> "$APP_OUT/chrome.stderr"
r=$?; printf '%s\n' "$r" > "$APP_OUT/chrome.exit"
"$REFPERF" -p "$QQ_PID" -P > "$APP_OUT/qq.stdout" 2> "$APP_OUT/qq.stderr"
r=$?; printf '%s\n' "$r" > "$APP_OUT/qq.exit"
```

Without `-P` the tool reads only resident pages. Set and restore the
performance governor for this block in the same way as above.

For the 50 applications, use the same package list, launch order and
seed on both arms, with the existing launch and relaunch workflow. Keep
the APP and RE rows with LaunchState, TotalTime, wait time and same_pid,
the survival counts, pgmajfault, PSI and MemAvailable at the end. Keep
failed and unavailable attempts. The wrapper does not launch
applications, change screen settings or run profilers. Analyze on the
host, from the repository root:

```sh
python3 device/launchpair.py observations.csv --apps apps.txt \
  --draws 4000 --seed 1 > launchpair.csv 2> launchpair.stderr
```

[launchpair.md](launchpair.md) defines the CSV header and the expected
roster: one package per line, including applications that failed.
Declare the ABBAAB boot pairs, the two regimes, the raw sources and the
per-boot notes, and keep missing boots, applications and attempts as
rows. Per-application ratios and 95% intervals need at least **three
usable boot pairs** and resample whole boot pairs, never launches or
applications. With fewer pairs the result is N/A and the diagnostics
are kept. The overall row summarizes the complete panel and is not any
single application's interval.

## Privacy and tracing

Only `memcomp.*` boot arguments go into the metadata. Serial numbers,
IMEI, ADB identifiers, `ro.boot.*` and other properties, and logcat are
never collected, and the full command line is not recorded. mcbench
prints `== /proc/cmdline` followed by the command line; the wrapper drops
that line before anything is stored and notes that it did. The supplied
binary must print in this format. Review the output before publishing,
and de-identify any command line you collect yourself. Leave `KNOB`,
`KNOBVALS` and `HUGE` unset; `/knob_toggle.ko` must not exist.

The output contains the metadata, read-only snapshots before and after,
the filtered workload stdout, the unchanged stderr, the workload's own
exit status in `workload.exit` and the filter's exit status. A missing
final status means the run is incomplete. mcbench can print
`RESULT: FAIL` and still exit 0, so exit 0 is not a pass and does not
show that pages went cold. Review the BENCH rows, the counts and errors
and the pagemap observations; with no page seen leaving memory, cold
latency is N/A.

**Do not read DDR `clock_measure` during measurements, and do not trace
all `sched_switch` events.** The package enables neither. If you add
kprobes, use your own group and event names, record them and remove only
those. Never clear `kprobe_events`, remove vendor probes or change the
global `tracing_on`. Keep profiled runs apart from the comparison runs.

The memcomp boot options are `memcomp.*`; the sysfs directory is
`/sys/kernel/mm/memcomp/`. Do not combine these results with older
device or energy results from a different kernel.

## Supplied files

`src/bench_init.c` and `src/mkcorpus.py` are the existing mcbench source
and corpus generator, unchanged. `build.sh` uses that source by default.
The static AArch64 mcbench build passed with `-Wall -Wextra -Werror`.
`SHA256SUMS` covers the sources and the supplied executables. The corpus
is generated separately and not included; use the same corpus hash on
both arms.

The optional refault, shmem and app_switch binaries come with their
sources in `../bench/compare` and are not used in place of mcbench. They
need `/dev/console` and a successful `tcdrain()` before timing, even with
stdout redirected; they were written for the VM guests and may not work
on every Android setup.

memcomp_selftest was built from ACK series v1 (tree
`4b45de7370f4d25b667776d02e7675c774dd1d08`); BUILD-selftest.txt records
its inputs, command and hash. The kernel tree holds its source. Do not
run its swapoff test on a phone.

mcbench still contains code for running as init. Never install or start
it as init. The wrapper runs it as an ordinary program and refuses to
start if `/knob_toggle.ko` exists, because mcbench would otherwise try to
load that module.
