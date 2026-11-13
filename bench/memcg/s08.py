#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""S08 pack/judge only. Never starts a build, QEMU or a source checkout."""
import argparse
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import stat
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ANCHOR = '265aaaf87ef9b49ec621562abb2abfc5f5a35174'
SIZE, PAGE, BAND = 64 * 1024 * 1024, 4096, 80 * 4096
PHASES = ('baseline', 'filled', 'compressed', 'readback', 'released')
REQUIRED = ('anon', 'file', 'shmem', 'kernel', 'pagetables', 'kernel_stack',
            'slab', 'percpu', 'vmalloc', 'sock')
DIAGNOSTIC = re.compile(r'(?im)(?:S08-FAIL|\bFAIL\b|\bWARNING:|\bBUG:|Oops:|'
                        r'Kernel panic|KASAN:|KFENCE:|Call Trace:|'
                        r'possible (?:circular locking|recursive locking)|'
                        r'INFO: .*?(?:hung|stuck|lockdep))')
CLEANUP_ERRORS = ('scratch_retained', 'supervisor_completion_unknown', 'acknowledgment_failed')
DRAIN = 'e2e-qemu: actual_exit=0 timed_out=0 actual_death_verified=1 group_drained=1'


def require(ok, message):
    if not ok:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for b in iter(lambda: f.read(1024 * 1024), b''):
            h.update(b)
    return h.hexdigest()


def legacy():
    spec = importlib.util.spec_from_file_location('s08_legacy', HERE / 'run.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def pairs(text):
    result = {}
    require(type(text) is str, 'missing raw numeric fields')
    for line in text.splitlines():
        words = line.split()
        require(len(words) == 2 and words[0] not in result and
                re.fullmatch(r'[0-9]+', words[1]), 'invalid/duplicate stat field')
        result[words[0]] = int(words[1])
    return result


def integer(n):
    return type(n) is int and n >= 0


def sample(s, pid):
    require(s.get('worker_pid') == pid and pid > 1, 'source PID changed')
    require(s.get('reader_cgroup') == '0::/\n' and
            s.get('source_cgroup') == '0::/s08-source\n', 'real A/root identity')
    require(s.get('a_procs', '').split() == [str(pid)], 'A not exclusive/live')
    require(pairs(s.get('a_cgroup_events')).get('populated') == 1 and
            pairs(s.get('a_cgroup_stat')).get('nr_descendants') == 0,
            'A must stay populated and have no descendants')
    proc = s.get('source_proc_stat', '')
    require(proc.startswith(str(pid) + ' (') and ') ' in proc and
            proc.rsplit(') ', 1)[1].split()[0] not in ('Z', 'X', 'x'),
            'source process missing/dead')
    events = pairs(s.get('a_events'))
    require(all(k in events and events[k] == 0 for k in
                ('low', 'high', 'max', 'oom', 'oom_kill')), 'memcg pressure/event')
    require(pairs(s.get('stat')).keys() >= set(REQUIRED), 'missing memory.stat keys')
    a = pairs(s['stat'])
    require(a['file'] >= a['shmem'] and not a['sock'] and
            not a.get('zswap', 0) and not a.get('zswapped', 0), 'unexpected charge type')
    require(s.get('swap_current') == '0\n', 'A has swap charge')
    for k in ('current', 'stored', 'payload', 'pool', 'ms'):
        require(integer(s.get(k)), 'invalid sample number: ' + k)
    for value, error in (('root_current', 'root_current_errno'),
                         ('root_stat', 'root_stat_errno')):
        require(integer(s.get(error)), 'missing root field errno')
        if s[error]:
            require(s.get(value) is None, 'absent root field fabricated')
        elif value == 'root_current':
            require(integer(s.get(value)), 'invalid root current')
        else:
            pairs(s.get(value))  # Hierarchical observation, never reader-zero.
    return dict(s, shmem=a['shmem'],
                data=s['current'] - a['anon'] - (a['file'] - a['shmem']) - a['kernel'])


def target(s, phase, base):
    shmem = SIZE if phase in ('filled', 'readback') else 0
    stored = SIZE if phase == 'compressed' else 0
    return (s['shmem'] == shmem and s['stored'] == stored and
            abs(s['data'] - base - shmem) <= BAND and
            (0 < s['payload'] < stored and s['pool'] > 0 if stored else
             s['payload'] == s['pool'] == 0))


def major_window(records, phase):
    samples = []
    for index in range(8):
        s = records.pop(0)
        require(s.get('event') == 'major_sample' and s.get('phase') == phase and
                s.get('index') == index and
                all(integer(s.get(k)) for k in ('ms', 'task', 'global', 'root', 'a')),
                'major sample identity/number')
        if samples:
            require(1000 <= s['ms'] - samples[-1]['ms'] and
                    all(s[k] >= samples[-1][k] for k in ('task', 'global', 'root', 'a')),
                    'major sample spacing/monotonicity')
        samples.append(s)
    require(samples[-1]['ms'] - samples[0]['ms'] < 9000, 'major collection deadline')
    return samples


def major_verdict(before, after):
    """Only called after every original content/accounting/cleanup gate passed."""
    keys = ('task', 'global', 'root', 'a')
    pre, post = before[-1], after[-1]
    require(all(all(s[k] == pre[k] for k in keys) for s in before[-3:]),
            'major baseline not stable')
    require(after[0]['ms'] >= pre['ms'] and
            all(all(s[k] == post[k] for k in keys) for s in after[-3:]),
            'major end not stable/order')
    require(all(s[k] == after[0][k] for s in after for k in ('task', 'global')),
            'major activity outside immediate read window')
    delta = {k: post[k] - pre[k] for k in keys}
    require(all(n >= 0 for n in delta.values()) and
            all(s['a'] == pre['a'] for s in after), 'major source A not idle/counter reset')
    expected = {k: 0 if k == 'a' else SIZE // PAGE for k in keys}
    if delta != expected:
        signatures = {(0, SIZE // PAGE, SIZE // PAGE): 'LT36-output-bit',
                      (SIZE // PAGE, 0, SIZE // PAGE): 'LT34-global',
                      (SIZE // PAGE, SIZE // PAGE, 0): 'LT35-memcg'}
        return {'status': 'FAIL', 'reason': 'major exact N mismatch',
                's08_main_gates': 'PASS', 'major_delta': delta,
                'major_samples': {'before': before, 'after': after},
                'signature_only': signatures.get(tuple(delta[k] for k in keys[:3]), 'other'),
                'note': 'not a mutation hit without independently bound real stock/mutant/restore'}
    return {'major_delta': delta, 'major_samples': {'before': before, 'after': after}}


def evaluate(text, binding, supervisor, supervisor_rc):
    """Collector verdict, not independent kernel-source provenance approval."""
    try:
        require(supervisor_rc == 0, 'supervisor nonzero')
        combined = text + '\n' + supervisor
        require(not any(marker in combined for marker in CLEANUP_ERRORS),
                'explicit supervisor cleanup exception')
        diagnostic = DIAGNOSTIC.search(combined)
        require(not diagnostic, 'guest/host diagnostic: ' +
                (combined[diagnostic.start():].splitlines()[0][:240] if diagnostic else ''))
        require(supervisor.splitlines().count(DRAIN) == 1, 'missing/duplicate owned drain')
        records = [json.loads(l[4:]) for l in text.splitlines() if l.startswith('S08 ')]
        require(records and records[0].get('event') == 'environment', 'missing environment')
        env = records.pop(0)
        require(json.loads(env['binding']) == binding, 'guest binding mismatch')
        require(env['release'] == binding['kernel_release'], 'runtime release mismatch')
        require(env.get('compressor') == 'zstd\n' and env.get('page_size') == PAGE and
                env.get('cpus') == 1 and env.get('reader_pid') == 1 and
                env.get('reader_euid') == 0, 'actual guest setup mismatch')
        require(len(env['swaps'].splitlines()) == 1 and
                env['swaps'].startswith('Filename'), 'guest swap present')
        pid = records[0].get('worker_pid')
        base = sample(records[0], pid)
        require(records[0].get('event') == 'sample' and records[0].get('phase') == 'baseline' and records[0].get('index') == -1 and
                base['shmem'] == base['stored'] == base['payload'] == base['pool'] == 0,
                'nonempty/missing origin')
        records.pop(0)
        summary, ms = {}, base['ms']
        for phase in PHASES:
            if phase == 'filled':
                r = records.pop(0)
                require(r.get('event') == 'file' and r.get('bytes') == SIZE and
                        integer(r.get('dev')) and integer(r.get('ino')) and r['ino'] > 0,
                        'missing payload identity')
            if phase == 'readback':
                warm = records.pop(0)
                require(warm.get('event') == 'major_sample' and warm.get('phase') == 'warmup' and
                        warm.get('index') == 0 and warm.get('ms', -1) >= ms and
                        all(integer(warm.get(k)) for k in ('ms', 'task', 'global', 'root', 'a')),
                        'major prewarm observation absent')
                r = records.pop(0)
                require(r == {'event': 'reader_fault_ready', 'pages': SIZE // PAGE,
                              'dontneed': 1, 'mincore_incore': SIZE // PAGE,
                              'pte_present': 0, 'pte_swap': 0},
                        'full reader PTE/mincore witness absent')
                before = major_window(records, 'before')
                require(before[0]['ms'] >= warm['ms'], 'major prewarm/baseline order')
                require(records.pop(0) == {'event': 'content', 'bytes': SIZE},
                        'full content witness absent')
                after = major_window(records, 'after')
                ms = after[-1]['ms']
            samples = []
            while records and records[0].get('event') == 'sample':
                s = sample(records.pop(0), pid)
                require(s.get('phase') == phase and s.get('index') == len(samples) and
                        s['ms'] >= ms, 'sample order/phase/index')
                ms = s['ms']
                samples.append(s)
            require(len(samples) >= 3 and records and
                    records.pop(0) == {'event': 'stable', 'phase': phase}, 'unstable ' + phase)
            require(samples[-1]['ms'] - samples[0]['ms'] < 20000 and
                    all(b['ms'] - a['ms'] >= 1000 for a, b in
                        zip(samples[-3:], samples[-2:])), 'poll spacing/deadline')
            require(all(target(s, phase, base['data']) for s in samples[-3:]),
                    phase + ': exact A shmem/store or current corroboration')
            summary[phase] = [{k: s[k] for k in ('current', 'shmem', 'data', 'stored',
                                                'payload', 'pool')} for s in samples[-3:]]
        require(len(records) == 1, 'extra/missing terminal records')
        end = records[0]
        require(end.get('event') == 'end' and end.get('rc') == 0 and
                all(end.get(k) == 1 for k in ('worker_reaped', 'file_absent',
                                              'cgroup_removed', 'tmpfs_unmounted')) and
                end.get('cleanup_errno') == 0 and end.get('worker_status') == 9, 'cleanup incomplete')
        major = major_verdict(before, after)
        if major.get('status') == 'FAIL':
            return major
        return {'status': 'PASS', **major, 'scope': 'S08-collector-only',
                'root_scope': 'hierarchical/optional; no direct reader-zero claim',
                'current_auxiliary_band': BAND, 'samples': summary,
                'provenance': 'requires independent producer/launch review'}
    except (ValueError, KeyError, TypeError, IndexError) as exc:
        return {'status': 'FAIL', 'reason': str(exc)}


def pack(a):
    receipt = a.provenance.read_bytes()
    provenance = json.loads(receipt)
    head = provenance.get('source_head', '')
    require(re.fullmatch(r'[0-9a-f]{40}', head), 'source_head must be full Git object')
    subprocess.run(['git', '-C', str(a.source), 'merge-base', '--is-ancestor', ANCHOR, head],
                   check=True, timeout=10)
    for key, path in (('kernel_sha256', a.kernel), ('config_sha256', a.config)):
        require(provenance.get(key) == sha(path), 'producer receipt mismatch: ' + key)
    require(type(provenance.get('kernel_release')) is str and
            provenance['kernel_release'] and '\n' not in provenance['kernel_release'],
            'missing exact kernel release')
    config = set(a.config.read_text().splitlines())
    required = ('64BIT', 'X86_64', 'MEMCG', 'CGROUPS', 'TMPFS', 'SHMEM', 'SYSFS',
                'PROC_FS', 'PROC_PAGE_MONITOR', 'DEVTMPFS', 'BLK_DEV_INITRD', 'MEMCOMP', 'SWAP', 'CRYPTO_ZSTD',
                'RD_GZIP', 'BINFMT_ELF', 'TRANSPARENT_HUGEPAGE')
    require(all('CONFIG_' + k + '=y' in config for k in required), 'guest config dependency')
    elf = a.guest_elf.read_bytes()
    require(elf[:6] == b'\x7fELF\x02\x01' and elf[18:20] == b'\x3e\x00', 'require 64-bit little-endian guest ELF')
    ph = subprocess.check_output(['readelf', '-l', str(a.guest_elf)], text=True, timeout=10)
    require('INTERP' not in ph and 'DYNAMIC' not in ph, 'guest must be static')
    old = legacy()
    binding = {k: provenance[k] for k in
               ('source_head', 'kernel_release', 'kernel_sha256', 'config_sha256')}
    binding.update(schema='memcg-s08-v1', anchor=ANCHOR, guest_sha256=hashlib.sha256(elf).hexdigest(),
                   guest_source_sha256=sha(HERE / 's08_guest.c'),
                   guest_major_source_sha256=sha(HERE / 's08_major.h'),
                   producer_receipt_sha256=hashlib.sha256(receipt).hexdigest(), legacy_newc_sha256=sha(HERE / 'run.py'),
                   tool_sha256=sha(__file__))
    encoded = json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()
    entries = [(p, stat.S_IFDIR | 0o755, b'', 0, 0) for p in ('proc', 'sys', 'dev')]
    entries += [('dev/console', stat.S_IFCHR | 0o600, b'', 5, 1),
                ('dev/null', stat.S_IFCHR | 0o666, b'', 1, 3),
                ('init', stat.S_IFREG | 0o755, elf, 0, 0),
                ('binding.json', stat.S_IFREG | 0o444, encoded, 0, 0)]
    archive = gzip.compress(old.newc(entries), mtime=0)
    require(all(sha(path) == binding[key] for key, path in (
        ('guest_sha256', a.guest_elf), ('kernel_sha256', a.kernel),
        ('config_sha256', a.config), ('producer_receipt_sha256', a.provenance))),
        'pack input changed during collection')
    # Nothing here executes the guest or grants producer provenance approval.
    a.out.mkdir(parents=False, exist_ok=False)
    (a.out / 'binding.json').write_bytes(encoded)
    (a.out / 'initramfs.cpio.gz').write_bytes(archive)
    (a.out / 'producer-receipt.json').write_bytes(receipt)
    (a.out / 'pack.json').write_text(json.dumps({
        'binding_sha256': sha(a.out / 'binding.json'),
        'initramfs_sha256': sha(a.out / 'initramfs.cpio.gz'),
        'kernel': str(a.kernel.resolve()), 'config': str(a.config.resolve()),
        'guest_elf': str(a.guest_elf.resolve()),
        'note': 'pack only; producer provenance and real launch require independent review'},
        indent=2) + '\n')
    print(json.dumps({'status': 'PACKED_NOT_RUN', 'out': str(a.out)}))


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='action', required=True)
    q = sub.add_parser('pack')
    for key in ('source', 'provenance', 'kernel', 'config', 'guest-elf', 'out'):
        q.add_argument('--' + key, required=True, type=Path)
    q = sub.add_parser('judge')
    for key in ('log', 'binding', 'supervisor-log'):
        q.add_argument('--' + key, required=True, type=Path)
    q.add_argument('--supervisor-rc', required=True, type=int)
    a = p.parse_args()
    if a.action == 'pack':
        pack(a)
        return 0
    result = evaluate(a.log.read_text(errors='replace'), json.loads(a.binding.read_text()),
                      a.supervisor_log.read_text(errors='replace'), a.supervisor_rc)
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({'status': 'FAIL', 'reason': str(exc)}))
        sys.exit(1)
