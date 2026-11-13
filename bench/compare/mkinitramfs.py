#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Pack prebuilt static comparison tools and explicit guest-only inputs."""
import argparse
import gzip
import io
import os
from pathlib import Path
import subprocess

BACKENDS = ("memcomp-noswap", "memcomp-swap", "zram", "zram-writeback", "zswap", "swap", "zswap-crosscheck")


def pack(tree, binaries, dest, backend, algorithm, label, command, zram_bytes, timeout,
         data_mib=128, memory_mib=1024, snapshot_vmstat_refresh=False,
         snapshot_memory_consistent=False):
    if os.geteuid() == 0:
        raise ValueError("pack as an unprivileged user")
    tree, binaries, dest = map(lambda p: Path(p).resolve(), (tree, binaries, dest))
    if any(any(c.isspace() for c in str(p)) for p in (tree, binaries, dest)):
        raise ValueError("gen_init_cpio paths cannot contain whitespace")
    if backend not in BACKENDS or algorithm not in ("zstd", "lz4", "none"):
        raise ValueError("invalid backend/algorithm")
    if not command or label not in ("w1", "w2", "w3", "w4") or command[0] not in ("/refault", "/shmem", "/app_switch"):
        raise ValueError("unknown workload")
    if not 1 <= len(command) <= 64 or any(len(s) > 4096 or "\n" in s or "\r" in s for s in command):
        raise ValueError("invalid workload arguments")
    if (label == "w1" and command.count("--rewrite") != 1) or (label != "w1" and "--rewrite" in command):
        raise ValueError("v4.6 requires --rewrite exactly once for W1 and never for other workloads")
    if type(timeout) is not int or not 5 <= timeout <= 3600 or (label == "w4" and timeout <= 300):
        raise ValueError("timeout must be 5..3600 seconds and exceed the W4 300-second wait cap")
    if type(data_mib) is not int or not 16 <= data_mib <= 256:
        raise ValueError("data disk must be 16..256 MiB")
    if type(memory_mib) is not int or not 256 <= memory_mib <= 4096:
        raise ValueError("configured guest memory must be 256..4096 MiB")
    if type(snapshot_vmstat_refresh) is not bool:
        raise ValueError("snapshot vmstat refresh must be explicit boolean")
    if snapshot_vmstat_refresh and label != "w1":
        raise ValueError("snapshot vmstat refresh requires W1 boundary ACKs")
    if type(snapshot_memory_consistent) is not bool:
        raise ValueError("consistent memory snapshot must be explicit boolean")
    if snapshot_memory_consistent and (label != "w1" or snapshot_vmstat_refresh):
        raise ValueError("consistent memory snapshot requires W1 and excludes legacy refresh")
    dest.mkdir(parents=True, exist_ok=False)
    tools = ("guest", "refault", "shmem", "app_switch")
    for tool in tools:
        if not (binaries / tool).is_file():
            raise ValueError("missing prebuilt static tool: " + tool)
    helper = binaries / "gen_init_cpio"
    if not helper.is_file():
        raise ValueError("prebuild gen_init_cpio separately; pack never invokes a compiler")
    values = {"COMPARE_DISPOSABLE_GUEST": "disposable comparison VM only\n", "backend": backend,
              "algorithm": algorithm, "workload-label": label, "command": "\n".join(command) + "\n",
              "zram-bytes": str(zram_bytes), "timeout-seconds": str(timeout),
              "data-bytes": str(data_mib * 1024**2),
              "guest-memory-bytes": str(memory_mib * 1024**2),
              "snapshot-vmstat-refresh": str(int(snapshot_vmstat_refresh)),
              "snapshot-memory-consistent": str(int(snapshot_memory_consistent))}
    lines = ["dir /dev 0755 0 0", "nod /dev/console 0600 0 0 c 5 1", "dir /proc 0555 0 0",
             "dir /sys 0555 0 0", "dir /run 0755 0 0", "dir /mnt 0755 0 0", "dir /mnt/test 0755 0 0"]
    for name, content in values.items():
        file = dest / name
        file.write_text(content if content.endswith("\n") else content + "\n")
        lines.append("file /" + name + " " + str(file) + " 0444 0 0")
    for tool in tools:
        name = "init" if tool == "guest" else tool
        lines.append("file /" + name + " " + str(binaries / tool) + " 0755 0 0")
    listing = dest / "initramfs.list"
    listing.write_text("\n".join(lines) + "\n")
    data = subprocess.check_output([str(helper), str(listing)])
    image = dest / "initramfs.gz"
    with image.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as stream:
            stream.write(data)
    return image


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tree", type=Path, required=True)
    p.add_argument("--binaries", type=Path, required=True)
    p.add_argument("--dest", type=Path, required=True)
    p.add_argument("--backend", choices=BACKENDS, required=True)
    p.add_argument("--algorithm", choices=("zstd", "lz4", "none"), required=True)
    p.add_argument("--label", choices=("w1", "w2", "w3", "w4"), required=True)
    p.add_argument("--zram-bytes", type=int, required=True)
    p.add_argument("--memory", type=int, default=1024)
    p.add_argument("--data-mib", type=int, default=128)
    p.add_argument("--timeout", type=int, default=1800)
    p.add_argument("--snapshot-vmstat-refresh", action="store_true",
                   help="diagnostic stock counter fold before snapshots; also affects PCP")
    p.add_argument("--snapshot-memory-consistent", action="store_true",
                   help="W1-only bounded buddy/PCP consistent memory snapshots")
    p.add_argument("command", nargs=argparse.REMAINDER)
    a = p.parse_args()
    command = a.command[1:] if a.command[:1] == ["--"] else a.command
    if not command or not 1 <= a.timeout <= 3600 or not 16 * 1024 * 1024 <= a.zram_bytes <= 16 * 1024**3:
        p.error("explicit workload, timeout 1..3600, and bounded zram size required")
    if not 256 <= a.memory <= 4096:
        p.error("configured guest memory must be 256..4096 MiB")
    print(pack(a.tree, a.binaries, a.dest, a.backend, a.algorithm, a.label, command,
               a.zram_bytes, a.timeout, a.data_mib, a.memory, a.snapshot_vmstat_refresh,
               snapshot_memory_consistent=a.snapshot_memory_consistent))


if __name__ == "__main__":
    main()
