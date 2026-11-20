#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Build/pack and optionally run one standalone memcg collector on macOS VZ."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys

import run as minimal
import s08

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MEMORY_MB = 1024
CPUS = 1
TIMEOUT = 180
POWERED_OFF = 'VZ guest powered off'
CONFIG = ('ARM64', '64BIT', 'ARM64_4K_PAGES', 'CGROUPS', 'MEMCG', 'MEMCOMP',
          'SWAP', 'CRYPTO_ZSTD', 'TRANSPARENT_HUGEPAGE', 'ADVISE_SYSCALLS',
          'PROC_FS', 'SYSFS', 'DEVTMPFS', 'BINFMT_ELF', 'BLK_DEV_INITRD',
          'RD_GZIP', 'VIRTIO_CONSOLE', 'PRINTK')
S08_CONFIG = ('TMPFS', 'SHMEM', 'PROC_PAGE_MONITOR', 'VM_EVENT_COUNTERS')
OPT_IN = {'minimal': 'memcg_minimal.disposable=1', 's08': 'memcg_s08.disposable=1'}
require, sha = minimal.require, minimal.sha


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n')


def git(tree, *args):
    return subprocess.check_output(['git', '-C', str(tree), *args], timeout=30)


def source_state(tree):
    diff = git(tree, 'diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--')
    others = git(tree, 'ls-files', '--others', '--exclude-standard', '-z')
    untracked = {}
    for entry in others.split(b'\0')[:-1]:
        name = os.fsdecode(entry)
        path = tree / name
        untracked[name] = {'symlink': os.readlink(path)} if path.is_symlink() else sha(path)
    state = {'source_head': git(tree, 'rev-parse', 'HEAD').decode().strip(),
             'source_diff_sha256': hashlib.sha256(diff).hexdigest(),
             'source_untracked': untracked}
    return state, diff


def source_profile(tree, scenario):
    # The old standalone proof used sizeof(memcomp_entry). This profile is
    # specifically for the replacement MC16 storage/accounting implementation.
    markers = {
        'mm/memcomp/format.h': ('sizeof(struct mc16_record) == 64',
                               'sizeof(struct mc16_member) == 24'),
        'mm/memcomp/lifetime.h': ('record->alloc_bytes = ksize(record)',
                                 'memcomp_objcg_charge(objcg'),
    }
    if scenario == 's08':
        markers['mm/shmem.c'] = ('memcomp_charge_folio(', 'VM_FAULT_MAJOR',
                                 'count_memcg_event_mm(fault_mm, PGMAJFAULT)')
        markers['mm/mincore.c'] = ('memcomp_mincore(',)
    for name, expected in markers.items():
        path = tree / name
        text = path.read_text() if path.is_file() else ''
        require(all(s in text for s in expected),
                'required MC16 source profile not found in ' + str(path))


def pack_guest(out, paths, binding):
    clang, lld, runtime = (paths[key] for key in ('clang', 'lld', 'runtime'))
    sysroot = paths['libc'].parent.parent
    cmd = [clang, '--target=aarch64-linux-musl', f'--sysroot={sysroot}',
           f'--ld-path={lld}', '-static', '-nostdlib', '-mno-outline-atomics',
           '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-Wformat=2',
           paths['crt1'], paths['crti'], paths['guest_source'],
           '-Wl,--start-group', paths['libc'], runtime, '-Wl,--end-group',
           paths['crtn'], '-o', out / 'init']
    require(minimal.execute(cmd, out, 'guest-build') == 0, 'guest build failed')
    elf = (out / 'init').read_bytes()
    require(elf[:6] == b'\x7fELF\x02\x01' and elf[18:20] == b'\xb7\x00',
            'expected a little-endian AArch64 guest ELF')
    headers = subprocess.check_output([paths['readelf'], '-l', out / 'init'], text=True,
                                      timeout=10)
    require(not re.search(r'\b(?:INTERP|DYNAMIC)\b', headers), 'guest ELF is not static')
    binding.update(guest_sha256=sha(out / 'init'))
    encoded = (json.dumps(binding, sort_keys=True, separators=(',', ':')) + '\n').encode()
    (out / 'binding.json').write_bytes(encoded)
    entries = [(p, 0o040755, b'', 0, 0) for p in ('dev', 'proc', 'sys')]
    entries += [('dev/console', 0o020600, b'', 5, 1), ('dev/null', 0o020666, b'', 1, 3),
                ('init', 0o100755, elf, 0, 0), ('binding.json', 0o100444, encoded, 0, 0)]
    (out / 'initramfs.cpio.gz').write_bytes(gzip.compress(minimal.newc(entries), mtime=0))


def validate_vz(host, info, rc, cmdline):
    require(rc == 0, 'VZ runner nonzero exit: ' + str(rc))
    require(host.splitlines().count(POWERED_OFF) == 1, 'missing/duplicate VZ shutdown receipt')
    require(not minimal.DIAGNOSTIC.search(host) and 'VZ timeout' not in host,
            'VZ host diagnostic')
    require(info.splitlines() == ['backend=Apple Virtualization Framework',
                                 f'memory_mb={MEMORY_MB}', f'cpus={CPUS}',
                                 'block_devices=0', 'cmdline=' + cmdline],
            'VZ setup does not attest to this diskless one-vCPU launch')


def main(a, out):
    require(platform.system() == 'Darwin' and platform.machine() == 'arm64',
            'this helper needs native macOS arm64')
    tree, build = a.kernel_tree.resolve(), a.kernel_out.resolve()
    brew = Path(os.environ.get('BREW_PREFIX', '/opt/homebrew'))
    llvm = brew / 'opt/llvm/bin'
    lib = ROOT / 'build/aarch64-musl/lib'
    paths = dict(image=build / 'arch/arm64/boot/Image', config=build / '.config',
                 kernel_release=build / 'include/config/kernel.release',
                 clang=llvm / 'clang', readelf=llvm / 'llvm-readelf',
                 lld=brew / 'opt/lld/bin/ld.lld',
                 runtime=ROOT / 'build/compiler-rt/lib/linux/libclang_rt.builtins-aarch64.a',
                 libc=lib / 'libc.a', crt1=lib / 'crt1.o', crti=lib / 'crti.o',
                 crtn=lib / 'crtn.o', helper=HERE / 'vz.py', minimal=HERE / 'run.py')
    if a.scenario == 's08':
        paths.update(guest_source=HERE / 's08_guest.c', major=HERE / 's08_major.h',
                     s08=HERE / 's08.py')
    else:
        paths['guest_source'] = HERE / 'guest.c'
    if not a.pack_only:
        paths['runner'] = a.runner.resolve()
        require(os.access(paths['runner'], os.X_OK), 'missing executable signed VZ runner')
    for path in paths.values():
        require(path.is_file(), 'missing input: ' + str(path))
    inputs = {key: sha(path) for key, path in paths.items()}
    with paths['image'].open('rb') as image:
        require(image.read(64)[56:60] == b'ARM\x64', 'expected an uncompressed arm64 Image')
    config = set(paths['config'].read_text().splitlines())
    settings = CONFIG + (S08_CONFIG if a.scenario == 's08' else ('SLUB',))
    missing = ['CONFIG_' + k for k in settings if 'CONFIG_' + k + '=y' not in config]
    require(not missing, 'missing built-in prerequisite(s): ' + ', '.join(missing))
    release = paths['kernel_release'].read_text().strip()
    require(re.fullmatch(r'\S+', release), 'missing exact kernel release')
    source, diff = source_state(tree)
    source_profile(tree, a.scenario)
    (out / 'kernel.diff').write_bytes(diff)
    write_json(out / 'kernel-source.json', source)
    write_json(out / 'inputs-before.json', {key: {'path': str(paths[key]), 'sha256': digest}
                                          for key, digest in inputs.items()})
    cmdline = ('console=hvc0 rdinit=/init panic=-1 oops=panic loglevel=6 '
               'kunit.enable=0 ' + OPT_IN[a.scenario])
    binding = dict(schema='memcg-vz-v1', scenario=a.scenario, backend='vz',
                   source_head=source['source_head'],
                   source_snapshot_sha256=sha(out / 'kernel-source.json'),
                   kernel_release=release, kernel_sha256=inputs['image'],
                   config_sha256=inputs['config'], guest_source_sha256=inputs['guest_source'],
                   cmdline=cmdline, smp=CPUS, memory_mb=MEMORY_MB, swap_mb=0)
    if a.scenario == 's08':
        binding['guest_major_source_sha256'] = inputs['major']
    pack_guest(out, paths, binding)
    artifacts = {name: sha(out / name) for name in ('init', 'binding.json', 'initramfs.cpio.gz',
                                                    'kernel-source.json', 'kernel.diff')}
    write_json(out / 'pack-sha256.json', artifacts)

    def verify_inputs():
        require(source_state(tree)[0] == source and
                all(sha(path) == inputs[key] for key, path in paths.items()) and
                all(sha(out / name) == digest for name, digest in artifacts.items()),
                'kernel source, inputs or packed artifacts changed')

    verify_inputs()
    if a.pack_only:
        return {'status': 'PACKED_NOT_RUN', 'scenario': a.scenario}
    console = out / 'console.log'
    console.touch(exist_ok=False)
    info = out / 'vz-info.txt'
    cmd = [paths['runner'], paths['image'], out / 'initramfs.cpio.gz', '-', console,
           MEMORY_MB, CPUS, TIMEOUT, cmdline, info]
    write_json(out / 'vz-command.json', [str(x) for x in cmd])
    with (out / 'host.log').open('w') as host:
        proc = subprocess.run([str(x) for x in cmd], stdin=subprocess.DEVNULL, stdout=host,
                              stderr=subprocess.STDOUT, timeout=TIMEOUT + 15)
    write_json(out / 'vz-exit.json', proc.returncode)
    verify_inputs()
    host_text = (out / 'host.log').read_text(errors='replace')
    validate_vz(host_text, info.read_text(errors='replace') if info.exists() else '',
                proc.returncode, cmdline)
    text = console.read_text(errors='replace')
    if a.scenario == 'minimal':
        result = minimal.evaluate(text, proc.returncode, vz_binding=binding)
    else:
        result = s08.evaluate(text, binding, host_text, proc.returncode, drain=POWERED_OFF)
    result.update(backend='vz', scenario=a.scenario, inputs_stable=True,
                  console_sha256=sha(console), host_sha256=sha(out / 'host.log'),
                  vz_info_sha256=sha(info))
    return result


def cli():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('scenario', choices=OPT_IN)
    p.add_argument('--kernel-tree', required=True, type=Path)
    p.add_argument('--kernel-out', required=True, type=Path)
    p.add_argument('--out', required=True, type=Path, help='new results directory')
    p.add_argument('--runner', type=Path, default=ROOT / 'build/macos-vz/vz-runner')
    p.add_argument('--pack-only', action='store_true', help='do not invoke the VZ runner')
    a = p.parse_args()
    out = a.out.resolve()
    for protected in (a.kernel_tree.resolve(), a.kernel_out.resolve(), ROOT / 'results/formal-965'):
        if out == protected or protected in out.parents:
            p.error('--out must be outside the kernel tree/output and published results')
    try:
        out.mkdir()
    except OSError as exc:
        p.error(str(exc))
    try:
        result = main(a, out)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        result = {'status': 'FAIL', 'scenario': a.scenario, 'reason': str(exc)}
    write_json(out / 'result.json', result)
    print(json.dumps(dict(result, out=str(out)), indent=2))
    return int(result['status'] not in ('PASS', 'PACKED_NOT_RUN'))


if __name__ == '__main__':
    sys.exit(cli())
