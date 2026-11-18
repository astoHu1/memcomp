# SPDX-License-Identifier: GPL-2.0
"""Host judge tests. Fixtures are parser coverage, never guest evidence."""
import json
import unittest

import run as minimal
import s08
import vz

SWAPS = 'Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority'
BINDING = dict(kernel_release='test-arm64', smp=1, swap_mb=0, memory_mb=1024)
LEGACY = dict(BINDING, source_head=minimal.HEAD, kernel_release=minimal.RELEASE)
DRAIN = 'e2e-qemu: actual_exit=0 timed_out=0 actual_death_verified=1 group_drained=1'


def minimal_log(record_size, binding=BINDING, swap=''):
    stored = minimal.PAGES * minimal.PAGE
    payload = stored // 3
    charge = payload + minimal.PAGES * record_size
    slab = minimal.PAGES * 24
    lines = ['RAW proc.swaps', SWAPS, *([swap] if swap else []),
             'BINDING ' + json.dumps(binding), 'KERNEL release=' + binding['kernel_release'],
             'COMPRESSOR zstd', f'WORKER pid=21 pages={minimal.PAGES}']
    for phase in minimal.PHASES:
        sample = dict.fromkeys(minimal.FIELDS, 0)
        sample['phase'] = phase
        if phase == 'filled':
            sample.update(anon=stored, current=stored)
        elif phase == 'compressed':
            sample.update(stored=stored, payload=payload, pool=payload,
                          current=charge + slab, kernel=charge + slab, slab=slab)
        lines += ['SAMPLE ' + json.dumps(sample), f'RAW phase={phase} memory.stat']
        lines += [f'{k} {sample[k]}' for k in ('anon', 'file', 'kernel', *minimal.SUBCOMPONENTS)]
        lines += ['anon_thp 0', f'RAW phase={phase} memory.events', 'max 0', 'oom 0',
                  'oom_kill 0', f'RAW phase={phase} cgroup.procs', '21']
    return '\n'.join(lines + ['MEMCG-COLLECTED', 'MEMCG-END rc=0', ''])


class VzTests(unittest.TestCase):
    def test_receipt_requires_diskless_poweroff(self):
        info = ('backend=Apple Virtualization Framework\nmemory_mb=1024\ncpus=1\n'
                'block_devices=0\ncmdline=test\n')
        host = vz.POWERED_OFF + '\n'
        vz.validate_vz(host, info, 0, 'test')
        for log, receipt, rc in ((host, info, 124), (host * 2, info, 0), ('', info, 0),
                                 (host + 'VZ timeout\n', info, 0),
                                 (host, info.replace('block_devices=0', 'block_devices=1'), 0),
                                 (host, info.replace('cpus=1', 'cpus=2'), 0),
                                 (host, info.replace('cmdline=test', 'cmdline=other'), 0)):
            with self.subTest(log=log, receipt=receipt, rc=rc), self.assertRaises(ValueError):
                vz.validate_vz(log, receipt, rc, 'test')

    def test_mc16_metadata_required_on_top_of_payload_and_slab(self):
        for ksize in (96, 128):
            with self.subTest(ksize=ksize):
                result = minimal.evaluate(minimal_log(ksize), 0, vz_binding=BINDING)
                self.assertEqual(result['status'], 'PASS', result)
        for ksize in (0, 192):
            with self.subTest(ksize=ksize):
                result = minimal.evaluate(minimal_log(ksize), 0, vz_binding=BINDING)
                self.assertIn('memory.current payload+entry charge outside bounds', result['problems'])

    def test_vz_minimal_rejects_swap_binding_mismatch_and_input_drift(self):
        for text, stable in ((minimal_log(96, swap='/dev/vda partition 100 0 -2'), True),
                             (minimal_log(96, dict(BINDING, source_head='other')), True),
                             (minimal_log(96), False)):
            result = minimal.evaluate(text, 0, stable=stable, vz_binding=BINDING)
            self.assertEqual(result['status'], 'FAIL')

    def test_legacy_minimal_still_requires_qemu_and_80_byte_charge(self):
        self.assertEqual(minimal.evaluate(minimal_log(80, LEGACY) + DRAIN, 0)['status'], 'PASS')
        self.assertEqual(minimal.evaluate(minimal_log(80, LEGACY), 0)['status'], 'FAIL')
        self.assertEqual(minimal.evaluate(minimal_log(128, LEGACY) + DRAIN, 0)['status'], 'FAIL')

    def test_s08_transport_does_not_bypass_collector(self):
        self.assertEqual(s08.evaluate('', BINDING, vz.POWERED_OFF, 0)['reason'],
                         'missing/duplicate owned drain')
        self.assertEqual(s08.evaluate('', BINDING, vz.POWERED_OFF, 0,
                                      drain=vz.POWERED_OFF)['reason'], 'missing environment')


if __name__ == '__main__':
    unittest.main()
