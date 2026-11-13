#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Bound, no-swap KVM guest; numeric verdicts are host parser coverage only."""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
SOURCE = (Path.home() / 'memcomp-work/linux-6.12.23')
# Historical cache name; current completed DEBUG build superseded it in place.
BUILD = (Path.home() / 'memcomp-work/build/e2e-debug-current-d7d5102-20261014')
PROVENANCE = (Path.home() / 'memcomp-work/e2e-debug-numa-hmm-current-5ac721-20261015')
BUILD_LOG = PROVENANCE / 'resumed-build.log'
BUILD_INFO = PROVENANCE / 'current-build.json'
LLVM_BIN = (Path.home() / 'toolchains/clang-r536225/bin')
WORK = (Path.home() / 'memcomp-work/memcg-current-5ac721-20261015')
HEAD = '5ac721ca3fdbe7388930148c4fe3957f3e792327'
RELEASE = '6.12.23-g5ac721ca3fdb'
STOCK = '83b4161a63b87ce40d9f24f09b5b006f63d95b7c'
SUPERVISOR = HERE.parent / 'e2e/tests/run_e2e_qemu.py'
ACTIVE = None
PHASES = ('baseline', 'filled', 'compressed', 'released1', 'released2', 'released3')
PAGE = 4096
PAGES = 16384
# Single CPU: 64-page memcg charge stock + conservative 16 pages for objcg
# remainder, rstat aggregation and bounded unrelated kernel deltas. Never
# expand after seeing data. Two tolerances remain smaller than entry charge.
TOLERANCE = (64 + 16) * PAGE
SUBCOMPONENTS = ('pagetables', 'kernel_stack', 'slab', 'percpu', 'vmalloc')
FIELDS = ('current', 'anon', 'file', 'kernel', *SUBCOMPONENTS,
          'swap', 'stored', 'payload', 'pool')
DIAGNOSTIC = re.compile(r'(?im)(?:\bFAIL\b|\bWARNING:|\bBUG:|Oops:|'
                        r'Kernel panic|KASAN:|KFENCE:|Call Trace:|'
                        r'possible (?:circular locking|recursive locking)|'
                        r'INFO: .*?(?:hung|stuck|lockdep))')


def require(test, message):
    if not test:
        raise ValueError(message)


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def evaluate(text, rc, entry_size=80, stable=True):
    """Reject absent compression/metadata, not an exact byte equality test."""
    problems = []
    report = {'status': 'FAIL', 'scope': 'minimal-memcg-no-swap',
              'tolerance_bytes': TOLERANCE, 'entry_size': entry_size}
    try:
        require(rc == 0, 'QEMU supervisor nonzero exit')
        require(stable, 'input/source/config changed during execution')
        require(not DIAGNOSTIC.search(text), 'guest/source diagnostic')
        require(text.count('MEMCG-COLLECTED') == 1, 'collector completion missing/duplicate')
        require(re.findall(r'^MEMCG-END rc=(\d+)\s*$', text, re.M) == ['0'],
                'guest terminal status missing/contradictory')
        require(re.findall(r'^COMPRESSOR (\S+)\s*$', text, re.M) == ['zstd'],
                'stock zstd not proven')
        require(re.findall(r'^KERNEL release=(\S+)\s*$', text, re.M) ==
                [RELEASE], 'running kernel release mismatch')
        require(entry_size == 80, 'entry size disagrees with current layout')
        bindings = re.findall(r'^BINDING (.+)$', text, re.M)
        require(len(bindings) == 1, 'binding absent/duplicate')
        binding = json.loads(bindings[0])
        require(binding['source_head'] == HEAD and binding['smp'] == 1 and
                binding['swap_mb'] == 0 and binding['memory_mb'] == 1024,
                'guest input binding mismatch')
        workers = re.findall(r'^WORKER pid=(\d+) pages=(\d+)\s*$', text, re.M)
        require(len(workers) == 1 and int(workers[0][1]) == PAGES,
                'worker identity/count missing')
        worker = int(workers[0][0])
        require(worker > 1, 'invalid guest worker pid')
        require(re.findall(r'^e2e-qemu: actual_exit=(\d+) timed_out=(\d+) '
                           r'actual_death_verified=(\d+) group_drained=(\d+)\s*$', text, re.M)
                == [('0', '0', '1', '1')], 'QEMU death/timeout receipt missing or failed')
        raw = {}
        for phase, category, body in re.findall(
                r'^RAW phase=(\w+) (memory\.stat|memory\.events|cgroup\.procs)\n'
                r'(.*?)(?=^(?:SAMPLE |RAW |MEMCG-|e2e-qemu:)|\Z)', text, re.M | re.S):
            key = (phase, category)
            require(key not in raw, 'duplicate raw counter block')
            raw[key] = body.strip()
        require(set(raw) == {(phase, key) for phase in PHASES for key in
                             ('memory.stat', 'memory.events', 'cgroup.procs')},
                'raw per-phase counters/membership missing')
        for phase in PHASES:
            require(raw[(phase, 'cgroup.procs')] == str(worker),
                    'child cgroup membership mismatch')
            for category in ('memory.stat', 'memory.events'):
                pairs = [line.split() for line in raw[(phase, category)].splitlines()]
                require(all(len(pair) == 2 and re.fullmatch(r'[0-9]+', pair[1])
                            for pair in pairs), 'malformed raw numeric counters')
                require(len({p[0] for p in pairs}) == len(pairs), 'duplicate raw stat key')
                raw[(phase, category)] = {k: int(v) for k, v in pairs}
            events = raw[(phase, 'memory.events')]
            require(all(k in events for k in ('max', 'oom', 'oom_kill')),
                    'required memory event missing')
            require(all(v == 0 for v in events.values()), 'unexpected memcg pressure event')
        samples = [json.loads(s) for s in re.findall(r'^SAMPLE (.+)$', text, re.M)]
        require(tuple(s['phase'] for s in samples) == PHASES,
                'missing, duplicate or reordered phase')
        for s in samples:
            require(all(type(s.get(k)) is int and 0 <= s[k] < 2**63 for k in FIELDS),
                    'missing/invalid numeric counter')
            require(s['swap'] == 0, 'unexpected cgroup swap usage')
            stats = raw[(s['phase'], 'memory.stat')]
            for key in ('anon', 'file', 'kernel', *SUBCOMPONENTS):
                require(key in stats and stats[key] == s[key],
                        'raw memory.stat disagrees with sample: ' + key)
            require(stats.get('anon_thp') == 0, 'unexpected THP entries')
        b, f, c, *released = samples
        require(b['stored'] == b['payload'] == b['pool'] == 0,
                'global baseline not empty; cannot attribute compression')
        delta = lambda a, z, k: a[k] - z[k]
        want = PAGES * PAGE
        require(abs(delta(f, b, 'anon') - want) <= TOLERANCE, 'fill anon charge wrong')
        require(abs(delta(f, b, 'current') - want) <= TOLERANCE, 'fill current charge wrong')
        require(f['stored'] == f['payload'] == 0, 'unexpected compression before pageout')
        stored = delta(c, b, 'stored')
        payload = delta(c, b, 'payload')
        require(stored == want, 'not all target pages actually compressed')
        require(want // 16 < payload < want * 3 // 4, 'zero/trivial/uncompressed payload')
        require(c['pool'] >= payload, 'allocator pool smaller than payload')
        require(abs(delta(c, b, 'anon')) <= TOLERANCE, 'compressed anon retained')
        require(delta(f, c, 'current') >= want // 2, 'no meaningful memory.current reduction')
        metadata = PAGES * entry_size
        expected = payload + metadata
        overhead = sum(delta(c, b, k) for k in SUBCOMPONENTS)
        observed_current = delta(c, b, 'current') - delta(c, b, 'anon') - delta(c, b, 'file') - overhead
        observed_kernel = delta(c, b, 'kernel') - overhead
        lower, upper = expected - TOLERANCE, expected + TOLERANCE
        require(metadata > 2 * TOLERANCE, 'test cannot distinguish missing metadata')
        require(lower > payload, 'lower bound permits absent metadata')
        require(lower <= observed_current <= upper, 'memory.current payload+entry charge outside bounds')
        require(lower <= observed_kernel <= upper, 'memory.stat kernel objcg charge outside bounds')
        report.update(stored_bytes=stored, payload_bytes=payload,
                      metadata_bytes=metadata, expected_objcg_bytes=expected,
                      observed_current_objcg_bytes=observed_current,
                      observed_kernel_objcg_bytes=observed_kernel,
                      lower_bound=lower, upper_bound=upper, samples=samples)
        for s in released:
            require(s['stored'] == s['payload'] == 0, 'memcomp objects/payload retained after munmap')
            require(abs(delta(s, b, 'current')) <= TOLERANCE, 'memory.current did not return to baseline band')
            require(abs(delta(s, b, 'kernel')) <= TOLERANCE, 'objcg kernel charge did not release')
            require(abs(delta(s, b, 'anon')) <= TOLERANCE, 'anonymous memory did not release')
        report['status'] = 'PASS'
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as e:
        problems.append(str(e))
    report['problems'] = problems
    return report


def execute(argv, directory, name, cwd=None, data=None):
    (directory / (name + '.command.json')).write_text(json.dumps(
        {'argv': [str(x) for x in argv], 'cwd': str(cwd or Path.cwd())}, indent=2) + '\n')
    env = dict(os.environ, TMPDIR=str(directory), PYTHONDONTWRITEBYTECODE='1')
    with (directory / (name + '.log')).open('wb') as f:
        p = subprocess.run([str(x) for x in argv], cwd=cwd, input=data, env=env,
                           stdout=f, stderr=subprocess.STDOUT)
    (directory / (name + '.exit')).write_text(str(p.returncode) + '\n')
    return p.returncode


def git(*args):
    return subprocess.check_output(['git', '-C', str(SOURCE), *args], text=True).strip()


def newc(entries):
    result = bytearray()
    for inode, (name, mode, data, major, minor) in enumerate(entries + [
            ('TRAILER!!!', 0, b'', 0, 0)], 1):
        name = name.encode() + b'\0'
        fields = (inode, mode, 0, 0, 1, 0, len(data), 0, 0,
                  major, minor, len(name), 0)
        result.extend(b'070701' + ''.join('%08x' % f for f in fields).encode())
        result.extend(name)
        result.extend(b'\0' * (-len(result) % 4))
        result.extend(data)
        result.extend(b'\0' * (-len(result) % 4))
    return bytes(result)


def get_layout(directory):
    line = (BUILD / 'mm/.memcomp.o.cmd').read_text().splitlines()[0].split(':=', 1)[1]
    words = shlex.split(line)
    require(words[0] == 'clang', 'unexpected layout compiler')
    words[0] = str(LLVM_BIN / 'clang')
    output, skip = [], False
    for w in words:
        if skip:
            skip = False
            continue
        if w == '-o':
            skip = True
            continue
        if w == '-c' or w.startswith('-Wp,-MMD,'):
            continue
        output.append(w)
    output += ['-fsyntax-only', '-Xclang', '-fdump-record-layouts']
    require(execute(output, directory, 'layout', BUILD) == 0, 'layout compiler failed')
    t = (directory / 'layout.log').read_text()
    m = re.search(r'\| struct memcomp_entry\n.*?\[sizeof=(\d+), align=', t, re.S)
    require(m is not None, 'entry layout not found')
    return int(m[1])


def verify_build_log(text):
    """Require actual completed Kbuild output, not an unrelated runtime log."""
    for path in (SOURCE, BUILD):
        require("Entering directory '" + str(path) + "'" in text,
                'build log directory mismatch: ' + str(path))
    require(re.search(r'^\s*LD\s+vmlinux$', text, re.M),
            'build log missing genuine vmlinux link')
    require(re.search(r'^Kernel: arch/x86/boot/bzImage is ready\b', text, re.M),
            'build log missing completed bzImage')
    require('# DRIVER FAILURE:' not in text and not re.search(
        r'(?:\*\*\*.*(?:Error|Terminated)|fatal error:)', text),
        'build log contains failed/interrupted build')


def read_record(path):
    """Existing evidence is data only; never execute its commands or paths."""
    require(path.is_file(), 'build evidence absent: ' + str(path))
    value = json.loads(path.read_text())
    require(isinstance(value, dict), 'build evidence is not an object: ' + str(path))
    return value


def verify_file_record(record, path, label):
    require(isinstance(record, dict) and record.get('path') == str(path),
            'build ' + label + ' path mismatch')
    require(record.get('sha256') == sha(path) and
            record.get('bytes') == path.stat().st_size,
            'build ' + label + ' content mismatch')


def parse_options(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--attempt', default='attempt01')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--fresh-build', action='store_true',
                      help='bind one new, normally completed build (explicit inputs required)')
    mode.add_argument('--legacy-build', action='store_true',
                      help='use the unchanged historical interrupted/resumed chain (default)')
    for name in ('source', 'build', 'provenance', 'work'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--head')
    options = parser.parse_args(argv)
    names = ('source', 'build', 'provenance', 'work', 'head')
    supplied = [getattr(options, n) is not None for n in names]
    if options.fresh_build:
        if not all(supplied):
            parser.error('--fresh-build requires --source/--build/--provenance/--work/--head')
        if not re.fullmatch(r'[0-9a-f]{40}', options.head) or options.head == '0' * 40:
            parser.error('--head must be a full nonzero lowercase commit ID')
    elif any(supplied):
        parser.error('path/HEAD overrides require --fresh-build; legacy evidence is fixed')
    return options


def fresh_release(info, head):
    require(info.get('build_mode') == 'fresh', 'fresh build_mode missing/mismatch')
    require(info.get('head') == head and info.get('source_clean') is True,
            'build source HEAD/cleanliness mismatch')
    release = info.get('kernel_release')
    match = re.fullmatch(r'[0-9][A-Za-z0-9._+-]*-g([0-9a-f]{12,40})',
                         release) if isinstance(release, str) else None
    require(match is not None and head.startswith(match[1]) and '-dirty' not in release,
            'fresh recorded release/HEAD suffix mismatch')
    return release


def configure_fresh_build(options):
    """All overrides are explicit and canonical; do not mutate legacy defaults."""
    global SOURCE, BUILD, PROVENANCE, WORK, HEAD, RELEASE, BUILD_LOG, BUILD_INFO
    paths = [getattr(options, n) for n in ('source', 'build', 'provenance', 'work')]
    for path in paths:
        require(path.is_absolute() and path.is_dir() and path.resolve(strict=True) == path,
                'fresh directory must exist and be canonical/non-symlink: ' + str(path))
    for i, path in enumerate(paths):
        for other in paths[i + 1:]:
            require(path != other and path not in other.parents and other not in path.parents,
                    'fresh source/build/provenance/work directories must be disjoint')
    source, build, proof, work = paths
    info = read_record(proof / 'current-build.json')
    release = fresh_release(info, options.head)
    require(info.get('source') == str(source), 'fresh recorded source path mismatch')
    require(info.get('in_place_cache') == str(build), 'build cache mismatch')
    SOURCE, BUILD, PROVENANCE, WORK = source, build, proof, work
    HEAD, RELEASE = options.head, release
    BUILD_LOG, BUILD_INFO = proof / 'build.log', proof / 'current-build.json'


def verify_existing_build(fresh=False):
    """Bind an already completed build; no prerequisite full E2E or new seal."""
    require(BUILD_LOG.is_file(), 'completed build log absent: ' + str(BUILD_LOG))
    verify_build_log(BUILD_LOG.read_text())
    info = read_record(BUILD_INFO)
    require(info.get('head') == HEAD and info.get('source_clean') is True,
            'build source HEAD/cleanliness mismatch')
    require(info.get('in_place_cache') == str(BUILD), 'build cache mismatch')
    require(info.get('kernel_release') == RELEASE, 'build recorded release mismatch')
    if fresh:
        require(fresh_release(info, HEAD) == RELEASE, 'fresh release mismatch')
        require(info.get('source') == str(SOURCE), 'fresh recorded source path mismatch')
        require(git('rev-parse', 'HEAD') == HEAD and not git(
            'status', '--porcelain=v1', '--untracked-files=normal'),
            'fresh live source HEAD/cleanliness mismatch')
        for label, path in (('object', BUILD / 'mm/memcomp.o'),
                            ('build_log', BUILD_LOG)):
            verify_file_record(info.get(label), path, label)
    for label, name in (('config', '.config'), ('kernel', 'arch/x86/boot/bzImage'),
                        ('vmlinux', 'vmlinux')):
        verify_file_record(info.get(label), BUILD / name, label)
    clean_sha = hashlib.sha256(b'').hexdigest()
    before = read_record(PROVENANCE / 'source-before.json')
    after = read_record(PROVENANCE / 'source-after-bzImage.json')
    require(before == after and before.get('head') == HEAD and
            before.get('status') == '' and before.get('diff_sha256') == clean_sha,
            'build source before/after mismatch')
    if fresh:
        require(before.get('index_tree') == git('rev-parse', HEAD + '^{tree}'),
                'fresh source index tree mismatch')
    expected_command = ['bash', str(HERE.parent / 'e2e/build.sh'), str(SOURCE),
                        str(BUILD), *[str(HERE.parent / 'e2e' / (n + '.fragment'))
                                      for n in ('debug', 'numa', 'hmm')]]
    evidence = [BUILD_INFO, BUILD_LOG, PROVENANCE / 'build.log',
                PROVENANCE / 'source-before.json',
                PROVENANCE / 'source-after-bzImage.json',
                PROVENANCE / 'sources-before.json', PROVENANCE / 'memcomp.o.cmd']
    commands = ('build.command.json',) if fresh else (
        'build.command.json', 'resumed-build.command.json')
    for name in commands:
        path = PROVENANCE / name
        command = read_record(path)
        require(command.get('command') == expected_command and command.get('uid') == 1000,
                'build command/user mismatch: ' + name)
        if fresh:
            require(type(command.get('uid')) is int and command.get('user') == 'nakixii',
                    'fresh build command user mismatch')
        env = command.get('environment')
        require(isinstance(env, dict) and env.get('JOBS') == '2' and
                env.get('LLVM_BIN') == str(LLVM_BIN), 'build command toolchain/jobs mismatch')
        evidence.append(path)
    exit_path = PROVENANCE / ('build.exit.json' if fresh else 'resumed-build.exit.json')
    exit_record = read_record(exit_path)
    require(type(exit_record.get('exit')) is int and exit_record['exit'] == 0 and
            exit_record.get('uid') == 1000, 'build exit missing/nonzero or wrong user')
    if fresh:
        require(type(exit_record.get('uid')) is int and exit_record.get('user') == 'nakixii',
                'fresh build exit user mismatch')
    evidence.append(exit_path)
    if fresh:
        text = BUILD_LOG.read_text()
        require(re.search(r'^[ \t]*CC[ \t]+mm/memcomp\.o[ \t]*$.*?'
                          r'^[ \t]*LD[ \t]+vmlinux[ \t]*$.*?'
                          r'^Kernel: arch/x86/boot/bzImage is ready\b', text, re.M | re.S),
                'fresh build log missing ordered memcomp compilation/link/bzImage')
        require(not re.search(r'^[ \t]*(?:Terminated|Interrupted|Killed)\b', text, re.M),
                'fresh build log contains interruption')
    else:
        # The original log legitimately ended in interruption. Keep the real chain;
        # only the resumed completion above is accepted as a successful link.
        require((PROVENANCE / 'build.log').is_file(), 'original build log absent')
        original = (PROVENANCE / 'build.log').read_text()
        require(re.search(r'^\s*CC\s+mm/memcomp\.o$', original, re.M),
                'original build log missing current memcomp compilation')
    inputs = read_record(PROVENANCE / 'sources-before.json')
    build_inputs = [HERE.parent / 'e2e' / n for n in
                    ('build.sh', 'x86.fragment', 'debug.fragment',
                     'numa.fragment', 'hmm.fragment')]
    build_inputs += [SOURCE / 'mm/memcomp.c', SOURCE / 'mm/Kconfig']
    if fresh:
        build_inputs.append(LLVM_BIN / 'clang')
    for path in build_inputs:
        verify_file_record(inputs.get(str(path)), path, 'input ' + str(path))
    evidence += build_inputs
    releases = re.findall(r'^#define UTS_RELEASE "([^"\n]+)"$',
                          (BUILD / 'include/generated/utsrelease.h').read_text(), re.M)
    require(releases == [RELEASE], 'built source release suffix mismatch')
    command_file = BUILD / 'mm/.memcomp.o.cmd'
    require(sha(command_file) == sha(PROVENANCE / 'memcomp.o.cmd'),
            'saved object command mismatch')
    words = shlex.split(command_file.read_text().splitlines()[0].split(':=', 1)[1])
    require(words[0] == 'clang' and words[-1] == str(SOURCE / 'mm/memcomp.c'),
            'object command points at other source/compiler')
    config = (BUILD / '.config').read_text()
    for name in ('MEMCG', 'CGROUPS', 'MEMCOMP', 'MEMCOMP_DEBUG_STATS', 'CRYPTO_ZSTD',
                 'ZSMALLOC', 'LRU_GEN', 'LRU_GEN_ENABLED', 'KSM', 'NUMA', 'KASAN'):
        require(re.search('^CONFIG_' + name + '=y$', config, re.M),
                'required current DEBUG config ' + name)
    for name in ('MEMCOMP_KUNIT_TEST', 'ZRAM', 'ZSWAP'):
        require(not re.search('^CONFIG_' + name + '=[ym]$', config, re.M),
                'forbidden config ' + name)
    return evidence


def run():
    global ACTIVE
    options = parse_options()
    require(re.fullmatch(r'attempt\d{2}', options.attempt) is not None, 'invalid attempt')
    require(os.getuid() == os.geteuid() == 1000, 'runner must run as nakixii (uid 1000)')
    if options.fresh_build:
        configure_fresh_build(options)
    require(WORK.is_dir() and not WORK.is_symlink(), 'scratch root absent/unsafe')
    directory = WORK / options.attempt
    directory.mkdir(exist_ok=False)
    ACTIVE = directory
    initial_status = git('status', '--porcelain=v1', '--untracked-files=normal')
    require(git('rev-parse', 'HEAD') == HEAD and not initial_status, 'candidate HEAD/cleanliness')
    (directory / 'source-head.txt').write_text(HEAD + '\n')
    (directory / 'source-status-before.txt').write_text(initial_status + '\n')
    build_evidence = verify_existing_build(fresh=options.fresh_build)
    paths = [Path(shutil.which(n)).resolve() for n in ('gcc', 'clang', 'qemu-system-x86_64', 'python3', 'readelf', 'sh')]
    paths += [HERE / n for n in ('guest.c', 'run.py', 'test_run.py', 'README.md')]
    paths += [BUILD / n for n in ('.config', 'arch/x86/boot/bzImage', 'vmlinux',
                                  'mm/memcomp.o', 'mm/.memcomp.o.cmd',
                                  'include/generated/utsrelease.h')]
    paths += build_evidence + [LLVM_BIN / 'clang', SUPERVISOR,
              SOURCE / 'mm/memcontrol.c', SOURCE / 'include/linux/memcontrol.h',
              SOURCE / 'scripts/extract-vmlinux']
    changed = git('diff', '--name-only', STOCK, HEAD).splitlines()
    paths += [SOURCE / n for n in changed if (SOURCE / n).is_file()]
    paths = sorted(set(paths))
    initial = {str(p): sha(p) for p in paths}
    (directory / 'inputs-before.json').write_text(json.dumps(initial, indent=2) + '\n')
    shutil.copyfile(BUILD / '.config', directory / 'kernel.config')
    entry_size = get_layout(directory)
    require(entry_size == 80, 'unexpected entry layout')
    # Cross-check ELF identity from the actual compressed boot image, not just
    # the adjacent vmlinux. Extracted ELF is scratch-only, hashed, then removed.
    extracted = directory / 'image-vmlinux'
    extract = ['sh', str(SOURCE / 'scripts/extract-vmlinux'), str(BUILD / 'arch/x86/boot/bzImage')]
    (directory / 'extract.command.json').write_text(json.dumps(extract) + '\n')
    with extracted.open('wb') as out, (directory / 'extract.log').open('wb') as err:
        proc = subprocess.run(extract, stdout=out, stderr=err, env=dict(os.environ, TMPDIR=str(directory)))
    (directory / 'extract.exit').write_text(str(proc.returncode) + '\n')
    require(proc.returncode == 0, 'bzImage extraction failed')
    elf_ids = []
    for tag, path in [('image', extracted), ('build', BUILD / 'vmlinux')]:
        require(execute(['readelf', '-n', path], directory, tag + '-notes') == 0, 'ELF notes failed')
        note = (directory / (tag + '-notes.log')).read_text()
        ids = re.findall(r'Build ID: ([0-9a-f]+)', note)
        require(len(ids) == 1, 'missing/duplicate ELF build ID')
        elf_ids.append(ids[0])
    require(elf_ids[0] == elf_ids[1], 'boot-image/build vmlinux identity differs')
    (directory / 'image-extraction.json').write_text(json.dumps({
        'image_extracted_sha256': sha(extracted), 'bytes': extracted.stat().st_size,
        'retention': 'owned temporary ELF removed after matching build-ID; image kept in original O='}, indent=2) + '\n')
    extracted.unlink()
    binding = dict(source_head=HEAD, config_sha256=sha(BUILD / '.config'),
                   image_sha256=sha(BUILD / 'arch/x86/boot/bzImage'),
                   guest_source_sha256=sha(HERE / 'guest.c'), smp=1, swap_mb=0,
                   memory_mb=1024, entry_size=entry_size, elf_build_id=elf_ids[0])
    (directory / 'binding.json').write_text(json.dumps(binding, sort_keys=True) + '\n')
    (directory / 'existing-build-binding.json').write_text(json.dumps({
        'binding_kind': ('fresh-completed-build-source-config-command-log' if options.fresh_build
                         else 'existing-completed-build-source-config-command-log'),
        'build_mode': 'fresh' if options.fresh_build else 'legacy',
        'build_info': str(BUILD_INFO), 'build_info_sha256': sha(BUILD_INFO),
        'build_log': str(BUILD_LOG), 'build_log_sha256': sha(BUILD_LOG),
        'elf_build_ids': elf_ids, 'source_status': initial_status,
        'source_compile_command': str(BUILD / 'mm/.memcomp.o.cmd'),
        'source_release_suffix': RELEASE.split('-', 1)[1]}, indent=2) + '\n')
    executable = directory / 'guest-init'
    rc = execute(['gcc', '-static', '-std=gnu11', '-O2', '-Wall', '-Wextra',
                  '-Werror', '-Wformat=2', '-Wshadow', HERE / 'guest.c', '-o', executable],
                 directory, 'guest-build')
    require(rc == 0, 'guest build failed (see preserved log)')
    require(execute(['gcc', '--version'], directory, 'host-compiler-version') == 0, 'compiler version')
    require(execute([executable], directory, 'host-pid1-guard') == 77, 'host PID1 safety refusal failed')
    require(execute(['qemu-system-x86_64', '--version'], directory, 'qemu-version') == 0, 'QEMU version')
    require(execute([sys.executable, '-B', '-m', 'unittest', '-v', 'test_run'],
                    directory, 'host-tests', HERE) == 0, 'host unit tests failed')
    entries = [(n, 0o040755, b'', 0, 0) for n in ('proc', 'sys', 'dev')]
    entries += [('dev/console', 0o020600, b'', 5, 1), ('dev/null', 0o020666, b'', 1, 3),
                ('init', 0o100755, executable.read_bytes(), 0, 0),
                ('binding.json', 0o100444, (directory / 'binding.json').read_bytes(), 0, 0)]
    initrd = directory / 'initramfs.cpio.gz'
    initrd.write_bytes(gzip.compress(newc(entries), mtime=0))
    argv = [sys.executable, '-B', str(SUPERVISOR), '--work', str(directory),
            '--qemu', shutil.which('qemu-system-x86_64'), '--kernel', str(BUILD / 'arch/x86/boot/bzImage'),
            '--initrd', str(initrd), '--guest-env', 'memcg_minimal.disposable=1',
            '--memory', '1024', '--swap-mb', '0', '--smp', '1',
            '--accelerator', 'kvm', '--timeout', '150']
    artifacts = [executable, initrd, directory / 'binding.json', SUPERVISOR]
    guest_inputs = {str(p): sha(p) for p in artifacts}
    (directory / 'guest-inputs-before.json').write_text(json.dumps(guest_inputs, indent=2) + '\n')
    print('Starting KVM: ' + str(directory), flush=True)
    rc = execute(argv, directory, 'qemu')
    final = {str(p): sha(p) for p in paths}
    (directory / 'inputs-after.json').write_text(json.dumps(final, indent=2) + '\n')
    final_status = git('status', '--porcelain=v1', '--untracked-files=normal')
    (directory / 'source-status-after.txt').write_text(final_status + '\n')
    final_guest = {str(p): sha(p) for p in artifacts}
    (directory / 'guest-inputs-after.json').write_text(json.dumps(final_guest, indent=2) + '\n')
    stable = initial == final and initial_status == final_status == '' and git('rev-parse', 'HEAD') == HEAD and guest_inputs == final_guest
    text = (directory / 'qemu.log').read_text(errors='replace').replace('\r\n', '\n')
    result = evaluate(text, rc, entry_size, stable)
    found = re.findall(r'^BINDING (.+)$', text, re.M)
    if len(found) != 1 or json.loads(found[0]) != binding:
        result['status'] = 'FAIL'
        result['problems'].append('guest binding differs from runner manifest')
    result['inputs_stable'] = stable
    result['kernel_source'] = str(SOURCE)
    result['kernel_build'] = str(BUILD)
    (directory / 'result.json').write_text(json.dumps(result, indent=2) + '\n')
    # Final evidence manifest hashes all artifacts; itself is intentionally excluded.
    (directory / 'SHA256SUMS').write_text(''.join(sha(p) + '  ' + p.name + '\n'
        for p in sorted(directory.iterdir()) if p.is_file() and p.name != 'SHA256SUMS'))
    print(json.dumps(result, indent=2))
    return 0 if result['status'] == 'PASS' else 1


if __name__ == '__main__':
    try:
        raise SystemExit(run())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        if ACTIVE is not None and not (ACTIVE / 'result.json').exists():
            (ACTIVE / 'result.json').write_text(json.dumps({'status': 'FAIL', 'problems': [str(error)], 'guest_pass': False}, indent=2) + '\n')
        print('FAIL ' + str(error), file=sys.stderr)
        raise SystemExit(1)
