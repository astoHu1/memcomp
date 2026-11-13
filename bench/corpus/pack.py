#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Private, deterministic corpus transport. Import read_header for preflight.

Never logs page bytes or copies manifest identity fields into the image.
Only pack() opens capture .pages files. read_header() reads image payload only
when explicitly requested with with_digest=True.
"""
import argparse
import errno
import fcntl
import hashlib
import json
import mmap
import os
from pathlib import Path
import re
import stat
import struct
import sys
from contextlib import ExitStack
from dataclasses import dataclass

PAGE_SIZE = HEADER_BYTES = 4096
MAGIC = b"MEMCOMP-CORPUS1\0"
MAX_SOURCES = 16
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = (1 << 63) - 1
DIGEST_CHUNK_BYTES = 1024 * 1024
LABEL = re.compile(r"[A-Za-z0-9_.-]{1,31}", re.ASCII)
HEX = re.compile(r"[0-9a-fA-F]{64}", re.ASCII)
# Linux UAPI, not filesystem-specific ioctls.
BLKROGET, BLKGETSIZE64, BLKSSZGET = 0x125E, 0x80081272, 0x1268


class CorpusError(ValueError):
    """Invalid metadata, privacy policy, or corpus integrity (no payload text)."""


def _require(ok, message):
    if not ok:
        raise CorpusError(message)


def _layout(pages):
    _require(type(pages) is int and 0 < pages <= (MAX_IMAGE_BYTES - 8191) // 32,
             "invalid total page count")
    data_offset = (HEADER_BYTES + pages * 32 + PAGE_SIZE - 1) & -PAGE_SIZE
    _require(pages <= (MAX_IMAGE_BYTES - data_offset) // PAGE_SIZE,
             "image offset overflow")
    return data_offset, data_offset + pages * PAGE_SIZE


def _valid_label(label):
    return (isinstance(label, str) and LABEL.fullmatch(label) is not None
            and label != "combined")


def _parse_header(header, actual_size):
    """The single Python format parser; no page or sidecar reads."""
    _require(len(header) == HEADER_BYTES, "truncated header")
    _require(header[:16] == MAGIC, "invalid magic")
    version, page_size, count, header_bytes = struct.unpack_from("<4I", header, 16)
    _require((version, page_size, header_bytes) == (1, PAGE_SIZE, HEADER_BYTES),
             "unsupported header format")
    _require(1 <= count <= MAX_SOURCES, "invalid source count")
    digest = hashlib.sha256(header[:4064]).digest()
    _require(digest == header[4064:], "header hash mismatch")
    total, hashes_offset, data_offset, image_bytes = struct.unpack_from("<4Q", header, 32)
    expected_offset, expected_bytes = _layout(total)
    _require(hashes_offset == HEADER_BYTES and data_offset == expected_offset
             and image_bytes == expected_bytes and actual_size == expected_bytes,
             "invalid offsets or image size")
    sources, labels, next_page = [], set(), 0
    for index in range(count):
        entry = header[64 + index * 128:64 + (index + 1) * 128]
        raw_label, separator, padding = entry[:32].partition(b"\0")
        _require(bool(separator) and not any(padding), "invalid label termination")
        try:
            label = raw_label.decode("ascii")
        except UnicodeError:
            raise CorpusError("invalid label encoding") from None
        _require(_valid_label(label) and label not in labels, "invalid or duplicate label")
        start, pages = struct.unpack_from("<2Q", entry, 32)
        _require(start == next_page and 0 < pages <= total - next_page,
                 "non-contiguous source index")
        _require(not any(entry[112:]), "nonzero source reserved bytes")
        sources.append(dict(label=label, start_page=start, pages=pages,
                            data_sha256=entry[48:80].hex(),
                            hashes_sha256=entry[80:112].hex()))
        labels.add(label)
        next_page += pages
    _require(next_page == total, "source index does not cover image")
    _require(not any(header[64 + count * 128:4064]), "nonzero header reserved bytes")
    return dict(total_pages=total, page_size=page_size, image_bytes=image_bytes,
                header_sha256=digest.hex(), sources=sources)


def _absolute(path):
    path = Path(path)
    _require(path.is_absolute() and ".." not in path.parts, "absolute canonical path required")
    return path


def _no_export_directory(fd, name):
    _require(name.casefold() not in {".git", "results", "public"},
             "Git/results/public directories are forbidden")
    try:
        os.stat(".git", dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return
    raise CorpusError("Git worktrees are forbidden")


def _open_dir(path, *, private=False, export_guard=False, create_leaf=False):
    """Walk directory FDs with O_NOFOLLOW; never resolve through a symlink."""
    path = _absolute(path)
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for index, part in enumerate(path.parts[1:]):
            if export_guard:
                _require(part.casefold() not in {".git", "results", "public"},
                         "Git/results/public directories are forbidden")
            if create_leaf and index == len(path.parts) - 2:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                            os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            if export_guard:
                _no_export_directory(fd, part)
        if private:
            st = os.fstat(fd)
            _require(st.st_uid == os.getuid() and stat.S_IMODE(st.st_mode) == 0o700,
                     "private directory must be owned by caller and mode 0700")
        return fd
    except BaseException:
        os.close(fd)
        raise


def _open_relative(root_fd, name):
    _require(isinstance(name, str) and name and "\0" not in name,
             "invalid source path")
    parts = name.split("/")
    _require(not name.startswith("/") and all(p not in {"", ".", ".."} for p in parts),
             "source path must stay beneath corpus root")
    fd = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW |
                            os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                       os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def _regular_private(fd):
    st = os.fstat(fd)
    _require(stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid()
             and stat.S_IMODE(st.st_mode) == 0o600, "input must be owned regular file mode 0600")
    return st


def _read_exact(fd, count):
    data = bytearray()
    while len(data) < count:
        try:
            chunk = os.read(fd, count - len(data))
        except InterruptedError:
            continue
        _require(bool(chunk), "truncated source")
        data.extend(chunk)
    return bytes(data)


def _pwrite_all(fd, data, offset):
    view = memoryview(data)
    while view:
        try:
            count = os.pwrite(fd, view, offset)
        except InterruptedError:
            continue
        if count <= 0:
            raise OSError(errno.EIO, "short output write")
        offset += count
        view = view[count:]


def read_header(path, with_identity=False, with_digest=False):
    """Return JSON-safe frozen header metadata, validating header and exact size.

    Regular-file host preflight uses O_RDONLY. For Linux block devices, require
    BLKROGET==1 and O_DIRECT, then read only the header into one mmap-aligned
    4096-byte buffer. Default inspection does not read image payload.

    with_identity returns (descriptor, stat7, full_header_sha256). with_digest
    requires with_identity and a regular file, and appends image_sha256 to that
    tuple. The entire image is hashed on the same FD, starting with the exact
    parsed header and reading only the initial stat size in bounded chunks.
    Both modes check stat7 again after all reads. The image digest fingerprints
    bytes; it does not validate per-page or whole-source hashes against metadata.
    No paths, serials, PIDs, or collection timestamps are returned.
    """
    _require(not with_digest or with_identity, "with_digest requires with_identity")
    path = _absolute(path)
    with ExitStack() as stack:
        parent = _open_dir(path.parent)
        stack.callback(os.close, parent)
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC |
                     os.O_NONBLOCK, dir_fd=parent)
        stack.callback(os.close, fd)
        st = os.fstat(fd)
        _require(not with_digest or stat.S_ISREG(st.st_mode),
                 "with_digest requires a regular image file")
        if stat.S_ISREG(st.st_mode):
            _require(st.st_size >= HEADER_BYTES, "truncated image")
            header = _read_exact(fd, HEADER_BYTES)
            size = st.st_size
        elif stat.S_ISBLK(st.st_mode):
            readonly = struct.unpack("=I", fcntl.ioctl(fd, BLKROGET, bytes(4)))[0]
            _require(readonly == 1, "block device must be read-only")
            size = struct.unpack("=Q", fcntl.ioctl(fd, BLKGETSIZE64, bytes(8)))[0]
            sector = struct.unpack("=I", fcntl.ioctl(fd, BLKSSZGET, bytes(4)))[0]
            _require(0 < sector <= PAGE_SIZE and PAGE_SIZE % sector == 0,
                     "unsupported block alignment")
            _require(hasattr(os, "O_DIRECT"), "O_DIRECT unavailable")
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_DIRECT)
            _require(fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_DIRECT, "O_DIRECT unavailable")
            with mmap.mmap(-1, HEADER_BYTES) as aligned:
                while True:
                    try:
                        count = os.preadv(fd, [aligned], 0)
                        break
                    except InterruptedError:
                        continue
                _require(count == HEADER_BYTES, "short direct header read")
                header = aligned[:]
        else:
            raise CorpusError("image must be a regular file or read-only block device")
        descriptor = _parse_header(header, size)
        if with_digest:
            image_digest = hashlib.sha256(header)
            remaining = size - HEADER_BYTES
            while remaining:
                count = min(remaining, DIGEST_CHUNK_BYTES)
                image_digest.update(_read_exact(fd, count))
                remaining -= count
        final = os.fstat(fd)
        identity = lambda value: (value.st_dev, value.st_ino, value.st_uid, value.st_mode,
                                  value.st_size, value.st_mtime_ns, value.st_ctime_ns)
        _require(identity(final) == identity(st), "image changed while inspecting header")
        if with_identity:
            bound = descriptor, identity(final), hashlib.sha256(header).hexdigest()
            return (*bound, image_digest.hexdigest()) if with_digest else bound
        return descriptor


@dataclass(frozen=True)
class _Source:
    label: str
    pages: int
    file: str
    hash_file: str
    data_hash: bytes
    sidecar_hash: bytes


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate manifest key")
        result[key] = value
    return result


def _manifest(root_fd, manifest_name="manifest.json"):
    _require(isinstance(manifest_name, str) and Path(manifest_name).name == manifest_name
             and manifest_name.endswith(".json"), "invalid manifest filename")
    fd = _open_relative(root_fd, manifest_name)
    try:
        st = _regular_private(fd)
        _require(0 < st.st_size <= MAX_MANIFEST_BYTES, "invalid manifest size")
        raw = _read_exact(fd, st.st_size)
        _require(not os.read(fd, 1), "manifest changed during read")
    finally:
        os.close(fd)
    try:
        manifest = json.loads(raw, object_pairs_hook=_unique_keys)
    except (UnicodeError, json.JSONDecodeError):
        raise CorpusError("invalid manifest JSON") from None
    _require(isinstance(manifest, dict), "manifest must be an object")
    _require(manifest.get("format") == "memcomp-private-corpus-v1"
             and type(manifest.get("page_bytes")) is int
             and manifest["page_bytes"] == PAGE_SIZE
             and manifest.get("complete") is True, "unsupported or incomplete manifest")
    applications = manifest.get("applications")
    _require(isinstance(applications, list) and 1 <= len(applications) <= MAX_SOURCES,
             "invalid manifest source count")
    sources, labels, paths = [], set(), set()
    for app in applications:
        _require(isinstance(app, dict), "invalid application entry")
        label, pages = app.get("app"), app.get("pages")
        _require(_valid_label(label) and label not in labels, "invalid or duplicate app label")
        _require(type(pages) is int and pages > 0 and app.get("complete") is True,
                 "invalid or incomplete source")
        _layout(pages)
        _require(type(app.get("bytes")) is int and app["bytes"] == pages * PAGE_SIZE,
                 "source byte count mismatch")
        names = (app.get("file"), app.get("per_page_hash_file"))
        for name in names:
            _require(isinstance(name, str) and name not in paths, "duplicate or invalid source path")
            paths.add(name)
        digests = (app.get("sha256"), app.get("per_page_hash_sha256"))
        _require(all(isinstance(d, str) and HEX.fullmatch(d) for d in digests),
                 "invalid source digest")
        sources.append(_Source(label, pages, *names, *(bytes.fromhex(d) for d in digests)))
        labels.add(label)
    if "requested_sources" in manifest:
        _require(manifest["requested_sources"] == [s.label for s in sources],
                 "manifest ordering mismatch")
    total = sum(s.pages for s in sources)
    _layout(total)
    for key, expected in (("total_pages", total), ("total_bytes", total * PAGE_SIZE)):
        if key in manifest:
            _require(type(manifest[key]) is int and manifest[key] == expected,
                     "manifest total mismatch")
    return sources


def _header(sources):
    total = sum(s.pages for s in sources)
    data_offset, image_bytes = _layout(total)
    header = bytearray(HEADER_BYTES)
    header[:16] = MAGIC
    struct.pack_into("<4I4Q", header, 16, 1, PAGE_SIZE, len(sources), HEADER_BYTES,
                     total, HEADER_BYTES, data_offset, image_bytes)
    start = 0
    for index, source in enumerate(sources):
        offset = 64 + index * 128
        label = source.label.encode("ascii")
        header[offset:offset + len(label)] = label
        struct.pack_into("<2Q", header, offset + 32, start, source.pages)
        header[offset + 48:offset + 80] = source.data_hash
        header[offset + 80:offset + 112] = source.sidecar_hash
        start += source.pages
    header[4064:] = hashlib.sha256(header[:4064]).digest()
    return bytes(header), data_offset, image_bytes


def _unchanged(before, after):
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns,
            before.st_ctime_ns) == (after.st_dev, after.st_ino, after.st_size,
                                    after.st_mtime_ns, after.st_ctime_ns)


def pack(root, output, manifest_name="manifest.json"):
    """Validate and stream exact pages/sidecars to ONE exclusively published image.

    Both arguments must be explicit absolute private paths outside Git/results/
    public. Sources are only opened underneath root with no symlink traversal.
    Existing final images and stale .partial files are never overwritten.
    """
    root, output = _absolute(root), _absolute(output)
    _require(output.name not in {"", ".", "..", ".git"}, "invalid output name")
    _require(root != output.parent and root not in output.parent.parents,
             "output must be outside source root")
    _require(output.name.casefold() not in {"results", "public"}, "invalid output name")
    with ExitStack() as stack:
        root_fd = _open_dir(root, private=True, export_guard=True)
        stack.callback(os.close, root_fd)
        sources = _manifest(root_fd, manifest_name)
        parent = _open_dir(output.parent, private=True, export_guard=True, create_leaf=True)
        stack.callback(os.close, parent)
        # Pin all source descriptors before output creation; max 32 source FDs.
        files, identities = [], set()
        for source in sources:
            pair = []
            for name, size in ((source.file, source.pages * PAGE_SIZE),
                               (source.hash_file, source.pages * 32)):
                fd = _open_relative(root_fd, name)
                stack.callback(os.close, fd)
                st = _regular_private(fd)
                identity = (st.st_dev, st.st_ino)
                _require(identity not in identities, "source file aliases are forbidden")
                _require(st.st_size == size, "source size mismatch")
                identities.add(identity)
                pair.append((fd, st))
            files.append(pair)
        try:
            os.stat(output.name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise FileExistsError(errno.EEXIST, "output already exists")
        header, data_offset, image_bytes = _header(sources)
        partial = output.name + ".partial"
        fd = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW |
                     os.O_CLOEXEC, 0o600, dir_fd=parent)
        published, partial_exists = False, True
        try:
            os.fchmod(fd, 0o600)
            os.ftruncate(fd, image_bytes)  # all alignment padding is zero
            page_index = 0
            for source, pair in zip(sources, files):
                (data_fd, data_st), (hash_fd, hash_st) = pair
                data_digest, hash_digest = hashlib.sha256(), hashlib.sha256()
                for _ in range(source.pages):
                    page = _read_exact(data_fd, PAGE_SIZE)
                    expected = _read_exact(hash_fd, 32)
                    _require(hashlib.sha256(page).digest() == expected, "per-page hash mismatch")
                    data_digest.update(page)
                    hash_digest.update(expected)
                    _pwrite_all(fd, expected, HEADER_BYTES + page_index * 32)
                    _pwrite_all(fd, page, data_offset + page_index * PAGE_SIZE)
                    page_index += 1
                _require(data_digest.digest() == source.data_hash, "whole-source hash mismatch")
                _require(hash_digest.digest() == source.sidecar_hash, "whole-sidecar hash mismatch")
                _require(not os.read(data_fd, 1) and not os.read(hash_fd, 1), "source grew")
                _require(_unchanged(data_st, os.fstat(data_fd))
                         and _unchanged(hash_st, os.fstat(hash_fd)), "source changed during copy")
            # Recheck earlier sources too, in case they changed during later copies.
            for pair in files:
                for input_fd, before in pair:
                    _require(_unchanged(before, os.fstat(input_fd)), "source changed during copy")
            _pwrite_all(fd, header, 0)
            os.fsync(fd)
            # linkat is atomic and fails EEXIST; unlike rename it cannot clobber.
            # The inode was created O_EXCL above; a partial final is never visible.
            os.link(partial, output.name, src_dir_fd=parent, dst_dir_fd=parent,
                    follow_symlinks=False)
            published = True
            os.fsync(parent)
            os.unlink(partial, dir_fd=parent)
            partial_exists = False
            os.fsync(parent)
        except BaseException:
            if published:
                os.unlink(output.name, dir_fd=parent)
            raise
        finally:
            try:
                if partial_exists:
                    os.unlink(partial, dir_fd=parent)
                    os.fsync(parent)
            finally:
                os.close(fd)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="absolute private corpus directory")
    parser.add_argument("--output", required=True, help="absolute private image path (must not exist)")
    parser.add_argument("--manifest", default="manifest.json", help="explicit private JSON filename within root")
    args = parser.parse_args(argv)
    try:
        pack(args.root, args.output, args.manifest)
    except (CorpusError, OSError) as exc:
        # Never render exception filenames, manifest values, identities or bytes.
        reason = str(exc) if isinstance(exc, CorpusError) else "I/O failure (errno %s)" % exc.errno
        print("corpus pack failed: " + reason, file=sys.stderr)
        return 1
    print("private corpus image created and verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
