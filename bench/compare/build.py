#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Build one neutral comparison kernel; no tests and no Git checkout."""
import argparse
import hashlib
import os
from pathlib import Path
import shlex
import shutil
import signal
import time
import subprocess

HERE = Path(__file__).resolve().parent
BACKENDS = ("memcomp", "zram", "zram-writeback", "zswap", "swap", "zswap-crosscheck")
STOCK = "83b4161a63b87ce40d9f24f09b5b006f63d95b7c"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tree", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--backend", choices=BACKENDS, required=True)
    parser.add_argument("--llvm", default="1", help="1 or absolute Clang bin/ prefix")
    parser.add_argument("--jobs", type=int, default=24)
    a = parser.parse_args()
    if os.geteuid() == 0 or not 1 <= a.jobs <= 24:
        parser.error("run as an unprivileged owner with 1..24 jobs")
    tree, out, dest = a.tree.resolve(), a.out.resolve(), a.dest.resolve()
    if dest.exists():
        parser.error("destination must be new, not an overwritten result")
    if out == tree or tree in out.parents or out in tree.parents:
        parser.error("use an independent O= cache outside the source tree")
    head = subprocess.check_output(["git", "-C", str(tree), "rev-parse", "HEAD"], text=True).strip()
    dirty = subprocess.check_output(["git", "-C", str(tree), "status", "--porcelain"], text=True)
    if dirty:
        parser.error("source must be clean")
    stock_backend = a.backend in ("zram", "zram-writeback", "zswap", "swap")
    if stock_backend and head != STOCK:
        parser.error("stock backends require the exact Linux 6.12.23 baseline")
    if not stock_backend and head == STOCK:
        parser.error("memcomp and cross-check require the completed series")
    if not stock_backend:
        excluded = ["lib", "crypto", "include/crypto", "mm/zsmalloc.c", "mm/zpool.c", "arch"]
        subprocess.run(["git", "-C", str(tree), "diff", "--exit-code", STOCK, head, "--", *excluded], check=True)
    dest.mkdir(parents=True)
    out.mkdir(parents=True, exist_ok=True)
    (dest / "tmp").mkdir()
    env = dict(os.environ, TMPDIR=str(dest / "tmp"), KBUILD_BUILD_USER="nakixii", KBUILD_BUILD_HOST="memcomp-builder")
    make = ["make", "-C", str(tree), "O=" + str(out), "ARCH=x86_64", "LLVM=" + a.llvm, "LLVM_IAS=1"]
    def run(name, command):
        (dest / (name + ".command.txt")).write_text(shlex.join(command) + "\n")
        print("START", a.backend, name, flush=True)
        with (dest / (name + ".log")).open("w") as log:
            p = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while p.poll() is None:
                    available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines() if line.startswith("MemAvailable:"))) * 1024
                    if available < 2 * 1024**3 or shutil.disk_usage("/mnt/c").free < 32 * 1024**3:
                        os.killpg(p.pid, signal.SIGTERM)
                        try:
                            p.wait(timeout=30)
                        except subprocess.TimeoutExpired:
                            os.killpg(p.pid, signal.SIGKILL)
                            p.wait()
                        raise RuntimeError("resource safety floor reached; owned build stopped")
                    time.sleep(2)
            except BaseException:
                if p.poll() is None:
                    os.killpg(p.pid, signal.SIGTERM)
                    try:
                        p.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        os.killpg(p.pid, signal.SIGKILL)
                        p.wait()
                raise
        (dest / (name + ".exit.txt")).write_text(str(p.returncode) + "\n")
        if p.returncode:
            raise SystemExit(p.returncode)
    (dest / "source.txt").write_text(head + "\nbackend=" + a.backend + "\n")
    run("defconfig", make + ["x86_64_defconfig"])
    backend = "zswap" if a.backend == "zswap-crosscheck" else a.backend
    fragments = [HERE / "configs/common.fragment", HERE / ("configs/" + backend + ".fragment")]
    for fragment in fragments:
        shutil.copyfile(fragment, dest / fragment.name)
    run("merge", ["bash", str(tree / "scripts/kconfig/merge_config.sh"), "-m", "-O", str(out), str(out / ".config"), *map(str, fragments)])
    run("olddefconfig", make + ["olddefconfig"])
    config = (out / ".config").read_text()
    requested = {}
    for fragment in fragments:
        for line in fragment.read_text().splitlines():
            if line.startswith("CONFIG_") and "=" in line:
                key, value = line.split("=", 1)
                requested[key] = value
    effective = {}
    for line in config.splitlines():
        if line.startswith("CONFIG_") and "=" in line:
            key, value = line.split("=", 1)
            effective[key] = value
    rejected = [key + "=" + value + " effective=" + effective.get(key, "n")
                for key, value in requested.items() if effective.get(key, "n") != value]
    (dest / "config-requests.txt").write_text("\n".join(rejected) + ("\n" if rejected else ""))
    shutil.copyfile(out / ".config", dest / ".config")
    if rejected:
        raise SystemExit("rejected configuration requests: " + "; ".join(rejected))
    run("kernel", make + ["-j" + str(a.jobs), "bzImage"])
    shutil.copyfile(out / "arch/x86/boot/bzImage", dest / "bzImage")
    (dest / "SHA256SUMS").write_text("".join(hashlib.file_digest((dest / name).open("rb"), "sha256").hexdigest() + "  " + name + "\n" for name in ("bzImage", ".config")))
    print("BUILT", a.backend, head, flush=True)


if __name__ == "__main__":
    main()
