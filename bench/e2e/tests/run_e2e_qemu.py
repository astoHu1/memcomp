#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Owned-disk QEMU supervisor. No timeout/sudo monitor may delete the disk.

The unprivileged relay owns a cancellation pipe to the sudo supervisor. Its
exit (including SIGKILL) produces EOF; only the supervisor owns cleanup. A
fork/exec gate pins the actual child with pidfd BEFORE it can execute QEMU.
Thus there is no root pidfile, sudo-PID inference, or fast-exit handshake race.
The supervisor must prove both actual child exit and its process-group drain.
SIGKILL of the supervisor leaves the disk, never an unsafe shell trap unlink.
"""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import select
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

WORK = (Path.home() / 'memcomp-work').resolve()
DATA_MAX_MB = 256


def require(ok, message):
    if not ok:
        raise RuntimeError(message)


def work_path(path):
    path = Path(path).resolve()
    require(path.is_relative_to(WORK), 'scratch must stay under memcomp-work')
    require(not any(c.isspace() for c in str(path)) and ',' not in str(path),
            'unsafe QEMU scratch path')
    return path


def integer(text):
    require(re.fullmatch(r'0|[1-9][0-9]*', text) is not None, 'invalid integer')
    return int(text)


def smp_spec(text):
    """Validate the QEMU -smp topology string and return its CPU count."""
    parts = text.split(',')
    require(parts and re.fullmatch(r'[1-9][0-9]*', parts[0]) is not None,
            'invalid SMP CPU count')
    cpus = int(parts[0])
    allowed = {
        'maxcpus', 'drawers', 'books', 'sockets', 'dies', 'clusters',
        'modules', 'cores', 'threads',
    }
    values = {}
    for item in parts[1:]:
        key, sep, value = item.partition('=')
        require(sep and key in allowed and key not in values and
                re.fullmatch(r'[1-9][0-9]*', value) is not None,
                'invalid SMP topology: ' + item)
        values[key] = int(value)
    if 'maxcpus' in values:
        require(values['maxcpus'] >= cpus, 'SMP maxcpus below initial CPUs')
    topology = [values[key] for key in
                ('drawers', 'books', 'sockets', 'dies', 'clusters',
                 'modules', 'cores', 'threads') if key in values]
    if topology:
        product = 1
        for value in topology:
            product *= value
        require(product == values.get('maxcpus', cpus),
                'SMP topology does not match CPU count')
    return cpus


def duration(text):
    match = re.fullmatch(r'([0-9]+(?:\.[0-9]+)?)([smhd]?)', text)
    require(match is not None, 'invalid timeout')
    seconds = float(match[1]) * {'': 1, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]
    require(0 < seconds < float('inf'), 'timeout must be finite and positive')
    return seconds


def validate_extra(words):
    # Fullmatrix NUMA remains supported; never accept block/network/chardev,
    # daemonization, a second kernel/initrd, monitor/QMP, or config-file options.
    require(len(words) % 2 == 0, 'QEMU_EXTRA must contain option/value pairs')
    for option, spec in zip(words[::2], words[1::2]):
        require(option in ('-object', '-numa'), 'unsafe QEMU_EXTRA option: ' + option)
        parts = spec.split(',')
        if option == '-object':
            require(parts[0] == 'memory-backend-ram', 'only anonymous RAM backend allowed')
            allowed = {'id': r'[A-Za-z][A-Za-z0-9_-]*', 'size': r'[1-9][0-9]*[KMGT]?',
                       'prealloc': r'on|off', 'merge': r'on|off', 'dump': r'on|off'}
            required = {'id', 'size'}
        else:
            require(parts[0] in ('node', 'dist', 'cpu'), 'unsupported NUMA specification')
            allowed = {'nodeid': r'[0-9]+', 'cpus': r'[0-9]+(?:-[0-9]+)?',
                       'memdev': r'[A-Za-z][A-Za-z0-9_-]*', 'mem': r'[0-9]+[KMGT]?',
                       'src': r'[0-9]+', 'dst': r'[0-9]+', 'val': r'[0-9]+',
                       'node-id': r'[0-9]+', 'socket-id': r'[0-9]+',
                       'core-id': r'[0-9]+', 'thread-id': r'[0-9]+'}
            required = set()
        keys = set()
        for item in parts[1:]:
            key, sep, value = item.partition('=')
            require(sep and key in allowed and key not in keys and
                    re.fullmatch(allowed[key], value) is not None,
                    'unsafe/duplicate QEMU_EXTRA property: ' + item)
            keys.add(key)
        require(required <= keys, 'incomplete memory backend')
    return words


def corpus_uid():
    """Use the original caller across sudo, never root ownership by accident."""
    if os.geteuid() == 0 and 'SUDO_UID' in os.environ:
        return integer(os.environ['SUDO_UID'])
    return os.getuid()


class CorpusImage:
    """Read-only input, never a Scratch: no create/truncate/export/unlink path.

    Directory-fd traversal forbids symlinks, including ancestor replacements.
    sudo may close relay fds, so the supervisor reopens and compares the exact
    identity supplied by the relay, which keeps its original fd until exit.
    The actual QEMU then inherits this fixed fd, not a re-resolved filename.
    """
    def __init__(self, path, uid=None, expected=None, header_sha256=None):
        self.fd = None
        self.uid = corpus_uid() if uid is None else uid
        self.header_sha256 = header_sha256
        root = WORK / 'corpus'
        path = Path(path).expanduser()
        require('..' not in path.parts, 'corpus parent traversal forbidden')
        self.path = Path(os.path.abspath(path))
        require(self.path != root and self.path.is_relative_to(root),
                'corpus input must stay under memcomp-work/corpus')
        directory = None
        try:
            flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
            directory = os.open('/', flags)
            current = Path('/')
            for part in self.path.parts[1:-1]:
                opened = os.open(part, flags, dir_fd=directory)
                os.close(directory)
                directory = opened
                current /= part
                st = os.fstat(directory)
                if current == root or current.is_relative_to(root):
                    require(st.st_uid == self.uid and stat.S_IMODE(st.st_mode) == 0o700,
                            'corpus ancestors must be caller-owned private 0700 directories')
                else:
                    require(st.st_uid in (0, self.uid) and not st.st_mode & 0o022,
                            'corpus ancestor owner/permissions unsafe')
            self.fd = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW |
                              os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=directory)
            require(3 <= self.fd < 65536, 'corpus fd outside QEMU descriptor range')
            st = os.fstat(self.fd)
            require(stat.S_ISREG(st.st_mode) and st.st_uid == self.uid and
                    stat.S_IMODE(st.st_mode) == 0o600 and st.st_nlink == 1,
                    'corpus requires caller-owned regular nonsymlink single-link 0600 input')
            self.identity = (st.st_dev, st.st_ino, st.st_uid, st.st_mode,
                             st.st_size, st.st_mtime_ns, st.st_ctime_ns)
            named = os.stat(self.path.name, dir_fd=directory, follow_symlinks=False)
            require((named.st_dev, named.st_ino) == self.identity[:2] and
                    Path('/proc/self/fd/%d' % self.fd).resolve(strict=True) == self.path,
                    'corpus path/inode changed while opening')
            if expected is not None:
                require(type(expected) in (tuple, list) and len(expected) == 7 and
                        all(type(value) is int and value >= 0 for value in expected) and
                        tuple(expected) == self.identity,
                        'corpus identity changed across supervisor handoff')
            if self.header_sha256 is not None:
                require(type(self.header_sha256) is str and
                        re.fullmatch(r'[0-9a-f]{64}', self.header_sha256) is not None,
                        'invalid expected corpus header digest')
                header = os.pread(self.fd, 4096, 0)
                require(len(header) == 4096 and hashlib.sha256(header).hexdigest() == self.header_sha256,
                        'corpus header differs from host preflight')
                after = os.fstat(self.fd)
                require((after.st_dev, after.st_ino, after.st_uid, after.st_mode,
                         after.st_size, after.st_mtime_ns, after.st_ctime_ns) == self.identity,
                        'corpus changed while checking expected header')
        except BaseException:
            self.close()
            raise
        finally:
            if directory is not None:
                os.close(directory)

    def verify(self):
        st = os.fstat(self.fd)
        require((st.st_dev, st.st_ino, st.st_uid, st.st_mode,
                 st.st_size, st.st_mtime_ns, st.st_ctime_ns) == self.identity and
                st.st_nlink == 1, 'pinned corpus metadata changed')
        # Recheck pathname and private ancestors immediately before launch.
        other = CorpusImage(self.path, self.uid, self.identity, self.header_sha256)
        other.close()

    def check_outputs(self, a):
        # Even internal supervisor flags must never turn this input into an
        # acknowledgment or data-export destination. Output writers already
        # enforce their pinned identities before writing after this preflight.
        for path, dev, ino in (
                (a.data_output, a.data_dev, a.data_ino),
                (a.ack_file, a.ack_dev, a.ack_ino)):
            require((dev, ino) != self.identity[:2] and
                    (path is None or Path(path).resolve() != self.path),
                    'corpus input cannot be an output destination')

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


class Scratch:
    def __init__(self, work, megabytes, name='memcomp-swap.owned.img'):
        self.directory = None
        self.path = None
        self.identity = None
        self.dir_identity = None
        self.dirfd = None
        if not megabytes:
            return
        self.directory = Path(tempfile.mkdtemp(prefix='.memcomp-qemu-', dir=work))
        st = self.directory.lstat()
        self.dir_identity = (st.st_dev, st.st_ino)
        dirfd = None
        pinned = False
        fd = None
        try:
            dirfd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            ds = os.fstat(dirfd)
            require((ds.st_dev, ds.st_ino) == self.dir_identity,
                    'scratch directory identity changed: retain scratch')
            self.dirfd = dirfd
            pinned = True
            if os.geteuid() == 0 and 'SUDO_UID' in os.environ:
                # Return only our new directory to the sudo caller. A killed
                # supervisor must still retain its root-owned disk, not leave
                # an inaccessible root-owned directory for the caller too.
                uid = integer(os.environ['SUDO_UID'])
                gid = integer(os.environ['SUDO_GID'])
                os.fchown(dirfd, uid, gid)
            require(name in ('memcomp-swap.owned.img', 'memcomp-data.owned.img'),
                    'unknown owned disk role')
            fd = os.open(name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=dirfd)
            st = os.fstat(fd)
            self.identity = (st.st_dev, st.st_ino)
            self.path = self.directory / name
            os.ftruncate(fd, megabytes << 20)
            os.fsync(fd)
        except BaseException:
            try:
                self.cleanup(True)  # No child can have been launched yet.
            finally:
                # A failed constructor cannot hand its retained pin to a caller.
                if self.dirfd is not None:
                    os.close(self.dirfd)
                    self.dirfd = None
            raise
        finally:
            if fd is not None:
                os.close(fd)
            if dirfd is not None and not pinned:
                os.close(dirfd)

    def export_data(self, a, death_proven):
        # Called only by the supervisor, after actual child/group/descendant
        # death. Copy the WHOLE sparse disk, including its header and unused
        # tail. Guest metadata cannot discard failed-run diagnostic bytes; the
        # unprivileged runner performs byte-count/SHA256 verification later.
        require(death_proven, 'actual QEMU death unknown: retain data disk')
        require(self.dirfd is not None, 'no pinned data directory: retain data disk')
        directory = os.dup(self.dirfd)
        source = target = None
        try:
            ds = os.fstat(directory)
            require((ds.st_dev, ds.st_ino) == self.dir_identity and
                    self.directory.resolve(strict=True) == self.directory,
                    'data directory identity changed: retain data disk')
            source = os.open(self.path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
            st = os.fstat(source)
            require(stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == self.identity and
                    st.st_size == (integer(a.data_mb) << 20) and st.st_nlink == 1,
                    'data disk inode/type/size changed: retain data disk')
            target = os.open(work_path(a.data_output),
                             os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            out = os.fstat(target)
            owner = integer(os.environ['SUDO_UID']) if 'SUDO_UID' in os.environ else os.getuid()
            require(stat.S_ISREG(out.st_mode) and
                    (out.st_dev, out.st_ino) == (a.data_dev, a.data_ino) and
                    out.st_uid == owner and out.st_nlink == 1 and out.st_size == 0,
                    'data output is not the relay-owned empty inode')
            count = st.st_size
            left = count
            while left:
                chunk = os.read(source, min(left, 1024 * 1024))
                require(chunk, 'short data-disk export: retain data disk')
                if chunk.strip(b'\0'):
                    view = memoryview(chunk)
                    while view:
                        n = os.write(target, view)
                        require(n > 0, 'short data-output write: retain data disk')
                        view = view[n:]
                else:
                    os.lseek(target, len(chunk), os.SEEK_CUR)
                left -= len(chunk)
            os.ftruncate(target, count)
            os.fsync(target)
            current = Path(a.data_output).lstat()
            require((current.st_dev, current.st_ino) == (a.data_dev, a.data_ino) and
                    stat.S_ISREG(current.st_mode) and os.fstat(target).st_size == count,
                    'data output path/size changed: retain data disk')
            print('e2e-qemu: data_export=%s dev=%d inode=%d bytes=%d format=raw-disk' %
                  (a.data_output, a.data_dev, a.data_ino, count), flush=True)
        finally:
            for fd in (target, source, directory):
                if fd is not None:
                    os.close(fd)

    def cleanup(self, death_proven):
        if self.directory is None:
            return
        require(death_proven, 'actual QEMU death unknown: retain scratch')
        require(self.dirfd is not None, 'no pinned scratch directory: retain scratch')
        ds = os.fstat(self.dirfd)
        require(stat.S_ISDIR(ds.st_mode) and (ds.st_dev, ds.st_ino) == self.dir_identity,
                'pinned scratch directory identity changed: retain scratch')
        original = (ds.st_uid, ds.st_gid, stat.S_IMODE(ds.st_mode))
        frozen = False
        try:
            if os.geteuid() == 0:
                # A caller-owned parent permits renaming this whole directory,
                # but cannot replace an internal filename after this freeze.
                os.fchown(self.dirfd, 0, 0)
                frozen = True
                os.fchmod(self.dirfd, 0o700)
                locked = os.fstat(self.dirfd)
                require(locked.st_uid == 0 and stat.S_IMODE(locked.st_mode) == 0o700,
                        'scratch directory freeze failed: retain scratch')
            current = self.directory.lstat()
            require(stat.S_ISDIR(current.st_mode) and
                    (current.st_dev, current.st_ino) == self.dir_identity and
                    self.directory.resolve(strict=True) == self.directory,
                    'scratch directory identity changed: retain scratch')
            if self.path is not None:
                st = os.stat(self.path.name, dir_fd=self.dirfd, follow_symlinks=False)
                require(stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == self.identity,
                        'scratch inode changed: retain scratch')
                os.unlink(self.path.name, dir_fd=self.dirfd)
                self.path = None
            # Guarded empty-directory cleanup assumes no hostile concurrent
            # parent/ancestor rename. It is not an atomic expected-inode rmdir.
            current = self.directory.lstat()
            require(stat.S_ISDIR(current.st_mode) and
                    (current.st_dev, current.st_ino) == self.dir_identity and
                    self.directory.resolve(strict=True) == self.directory,
                    'scratch directory path changed: retain empty directory')
            self.directory.rmdir()  # Empty only; never recursively remove data.
            self.directory = None
        finally:
            if frozen and self.directory is not None:
                # Restore access only through the pinned object, then do no
                # further deletion. A failed cleanup must not receive an ACK.
                os.fchown(self.dirfd, original[0], original[1])
                os.fchmod(self.dirfd, original[2])
            if self.directory is None:
                os.close(self.dirfd)
                self.dirfd = None


def group_members(pgid):
    members = []
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            fields = (directory / 'stat').read_text().rsplit(') ', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        # Permission/format errors are deliberately not death proof.
        if int(fields[2]) == pgid and fields[0] not in ('Z', 'X'):
            members.append(int(directory.name))
    return members


class OwnedQemu:
    """Actual direct child identity, pinned before exec, not the sudo monitor."""
    def __init__(self, command, pass_fds=()):
        self.command = list(command)
        self.pass_fds = tuple(sorted(set(pass_fds)))
        require(all(type(fd) is int and 3 <= fd < 65536 for fd in self.pass_fds),
                'invalid QEMU inherited fd')
        for fd in self.pass_fds:
            os.fstat(fd)
        self.pid = None
        self.fd = None
        self.started = False
        self.reaped = False
        self.returncode = None
        self.starttime = None
        self.ready = False

    def launch(self):
        gate_r, gate_w = os.pipe2(os.O_CLOEXEC)
        ready_r, ready_w = os.pipe2(os.O_CLOEXEC)
        try:
            pid = os.fork()
        except BaseException:
            for fd in (gate_r, gate_w, ready_r, ready_w):
                os.close(fd)
            raise
        if pid == 0:
            try:
                os.close(gate_w); os.close(ready_r)
                os.setsid()
                # This child cannot execute/fork QEMU until parent pins its PID.
                os.write(ready_w, b'R'); os.close(ready_w)
                if os.read(gate_r, 1) != b'G':
                    os._exit(125)
                os.close(gate_r)
                null = os.open('/dev/null', os.O_RDONLY)
                os.dup2(null, 0); os.close(null)
                # Preserve only explicit read-only corpus input fds. Never
                # leak supervisor cancellation/ack/directory fds into QEMU.
                first = 3
                for fd in self.pass_fds:
                    os.closerange(first, fd)
                    os.set_inheritable(fd, True)
                    first = fd + 1
                os.closerange(first, 65536)
                os.execv(self.command[0], self.command)
            except BaseException:
                os._exit(127)
        self.pid = pid
        self.started = True
        os.close(gate_r); os.close(ready_w)
        try:
            self.fd = os.pidfd_open(pid)
            poll = select.poll(); poll.register(ready_r, select.POLLIN | select.POLLHUP)
            require(bool(poll.poll(5000)) and os.read(ready_r, 1) == b'R',
                    'QEMU exec gate failed')
            self.ready = True
            self.starttime = int(Path('/proc/%d/stat' % pid).read_text().rsplit(') ', 1)[1].split()[19])
            print('e2e-qemu: actual_pid=%d starttime=%d command_sha256=%s' %
                  (pid, self.starttime, hashlib.sha256(json.dumps(self.command).encode()).hexdigest()), flush=True)
            os.write(gate_w, b'G')
        finally:
            os.close(gate_w); os.close(ready_r)

    def dead(self):
        if not self.started:
            return True
        if self.fd is None:
            return False
        poll = select.poll(); poll.register(self.fd, select.POLLIN)
        return bool(poll.poll(0))

    def finish(self):
        # Keep the child unreaped while using its process-group number. Its
        # zombie reserves this PID, so it cannot be reused for an unrelated PG.
        require(self.fd is not None, 'no actual QEMU pidfd: retain scratch')
        for sig in (signal.SIGTERM, signal.SIGKILL):
            if self.ready:
                members = group_members(self.pid)
                if members:
                    try:
                        os.killpg(self.pid, sig)
                    except ProcessLookupError:
                        require(not group_members(self.pid), 'QEMU group disappeared without proof')
            if not self.dead():
                try:
                    signal.pidfd_send_signal(self.fd, sig)
                except ProcessLookupError:
                    require(self.dead(), 'QEMU signal race without death proof')
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                if self.dead() and (not self.ready or not group_members(self.pid)):
                    break
                time.sleep(.01)
            if self.dead() and (not self.ready or not group_members(self.pid)):
                break
        require(self.dead() and (not self.ready or not group_members(self.pid)),
                'actual QEMU/group still live or unknown: retain scratch')
        _, status = os.waitpid(self.pid, 0)
        self.reaped = True
        self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def close(self):
        if self.fd is not None:
            os.close(self.fd); self.fd = None


def command(a, scratch, data=None, corpus=None):
    accel = ['-enable-kvm', '-cpu', 'host'] if a.accelerator == 'kvm' else ['-accel', 'tcg', '-cpu', 'max']
    words = [a.qemu, *accel, '-smp', a.smp, '-m', a.memory,
             '-kernel', a.kernel, '-initrd', a.initrd]
    if scratch.path is not None:
        if data is not None and data.path is not None:
            words += ['-drive', 'file=%s,if=none,id=comparebacking,format=raw,cache=unsafe' % scratch.path,
                      '-device', 'virtio-blk-pci,drive=comparebacking']
        else:
            words += ['-drive', 'file=%s,if=virtio,format=raw,cache=unsafe' % scratch.path]
    if data is not None and data.path is not None:
        # The first virtio disk stays vda even for memcomp-noswap. Only the
        # second disk (vdb, serial checked in PID1) carries raw comparison data.
        words += ['-drive', 'file=%s,if=none,id=comparedata,format=raw,cache=writeback' % data.path,
                  '-device', 'virtio-blk-pci,drive=comparedata,serial=memcomp-data']
    if corpus is not None:
        require(scratch.path is not None and data is not None and data.path is not None,
                'corpus requires existing vda swap and vdb data slots')
        words += ['-drive', 'file=/proc/self/fd/%d,if=none,id=comparecorpus,format=raw,readonly=on' % corpus.fd,
                  '-device', 'virtio-blk-pci,drive=comparecorpus,serial=memcomp-corpus']
    env = a.guest_env + ('' if integer(a.swap_mb) else ' NO_SWAP=1')
    return words + validate_extra(a.extra_word) + ['-append',
            'console=ttyS0 panic=-1 oops=panic loglevel=6 ' + env,
            '-nographic', '-no-reboot', '-net', 'none']



def become_subreaper():
    # Adopt any double-forked descendants if the VM dies first. These processes
    # remain ours to identify/reap even if they escaped the original session.
    libc = ctypes.CDLL(None, use_errno=True)
    require(libc.prctl(36, 1, 0, 0, 0) == 0, 'cannot enable child subreaper')


def adopted_children():
    found = []
    for directory in Path('/proc').iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            fields = (directory / 'stat').read_text().rsplit(') ', 1)[1].split()
        except (FileNotFoundError, ProcessLookupError):
            continue
        if int(fields[1]) == os.getpid():
            found.append(int(directory.name))
    return found


def drain_descendants():
    deadline = time.monotonic() + 5
    while True:
        children = adopted_children()
        if not children:
            return
        for pid in children:
            # These are unreaped direct children; PIDs cannot be reused until
            # waitpid. Pin BEFORE signaling; never kill an arbitrary numeric PID.
            fd = os.pidfd_open(pid)
            try:
                poll = select.poll(); poll.register(fd, select.POLLIN)
                if not poll.poll(0):
                    signal.pidfd_send_signal(fd, signal.SIGKILL)
                if poll.poll(100):
                    os.waitpid(pid, 0)
            finally:
                os.close(fd)
        require(time.monotonic() < deadline, 'descendants live/unknown: retain scratch')


def supervise(a):
    canceled = [False]
    def cancel(_sig, _frame):
        canceled[0] = True
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, cancel)
    scratch = data = None
    vm = None
    safe = True
    rc = 125
    cleaned = False
    try:
        corpus = getattr(a, 'corpus_input', None)
        require(not getattr(a, 'corpus_image', None) or corpus is not None,
                'corpus must be validated before supervisor entry')
        if corpus is not None:
            corpus.verify()
        become_subreaper()
        scratch = Scratch(work_path(a.work), integer(a.swap_mb))
        if scratch.path is not None:
            print('e2e-qemu: scratch=%s dev=%d inode=%d' %
                  (scratch.path, *scratch.identity), flush=True)
        if integer(a.data_mb):
            data = Scratch(work_path(a.work), integer(a.data_mb), 'memcomp-data.owned.img')
            print('e2e-qemu: data_scratch=%s dev=%d inode=%d' %
                  (data.path, *data.identity), flush=True)
        if corpus is None:
            vm = OwnedQemu(command(a, scratch, data))
        else:
            corpus.verify()
            vm = OwnedQemu(command(a, scratch, data, corpus), pass_fds=(corpus.fd,))
            print('e2e-qemu: corpus_readonly dev=%d inode=%d uid=%d size=%d' %
                  (corpus.identity[0], corpus.identity[1], corpus.uid, corpus.identity[4]), flush=True)
        # Set unknown before launch: an exception after fork must not authorize
        # cleanup merely because launch did not return normally.
        safe = False
        vm.launch()
        deadline = time.monotonic() + duration(a.timeout)
        control = select.poll(); control.register(0, select.POLLIN | select.POLLHUP | select.POLLERR)
        timed_out = False
        while not vm.dead():
            if canceled[0] or control.poll(0):
                rc = 143
                break
            if time.monotonic() >= deadline:
                rc = 124; timed_out = True
                break
            time.sleep(.01)
        else:
            rc = 0
        actual_rc = vm.finish()
        drain_descendants()
        safe = True
        if rc == 0:
            rc = actual_rc if actual_rc >= 0 else 128 - actual_rc
        print('e2e-qemu: actual_exit=%d timed_out=%d actual_death_verified=1 group_drained=1' %
              (actual_rc, timed_out), flush=True)
    except BaseException as exc:
        print('e2e-qemu: failure=' + str(exc), file=sys.stderr, flush=True)
        rc = 125
    finally:
        if vm is not None:
            if not vm.reaped and vm.started:
                try:
                    vm.finish(); drain_descendants(); safe = True
                except BaseException as exc:
                    print('e2e-qemu: cleanup_failure=' + str(exc), file=sys.stderr, flush=True)
                    safe = False; rc = 125
            elif not vm.started:
                safe = True
            vm.close()
        all_cleaned = True
        for disk, role in ((data, 'data'), (scratch, 'swap')):
            if disk is None:
                continue
            try:
                if role == 'data':
                    disk.export_data(a, safe)
                disk.cleanup(safe)
            except BaseException as exc:
                print('e2e-qemu: scratch_retained role=%s reason=%s' %
                      (role, exc), file=sys.stderr, flush=True)
                all_cleaned = False
                rc = 125
        if all_cleaned and scratch is not None:
            cleaned = True
            print('e2e-qemu: scratch_cleanup=exact_inode_only', flush=True)
    if a.ack_file:
        try:
            ack = work_path(a.ack_file)
            fd = os.open(ack, os.O_WRONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                st = os.fstat(fd)
                require(stat.S_ISREG(st.st_mode) and
                        (st.st_dev, st.st_ino) == (a.ack_dev, a.ack_ino),
                        'supervisor acknowledgment inode changed')
                value = json.dumps({'token': a.ack_token, 'rc': rc,
                                    'death_proven': safe, 'scratch_cleaned': cleaned}).encode()
                require(os.write(fd, value) == len(value), 'short lifecycle acknowledgment')
                os.fsync(fd)
            finally:
                os.close(fd)
        except BaseException as exc:
            print('e2e-qemu: acknowledgment_failed=' + str(exc), file=sys.stderr, flush=True)
            rc = 125
    return rc


def read_ack(fd, token):
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 4097)
    require(len(raw) <= 4096, 'oversized lifecycle acknowledgment')
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None  # Writer is not finished, never provisional success.
    require(isinstance(value, dict) and value.get('token') == token and
            type(value.get('rc')) is int and 0 <= value['rc'] <= 255 and
            type(value.get('death_proven')) is bool and type(value.get('scratch_cleaned')) is bool,
            'invalid lifecycle acknowledgment')
    return value


def unlink_ack(path, identity):
    st = path.lstat()
    require(stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == identity,
            'acknowledgment path/inode changed: retain it')
    path.unlink()


def relay(a):
    # No QEMU-disk creation/deletion here; only the supervisor handles those.
    # The relay creates the acknowledgment and a separate caller-owned export.
    fd, name = tempfile.mkstemp(prefix='.memcomp-e2e-ack-', dir=work_path(a.work))
    ack_path = Path(name)
    st = os.fstat(fd); identity = (st.st_dev, st.st_ino)
    token = secrets.token_hex(32)
    print('e2e-qemu: ack_file=%s dev=%d inode=%d' % (ack_path, *identity), flush=True)
    output_fd = None
    data_args = []
    try:
        corpus = getattr(a, 'corpus_input', None)
        if corpus is not None:
            corpus.verify()
            # sys.argv still contains the caller's relative/tilde spelling.
            # Override it with the pinned absolute name across sudo HOME/cwd.
            data_args += ['--corpus-image', str(corpus.path),
                          '--corpus-identity', json.dumps(corpus.identity)]
            if corpus.header_sha256 is not None:
                data_args += ['--corpus-header-sha256', corpus.header_sha256]
        if integer(a.data_mb):
            output_fd = os.open(work_path(a.data_output), os.O_RDWR | os.O_CREAT | os.O_EXCL |
                                os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
            output_st = os.fstat(output_fd)
            data_args += ['--data-dev', str(output_st.st_dev), '--data-ino', str(output_st.st_ino)]
    except BaseException:
        if output_fd is not None:
            os.close(output_fd)
        os.close(fd)
        unlink_ack(ack_path, identity)  # Supervisor has not been started.
        raise
    supervisor = [shutil.which('sudo') or 'sudo', '-n', sys.executable, '-B',
                  str(Path(__file__).resolve()), '--supervise', *sys.argv[1:],
                  '--ack-file', str(ack_path), '--ack-dev', str(st.st_dev),
                  '--ack-ino', str(st.st_ino), '--ack-token', token, *data_args]
    parent = os.getppid()
    parentfd = os.pidfd_open(parent)
    child = None
    acknowledged = False
    canceled = [False]
    def cancel(_sig, _frame):
        canceled[0] = True
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, cancel)
    poll = select.poll(); poll.register(parentfd, select.POLLIN)
    try:
        child = subprocess.Popen(supervisor, stdin=subprocess.PIPE, start_new_session=True)
        deadline = time.monotonic() + duration(a.timeout) + 30
        rc = None
        terminal_deadline = None
        while True:
            monitor_rc = child.poll()
            if (canceled[0] or poll.poll(0) or time.monotonic() >= deadline or
                    monitor_rc is not None) and not child.stdin.closed:
                child.stdin.close()  # EOF cancels actual supervisor, not sudo PID.
                terminal_deadline = time.monotonic() + 20
                if canceled[0] or poll.poll(0):
                    rc = 143
                elif time.monotonic() >= deadline:
                    rc = 124
            value = read_ack(fd, token)
            if value is not None:
                acknowledged = True
                require(value['death_proven'] and value['scratch_cleaned'],
                        'actual death/cleanup not acknowledged; disk retained')
                # A monitor's successful exit cannot promote a canceled VM.
                if monitor_rc is None:
                    monitor_rc = child.wait(timeout=5)
                require(monitor_rc == value['rc'], 'sudo/actual-supervisor exit mismatch')
                return rc if rc is not None else value['rc']
            if terminal_deadline is not None and time.monotonic() >= terminal_deadline:
                print('e2e-qemu: supervisor_completion_unknown scratch_retained', file=sys.stderr)
                return 125
            time.sleep(.01)
    finally:
        if child is not None and not child.stdin.closed:
            child.stdin.close()
        os.close(parentfd); os.close(fd)
        if output_fd is not None:
            os.close(output_fd)
        # Unknown supervisor => retain acknowledgment too. No cleanup inference
        # is made from the sudo PID. The disk always belongs to the supervisor.
        if acknowledged or child is None:
            unlink_ack(ack_path, identity)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--supervise', action='store_true', help=argparse.SUPPRESS)
    p.add_argument('--check-work', action='store_true')
    p.add_argument('--ack-file', help=argparse.SUPPRESS)
    p.add_argument('--ack-dev', type=int, help=argparse.SUPPRESS)
    p.add_argument('--ack-ino', type=int, help=argparse.SUPPRESS)
    p.add_argument('--ack-token', help=argparse.SUPPRESS)
    p.add_argument('--work', required=True)
    p.add_argument('--qemu')
    p.add_argument('--kernel')
    p.add_argument('--initrd')
    p.add_argument('--guest-env', default='')
    p.add_argument('--memory', default='1024')
    p.add_argument('--swap-mb', default='2048')
    p.add_argument('--data-mb', default='0', help='opt-in 16..256 MiB comparison raw disk')
    p.add_argument('--data-output', help='new relay-owned export file under memcomp-work')
    p.add_argument('--corpus-image', help='existing private raw input, read-only vdc after vda/vdb')
    p.add_argument('--corpus-identity', help=argparse.SUPPRESS)
    p.add_argument('--corpus-expected-identity', help='host preflight seven-field stat identity')
    p.add_argument('--corpus-header-sha256', help='host preflight SHA256 of the exact 4096-byte header')
    p.add_argument('--data-dev', type=int, help=argparse.SUPPRESS)
    p.add_argument('--data-ino', type=int, help=argparse.SUPPRESS)
    p.add_argument('--smp', default='4')
    p.add_argument('--accelerator', choices=('kvm', 'tcg'), default='kvm')
    p.add_argument('--timeout', default='900')
    p.add_argument('--extra-word', action='append', default=[])
    a = p.parse_args()
    corpus = None
    try:
        work_path(a.work)
        require(a.supervise or not any(x is not None for x in
                (a.ack_file, a.ack_dev, a.ack_ino, a.ack_token, a.data_dev, a.data_ino, a.corpus_identity)),
                'internal acknowledgment options are supervisor-only')
        if a.ack_file:
            work_path(a.ack_file)
            require(a.ack_dev is not None and a.ack_ino is not None and
                    re.fullmatch(r'[0-9a-f]{64}', a.ack_token or '') is not None,
                    'invalid acknowledgment identity')
        if a.check_work:
            return 0
        require(all((a.qemu, a.kernel, a.initrd)), 'missing runtime input')
        a.qemu = str(Path(a.qemu).resolve(strict=True))
        for name in (a.qemu, a.kernel, a.initrd):
            require(Path(name).is_file(), 'non-regular runtime input: ' + name)
        require(integer(a.memory) > 0 and smp_spec(a.smp) > 0, 'memory/CPUs must be positive')
        require(integer(a.swap_mb) <= (1 << 40), 'swap capacity overflow')
        data_mb = integer(a.data_mb)
        require(data_mb == 0 or 16 <= data_mb <= DATA_MAX_MB, 'data capacity outside 16..256 MiB')
        require(bool(data_mb) == bool(a.data_output), 'data disk/output must be requested together')
        if data_mb:
            require(integer(a.swap_mb) > 0, 'data disk requires the separate vda backing slot')
            a.data_output = str(work_path(a.data_output))
            if a.supervise:
                require(a.data_dev is not None and a.data_ino is not None,
                        'data export requires the relay-owned inode identity')
        else:
            require(a.data_dev is None and a.data_ino is None, 'unexpected data output identity')
        require(a.corpus_image or (a.corpus_identity is None and
                a.corpus_expected_identity is None and a.corpus_header_sha256 is None),
                'unexpected corpus identity/header binding')
        if a.corpus_image:
            require(data_mb > 0 and integer(a.swap_mb) > 0,
                    'corpus requires existing vda swap and vdb data slots')
            expected = json.loads(a.corpus_identity) if a.corpus_identity is not None else None
            outer = json.loads(a.corpus_expected_identity) if a.corpus_expected_identity is not None else None
            corpus = CorpusImage(a.corpus_image, expected=outer if outer is not None else expected,
                                 header_sha256=a.corpus_header_sha256)
            if expected is not None:
                require(type(expected) in (tuple, list) and tuple(expected) == corpus.identity,
                        'corpus supervisor identity disagrees with preflight')
            corpus.check_outputs(a)
            a.corpus_image = str(corpus.path)
            a.corpus_input = corpus
        duration(a.timeout); validate_extra(a.extra_word)
        return supervise(a) if a.supervise else relay(a)
    except BaseException as exc:
        print('e2e-qemu: refusal=' + str(exc), file=sys.stderr, flush=True)
        return 125
    finally:
        if corpus is not None:
            corpus.close()


if __name__ == '__main__':
    raise SystemExit(main())
