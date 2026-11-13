#!/system/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Method F: manual one-boot entry; never boot or change policy here.
set -f
umask 077
usage()
{
	cat <<'USAGE'
Manual Android entry (no arguments prints this help):
  run.sh --collect --out /absolute/NEW-directory
  run.sh --run-w1 --out /absolute/NEW-directory --arm A|B --boot-slot 1..6 \
    --regime fixed|default --source-commit HEX40 --image-sha256 HEX64 \
    --confirm-env --corpus /absolute/corpus [--mcbench /absolute/mcbench]

Method F: user boots ABBAAB (three fresh boots per arm), waits >=180 s;
keeps screen awake/unlocked, non-Doze, temperature <45 C before every round.
--confirm-env asserts those checks AND matching ACK/toolchain/local changes.
It is not image attestation. A=series MEMCOMP=y; B=stock without series.
W1 reuses ab.sh - 9's no-knob mcbench command, with CPU=2 NPAGES=32768
SINGLE=10000 ROUNDS=9. mcbench defaults to /data/local/tmp/mcbench.
fixed: user first sets both policies to performance and waits for real peak;
default: user first restores both ORIGINAL governors. Wrapper only reads them.
No automatic governor/cgroup/sysfs/swap/tracing/app/root/boot operations.
For refault-perf -P Chrome/QQ and 50-app alive/relaunch, see README.md.
Legacy --pages/--run-w2 are not this method-F entry; no synthetic W2 substitute.
USAGE
}
die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
mode=help
out= arm= slot= regime= source_commit= image_sha= corpus=
mcbench=/data/local/tmp/mcbench
confirmed=no
while [ "$#" -gt 0 ]; do
	case "$1" in
	--help|-h) usage; exit 0 ;;
	--collect|--run-w1)
		[ "$mode" = help ] || die 'choose one mode'
		mode=$1; shift; continue ;;
	--confirm-env) confirmed=yes; shift; continue ;;
	--out|--arm|--boot-slot|--regime|--source-commit|--image-sha256|--corpus|--mcbench)
		[ "$#" -ge 2 ] || die "$1 needs a value" ;;
	*) die "unknown option: $1" ;;
	esac
	case "$1" in
	--out) out=$2 ;; --arm) arm=$2 ;; --boot-slot) slot=$2 ;;
	--regime) regime=$2 ;; --source-commit) source_commit=$2 ;;
	--image-sha256) image_sha=$2 ;; --corpus) corpus=$2 ;; --mcbench) mcbench=$2 ;;
	esac
	shift 2
done
if [ "$mode" = help ]; then usage; exit 0; fi
case "$out" in /*) ;; *) die '--out must be an absolute new leaf' ;; esac
case "$out" in /|*/|*/.|*/..) die 'invalid output leaf' ;; esac
if [ "$mode" = --run-w1 ]; then
	[ "$confirmed" = yes ] || die 'human environmental/provenance confirmation required'
	case "$slot:$arm" in 1:A|2:B|3:B|4:A|5:A|6:B) ;; *) die 'slot/arm must follow ABBAAB' ;; esac
	case "$regime" in fixed|default) ;; *) die 'choose fixed or default governor regime' ;; esac
	[ "${#source_commit}" -eq 40 ] || die 'source-commit needs 40 hex digits'
	[ "${#image_sha}" -eq 64 ] || die 'image-sha256 needs 64 hex digits'
	case "$source_commit$image_sha" in *[!0-9a-fA-F]*) die 'invalid provenance digest' ;; esac
	case "$corpus" in /*) ;; *) die 'absolute existing corpus required' ;; esac
	case "$mcbench" in /*) ;; *) die 'absolute existing mcbench required' ;; esac
	[ -r "$corpus" ] && [ -f "$corpus" ] || die 'corpus unavailable'
	[ -x "$mcbench" ] && [ -f "$mcbench" ] || die 'mcbench unavailable'
	# The inspected legacy source tries finit_module even outside PID1 mode.
	[ ! -e /knob_toggle.ko ] || die 'legacy auto-load path /knob_toggle.ko must be absent'
	command -v taskset >/dev/null 2>&1 || die 'taskset required to enforce CPU 2 affinity'
	IFS=' ' read -r uptime_seconds uptime_idle < /proc/uptime || die 'cannot read uptime'
	uptime_seconds=${uptime_seconds%%.*}
	case "$uptime_seconds" in ''|*[!0-9]*) die 'invalid uptime' ;; esac
	[ "$uptime_seconds" -ge 180 ] || die 'human must wait until uptime >=180 s'
	if [ "$regime" = fixed ]; then
		for policy in 0 6; do
			p=/sys/devices/system/cpu/cpufreq/policy$policy
			g=$(cat "$p/scaling_governor") || die 'policy unavailable; do not guess a replacement'
			cur=$(cat "$p/cpuinfo_cur_freq") || die 'actual frequency unavailable'
			max=$(cat "$p/scaling_max_freq") || die 'maximum frequency unavailable'
			[ "$g" = performance ] && [ "$cur" = "$max" ] ||
				die 'human must set performance and wait for BOTH actual clocks to reach maximum'
		done
	fi
fi
# Exclusive new directory, never reuse/delete another result.
mkdir "$out" || die 'output must be NEW with an existing parent'
set -C
printf '%s\n' 'method=plan-v4.7-F' \
	'plan_sha256=1c28ee1eca21d3291d03555f1581dbbf773cb276820e46bca5f13ff41d424a81' \
	"mode=$mode" "arm=${arm:-N/A}" "boot_slot=${slot:-N/A}" \
	"regime=${regime:-N/A}" "source_commit=${source_commit:-N/A}" \
	"image_sha256=${image_sha:-N/A}" "operator_confirmed=$confirmed" \
	'provenance=operator assertion; compare both build manifests separately' \
	'full_cmdline_collection=disabled' > "$out/metadata.txt" || die 'metadata write failed'
bootargs()
{
	printf '%s\n' 'full_cmdline_collection=disabled' || return 1
	IFS= read -r cmdline < /proc/cmdline || return 1
	for arg in $cmdline; do
		case "$arg" in
		*[sS][eE][rR][iI][aA][lL]*|*[iI][mM][eE][iI]*|*[aA][dD][bB]*|*[tT][oO][kK][eE][nN]*) ;;
		memcomp.*=*) printf 'bootarg=%s\n' "$arg" || return 1 ;;
		esac
	done
	return 0
}
capture()
{
	name=$1; shift
	"$@" > "$out/$phase/$name.stdout" 2> "$out/$phase/$name.stderr"
	rc=$?
	printf '%s\n' "$rc" > "$out/$phase/$name.exit" || die 'cannot save capture status'
}
snapshot()
{
	phase=$1
	mkdir "$out/$phase" || die 'snapshot directory failed'
	capture uname uname -s -r -v -m
	capture uptime cat /proc/uptime
	capture meminfo cat /proc/meminfo
	capture vmstat cat /proc/vmstat
	capture swaps cat /proc/swaps
	capture psi-memory cat /proc/pressure/memory
	capture psi-cpu cat /proc/pressure/cpu
	capture psi-io cat /proc/pressure/io
	capture diskstats cat /proc/diskstats
	capture proc-stat cat /proc/stat
	capture self-status cat /proc/self/status
	capture metadata bootargs
	capture memcomp-stat cat /sys/kernel/mm/memcomp/stat
	capture memcomp-compressor cat /sys/kernel/mm/memcomp/compressor
	capture memcomp-events cat /sys/kernel/mm/memcomp/events
	for policy in 0 6; do
		for node in scaling_governor cpuinfo_cur_freq scaling_max_freq; do
			capture "policy$policy-$node" cat "/sys/devices/system/cpu/cpufreq/policy$policy/$node"
		done
	done
}
snapshot before
[ "$mode" = --collect ] && exit 0
# The legacy tool probes this optional module even outside PID1 mode.
# Refuse that environment instead of silently loading a module.
[ ! -e /knob_toggle.ko ] || die 'remove the optional test module from this test environment first'
# Same ordinary-program invocation as ab.sh - 9, but no policy writes.
unset KNOB KNOBVALS HUGE COMPARE_PHASE_WRITE_FD COMPARE_PHASE_READ_FD
CPU=2 NPAGES=32768 SINGLE=10000 ROUNDS=9
export CPU NPAGES SINGLE ROUNDS
printf '%s\n' 'CPU=2 NPAGES=32768 SINGLE=10000 ROUNDS=9' \
	"mcbench=$mcbench" "corpus=$corpus" >> "$out/metadata.txt" || die 'metadata write failed'
# Privacy-only stream filter for the inspected bench_init.c dump() format.
# Never write the raw cmdline to storage; do not filter FAIL/BENCH/diagnostics.
# The tool status is saved INSIDE the producer, never taken from the pipeline.
(
	taskset -c 2 "$mcbench" "$corpus" 2> "$out/workload.stderr"
	tool_rc=$?
	printf '%s\n' "$tool_rc" > "$out/workload.exit" || exit 74
	exit "$tool_rc"
) | (
	omit=no
	while IFS= read -r line || [ -n "$line" ]; do
		if [ "$omit" = yes ]; then omit=no; continue; fi
		case "$line" in
		'== /proc/cmdline')
			printf '%s\n' '== /proc/cmdline [omitted; selected bootargs in metadata]' || exit 74
			omit=yes ;;
		*) printf '%s\n' "$line" || exit 74 ;;
		esac
	done
	[ "$omit" = no ] || exit 74
) > "$out/workload.stdout"
filter_rc=$?
printf '%s\n' "$filter_rc" > "$out/privacy-filter.exit" || die 'cannot save stream status'
IFS= read -r tool_rc < "$out/workload.exit" || die 'missing tool status: incomplete run'
snapshot after
# Legacy mcbench can print RESULT: FAIL and return 0. No PASS is inferred here.
[ "$filter_rc" -eq 0 ] || exit 74
case "$tool_rc" in ''|*[!0-9]*) die 'invalid tool status' ;; esac
exit "$tool_rc"
