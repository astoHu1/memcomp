#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Build/run the source-attribution verifier in a diskless disposable guest.

The guest is explicitly opt-in, has no network or disk, and never touches host
cgroups or swap. A PASS requires complete marker coverage and unchanged inputs.
This does not prove limits, reclaim, offline/reparent, or the full E2E matrix.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time

HERE = Path(__file__).resolve().parent
DIAGNOSTICS = re.compile(
    r"^.*(?:WARNING:|BUG:|Oops:|Call Trace:|Kernel panic|KASAN:|KFENCE:|"
    r"UBSAN:|general protection|bad page|possible circular locking dependency|"
    r"possible recursive locking|inconsistent lock state).*$", re.M)
PASS_LINE = "RESULT: PASS scope=memcg-source-attribution"
TRACE_RESULTS = (
    "RESULT source_payload_charge PASS",
    "RESULT destination_no_payload_charge PASS",
    "RESULT source_release PASS",
    "PASS real_memcg_attribution",
)
END_LINE = re.compile(r"MEMCG-GUEST: END exit=(\d+)")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_paths(paths):
    return {name: sha(path) if path.exists() else None
            for name, path in paths.items()}


def evaluate(text, rc, before, after):
    """Fail closed: reject malformed, duplicate, or conflicting markers."""
    diagnostics = DIAGNOSTICS.findall(text)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    result_lines = [line for line in lines if line.startswith("RESULT:")]
    end_lines = [line for line in lines if line.startswith("MEMCG-GUEST: END")]
    valid_end = [line for line in end_lines if END_LINE.fullmatch(line)]
    exits = [int(END_LINE.fullmatch(line).group(1)) for line in valid_end]
    forbidden = re.search(
        r"RESULT: FAIL|RESULT: SKIP|MEMCOMP_MEMCG_RESULT|^not ok |# (?:SKIP|XFAIL|XPASS)",
        text, re.M)
    trace_lines = [line for line in lines
                   if line.startswith(("RESULT ", "PASS real_memcg_attribution"))]
    trace_ok = (trace_lines == list(TRACE_RESULTS) and
                PASS_LINE in lines and
                lines.index(TRACE_RESULTS[-1]) < lines.index(PASS_LINE))
    passed = (rc == 0 and before == after and not diagnostics and trace_ok and
              result_lines == [PASS_LINE] and end_lines == valid_end and
              exits == [0] and not forbidden)
    return bool(passed), diagnostics


def positive(value):
    n = int(value)
    if n <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return n


def stop_process_group(process):
    if process.poll() is not None:
        return process.returncode
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
    return process.returncode


def run_guest(command, log_path, timeout):
    timed_out = False
    with log_path.open("w") as log:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                   stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            stop_process_group(process)
        except BaseException:
            stop_process_group(process)
            raise
    return process.returncode, timed_out


def write_receipt(out, receipt):
    (out / "receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("kernel-tree", "kernel-out", "source-manifest", "source-links",
                 "build-receipt", "out"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--cpus", type=positive, default=1)
    parser.add_argument("--memory-mib", type=positive, default=1024)
    parser.add_argument("--timeout", type=positive, default=180)
    args = parser.parse_args()
    tree = args.kernel_tree.resolve()
    kernel_out = args.kernel_out.resolve()
    out = args.out.resolve()
    require(not out.exists(), "refusing to overwrite results")
    require(out != tree and tree not in out.parents, "results must be outside kernel tree")
    manifest = args.source_manifest.resolve()
    links = args.source_links.resolve()
    build_receipt = args.build_receipt.resolve()
    binding = json.loads(build_receipt.read_text())
    kernel = kernel_out / "arch/x86/boot/bzImage"
    config = kernel_out / ".config"
    build_log = build_receipt.parent / "kernel-build.log"
    require(Path(binding["kernel_out"]).resolve() == kernel_out, "kernel output mismatch")
    require(binding["build_exit"] == 0 and
            binding["source_verified_before_and_after_build"] is True,
            "kernel build was not accepted")
    require(sha(build_log) == binding["build_log_sha256"], "kernel build log mismatch")
    flags = config.read_text()
    for flag in ("X86_64", "CGROUPS", "MEMCG", "MEMCOMP", "PROC_PAGE_MONITOR",
                 "TRACEPOINTS", "TRACING", "FTRACE"):
        require("CONFIG_" + flag + "=y\n" in flags,
                "missing prerequisite: " + flag)

    def verify():
        head = subprocess.check_output(
            ["git", "-C", str(tree), "rev-parse", "HEAD"], text=True).strip()
        require(head == binding["source_commit"], "source commit mismatch")
        dirty = subprocess.check_output(
            ["git", "-C", str(tree), "status", "--porcelain"], text=True)
        require(not dirty.strip(), "kernel tree is dirty or mutation is running")
        require(sha(manifest) == binding["source_manifest_sha256"],
                "source manifest mismatch")
        require(sha(links) == binding["source_links_sha256"],
                "source symlink manifest mismatch")
        for name, target in json.loads(links.read_text()).items():
            path = tree / name
            require(path.is_symlink() and os.readlink(path) == target,
                    "source symlink changed: " + name)
        subprocess.run(["sha256sum", "--quiet", "-c", str(manifest)],
                       cwd=tree, check=True)
        require(sha(kernel) == binding["kernel_sha256"], "kernel image mismatch")
        require(sha(config) == binding["config_sha256"], "kernel config mismatch")

    verify()
    out.mkdir(parents=False)
    source = HERE.parent / "memcomp_memcg.c"
    init_source = HERE.parent / "memcomp_memcg_init.c"
    trace_header = HERE.parent / "memcomp_account_trace.h"
    source_before_build = {str(p): sha(p) for p in
                           (source, init_source, trace_header)}
    snapshot_dir = out / "source-snapshot"
    snapshot_dir.mkdir()
    verifier_snapshot = snapshot_dir / source.name
    init_snapshot = snapshot_dir / init_source.name
    shutil.copyfile(source, verifier_snapshot)
    shutil.copyfile(init_source, init_snapshot)
    trace_snapshot = snapshot_dir / trace_header.name
    shutil.copyfile(trace_header, trace_snapshot)
    require(all(sha(Path(p)) == h for p, h in source_before_build.items()),
            "userspace source changed while making build snapshot")
    build_error = None
    try:
        with (out / "userspace-build.log").open("w") as log:
            for src, binary in ((verifier_snapshot, out / "memcomp-memcg"),
                                (init_snapshot, out / "init")):
                subprocess.run(
                    ["gcc", "-static", "-O2", "-Wall", "-Wextra", "-Werror",
                     str(src), "-o", str(binary)],
                    stdout=log, stderr=subprocess.STDOUT, check=True)
            subprocess.run(["gcc", "-O2", str(tree / "usr/gen_init_cpio.c"),
                            "-o", str(out / "gen_init_cpio")],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
    except (OSError, subprocess.CalledProcessError) as exc:
        build_error = str(exc)
    if build_error:
        write_receipt(out, {"schema_version": 2,
                            "scope": "real memcg source attribution and release only",
                            "status": "FAIL", "phase": "userspace-build",
                            "error": build_error,
                            "source_sha256_before_build": source_before_build,
                            "source_sha256_after_build": hash_paths(
                                {"verifier": source, "init": init_source,
                                 "trace_header": trace_header}),
                            "swap": "no guest disk; host swap untouched"})
        return 1
    require(all(sha(Path(p)) == h for p, h in source_before_build.items()),
            "userspace source changed during build")
    verify()
    listing = "".join("dir /%s 0755 0 0\n" % name
                       for name in ("dev", "proc", "sys", "tmp"))
    listing += "nod /dev/console 0600 0 0 c 5 1\n"
    listing += ("file /init %s/init 0755 0 0\n"
                "file /memcomp-memcg %s/memcomp-memcg 0755 0 0\n" %
                (out, out))
    (out / "initramfs.list").write_text(listing)
    cpio = subprocess.check_output([str(out / "gen_init_cpio"),
                                    str(out / "initramfs.list")])
    with (out / "initramfs.gz").open("wb") as f:
        subprocess.run(["gzip", "-n", "-9"], input=cpio, stdout=f, check=True)
    paths = {"kernel": kernel, "config": config, "source_manifest": manifest,
             "source_links": links, "kernel_build_receipt": build_receipt,
             "kernel_build_log": build_log, "runner": Path(__file__).resolve(),
             "verifier_source": source, "init_source": init_source,
             "verifier_snapshot": verifier_snapshot, "init_snapshot": init_snapshot,
             "trace_header": trace_header, "trace_snapshot": trace_snapshot,
             "verifier_binary": out / "memcomp-memcg", "init_binary": out / "init",
             "initramfs": out / "initramfs.gz",
             "userspace_build_log": out / "userspace-build.log"}
    before = hash_paths(paths)
    require(all(value is not None for value in before.values()),
            "build artifact missing")
    command = ["sudo", "-n", "qemu-system-x86_64", "-enable-kvm", "-cpu", "host",
               "-smp", str(args.cpus), "-m", str(args.memory_mib), "-kernel", str(kernel),
               "-initrd", str(out / "initramfs.gz"), "-append",
               "console=ttyS0 panic=-1 oops=panic loglevel=6 "
               "memcomp_memcg.disposable_guest=1", "-nographic", "-no-reboot",
               "-net", "none"]
    start = time.monotonic()
    timed_out = False
    launch_error = None
    try:
        result_code, timed_out = run_guest(command, out / "guest.log", args.timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        result_code = 127
        launch_error = str(exc)
        (out / "guest.log").write_text("runner launch failed: %s\n" % exc)
    after = hash_paths(paths)
    text = (out / "guest.log").read_text(errors="replace")
    verified = True
    verification_error = None
    try:
        verify()
        require(all(value is not None for value in after.values()),
                "runtime artifact missing")
    except (RuntimeError, subprocess.CalledProcessError, OSError) as exc:
        verified = False
        verification_error = str(exc)
    passed, diagnostics = evaluate(text, result_code, before, after)
    passed = passed and verified and not timed_out and launch_error is None
    receipt = {"schema_version": 2,
               "scope": "real memcg source attribution and release only",
               "source_commit": binding["source_commit"], "before": before,
               "after": after, "source_sha256_before_build": source_before_build,
               "source_sha256_after_run": hash_paths(
                   {"verifier": source, "init": init_source,
                                 "trace_header": trace_header}),
               "argv": command, "qemu_exit": result_code,
               "timed_out": timed_out, "launch_error": launch_error,
               "diagnostics": diagnostics,
               "source_verified_before_and_after_run": verified,
               "verification_error": verification_error,
               "log_sha256": sha(out / "guest.log"),
               "runtime_seconds": round(time.monotonic() - start, 3),
               "status": "PASS" if passed else "FAIL",
               "swap": "no guest disk; host swap untouched",
               "qualification": "Does not close limits/reclaim/offline/reparent or the full E2E matrix"}
    write_receipt(out, receipt)
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())