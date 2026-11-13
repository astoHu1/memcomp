#!/system/bin/sh
# SPDX-License-Identifier: GPL-2.0-only
# Host entry: explicitly invoke with the host's sh. Never runs built tools.
set -f
umask 077
usage()
{
	cat <<'USAGE'
Host build (main/operator may build; no device access):
  sh device/build.sh --static --arch aarch64 [--cc aarch64-linux-gnu-gcc] \
    [--target mcbench --mcbench-source /absolute/bench_init.c]
  sh device/build.sh --static --arch aarch64 --target compare
Default target mcbench uses the packaged src/bench_init.c in ordinary program mode.
An explicit source may also be a path relative to the host's current directory.
compare builds only refault/shmem/app_switch from ../bench/compare, NOT guest.
Outputs: bin/ and BUILD-mcbench.txt or BUILD-compare.txt (private paths omitted).
Builds are serial. No arguments: help only. No kernel or tests run.
USAGE
}
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
digest()
{
	sum=$(sha256sum < "$1") || return 1
	printf '%s' "${sum%% *}"
}
label() { printf '%s' "${1##*/}" | tr -c 'A-Za-z0-9._+-' '_'; }
[ "$#" -gt 0 ] || { usage; exit 0; }
static=no
arch=
cc=aarch64-linux-gnu-gcc
target=mcbench
mcsource=
while [ "$#" -gt 0 ]; do
	case "$1" in
	--help|-h) usage; exit 0 ;;
	--static) static=yes; shift; continue ;;
	--arch|--cc|--target|--mcbench-source) [ "$#" -ge 2 ] || fail "$1 needs a value" ;;
	*) fail "unknown option: $1" ;;
	esac
	case "$1" in
	--arch) arch=$2 ;; --cc) cc=$2 ;; --target) target=$2 ;; --mcbench-source) mcsource=$2 ;;
	esac
	shift 2
done
[ "$static" = yes ] && [ "$arch" = aarch64 ] || fail 'explicit --static --arch aarch64 required'
case "$target" in mcbench|compare) ;; *) fail 'target must be mcbench or compare' ;; esac
script_dir=${0%/*}
[ "$script_dir" != "$0" ] || script_dir=.
script_dir=$(CDPATH= cd -P "$script_dir" && pwd) || fail 'cannot locate device directory'
[ -n "$mcsource" ] || mcsource=$script_dir/src/bench_init.c
case "$cc" in -*) fail 'invalid compiler' ;; esac
cc=$(command -v "$cc") || fail 'compiler unavailable'
case "$cc" in /*) ;; *) cc=$PWD/$cc ;; esac
[ -f "$cc" ] && [ -x "$cc" ] || fail 'compiler must be an executable file'
machine=$("$cc" -dumpmachine) || fail 'cannot query compiler target'
case "$machine" in aarch64*-linux-*) ;; *) fail 'compiler target must be aarch64 Linux' ;; esac
version=$("$cc" -dumpfullversion -dumpversion) || fail 'cannot query compiler version'
case "$version" in ''|*[!0-9.]*) fail 'invalid compiler version' ;; esac
compiler_sha=$(digest "$cc") || fail 'cannot hash compiler'
cflags='-O2 -g0 -std=gnu11 -Wall -Wextra -Werror'
if [ "$target" = mcbench ]; then
	[ -r "$mcsource" ] && [ -f "$mcsource" ] || fail 'supply existing bench_init.c with --mcbench-source'
	case "$mcsource" in /*) ;; *) mcsource=$PWD/$mcsource ;; esac
	source_dir=$(CDPATH= cd -P "${mcsource%/*}" && pwd) || fail 'cannot locate source directory'
	source_name=${mcsource##*/}
	source_label=external/$(label "$source_name")
	if [ "$source_dir/$source_name" = "$script_dir/src/bench_init.c" ]; then
		source_label=src/bench_init.c
	fi
	tools=mcbench
else
	source_dir=$(CDPATH= cd -P "$script_dir/../bench/compare" && pwd) || fail 'missing ../bench/compare'
	cflags="$cflags -Wformat=2 -Wshadow"
	tools='refault shmem app_switch'
fi
inputs()
{
	hash=$(digest "$script_dir/build.sh") || return 1
	printf 'input_sha256=%s  build.sh\n' "$hash" || return 1
	if [ "$target" = mcbench ]; then
		hash=$(digest "$source_dir/$source_name") || return 1
		printf 'input_sha256=%s  %s\n' "$hash" "$source_label" || return 1
	else
		for name in Makefile common.h corpus.h refault.c shmem.c app_switch.c; do
			hash=$(digest "$source_dir/$name") || return 1
			printf 'input_sha256=%s  ../bench/compare/%s\n' "$hash" "$name" || return 1
		done
	fi
}
before=$(inputs) || fail 'cannot hash build inputs'
out=$script_dir/bin
record=$script_dir/BUILD-$target.txt
[ ! -L "$out" ] && [ ! -L "$record" ] || fail 'output directory/record must not be symlinks'
[ -d "$out" ] || mkdir "$out" || fail 'cannot create bin'
for name in $tools; do
	[ ! -L "$out/$name" ] || fail 'output executable must not be a symlink'
done
work=$(mktemp -d "$script_dir/.build-$target.XXXXXX") || fail 'cannot create private build directory'
cleanup()
{
	rm -f -- "$work/BUILD.txt" "$work/mcbench" "$work/refault" "$work/shmem" "$work/app_switch"
	rmdir -- "$work" 2>/dev/null || :
}
trap cleanup 0
trap 'exit 130' 2
trap 'exit 143' 15
# A failed rebuild must not leave an old success record beside changed outputs.
rm -f -- "$record" || fail 'cannot invalidate old build record'
TMPDIR=$work
export TMPDIR
if [ "$target" = mcbench ]; then
	# A relative source operand and -g0 keep host directory paths out of the ELF.
	(CDPATH= cd -P "$source_dir" && "$cc" $cflags -static \
		-o "$work/mcbench" "./$source_name") || fail 'mcbench build failed; no build record'
else
	# Do not inherit jobserver options, injected makefiles or unrelated targets.
	(unset MAKEFLAGS MFLAGS GNUMAKEFLAGS MAKEFILES
	make --no-print-directory -j1 -B -C "$source_dir" O="$work" CC="$cc" \
		CFLAGS="$cflags" CPPFLAGS= LDFLAGS=-static LDLIBS= \
		"$work/refault" "$work/shmem" "$work/app_switch") || fail 'compare build failed; no build record'
fi
after=$(inputs) || fail 'cannot recheck build inputs'
[ "$before" = "$after" ] || fail 'inputs changed during build; no build record'
[ "$compiler_sha" = "$(digest "$cc")" ] || fail 'compiler changed during build; no build record'
{
	printf '%s\n' 'format=memcomp-device-build-v1' "target=$target" "arch=$arch" \
		"compiler=$(label "$cc")" "compiler_version=$version" \
		"compiler_target=$machine" "compiler_sha256=$compiler_sha" \
		"cflags=$cflags" 'cppflags=' 'ldflags=-static' 'ldlibs=' \
		'path_operands=package-relative labels; external source directory omitted' \
		"$before" || exit 1
	for name in $tools; do
		hash=$(digest "$work/$name") || exit 1
		printf 'output_sha256=%s  bin/%s\n' "$hash" "$name" || exit 1
	done
} > "$work/BUILD.txt" || fail 'cannot write build record'
for name in $tools; do
	mv -- "$work/$name" "$out/$name" || fail 'cannot install built executable; no build record'
done
mv -- "$work/BUILD.txt" "$record" || fail 'cannot publish build record'
printf 'Wrote BUILD-%s.txt; refresh SHA256SUMS before distributing changed artifacts.\n' "$target"
