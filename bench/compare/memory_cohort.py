#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""J.4 W1 memory-decision cohort-v4: 34 fresh jobs, protocol v3, no J.5."""
import argparse
import fcntl
import hashlib
import json
import os
import re
from pathlib import Path
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import run
import telemetry

SOURCES = ('system_server', 'systemui', 'qq', 'wechat', 'chrome',
           'netease_music', 'discord', 'telegram')
BACKENDS = (('zram', 'zram'), ('memcomp-noswap', 'memcomp'),
            ('zswap', 'zswap'), ('memcomp-swap', 'memcomp'))
COHORT = 'cohort-v4'
PROTOCOL = telemetry.MEMORY_PROTOCOL
STOCK_SOURCE = '83b4161a63b87ce40d9f24f09b5b006f63d95b7c'
PROBE_PATHS = frozenset((
    'lib/radix-tree.c', 'lib/xarray.c', 'mm/memcomp.c',
    'mm/memcomp_counts.h', 'mm/memcomp_gc.h', 'mm/memcomp_index.h',
    'mm/memcomp_j4_hooks.h', 'mm/memcomp_j4_probe.h',
    'mm/memcomp_j4_report.h', 'mm/memcomp_pending_lifetime.h',
    'mm/memcontrol.c',
))
# Source excerpts used for accounting, plus memcomp implementation/build inputs.
# This is deliberately not a buildable copy of the complete kernel checkout.
SOURCE_FILES = ('Makefile', 'mm/Makefile', 'mm/Kconfig', 'fs/proc/meminfo.c',
                'include/linux/mmzone.h', 'include/linux/mm_inline.h', 'include/linux/swap.h',
                'mm/vmscan.c', 'mm/vmstat.c', 'mm/slub.c', 'mm/vmalloc.c', 'mm/percpu-vm.c',
                'kernel/fork.c', 'mm/swap_cgroup.c', 'mm/swapfile.c', 'mm/zsmalloc.c',
                'mm/zswap.c', 'drivers/block/zram/zram_drv.c', 'mm/madvise.c', 'mm/memory.c',
                'mm/rmap.c', 'mm/shmem.c', 'mm/userfaultfd.c', 'usr/gen_init_cpio.c')


def source_commit(value=None, binding=None):
    if (value is None) == (binding is None):
        raise ValueError('explicit --memcomp-source-commit OR --source-binding is required')
    if binding is not None:
        value = json.loads(Path(binding).read_text())['memcomp_source_commit']
    if not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{40}', value) or value == STOCK_SOURCE:
        raise ValueError('memcomp source commit must be a full committed-series SHA')
    return value


def git(tree, *args):
    return subprocess.check_output(['git', '-C', str(tree), *args],
                                   env={**os.environ, 'GIT_OPTIONAL_LOCKS': '0'})


def clean_source_identity(tree, commit):
    head = git(tree, 'rev-parse', 'HEAD').decode().strip()
    status = git(tree, 'status', '--porcelain=v1', '--untracked-files=all')
    flags = git(tree, 'ls-files', '-v', '-z').split(b'\0')
    if (head != commit or status or git(tree, 'diff', '--binary', 'HEAD') or
            any(p and (p[:1].islower() or p[:1] == b'S') for p in flags)):
        raise ValueError('memcomp build/pack tree must equal the explicitly pinned clean commit')
    return dict(commit=head, tree=git(tree, 'rev-parse', 'HEAD^{tree}').decode().strip(),
                index_sha256=hashlib.sha256(git(tree, 'ls-files', '--stage', '-z')).hexdigest())


def source_inventory(tree, commit):
    entries = {}
    for item in git(tree, 'ls-tree', '-r', '-z', commit).split(b'\0'):
        if item:
            meta, name = item.split(b'\t', 1)
            mode, kind, blob = meta.decode().split()
            entries[name.decode()] = (mode, kind, blob)
    if not set(SOURCE_FILES).issubset(entries):
        raise ValueError('source commit missing inspected accounting/build inputs')
    paths = sorted(set(SOURCE_FILES) | {p for p in entries if p.startswith(
        ('mm/memcomp', 'include/linux/memcomp', 'Documentation/ABI/testing/sysfs-kernel-mm-memcomp'))})
    files, payload = {}, {}
    for name in paths:
        mode, kind, blob = entries[name]
        if mode not in ('100644', '100755') or kind != 'blob':
            raise ValueError('nonregular source archive input: ' + name)
        data = git(tree, 'cat-file', 'blob', blob)
        data.decode('utf-8')
        if b'\0' in data:
            raise ValueError('binary source archive input: ' + name)
        payload[name] = data
        files[name] = dict(sha256=hashlib.sha256(data).hexdigest(), bytes=len(data), git_blob=blob, mode=mode)
    if sum(v['bytes'] for v in files.values()) > 32 * 1024**2:
        raise ValueError('source excerpts exceed 32 MiB bound; inspect inventory')
    manifest = dict(source_commit=commit, source_tree=git(tree, 'rev-parse', commit+'^{tree}').decode().strip(),
                    role='accounting and memcomp source excerpts; full build tree bound by commit', files=files)
    return manifest, payload


def source_archive(tree, commit, path, prepare=False):
    manifest, payload = source_inventory(tree, commit)
    if prepare:
        if path.exists() or path.is_symlink():
            raise ValueError('source archive already exists; never overwrite')
        path.mkdir(parents=True, exist_ok=False)
        for name, data in payload.items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        (path/'SOURCE_COMMIT').write_text(commit+'\n')
        write(path/'SOURCE_MANIFEST.json', manifest)
    if path.is_symlink() or json.loads((path/'SOURCE_MANIFEST.json').read_text()) != manifest:
        raise ValueError('source archive manifest differs from committed blobs')
    expected = set(payload) | {'SOURCE_COMMIT', 'SOURCE_MANIFEST.json'}
    actual = set()
    for item in path.rglob('*'):
        if item.is_symlink():
            raise ValueError('source archive symlink')
        if item.is_file():
            actual.add(item.relative_to(path).as_posix())
    if actual != expected or (path/'SOURCE_COMMIT').read_text() != commit+'\n':
        raise ValueError('source archive membership/commit mismatch')
    if any(sha(path/name) != entry['sha256'] for name, entry in manifest['files'].items()):
        raise ValueError('source archive bytes differ from actual build commit')
    return manifest


PHASES = ('before_backend_setup', 'workload_start', 'r0_seq_fresh_mapping',
          'r0_seq_fill_begin', 'r0_seq_prepared', 'r0_seq_read_begin', 'r0_seq_read_end',
          'r0_rand_prepared', 'r0_rand_read_begin', 'r0_rand_read_end',
          'r0_single_read_begin', 'r0_single_read_end',
          'r0_rewrite_prime_fresh_mapping', 'r0_rewrite_prime_fill_begin',
          'r0_rewrite_prime_prepared', 'r0_rewrite_prime_read_begin', 'r0_rewrite_prime_read_end',
          'r0_rewrite_fill_begin', 'r0_rewrite_prepared', 'r0_rewrite_read_begin',
          'r0_rewrite_read_end', 'workload_end')


def jobs(header):
    if tuple(s['label'] for s in header['sources']) != SOURCES:
        raise ValueError('eight-source order differs from the approved capture order')
    if any(s['pages'] < 32768 for s in header['sources']) or header['total_pages'] < 507904:
        raise ValueError('approved unique-index capacity is not met')
    common = ['/refault', '--corpus', '/dev/vdc', '--seed', '1', '--order', 'rand',
              '--cpu', '1', '--corpus-sampling', 'uniform-v411', '--rounds', '1',
              '--warmup', '0', '--single', '32', '--rewrite']
    result = []
    for source in SOURCES:
        for backend, kernel in BACKENDS:
            result.append(dict(name='w1-'+source+'-'+backend+'-zstd', backend=backend,
                               kernel=kernel, source=source, pages=16384, memory_mib=1024,
                               command=[*common, '--corpus-source', source, '--pages', '16384']))
    for backend, kernel in BACKENDS[:2]:
        result.append(dict(name='w1-combined-512m-'+backend+'-zstd', backend=backend,
                           kernel=kernel, source='combined', pages=131072, memory_mib=1024,
                           command=[*common, '--pages', '131072']))
    # Exercise both formerly failing labels early. This order is fixed before
    # observing any v4.11 memory result, not selected after looking at outcomes.
    priority = ('w1-systemui-zram-zstd', 'w1-qq-zram-zstd')
    by_name = {job['name']: job for job in result}
    result = [by_name[name] for name in priority] + [job for job in result if job['name'] not in priority]
    if len(result) != 34 or len(by_name) != 34:
        raise ValueError('fixed memory-decision population changed')
    return result


def sha(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, obj):
    tmp = path.with_name(path.name+'.tmp')
    with tmp.open('x') as stream:
        json.dump(obj, stream, indent=2); stream.write('\n')
        stream.flush(); os.fsync(stream.fileno())
    tmp.replace(path)


def snapshot_decision(report):
    """Only a complete raw-verified protocol may pass or receive one INVALID retry."""
    windows = report.get('windows', []) + report.get('totals', [])
    if (report.get('gate') == 'FAIL' or any(w.get('gate') == 'FAIL' for w in windows) or
            report.get('cluster_metadata', {}).get('teardown_gate') == 'FAIL'):
        return 'STOP_FAIL'
    if (report.get('memory_snapshot_validity') == 'ERROR' or
            report.get('named_cache_required_gate') != 'PASS' or
            report.get('issues') != [] or report.get('named_cache_issues') != [] or
            report.get('evidence', {}).get('issues') != [] or
            report.get('threshold_bytes') != 500000 or
            report.get('memory_snapshot_protocol') != telemetry.MEMORY_PROTOCOL):
        return 'STOP_ERROR'
    snapshots = report.get('memory_snapshots', [])
    if (tuple(s.get('phase') for s in snapshots) != PHASES or
            any(s.get('status') not in ('CONSISTENT', 'INVALID') or
                s.get('version') != telemetry.MEMORY_PROTOCOL for s in snapshots)):
        return 'STOP_ERROR'
    invalid = [s for s in snapshots if s.get('status') == 'INVALID']
    if report.get('gate') == 'INVALID' and report.get('memory_snapshot_validity') == 'INVALID' and invalid:
        for item in invalid:
            attempts = item.get('attempts', [])
            if (item.get('selected_attempt') is not None or len(attempts) != 10 or
                    [a.get('attempt') for a in attempts] != list(range(1,11)) or
                    any(a.get('status') != 'INCONSISTENT' for a in attempts)):
                return 'STOP_ERROR'
        return 'RETRY_INVALID_ONCE'
    if report.get('gate') != 'PASS' or report.get('memory_snapshot_validity') != 'VALID' or invalid:
        return 'STOP_ERROR'
    for window in windows:
        if window.get('gate') != 'PASS' or type(window.get('residual_bytes')) is not int or abs(window['residual_bytes']) > 500000:
            return 'STOP_ERROR'
    if not report.get('windows') or not report.get('totals'):
        return 'STOP_ERROR'
    return 'ACCEPT'


def verify_probe_receipt(path, tree, commit, hashes):
    """Bind debug-only artifacts to a clean production commit and external patch.

    Git replay uses temporary index/object storage; it never installs the probe.
    Header copies and both receipts remain frozen throughout the 34 fresh jobs.
    """
    path = Path(path).absolute()
    def regular(p):
        if p != p.resolve() or not p.is_file():
            raise ValueError('probe input must be a regular nonsymlink file: ' + str(p))
        return p.read_bytes()
    raw = regular(path)
    receipt = json.loads(raw)
    base_tree = git(tree, 'rev-parse', commit+'^{tree}').decode().strip()
    if (receipt.get('schema') != 'v423-j4-debug-probe-v1' or
            receipt.get('external_debug_only') is not True or
            receipt.get('production_commit') != commit or receipt.get('production_tree') != base_tree or
            receipt.get('production_restored') is not True or receipt.get('layout_unchanged') is not True or
            receipt.get('config_sha256') != hashes['.config'] or
            receipt.get('bzimage_sha256') != hashes['bzImage']):
        raise ValueError('probe receipt base/build/config/image/restoration/layout audit mismatch')
    frozen = {str(path): hashlib.sha256(raw).hexdigest()}
    def freeze(item, expected=None):
        if not item.is_absolute():
            item = path.parent/item
        payload = regular(item)
        digest = hashlib.sha256(payload).hexdigest()
        if expected is not None and digest != expected:
            raise ValueError('probe input hash mismatch: ' + str(item))
        frozen[str(item)] = digest
        return payload
    restored = json.loads(freeze(path.parent/'restored.json'))
    if any(restored.get(k) is not True for k in ('production_restored', 'config_restored',
                                                'output_binding_restored', 'build_restored')):
        raise ValueError('probe transaction restoration incomplete')
    patch = freeze(path.parent/'probe.patch', receipt.get('probe_patch_sha256'))
    audit_hash = receipt.get('layout_audit_sha256', '')
    if not re.fullmatch(r'[0-9a-f]{64}', audit_hash):
        raise ValueError('independent layout audit hash missing')
    audit = json.loads(freeze(path.parent/'layout-audit.json', audit_hash))
    if (audit.get('layout_unchanged') is not True or
            audit.get('probe_patch_sha256') != receipt.get('probe_patch_sha256')):
        raise ValueError('independent layout audit does not bind probe patch')
    if (not patch or len(patch) > 8*1024**2 or
            not re.fullmatch(r'[0-9a-f]{64}', receipt.get('probe_patch_sha256', ''))):
        raise ValueError('probe patch hash/size missing or invalid')
    objects = git(tree, 'rev-parse', '--git-path', 'objects').decode().strip()
    objects = str((Path(tree)/objects).resolve()) if not Path(objects).is_absolute() else objects
    with tempfile.TemporaryDirectory(prefix='v423-probe-verify-') as temp:
        object_dir = Path(temp)/'objects'
        object_dir.mkdir()
        env = {**os.environ, 'GIT_OPTIONAL_LOCKS': '0', 'GIT_INDEX_FILE': str(Path(temp)/'index'),
               'GIT_OBJECT_DIRECTORY': str(object_dir), 'GIT_ALTERNATE_OBJECT_DIRECTORIES': objects}
        def isolated(*args, payload=None):
            try:
                return subprocess.run(['git', '-C', str(tree), *args], input=payload, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True).stdout
            except subprocess.CalledProcessError as error:
                raise ValueError('probe patch replay failed: ' + error.stderr.decode(errors='replace')) from error
        isolated('read-tree', commit)
        isolated('apply', '--cached', '--binary', '-', payload=patch)
        applied = isolated('write-tree').decode().strip()
        if applied == base_tree:
            raise ValueError('empty probe patch')
        changed = isolated('diff', '--cached', '--name-only', '-z', commit, '--').split(b'\0')
        modified, headers = {}, {}
        for name in changed:
            if name:
                name = name.decode()
                if name not in PROBE_PATHS:
                    raise ValueError('probe patch outside independently reviewed observer paths')
                mode = isolated('ls-files', '--stage', '--', name).split(b' ', 1)[0]
                if mode != b'100644':
                    raise ValueError('probe input must remain a regular source file')
                blob = isolated('show', ':'+name)
                modified[name] = hashlib.sha256(blob).hexdigest()
                if name.endswith('.h'):
                    headers[name] = blob
        if 'audited_paths' in audit and sorted(audit['audited_paths']) != sorted(modified):
            raise ValueError('layout audit paths differ from applied probe')
        if receipt.get('instrumented_source_sha256') != modified:
            raise ValueError('instrumented source map differs from replayed production base')
        header_receipts = receipt.get('probe_headers')
        if not headers or not isinstance(header_receipts, dict) or set(header_receipts) != set(headers):
            raise ValueError('probe header receipts missing or incomplete')
        for name, blob in headers.items():
            entry = header_receipts[name]
            if entry.get('sha256') != modified[name] or freeze(Path(entry['path']), modified[name]) != blob:
                raise ValueError('probe header differs from applied production tree')
        canonical = isolated('diff', '--cached', '--binary', '--full-index', '--no-ext-diff',
                             '--no-textconv', commit, '--')
    return dict(build_kind='debug-only-external-probe', receipt=receipt, frozen_inputs=frozen,
                replayed_tree=applied, applied_diff_sha256=hashlib.sha256(canonical).hexdigest())


def verify_kernel_sources(kernel_dirs, build_source, probe_receipt=None, tree=None):
    receipts = {}
    for kernel, directory in kernel_dirs.items():
        artifact_inputs = {str(directory/name): sha(directory/name)
                           for name in ('source.txt', 'SHA256SUMS', 'bzImage', '.config')}
        identity, hashes = run.kernel_identity(directory, kernel)
        if identity[0] != build_source[kernel]:
            raise ValueError('actual build source differs from explicit source binding: ' + kernel)
        entry = dict(source_commit=identity[0], sha256=hashes)
        if kernel == 'memcomp':
            if probe_receipt is None or tree is None:
                raise ValueError('J.4 memcomp requires explicit --memcomp-probe-receipt and production tree')
            if Path(probe_receipt).resolve().parent != directory.resolve():
                raise ValueError('probe receipt must reside in the memcomp artifact directory')
            if 'instrumentation=external-debug-only-j4' not in identity:
                raise ValueError('memcomp source receipt must explicitly label external J.4 instrumentation')
            entry.update(verify_probe_receipt(probe_receipt, tree, build_source[kernel], hashes))
        else:
            entry['build_kind'] = 'stock-committed-build'
        entry.setdefault('frozen_inputs', {}).update(artifact_inputs)
        check_probe_inputs(entry['frozen_inputs'])
        receipts[kernel] = entry
    return receipts


def check_probe_inputs(inputs):
    for name, digest in inputs.items():
        path = Path(name)
        if path.is_symlink() or not path.is_file() or path.resolve() != path.absolute() or sha(path) != digest:
            raise ValueError('frozen kernel/probe input changed: ' + name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=HERE.parent.parent)
    parser.add_argument('--tree', type=Path, default=(Path.home() / 'memcomp-work/linux-6.12.23'))
    parser.add_argument('--image', type=Path)
    parser.add_argument('--binaries', type=Path)
    parser.add_argument('--kernel-dir', type=Path, default=(Path.home() / 'memcomp-work/v47-20261017/compare-kernels'))
    parser.add_argument('--source-archive-root', type=Path,
                        default=(Path.home() / 'memcomp-work/v423-cohort-sources'))
    parser.add_argument('--memcomp-kernel-dir', type=Path, help='new debug-only probe memcomp artifacts; stock dirs may be reused')
    parser.add_argument('--memcomp-probe-receipt', type=Path, help='v423-j4-debug-probe-v1 receipt from transactional probe builder')
    pin = parser.add_mutually_exclusive_group(required=True)
    pin.add_argument('--memcomp-source-commit')
    pin.add_argument('--source-binding', type=Path, help='JSON with memcomp_source_commit full SHA')
    parser.add_argument('--prepare-source-archives', action='store_true', help='only small committed text excerpts; no build/VM')
    parser.add_argument('--dest', type=Path)
    parser.add_argument('--work', type=Path)
    parser.add_argument('--plan-only', action='store_true')
    args = parser.parse_args()
    if os.getuid() != 1000:
        parser.error('run as nakixii uid1000')
    commit = source_commit(args.memcomp_source_commit, args.source_binding)
    build_source = dict(memcomp=commit, zram=STOCK_SOURCE, zswap=STOCK_SOURCE)
    kernel_dirs = {k: (args.memcomp_kernel_dir if k == 'memcomp' and args.memcomp_kernel_dir else args.kernel_dir/k)
                   for k in build_source}
    source_roots = {c: args.source_archive_root / ('memory-source-'+c[:12]) for c in set(build_source.values())}
    if telemetry.MEMORY_PROTOCOL != PROTOCOL:
        parser.error('cohort-v4 requires the current memory snapshot protocol')
    if args.prepare_source_archives:
        if args.plan_only:
            parser.error('choose archive preparation or plan-only')
        clean_source_identity(args.tree, commit)
        for c, path in source_roots.items():
            source_archive(args.tree, c, path, prepare=True)
        print(json.dumps(dict(cohort=COHORT, source_archives={c: str(p) for c,p in source_roots.items()},
                              kernel='NOT_BUILT', QEMU='NOT_RUN', stop_at='J.4', J5=False), indent=2))
        return
    if args.image is None:
        parser.error('--image is required for plan-only or execution')
    if args.plan_only:
        header = run.corpus_header(args.image)
        selected = jobs(header)
        for job in selected:
            spec = run.workload_spec('w1', job['command'], True, job['backend'])
            run.corpus_preflight(header, spec, job['memory_mib'])
        print(json.dumps(dict(cohort=COHORT, planned_jobs=len(selected), jobs=selected, formal_matrix=False,
                              reused_accepted_jobs=False, memory_snapshot_protocol=PROTOCOL, threshold_bytes=500000,
                              build_source=build_source, kernel_dirs={k: str(p) for k,p in kernel_dirs.items()},
                              build_kind='debug-only-external-probe',
                              probe_receipt=str(args.memcomp_probe_receipt) if args.memcomp_probe_receipt else None,
                              status='PLAN_ONLY_NOT_RUN', QEMU='NOT_RUN',
                              stop_at='J.4', J5=False), indent=2))
        return
    if not all((args.dest, args.work, args.binaries)):
        parser.error('--dest, --work and --binaries are required for execution')
    header, image_identity, header_digest, image_digest = run.corpus_header(
        args.image, with_identity=True, with_digest=True)
    selected = jobs(header)
    for job in selected:
        spec = run.workload_spec('w1', job['command'], True, job['backend'])
        run.corpus_preflight(header, spec, job['memory_mib'])
    source_identity = clean_source_identity(args.tree, commit)
    kernel_receipts = verify_kernel_sources(kernel_dirs, build_source, args.memcomp_probe_receipt, args.tree)
    archive_manifests = {c: source_archive(args.tree, c, path) for c,path in source_roots.items()}
    if args.dest.exists() or args.work.exists():
        raise ValueError('cohort-v4 requires new destination AND new work; no accepted-job reuse')
    args.dest.mkdir(parents=True, exist_ok=False)
    args.work.mkdir(parents=True, exist_ok=False)
    inputs = [p for p in HERE.iterdir() if p.suffix in ('.py','.c','.h')]
    inputs += [args.repo/'bench/e2e/tests/run_e2e_qemu.py', args.repo/'bench/corpus/pack.py']
    inputs += [p for root in source_roots.values() for p in root.rglob('*') if p.is_file()]
    binaries = [args.binaries/name for name in ('refault','shmem','app_switch','guest','gen_init_cpio')]
    inputs += binaries
    if args.source_binding is not None:
        inputs.append(args.source_binding)
    inputs += [directory/name for directory in kernel_dirs.values()
               for name in ('source.txt', 'SHA256SUMS', 'bzImage', '.config')]
    probe_inputs = {p: digest for receipt in kernel_receipts.values()
                    for p, digest in receipt['frozen_inputs'].items()}
    inputs += [Path(p) for p in probe_inputs]
    frozen = {str(p):sha(p) for p in inputs}
    if args.source_binding is not None and source_commit(binding=args.source_binding) != commit:
        raise ValueError('source binding changed after explicit pin read')
    if any(frozen[p] != digest for p, digest in probe_inputs.items()):
        raise ValueError('probe inputs changed during receipt verification')
    archive_members = {str(root): {str(p.relative_to(root)) for p in root.rglob('*') if p.is_file()}
                       for root in source_roots.values()}
    def image_stat():
        st = args.image.lstat()
        return (st.st_dev, st.st_ino, st.st_uid, st.st_mode, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    def check_frozen():
        check_probe_inputs(probe_inputs)
        for root, expected in archive_members.items():
            members = list(Path(root).rglob('*'))
            if (any(p.is_symlink() for p in members) or
                    {str(p.relative_to(root)) for p in members if p.is_file()} != expected):
                raise RuntimeError('source archive membership changed')
        if clean_source_identity(args.tree, commit) != source_identity:
            raise RuntimeError('committed memcomp source/index changed')
        if image_stat() != tuple(image_identity) or any(sha(p) != digest for p,digest in frozen.items()):
            raise RuntimeError('frozen source, binary or input changed; stop without mixing protocols')
    check_frozen()
    write(args.dest/'bindings.json', dict(kind='v423-j4-34-job-memory-decision', cohort=COHORT,
          memory_snapshot_protocol=telemetry.MEMORY_PROTOCOL, reused_accepted_jobs=False, formal_matrix=False,
          planned_jobs=34, maximum_invalid_retries_per_job=1, jobs=selected,
          image_header=header, image_identity=image_identity, image_sha256=image_digest,
          image_header_sha256=header_digest, producer_and_binary_hashes=frozen,
          build_source=build_source, kernel_receipts=kernel_receipts, J4_complete=False,
          committed_source_identity=source_identity,
          kernel_dirs={k: str(p) for k,p in kernel_dirs.items()},
          source_archives={c:str(p) for c,p in source_roots.items()}, source_manifests=archive_manifests,
          threshold_bytes=500000, stop_at='J.4', J5=False))
    lock = os.open(str(Path.home() / 'memcomp-work/v47-20261017/heavy.lock'), os.O_RDWR|os.O_NOFOLLOW)
    fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    journal = []
    try:
        for number, job in enumerate(selected, 1):
            accepted = False
            for attempt in range(1, 3):
                check_frozen()
                dest = args.dest/(job['name']+'-attempt'+str(attempt))
                command = [sys.executable, '-B', str(HERE/'run.py'), '--tree', str(args.tree),
                           '--kernel-dir', str(kernel_dirs[job['kernel']]), '--binaries', str(args.binaries),
                           '--work', str(args.work), '--dest', str(dest), '--host-volume', '/mnt/c',
                           '--backend', job['backend'], '--algorithm', 'zstd', '--label', 'w1',
                           '--memory', str(job['memory_mib']), '--corpus-image', str(args.image),
                           '--corpus-expected-identity', json.dumps(image_identity),
                           '--corpus-header-sha256', header_digest, '--allow-pressure', '--smoke',
                           '--snapshot-memory-consistent', '--timeout', '900', '--', *job['command']]
                print('MEMORY JOB', number, '/34', job['name'], 'attempt', attempt, flush=True)
                began = time.monotonic()
                with (args.dest/(dest.name+'.driver.log')).open('x') as log:
                    rc = subprocess.call(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
                check_frozen()
                status = json.loads((dest/'status.json').read_text())[0] if (dest/'status.json').exists() else {}
                row = dict(job=job['name'], attempt=attempt, run_exit=rc, elapsed_s=time.monotonic()-began,
                           execution_valid=status.get('valid'), lifecycle_safe=status.get('lifecycle_safe_to_continue'),
                           data_integrity=status.get('data_integrity'), kernel_failure=status.get('kernel_or_data_failure'))
                journal.append(row); write(args.dest/'journal.json', journal)
                if (rc != 0 or row['execution_valid'] is not True or row['lifecycle_safe'] is not True or
                        row['data_integrity'] is not True or row['kernel_failure'] is not False):
                    raise RuntimeError('execution, data or lifecycle failure; not a consistency retry')
                metadata = json.loads((dest/'metadata.json').read_text())
                source = metadata['source']
                if (source != build_source[job['kernel']] or metadata.get('snapshot_memory_consistent') is not True or
                        metadata.get('snapshot_counter_protocol') != telemetry.MEMORY_PROTOCOL or
                        metadata.get('corpus_sampling_protocol') != run.CORPUS_SAMPLING_PROTOCOL or
                        metadata.get('backend') != job['backend'] or metadata.get('algorithm_requested') != 'zstd' or
                        metadata.get('label') != 'w1' or metadata.get('workload_argv') != job['command'] or
                        tuple(metadata.get('corpus_identity', ())) != tuple(image_identity) or
                        metadata.get('corpus_header_page_sha256') != header_digest):
                    raise RuntimeError('actual source or snapshot option binding mismatch')
                account = [sys.executable, '-B', str(HERE/'memory_account.py'), str(dest/'run1.log.gz'),
                           '--source-root', str(source_roots[source]), '--guest-source', str(HERE/'guest.c'),
                           '--config', str(dest/'.config'), '--output', str(dest/'memory-account.json')]
                memory_rc = subprocess.call(account, stdin=subprocess.DEVNULL)
                check_frozen()
                report = json.loads((dest/'memory-account.json').read_text())
                decision = snapshot_decision(report)
                expected_rc = {'PASS': 0, 'FAIL': 1, 'undefined': 2, 'INVALID': 3}.get(report.get('gate'))
                if memory_rc != expected_rc or report.get('backend') != job['backend'] or report.get('workload') != 'w1':
                    decision = 'STOP_ERROR'
                row.update(memory_exit=memory_rc, memory_gate=report.get('gate'), decision=decision)
                write(args.dest/'journal.json', journal)
                print('MEMORY RESULT', job['name'], decision, flush=True)
                if decision == 'RETRY_INVALID_ONCE':
                    if attempt == 1:
                        continue
                    raise RuntimeError('second consistency INVALID; stop; original evidence retained')
                if decision != 'ACCEPT' or memory_rc:
                    raise RuntimeError('strict memory decision '+decision+'; original evidence retained')
                accepted = True
                break
            if not accepted:
                raise RuntimeError('second consistency INVALID; stop')
        check_frozen()
        write(args.dest/'complete.json', dict(all_34_jobs_accepted=True, actual_boots=len(journal),
              cohort=COHORT, memory_snapshot_protocol=telemetry.MEMORY_PROTOCOL,
              reused_accepted_jobs=False, formal_matrix=False, stop_at='J.4', J5=False,
              build_source=build_source, threshold_bytes=500000, J4_complete=False,
              decision_report='pending aggregation and independent review'))
    except BaseException as error:
        write(args.dest/'stopped.json', dict(all_34_jobs_accepted=False, actual_boots=len(journal),
              cohort=COHORT, stop_at='J.4', J5=False, reason=str(error), original_evidence_preserved=True))
        raise
    finally:
        os.close(lock)


if __name__ == '__main__':
    main()
