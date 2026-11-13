#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Offline J.4 verifier: no VM, build, checkout mutation or J.5.

Usage: summarize_j4.py COHORT --fixed-objects FIXED_REPORT [--output NEW_JSON]
COHORT is unmodified memory_cohort.py cohort-v4 output. FIXED_REPORT is
report-v423-fixed-objects.py output with retained nm/config/build receipts.
Density is sampled at r0_seq_read_begin, the right endpoint of the W1 payload
window. Other snapshots remain visible; they cannot replace that sample.
Exit 0 means J4_complete=true. All evidence/gate failures exit 1, J5=false.
--output uses exclusive create; existing evidence is never replaced.
"""
import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import struct
import sys

import memory_account
import telemetry

# Producers stay frozen even when this verifier is run from an external draft.
HERE = Path(memory_account.__file__).resolve().parent
PROTOCOL = telemetry.MEMORY_PROTOCOL
SAMPLING = 'corpus-page-uniform-disjoint-v411'
STOCK = '83b4161a63b87ce40d9f24f09b5b006f63d95b7c'
ENTRY80 = '6730cd182e692cee2fd7e571e68f7fd49ccf7eb1'
SOURCES = ('system_server', 'systemui', 'qq', 'wechat', 'chrome',
           'netease_music', 'discord', 'telegram')
BACKENDS = ('zram', 'memcomp-noswap', 'zswap', 'memcomp-swap')
BINS = ('refault', 'shmem', 'app_switch', 'guest', 'gen_init_cpio')
SPANS = (1, 2, 4, 8, 16)
PHASES = ('before_backend_setup', 'workload_start', 'r0_seq_fresh_mapping',
          'r0_seq_fill_begin', 'r0_seq_prepared', 'r0_seq_read_begin', 'r0_seq_read_end',
          'r0_rand_prepared', 'r0_rand_read_begin', 'r0_rand_read_end',
          'r0_single_read_begin', 'r0_single_read_end',
          'r0_rewrite_prime_fresh_mapping', 'r0_rewrite_prime_fill_begin',
          'r0_rewrite_prime_prepared', 'r0_rewrite_prime_read_begin',
          'r0_rewrite_prime_read_end', 'r0_rewrite_fill_begin', 'r0_rewrite_prepared',
          'r0_rewrite_read_begin', 'r0_rewrite_read_end', 'workload_end')
DENSITY_PHASE = 'r0_seq_read_begin'
COMPONENTS = ('slot_bytes', 'side_bytes', 'cluster_bytes', 'key_bytes',
              'counts_bytes', 'overflow_bytes', 'pending_bytes')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def integer(value, name, minimum=0):
    require(type(value) is int and value >= minimum,
            name + ': expected integer >= ' + str(minimum))
    return value


def same(left, right):
    """Compare JSON facts without Python's bool/int/float equivalence."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(same(left[k], right[k]) for k in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(same(a, b) for a, b in zip(left, right))
    return left == right


def hexhash(value, size=64):
    require(isinstance(value, str) and re.fullmatch(r'[0-9a-f]{%d}' % size, value),
            'missing/invalid full hash: ' + str(value))
    return value


def digest(data):
    return hashlib.sha256(data).hexdigest()


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON field: ' + key)
        result[key] = value
    return result


def decode(data):
    def invalid(value):
        raise ValueError('nonfinite JSON number: ' + value)
    return json.loads(data, object_pairs_hook=unique_object, parse_constant=invalid)


class Evidence:
    """Hash inspected files, then verify again before declaring success."""
    def __init__(self):
        self.hashes = {}

    def read(self, path, expected=None):
        path = Path(path).absolute()
        require(path == path.resolve() and path.is_file(),
                'nonregular/symlink evidence: ' + str(path))
        data = path.read_bytes()
        actual = digest(data)
        if expected is not None:
            require(actual == hexhash(expected), 'evidence hash mismatch: ' + str(path))
        old = self.hashes.get(str(path))
        require(old is None or old == actual, 'evidence changed while reading: ' + str(path))
        self.hashes[str(path)] = actual
        return data

    def json(self, path, expected=None):
        return decode(self.read(path, expected))

    def recheck(self):
        for path, expected in list(self.hashes.items()):
            self.read(path, expected)


def expected_jobs():
    common = ['/refault', '--corpus', '/dev/vdc', '--seed', '1', '--order', 'rand',
              '--cpu', '1', '--corpus-sampling', 'uniform-v411', '--rounds', '1',
              '--warmup', '0', '--single', '32', '--rewrite']
    jobs = []
    for source in (*SOURCES, 'combined'):
        for backend in BACKENDS if source != 'combined' else BACKENDS[:2]:
            combined = source == 'combined'
            pages = 131072 if combined else 16384
            jobs.append(dict(
                name='w1-' + ('combined-512m' if combined else source) + '-' + backend + '-zstd',
                backend=backend, kernel='memcomp' if backend.startswith('memcomp-') else backend,
                source=source, pages=pages, memory_mib=1024,
                command=common + ([] if combined else ['--corpus-source', source]) + ['--pages', str(pages)]))
    priority = ('w1-systemui-zram-zstd', 'w1-qq-zram-zstd')
    return [next(j for j in jobs if j['name'] == n) for n in priority] + [j for j in jobs if j['name'] not in priority]


def verify_bindings(b, evidence):
    """Check source/tool/kernel freeze without touching a live build tree.

    The corpus whole-image digest remains a frozen capture receipt; the large
    disk image is not scanned again. All jobs bind the same identity/header.
    """
    require(b['kind'] == 'v423-j4-34-job-memory-decision' and b['cohort'] == 'cohort-v4', 'wrong cohort kind')
    require(b['memory_snapshot_protocol'] == PROTOCOL, 'wrong snapshot protocol')
    require(b['reused_accepted_jobs'] is False and b['formal_matrix'] is False and
            b['J4_complete'] is False and b['J5'] is False and b['stop_at'] == 'J.4', 'invalid freshness/scope flags')
    require(type(b['planned_jobs']) is int and b['planned_jobs'] == 34 and
            type(b['maximum_invalid_retries_per_job']) is int and b['maximum_invalid_retries_per_job'] == 1 and
            type(b['threshold_bytes']) is int and b['threshold_bytes'] == 500000, 'changed population/retry/threshold')
    require(same(b['jobs'], expected_jobs()), 'job population/order/command differs from fixed 34 jobs')
    sources = b['build_source']
    require(set(sources) == {'memcomp', 'zram', 'zswap'} and sources['zram'] == sources['zswap'] == STOCK,
            'stock source binding mismatch')
    commit = hexhash(sources['memcomp'], 40)
    require(commit not in (STOCK, ENTRY80), 'J.4 requires new committed slot-table source')
    identity = b['committed_source_identity']
    require(identity['commit'] == commit, 'committed source identity mismatch')
    tree = hexhash(identity['tree'], 40)
    hexhash(identity['index_sha256'])
    header = b['image_header']
    require(tuple(s['label'] for s in header['sources']) == SOURCES, 'wrong corpus sources/order')
    require(all(integer(s['pages'], 'source pages') >= 32768 for s in header['sources']) and
            integer(header['total_pages'], 'total pages') >= 507904, 'insufficient unique corpus pages')
    require(len(b['image_identity']) == 7 and all(type(v) is int and v >= 0 for v in b['image_identity']),
            'invalid corpus stat7 identity')
    hexhash(b['image_sha256']); hexhash(b['image_header_sha256'])
    frozen = b['producer_and_binary_hashes']
    require(isinstance(frozen, dict) and frozen, 'missing frozen inputs')
    for path, expected in frozen.items():
        require(Path(path).is_absolute(), 'relative frozen path')
        evidence.read(path, expected)
    producers = [p for p in HERE.iterdir() if p.suffix in ('.py', '.c', '.h')]
    producers += [HERE.parent/'e2e/tests/run_e2e_qemu.py', HERE.parent/'corpus/pack.py']
    for path in producers:
        require(str(path) in frozen, 'unfrozen producer: ' + str(path))
    binary_hashes = {}
    for name in BINS:
        candidates = [value for path, value in frozen.items() if Path(path).name == name]
        require(len(candidates) == 1, 'missing/ambiguous frozen binary: ' + name)
        binary_hashes[name] = candidates[0]
    require(set(b['source_archives']) == set(sources.values()) == set(b['source_manifests']), 'incomplete source archives')
    archives = {}
    for source, root in b['source_archives'].items():
        root = Path(root)
        manifest = b['source_manifests'][source]
        require(same(manifest, evidence.json(root/'SOURCE_MANIFEST.json')), 'archive manifest mismatch')
        require(manifest['source_commit'] == source, 'archive source mismatch')
        hexhash(manifest['source_tree'], 40)
        if source == commit:
            require(manifest['source_tree'] == tree, 'production source tree mismatch')
        files = manifest['files']
        needed = {'fs/proc/meminfo.c', 'mm/zsmalloc.c'} | ({'mm/memcomp.c'} if source == commit else set())
        require(files and needed <= files.keys(), 'missing accounting source excerpts')
        members = list(root.rglob('*'))
        require(not any(p.is_symlink() for p in members), 'symlink in source archive')
        require({str(p.relative_to(root)) for p in members if p.is_file()} == set(files) | {'SOURCE_MANIFEST.json'},
                'source archive membership changed')
        for name, entry in files.items():
            require(not Path(name).is_absolute() and '..' not in Path(name).parts, 'unsafe source archive path')
            path = root/name
            require(str(path) in frozen and frozen[str(path)] == entry['sha256'], 'unfrozen source archive member')
            data = evidence.read(path, entry['sha256'])
            require(type(entry['bytes']) is int and len(data) == entry['bytes'] and entry['mode'] in ('100644', '100755'),
                    'archive size/mode mismatch')
            blob = hashlib.sha1(b'blob ' + str(len(data)).encode() + b'\0' + data).hexdigest()
            require(blob == entry['git_blob'], 'archive does not match Git blob')
        require(str(root/'SOURCE_MANIFEST.json') in frozen, 'unfrozen source manifest')
        archives[source] = root
    require(set(b['kernel_dirs']) == set(sources) == set(b['kernel_receipts']), 'missing kernel binding')
    for kernel, directory in b['kernel_dirs'].items():
        verify_kernel(kernel, Path(directory), b, evidence, tree)
    return dict(commit=commit, archives=archives, binary_hashes=binary_hashes, frozen=frozen)


def verify_kernel(kernel, directory, b, evidence, tree):
    frozen, sources = b['producer_and_binary_hashes'], b['build_source']
    receipt = b['kernel_receipts'][kernel]
    hashes = receipt['sha256']
    require(receipt['source_commit'] == sources[kernel] and set(hashes) == {'.config', 'bzImage'}, 'kernel source/hash schema mismatch')
    for name in ('source.txt', 'SHA256SUMS', '.config', 'bzImage'):
        path = str(directory/name)
        require(path in frozen and path in receipt['frozen_inputs'], 'unfrozen kernel artifact: ' + name)
        require(frozen[path] == receipt['frozen_inputs'][path], 'kernel frozen hash mismatch')
        evidence.read(path, hashes[name] if name in hashes else frozen[path])
    source_lines = evidence.read(directory/'source.txt').decode().splitlines()
    require(source_lines[0] == sources[kernel] and 'backend=' + kernel in source_lines, 'source.txt mismatch')
    sums = {}
    for line in evidence.read(directory/'SHA256SUMS').decode().splitlines():
        value, name = line.split('  ')
        require(name not in sums, 'duplicate kernel checksum')
        sums[name] = hexhash(value)
    require(sums == hashes, 'kernel SHA256SUMS mismatch')
    for path, value in receipt['frozen_inputs'].items():
        require(frozen[path] == value, 'receipt input absent from cohort freeze')
        evidence.read(path, value)
    if kernel != 'memcomp':
        require(receipt['build_kind'] == 'stock-committed-build', 'nonstock comparison build')
        return
    commit = sources[kernel]
    require(receipt['build_kind'] == 'debug-only-external-probe' and
            'instrumentation=external-debug-only-j4' in source_lines, 'probe not explicitly debug-only')
    probe = receipt['receipt']
    require(probe['schema'] == 'v423-j4-debug-probe-v1' and probe['external_debug_only'] is True and
            probe['production_restored'] is True and probe['layout_unchanged'] is True and
            probe['production_commit'] == commit and probe['production_tree'] == tree and
            probe['config_sha256'] == hashes['.config'] and probe['bzimage_sha256'] == hashes['bzImage'],
            'probe source/config/layout binding mismatch')
    require(any(evidence.json(p) == probe for p in receipt['frozen_inputs'] if Path(p).name == 'probe-binding.json'),
            'missing retained probe-binding.json')
    for name in ('probe.patch', 'layout-audit.json', 'restored.json'):
        require(str(directory/name) in receipt['frozen_inputs'], 'missing probe input: ' + name)
    evidence.read(directory/'probe.patch', probe['probe_patch_sha256'])
    audit = evidence.json(directory/'layout-audit.json', probe['layout_audit_sha256'])
    require(audit['layout_unchanged'] is True and audit['production_commit'] == commit and
            audit['production_tree'] == tree and audit['probe_patch_sha256'] == probe['probe_patch_sha256'],
            'independent layout audit mismatch')
    restored = evidence.json(directory/'restored.json')
    require(all(restored[k] is True for k in ('production_restored', 'config_restored', 'output_binding_restored', 'build_restored')),
            'probe transaction not fully restored')
    hexhash(receipt['replayed_tree'], 40); hexhash(receipt['applied_diff_sha256'])
    require(receipt['replayed_tree'] != tree, 'probe replay tree equals production')
    require(set(audit['audited_paths']) == set(probe['instrumented_source_sha256']), 'probe audited paths mismatch')
    require(probe['probe_headers'], 'missing probe header inventory')
    for name, value in probe['instrumented_source_sha256'].items():
        hexhash(value)
    for name, entry in probe['probe_headers'].items():
        require(name.endswith('.h') and entry['sha256'] == probe['instrumented_source_sha256'][name] and
                frozen[entry['path']] == entry['sha256'], 'probe header receipt mismatch')
        evidence.read(entry['path'], entry['sha256'])


def verify_report(report, accepted=True):
    """Recheck numeric thresholds and retries independently of runner decisions."""
    require(report['issues'] == [] and report['named_cache_issues'] == [] and report['evidence']['issues'] == [] and
            report['named_cache_required_gate'] == 'PASS', 'memory report has errors')
    require(report['page_bytes'] == 4096 and type(report['page_bytes']) is int and
            report['memory_snapshot_protocol'] == PROTOCOL and type(report['threshold_bytes']) is int and
            report['threshold_bytes'] == 500000, 'memory report protocol/threshold mismatch')
    require(isinstance(report['supplemental_layout_records'], list), 'missing supplemental layout inventory')
    snapshots = report['memory_snapshots']
    require(tuple(s['phase'] for s in snapshots) == PHASES and type(report['memory_snapshot_count']) is int and
            report['memory_snapshot_count'] == len(PHASES), 'missing/duplicate/out-of-order memory phases')
    require(all(s['version'] == PROTOCOL and s['status'] in ('CONSISTENT', 'INVALID') for s in snapshots), 'invalid raw snapshot protocol/status')
    windows, totals = report['windows'], report['totals']
    require(len(windows) == 2 and len(totals) == 1, 'missing/unexpected W1 residual windows')
    require([w['scope'] for w in windows] == ['setup_global_observation', 'W1_payload_global_observation'] and
            totals[0]['scope'] == 'setup_plus_W1_payload_not_contiguous' and
            type(totals[0]['round']) is int and totals[0]['round'] == 0, 'wrong W1 accounting windows')
    for w in windows + totals:
        require(w['issues'] == [] and w['gate'] in ('PASS', 'INVALID'), 'failed/error residual window')
        if w['gate'] == 'INVALID':
            require(not accepted, 'INVALID in accepted job')
            continue
        for key in ('observed_loss_bytes', 'classified_bytes', 'residual_bytes'):
            require(type(w[key]) is int, 'missing/noninteger residual term: ' + key)
        require(w['residual_bytes'] == w['observed_loss_bytes'] - w['classified_bytes'] and
                abs(w['residual_bytes']) <= 500000, 'residual arithmetic/threshold failure')
        require(isinstance(w['components_bytes'], dict) and w['components_bytes'] and
                all(type(v) is int for v in w['components_bytes'].values()) and
                sum(w['components_bytes'].values()) == w['classified_bytes'], 'residual classification mismatch')
    if accepted:
        require(report['gate'] == report['residual_gate'] == 'PASS' and report['memory_snapshot_validity'] == 'VALID' and
                all(s['status'] == 'CONSISTENT' for s in snapshots), 'accepted job is not valid/PASS')
        for key in ('observed_loss_bytes', 'classified_bytes', 'residual_bytes'):
            require(totals[0][key] == sum(w[key] for w in windows), 'residual total mismatch')
    else:
        require(report['gate'] == report['residual_gate'] == 'INVALID' and
                report['memory_snapshot_validity'] == 'INVALID', 'retry allowed only for consistency INVALID')
        invalid = [s for s in snapshots if s['status'] == 'INVALID']
        require(invalid, 'INVALID without invalid snapshots')
        for s in invalid:
            require(s['selected_attempt'] is None and len(s['attempts']) == 10 and
                    [a['attempt'] for a in s['attempts']] == list(range(1, 11)) and
                    all(a['status'] == 'INCONSISTENT' for a in s['attempts']), 'invalid ten-attempt retry evidence')
    if report['backend'].startswith('memcomp-'):
        cluster = report['cluster_metadata']
        require(cluster['integrity_gate'] == cluster['teardown_gate'] == 'PASS' and cluster['issues'] == [],
                'metadata integrity/teardown failure')


def parse_occupancy(snapshot, account):
    """Parse the real debug-only memory_occupancy schema, including zero bins."""
    block = memory_account.block(snapshot, telemetry.MEMCOMP_OCCUPANCY)
    lines = block.lines[:-1] if block.lines and block.lines[-1] == '' else block.lines
    fields = {}
    for line in lines:
        match = re.fullmatch(r'([a-z0-9_.]+) (0|[1-9][0-9]*)', line)
        require(match is not None, 'malformed occupancy row')
        key, value = match.groups()
        require(key not in fields, 'duplicate occupancy field: ' + key)
        fields[key] = int(value)
    fixed = ('version', 'observer_dynamic_bytes', 'fixed_cluster_header_bytes', 'fixed_key_header_bytes', 'fixed_probe_bytes')
    require(all(k in fields for k in fixed), 'missing occupancy headers')
    require(fields['version'] == 1 and fields['fixed_cluster_header_bytes'] == 144 and
            fields['fixed_key_header_bytes'] > 0, 'occupancy layout/version mismatch')
    clusters, keys = {}, {}
    for name, value in fields.items():
        if name in fixed:
            continue
        match = re.fullmatch(r'cluster\.(0|[1-9][0-9]*)\.([a-z0-9_]+)', name)
        key_match = re.fullmatch(r'key\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.([01])\.([a-z0-9_]+)', name)
        if match:
            clusters.setdefault(match[1], {})[match[2]] = value
        elif key_match:
            owner, nid, shmem, attr = key_match.groups()
            keys.setdefault(owner + '.' + nid + '.' + shmem, {})[attr] = value
        else:
            raise ValueError('unknown occupancy field: ' + name)
    bins = {f'order{s}_slots' for s in SPANS} | {f'span{s}_records' for s in SPANS}
    counts = {'capacity_slots', 'used_slots', 'dead_slots', 'stored_nonzero_pages'}
    grouped = defaultdict(list)
    for cluster in clusters.values():
        require(set(cluster) == counts | bins | {'ownerid', 'nid', 'shmem'}, 'missing/unknown per-cluster occupancy/span fields')
        require(cluster['capacity_slots'] == 512 and cluster['shmem'] in (0, 1) and
                str(cluster['ownerid']) in account['objcgs'], 'cluster capacity/owner mismatch')
        require(cluster['dead_slots'] <= cluster['used_slots'] <= 512 and
                cluster['stored_nonzero_pages'] <= cluster['used_slots'], 'impossible cluster occupancy')
        require(sum(cluster[f'order{s}_slots'] for s in SPANS) == cluster['stored_nonzero_pages'], 'order slot bins do not reconcile')
        require(sum(s * cluster[f'span{s}_records'] for s in SPANS) <= cluster['used_slots'], 'span records exceed occupied slots')
        grouped['.'.join(str(cluster[k]) for k in ('ownerid', 'nid', 'shmem'))].append(cluster)
    require(set(grouped) == set(keys), 'per-key inventory differs from clusters')
    for key, row in keys.items():
        require(set(row) == counts | bins | {'clusters'}, 'missing/unknown per-key occupancy/span fields')
        require(row['clusters'] == len(grouped[key]), 'per-key cluster count mismatch')
        for field in counts | bins:
            require(row[field] == sum(c[field] for c in grouped[key]), 'per-key total mismatch: ' + field)
    v = account['values']
    # pins==0/RCU allocations cannot be pinned for occupancy, but their bytes
    # must remain in dynamic accounting until actually freed.
    require(0 <= v['clusters'] - len(clusters) <= v['retired_clusters'] and len(keys) <= v['keys'],
            'allocated/visible cluster inventory mismatch')
    for field in ('used_slots', 'dead_slots', 'stored_nonzero_pages'):
        require(sum(c[field] for c in clusters.values()) == v[field], 'occupancy/account mismatch: ' + field)
    require(sum(c[f'span{s}_records'] for c in clusters.values() for s in SPANS) <= v['span_records'] and
            sum(s * c[f'span{s}_records'] for c in clusters.values() for s in SPANS) <= v['span_members'],
            'bound spans exceed all pending records')
    return dict(phase=snapshot.phase, clusters=clusters, keys=keys,
                allocated_unpinnable_clusters=v['clusters'] - len(clusters), span_sizes=list(SPANS),
                observer_dynamic_bytes=fields['observer_dynamic_bytes'], fixed_probe_bytes=fields['fixed_probe_bytes'],
                fixed_cluster_header_bytes=fields['fixed_cluster_header_bytes'], fixed_key_header_bytes=fields['fixed_key_header_bytes'])


def verify_clusters(report, log, enforce_density=False):
    cluster = report['cluster_metadata']
    require(cluster['source'] == telemetry.MEMCOMP_ACCOUNT and cluster['integrity_gate'] == 'PASS' and
            cluster['teardown_gate'] == 'PASS' and cluster['issues'] == [], 'cluster report is incomplete')
    # analyze() retains supplemental implicit swap-layout records in the log.
    # Match its original-source classification, not a fresh classification of
    # blocks already filtered by analyze() (which could hide a foreign source).
    supplemental = report['supplemental_layout_records']
    require(isinstance(supplemental, list), 'missing supplemental layout inventory')
    excluded = set()
    for row in supplemental:
        require(row['role'] == 'supplemental_layout_not_measurement' and row['raw_sources'] and
                all(name.startswith(memory_account.SWAP_LAYOUT) for name in row['raw_sources']),
                'invalid supplemental layout record')
        matches = [s for s in log.snapshots if s.phase == row['phase'] and s.ns == row['ns']]
        require(len(matches) == 1 and not matches[0].explicit and not matches[0].limits and
                sorted(matches[0].blocks) == row['raw_sources'] and id(matches[0]) not in excluded,
                'ambiguous/modified supplemental layout record')
        excluded.add(id(matches[0]))
    samples = [s for s in log.snapshots if s.phase != 'boot' and
               not memory_account.is_thp_policy_event(s) and id(s) not in excluded]
    require(tuple(s.phase for s in samples) == PHASES and
            tuple(s['phase'] for s in cluster['snapshots']) == PHASES, 'cluster snapshot inventory mismatch')
    seen, occupancy, accounts = set(), [], []
    for snapshot, saved in zip(samples, cluster['snapshots']):
        account = telemetry.memcomp_accounting(snapshot, 4096)
        v = account['values']
        require(v['cluster_header_bytes'] == 144 and v['xa_node_bytes'] == 576, 'expected 144B header / 576B XA nodes')
        require(v['slot_bytes'] == v['clusters'] * 4096 and v['side_bytes'] == v['clusters'] * 1024,
                'missing allocated slot pages/sidecars')
        require(account['total_bytes'] == sum(v[k] for k in COMPONENTS) + 576 * v['cluster_xa_nodes'],
                'full dynamic metadata does not reconcile')
        require(not (seen - account['objcgs'].keys()), 'lost objcg tombstone')
        seen.update(account['objcgs'])
        require(all(same(saved[k], account[k]) for k in ('values', 'objcgs', 'total_bytes')), 'saved metadata differs from raw')
        accounts.append(dict(phase=snapshot.phase, **account))
        occupancy.append(parse_occupancy(snapshot, account))
    final = accounts[-1]
    require(seen and all(o['payload_bytes'] == o['metadata_bytes'] == 0 for o in final['objcgs'].values()),
            'each observed objcg must have zero teardown charge')
    chosen = next(a for a in accounts if a['phase'] == DENSITY_PHASE)
    pages, total = chosen['values']['stored_nonzero_pages'], chosen['total_bytes']
    if enforce_density:
        require(integer(pages, 'stored_nonzero_pages', 1) <= 131072, 'combined sample exceeds 512 MiB')
        require(2 * total <= 21 * pages, '512 MiB combined dynamic metadata exceeds 10.5 B/stored nonzero page')
    return dict(snapshots=accounts, occupancy=occupancy, teardown_objcgs=final['objcgs'],
                density=dict(phase=DENSITY_PHASE, dynamic_metadata_bytes=total, stored_nonzero_pages=pages,
                             bytes_per_nonzero_page=total/pages if pages else None, limit=10.5,
                             gate_applied=enforce_density, cluster_header_bytes=144, xa_node_bytes=576,
                             retired_bytes_are_subset=True, fixed_objects_included=False, probe_overhead_included=False))


def elf_defined_symbols(data):
    """Read bounded x86_64 ELF64 relocatable symbols for omitted nm sizes."""
    require(isinstance(data, bytes) and len(data) >= 64, 'truncated ELF header')
    header = struct.unpack_from('<16sHHIQQQIHHHHHH', data)
    ident, kind, machine, version, _, phoff, shoff, _, ehsize, _, phnum, shsize, count, names = header
    require(ident[:7] == b'\x7fELF\x02\x01\x01' and
            kind == 1 and machine == 62 and version == 1 and ehsize == 64,
            'expected little-endian x86_64 ELF64 ET_REL')
    require(phoff == 0 and phnum == 0 and shsize == 64 and 0 < count < 0xff00 and
            shoff >= 64 and shoff <= len(data) and count <= (len(data) - shoff) // 64 and
            names < count, 'invalid ELF section table')
    sections = [struct.unpack_from('<IIQQQQIIQQ', data, shoff + i * 64) for i in range(count)]
    require(sections[0] == (0,) * 10, 'invalid ELF null section')

    def contents(section):
        offset, size = section[4:6]
        require(offset <= len(data) and size <= len(data) - offset, 'ELF section out of bounds')
        return data[offset:offset + size]

    for section in sections[1:]:
        if section[1] != 8:  # SHT_NOBITS has no stored section bytes.
            contents(section)
    symbols = defaultdict(list)
    tables = [section for section in sections if section[1] == 2]  # SHT_SYMTAB
    require(tables, 'ELF symbol table missing')
    for table in tables:
        require(table[9] == 24 and table[5] >= 24 and table[5] % 24 == 0 and
                0 < table[6] < count and table[7] <= table[5] // 24,
                'invalid ELF symbol table')
        strings = sections[table[6]]
        require(strings[1] == 3, 'ELF symbol strings are not SHT_STRTAB')
        names_data, entries = contents(strings), contents(table)
        require(names_data[:1] == b'\0' and names_data[-1:] == b'\0' and
                entries[:24] == b'\0' * 24, 'invalid ELF string/null symbol table')
        for offset in range(24, len(entries), 24):
            name_at, info, _, index, value, size = struct.unpack_from('<IBBHQQ', entries, offset)
            require(name_at < len(names_data), 'ELF symbol name out of bounds')
            end = names_data.find(b'\0', name_at)
            require(end >= 0, 'unterminated ELF symbol name')
            name = names_data[name_at:end].decode()
            require(index != 0xffff and (index < count or index >= 0xff00),
                    'invalid/extended ELF symbol section index')
            nm_kind = None
            if 0 < index < count:
                section = sections[index]
                require(value <= section[5] and size <= section[5] - value,
                        'ELF symbol exceeds section bounds')
                flags = section[2]
                # Only ordinary local/global definitions in allocated sections
                # prove BSD nm b/d/r/t rows. NOTYPE includes initcall labels.
                if flags & 2 and info >> 4 in (0, 1) and info & 15 in (0, 1, 2):
                    nm_kind = 't' if flags & 4 else 'b' if section[1] == 8 else 'd' if flags & 1 else 'r'
                    if info >> 4 == 1:
                        nm_kind = nm_kind.upper()
            if name:
                symbols[name].append((value, size, nm_kind))
    return symbols


def nm_rows(data, elf=None):
    rows, symbols = [], None
    for line in data.decode().splitlines():
        if not line.strip() or re.fullmatch(r'\s*[Uwv]\s+\S.*', line):
            continue
        match = re.fullmatch(r'\s*([0-9]+)\s+([0-9]+)\s+([A-Za-z?])\s+(\S.*)', line)
        if match is None:
            missing = re.fullmatch(r'\s*([0-9]+)\s+([A-Za-z?])\s+(\S.*)', line)
            require(missing is not None and elf is not None, 'invalid decimal nm transcript; omitted size needs ELF')
            address, kind, name = missing.groups()
            if symbols is None:
                symbols = elf_defined_symbols(elf)
            candidates = symbols.get(name, [])
            require(len(candidates) == 1 and candidates[0] == (int(address), 0, kind),
                    'omitted nm size lacks unique matching zero-size ELF definition')
            size = '0'
        else:
            address, size, kind, name = match.groups()
        if kind in 'bBdDrR':
            rows.append(dict(symbol=name, type=kind, bytes=int(size), address=int(address)))
    require(rows, 'no fixed symbols in nm transcript')
    return sorted(rows, key=lambda r: (r['symbol'], r['type'], r['bytes'], r['address']))


def symbol_change(before, after):
    def grouped(rows):
        out = defaultdict(list)
        for row in rows:
            out[row['symbol']].append(dict(type=row['type'], bytes=row['bytes']))
        return out
    left, right = grouped(before), grouped(after)
    result = dict(added=[], removed=[], resized=[], type_changed=[])
    for name in sorted(left.keys() | right.keys()):
        a, b = left[name], right[name]
        if a == b:
            continue
        row = dict(symbol=name, old=a, new=b, old_bytes=sum(r['bytes'] for r in a), new_bytes=sum(r['bytes'] for r in b))
        row['delta_bytes'] = row['new_bytes'] - row['old_bytes']
        if not a:
            result['added'].append(row)
        elif not b:
            result['removed'].append(row)
        else:
            if sorted(r['bytes'] for r in a) != sorted(r['bytes'] for r in b):
                result['resized'].append(row)
            if sorted(r['type'] for r in a) != sorted(r['type'] for r in b):
                result['type_changed'].append(row)
    result.update(old_sum_bytes=sum(r['bytes'] for r in before), new_sum_bytes=sum(r['bytes'] for r in after))
    result['delta_bytes'] = result['new_sum_bytes'] - result['old_sum_bytes']
    return result


def verify_fixed(path, bindings, evidence):
    report = evidence.json(path)
    require(report['schema'] == 'v423-j4-fixed-objects-v1' and report['symbol_types'] == 'bBdDrR' and
            report['included_in_production_dynamic_ratio'] is False, 'fixed-object report schema/ratio mismatch')
    inputs = report['inputs']
    def read_input(key):
        item = inputs[key]
        return evidence.read(item['path'], item['sha256'])
    receipt = decode(read_input('build_receipt'))
    require(same(receipt, report['build_receipt']) and receipt['schema'] == 'v423-j4-fixed-object-build-v1', 'fixed build receipt mismatch')
    config = read_input('config')
    config_hash = digest(config)
    require(config_hash == receipt['config_sha256'] == report['config_sha256'] == bindings['kernel_receipts']['memcomp']['sha256']['.config'],
            'fixed objects must use cohort production config')
    require(b'CONFIG_MEMCOMP=y\n' in config and b'CONFIG_DEBUG_FS=y\n' in config and
            not re.search(rb'^CONFIG_(?:KUNIT|MEMCOMP_KUNIT_TEST)=y$', config, re.M), 'not a production config')
    require(set(report['builds']) == set(receipt['builds']) == {'old', 'new', 'probe'}, 'old/current/probe objects all required')
    rows, compilers = {}, []
    for label in ('old', 'new', 'probe'):
        saved, build = report['builds'][label], receipt['builds'][label]
        commit = ENTRY80 if label == 'old' else bindings['build_source']['memcomp']
        require(saved['source_commit'] == build['source_commit'] == commit, 'fixed object source mismatch')
        require(saved['config_sha256'] == build['config_sha256'] == config_hash, 'fixed config differs')
        compiler = build.get('compiler', receipt.get('compiler'))
        require(isinstance(compiler, dict) and same(compiler, saved['compiler']) and
                isinstance(compiler['version'], str) and compiler['version'].strip(), 'compiler evidence missing/mismatch')
        compilers.append((hexhash(compiler['sha256']), compiler['version']))
        data = read_input(label + '_nm')
        require(digest(data) == saved['nm_sha256'] == build['nm_sha256'], 'nm receipt hash mismatch')
        require(hexhash(saved['object_sha256']) == hexhash(build['object_sha256']), 'object receipt hash mismatch')
        obj = read_input(label + '_object') if label + '_object' in inputs else None
        if obj is not None:
            require(digest(obj) == build['object_sha256'], 'object bytes differ from receipt')
        rows[label] = nm_rows(data, obj)
        require(same(saved['rows'], rows[label]) and type(saved['sum_bytes']) is int and
                saved['sum_bytes'] == sum(r['bytes'] for r in rows[label]) and type(saved['symbol_count']) is int and saved['symbol_count'] == len(rows[label]),
                'fixed-object totals/rows differ from actual nm')
    require(len(set(compilers)) == 1, 'fixed comparison compiler differs')
    production = symbol_change(rows['old'], rows['new'])
    overhead = symbol_change(rows['new'], rows['probe'])
    require(all(same(report['production_change'][k], v) for k, v in production.items()), 'fixed production difference mismatch')
    probe = report['probe_overhead']
    require(probe['provided'] is True and probe['included_in_production_dynamic_ratio'] is False and
            all(same(probe['static_symbol_delta'][k], v) for k, v in overhead.items()), 'probe static overhead mismatch')
    read_input('reporter')  # Retain identity, never execute arbitrary reporter code.
    return report


def verify_metadata(meta, status, job, bindings, context, evidence, directory):
    require(isinstance(status, list) and len(status) == 1, 'missing/ambiguous boot status')
    status = status[0]
    require(all(status[k] is True for k in ('valid', 'lifecycle_safe_to_continue', 'data_integrity')) and
            status['kernel_or_data_failure'] is False, 'execution/data/lifecycle failed')
    source = bindings['build_source'][job['kernel']]
    hashes = bindings['kernel_receipts'][job['kernel']]['sha256']
    require(meta['source'] == source and meta['kernel_sha256'] == hashes['bzImage'] and
            meta['config_sha256'] == hashes['.config'], 'job kernel/source/config binding mismatch')
    evidence.read(directory/'.config', hashes['.config'])
    require(meta['backend'] == job['backend'] and meta['algorithm_requested'] == 'zstd' and meta['label'] == 'w1' and
            meta['workload_argv'] == job['command'] and type(meta['guest_memory_mib']) is int and meta['guest_memory_mib'] == 1024 and
            meta['snapshot_memory_consistent'] is True and meta['snapshot_counter_protocol'] == PROTOCOL and
            meta['corpus_sampling_protocol'] == SAMPLING, 'job protocol/backend/command mismatch')
    require(meta['fresh_sparse_disk'] is True and meta['fresh_data_disk'] is True and
            same(meta['corpus_identity'], bindings['image_identity']) and
            meta['corpus_header_page_sha256'] == bindings['image_header_sha256'] and
            same(meta['corpus'], bindings['image_header']), 'job freshness/corpus binding mismatch')
    require(same(meta['binary_sha256'], context['binary_hashes']), 'job binaries differ from frozen tools')
    expected = {Path(p).name: value for p, value in context['frozen'].items() if Path(p).parent == HERE}
    require(same(meta['workload_source_hashes'], expected), 'job producer hashes differ from freeze')
    require(meta['qemu_helper_sha256'] == context['frozen'][str(HERE.parent/'e2e/tests/run_e2e_qemu.py')] and
            meta['corpus_format_tool_sha256'] == context['frozen'][str(HERE.parent/'corpus/pack.py')], 'helper/corpus tool binding mismatch')


def summarize(root, fixed_objects):
    """Return fail-closed JSON, including diagnostics; never writes evidence."""
    out = dict(schema='v423-j4-summary-v1', J4_complete=False, J5=False, cohort='cohort-v4',
               memory_snapshot_protocol=PROTOCOL, accepted_jobs=0, jobs=[], errors=[])
    evidence = Evidence()
    try:
        evidence.read(Path(__file__).absolute())
        root = Path(root).absolute()
        require(root == root.resolve() and root.is_dir(), 'cohort directory missing/symlink')
        require(not (root/'stopped.json').exists(), 'cohort has a stop/failure record')
        bindings = evidence.json(root/'bindings.json')
        context = verify_bindings(bindings, evidence)
        complete = evidence.json(root/'complete.json')
        require(complete['all_34_jobs_accepted'] is True and complete['reused_accepted_jobs'] is False and
                complete['formal_matrix'] is False and complete['J4_complete'] is False and complete['J5'] is False and
                complete['cohort'] == 'cohort-v4' and complete['stop_at'] == 'J.4' and
                complete['memory_snapshot_protocol'] == PROTOCOL and type(complete['threshold_bytes']) is int and
                complete['threshold_bytes'] == 500000 and complete['build_source'] == bindings['build_source'],
                'invalid/incomplete cohort completion receipt')
        journal = evidence.json(root/'journal.json')
        require(type(complete['actual_boots']) is int and complete['actual_boots'] == len(journal), 'boot count mismatch')
        require(34 <= len(journal) <= 68, 'wrong actual boot count')
        out['fixed_objects'] = verify_fixed(fixed_objects, bindings, evidence)
        cursor, expected_dirs, raw_inodes = 0, set(), set()
        for job in expected_jobs():
            attempts = []
            for attempt in (1, 2):
                require(cursor < len(journal), 'journal incomplete: ' + job['name'])
                row = journal[cursor]
                require(row['job'] == job['name'] and type(row['attempt']) is int and row['attempt'] == attempt,
                        'journal population/order/retry mismatch')
                require(type(row['run_exit']) is int and row['run_exit'] == 0 and
                        all(row[k] is True for k in ('execution_valid', 'lifecycle_safe', 'data_integrity')) and
                        row['kernel_failure'] is False, 'journal execution/data/lifecycle failure')
                directory = root/(job['name'] + '-attempt' + str(attempt))
                require(directory == directory.resolve() and directory.is_dir(), 'job evidence missing/symlink')
                expected_dirs.add(directory.name)
                meta = evidence.json(directory/'metadata.json')
                status = evidence.json(directory/'status.json')
                verify_metadata(meta, status, job, bindings, context, evidence, directory)
                path = directory/'run1.log.gz'
                evidence.read(path)
                st = path.stat()
                require((st.st_dev, st.st_ino) not in raw_inodes and st.st_nlink == 1, 'reused/hardlinked raw log')
                raw_inodes.add((st.st_dev, st.st_ino))
                saved = evidence.json(directory/'memory-account.json')
                report, log = memory_account.analyze(path, context['archives'][meta['source']], directory/'.config',
                                                     guest_source=HERE/'guest.c')
                require(same(saved, report), 'saved memory report differs from raw reanalysis: ' + directory.name)
                require(report['backend'] == job['backend'] and report['workload'] == 'w1', 'raw request backend/workload mismatch')
                invalid = report['gate'] == 'INVALID'
                verify_report(report, accepted=not invalid)
                require(type(row['memory_exit']) is int and row['memory_exit'] == (3 if invalid else 0) and
                        row['memory_gate'] == report['gate'] and row['decision'] == ('RETRY_INVALID_ONCE' if invalid else 'ACCEPT'),
                        'journal disagrees with recomputed decision')
                attempts.append(dict(attempt=attempt, gate=report['gate'], directory=str(directory)))
                cursor += 1
                if invalid:
                    require(attempt == 1, 'second INVALID: stop required')
                    continue
                item = dict(job=job['name'], attempts=attempts, residual_windows=report['windows'], residual_totals=report['totals'])
                if job['kernel'] == 'memcomp':
                    item['dynamic_metadata'] = verify_clusters(report, log, job['source'] == 'combined' and job['backend'] == 'memcomp-noswap')
                out['jobs'].append(item)
                out['accepted_jobs'] += 1
                break
        require(cursor == len(journal), 'extra/unconsumed journal jobs')
        require({p.name for p in root.iterdir() if p.is_dir() or p.is_symlink()} == expected_dirs,
                'extra/reused/unreported job directories')
        require(out['accepted_jobs'] == 34, 'not all 34 jobs accepted')
        out['production_dynamic_density'] = next(j['dynamic_metadata']['density'] for j in out['jobs']
            if j['job'] == 'w1-combined-512m-memcomp-noswap-zstd')
        out['probe_runtime_overhead'] = dict(included_in_production_dynamic_ratio=False, snapshots=[
            dict(job=j['job'], phase=s['phase'], observer_dynamic_bytes=s['observer_dynamic_bytes'], fixed_probe_bytes=s['fixed_probe_bytes'])
            for j in out['jobs'] if 'dynamic_metadata' in j for s in j['dynamic_metadata']['occupancy']])
        out['bindings'] = dict(build_source=bindings['build_source'], image_sha256=bindings['image_sha256'],
                               corpus_digest_verification='frozen capture receipt; large image not reread',
                               actual_boots=len(journal), reused_accepted_jobs=False)
        evidence.recheck()
        out['evidence_sha256'] = dict(evidence.hashes)
        out['J4_complete'] = True
    except (OSError, ValueError, TypeError, KeyError, IndexError, AttributeError, EOFError, OverflowError) as error:
        out['errors'].append(type(error).__name__ + ': ' + str(error))
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('cohort', type=Path)
    parser.add_argument('--fixed-objects', type=Path, required=True)
    parser.add_argument('--output', type=Path, help='new JSON output, exclusive create; default stdout')
    args = parser.parse_args(argv)
    result = summarize(args.cohort, args.fixed_objects)
    text = json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + '\n'
    if args.output:
        try:
            with args.output.open('x') as stream:
                stream.write(text)
        except OSError as error:
            result['J4_complete'] = False
            result['errors'].append('output error: ' + str(error))
            print(json.dumps(result, sort_keys=True, allow_nan=False))
            return 1
    else:
        print(text, end='')
    return 0 if result['J4_complete'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
