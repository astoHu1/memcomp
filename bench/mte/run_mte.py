#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
"""Build and run the isolated arm64 MTE stage-2 gate under QEMU TCG."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
SOURCE_NAMES = ("run_mte.py", "test_run_mte.py", "README.md", "build.sh",
                "mte_guest.c")
CASES = ("no-swap", "swap-on")
POSITIVE = r"[1-9][0-9]*"
COMPRESSOR_NAME = re.compile(r"[a-z][a-z0-9_-]{0,63}")
REQUIRED_CONFIG = (
    "CONFIG_MEMCOMP", "CONFIG_MEMCOMP_DEBUG_STATS", "CONFIG_ZSMALLOC",
    "CONFIG_ARM64_MTE", "CONFIG_SWAP", "CONFIG_LRU_GEN", "CONFIG_LRU_GEN_ENABLED",
)
CODEC_CONFIG = {
    "zstd": "CONFIG_CRYPTO_ZSTD", "lzo": "CONFIG_CRYPTO_LZO",
    "lz4": "CONFIG_CRYPTO_LZ4", "lz4hc": "CONFIG_CRYPTO_LZ4HC",
    "deflate": "CONFIG_CRYPTO_DEFLATE",
}
MARKER_PATTERNS = {
    "case": re.compile(r"MTE_CASE prot_mte=PASS tagged_write=PASS"),
    "compress": re.compile(
        rf"MTE_COMPRESS stored_bytes_delta=(?P<stored_bytes_delta>{POSITIVE}) "
        rf"pageout_delta=(?P<pageout_delta>{POSITIVE}) "
        r"swapped_pages=(?P<swapped_pages>64)"),
    "pageout": re.compile(r"MTE_PAGEOUT data=PASS tag=PASS pages=(?P<pages>64)"),
    "bad_tag": re.compile(
        r"MTE_BAD_TAG probe=PASS signal=SIGSEGV si_code=SEGV_MTESERR"),
    "writeback": re.compile(
        rf"MTE_WRITEBACK payloads_delta=(?P<payloads_delta>{POSITIVE}) "
        rf"swapped_read_io_delta=(?P<swapped_read_io_delta>{POSITIVE}) "
        r"data=PASS tag=PASS"),
    "result": re.compile(r"MTE_RESULT PASS"),
}
MARKER_KEYS = {
    "MTE_CASE": "case", "MTE_COMPRESS": "compress",
    "MTE_PAGEOUT": "pageout", "MTE_BAD_TAG": "bad_tag",
    "MTE_SWAP": "swap", "MTE_WRITEBACK": "writeback",
    "MTE_RESULT": "result", "MTE_NUMA": "numa",
}
# Match diagnostics, not words embedded in a kernel command line or prose.
FATAL_LINE = re.compile(
    r"^[ \t]*(?:<[0-7]>)?(?:\[[ \t]*[0-9]+(?:\.[0-9]+)?\][ \t]*)?"
    r"(?:\[[ \t]*[TC][0-9]+\][ \t]*)?"
    r"[ \t]*(?:WARNING\b|(?:kernel |watchdog:[ \t]*)?BUG\b|Oops\b|"
    r"Internal error:[ \t]*Oops\b|KASAN\b|Call [Tt]race\b|"
    r"Kernel panic\b|MTE_FAIL\b|MTE_RUNNER\b)")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, check=check)


def require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise RuntimeError(f"missing {label}: {path}")


def source_hashes() -> dict[str, str]:
    return {name: sha256(HERE / name) for name in SOURCE_NAMES}


def make_swap(path: Path, size_mb: int) -> None:
    # Refuse an existing swap image before fallocate/mkswap can alter it.
    with path.open("xb"):
        pass
    run(["fallocate", "-l", f"{size_mb}M", str(path)])
    path.chmod(0o600)
    run(["mkswap", "-f", str(path)])


def guest_markers(log: Path, case: str, numa: str | None = None) -> dict:
    """Validate exact v2 records, optionally binding the explicit NUMA request.

    numa=None preserves offline parsing of historical v2 evidence. Current
    case_receipt always supplies n/y; it cannot PASS without node observation.
    """
    if case not in CASES:
        raise ValueError(f"unknown MTE case: {case}")
    if numa not in (None, "n", "y"):
        raise ValueError(f"unknown NUMA mode: {numa}")
    expected_swap = "on" if case == "swap-on" else "off"
    patterns = dict(MARKER_PATTERNS)
    patterns["swap"] = re.compile(
        f"MTE_SWAP requested={expected_swap} active={expected_swap}")
    patterns["numa"] = re.compile(
        r"MTE_NUMA (?:requested=n online=(?:absent|0)|requested=y online=0-1)"
        if numa is None else
        (r"MTE_NUMA requested=n online=(?:absent|0)" if numa == "n" else
         r"MTE_NUMA requested=y online=0-1"))
    required = ("case", "compress", "pageout", "bad_tag", "swap", "result")
    if numa is not None:
        required += ("numa",)
    if case == "swap-on":
        required += ("writeback",)
    markers = {key: False for key in patterns}
    occurrences = {key: 0 for key in patterns}
    counts = {}
    numa_observation = None
    errors = []
    fatal_lines = []
    result_line = None
    last_guest_line = None
    text = log.read_bytes().decode("utf-8", errors="replace")
    # Serial CRLF is allowed; do not strip other whitespace or split records
    # on control characters such as an embedded carriage return.
    for number, raw_line in enumerate(text.split("\n"), 1):
        line = raw_line.removesuffix("\r")
        if FATAL_LINE.match(line):
            fatal_lines.append({"line": number, "text": line})
        if line.startswith("MTE_"):
            last_guest_line = number
        token = re.match(r"(MTE_[A-Z_]+)\b", line)
        if token is None:
            continue
        key = MARKER_KEYS.get(token[1])
        if key is None:
            continue
        occurrences[key] += 1
        if occurrences[key] > 1:
            errors.append(f"line {number}: duplicate {token[1]}")
        match = patterns[key].fullmatch(line)
        if match is None:
            errors.append(f"line {number}: invalid {token[1]}")
            continue
        try:
            observed = {name: int(value) for name, value in match.groupdict().items()}
        except ValueError:
            errors.append(f"line {number}: invalid numeric value in {token[1]}")
            continue
        markers[key] = True
        if key == "numa":
            numa_observation = dict(field.split("=", 1) for field in line.split()[1:])
        if observed:
            counts[key] = observed
        if key == "result":
            result_line = number
    for key in patterns:
        markers[key] = markers[key] and occurrences[key] == 1
    for key in required:
        if not markers[key]:
            errors.append(f"missing or invalid required marker: {key}")
    if result_line is not None and result_line != last_guest_line:
        markers["result"] = False
        errors.append("MTE_RESULT PASS is not the final guest marker")
    if fatal_lines:
        errors.append("fatal runtime diagnostic in guest log")
    markers.update(counts=counts, errors=errors, fatal_lines=fatal_lines,
                   numa_observation=numa_observation, failure=bool(errors))
    return markers


def case_receipt(args: argparse.Namespace, case: str, work: Path,
                 artifacts: dict[str, Path], command: list[str],
                 log: Path, result: subprocess.CompletedProcess[str],
                 swap_image: Path | None) -> dict:
    if args.numa not in ("n", "y"):
        raise ValueError("current case receipt requires explicit NUMA n/y")
    markers = guest_markers(log, case, args.numa)
    passed = result.returncode == 0 and not markers["failure"]
    return {
        "schema": "memcomp-mte-case-v2",
        "status": "PASS" if passed else "FAIL",
        "case": case,
        "harness": {"directory": str(HERE), "source_sha256": source_hashes()},
        "kernel": {"path": str(args.kernel), "sha256": sha256(args.kernel)},
        "config": None if args.config is None else {
            "path": str(args.config), "sha256": sha256(args.config)},
        "binary": {
            "guest": {"path": str(artifacts["guest"]),
                      "sha256": sha256(artifacts["guest"])},
            "initramfs": {"path": str(artifacts["initramfs"]),
                          "sha256": sha256(artifacts["initramfs"])},
        },
        "qemu": {"command": command, "accelerator": "tcg",
                 "machine": "virt,mte=on", "no_reboot": True,
                 "compressor": args.compressor, "numa": args.numa},
        "guest": {"returncode": result.returncode, "log": str(log),
                  "log_sha256": sha256(log), "markers": markers},
        "swap_image": None if swap_image is None else {
            "path": str(swap_image), "sha256": sha256(swap_image)},
        "work": str(work),
    }


def compressor_name(value: str) -> str:
    """Only accept a single safe crypto name, never kernel command-line text."""
    if COMPRESSOR_NAME.fullmatch(value) is None:
        raise argparse.ArgumentTypeError(
            "compressor must match [a-z][a-z0-9_-]{0,63}")
    return value


def preflight_config(config: Path, compressor: str | None,
                     numa: str | None = None) -> None:
    """Reject known unavailable prerequisites before building or booting."""
    values = {}
    for line in config.read_text(encoding="utf-8").splitlines():
        setting = re.fullmatch(r"(CONFIG_[A-Z0-9_]+)=(.*)", line)
        unset = re.fullmatch(r"# (CONFIG_[A-Z0-9_]+) is not set", line)
        if setting is not None:
            values[setting[1]] = setting[2]
        elif unset is not None:
            values[unset[1]] = "n"
    errors = []
    for symbol in REQUIRED_CONFIG:
        value = values.get(symbol, "missing")
        if value != "y":
            errors.append(f"{symbol}={value}; required y")
    if numa is not None:
        if numa not in ("n", "y"):
            errors.append(f"unknown NUMA mode: {numa}")
        elif values.get("CONFIG_NUMA", "n") != numa:
            errors.append(f"CONFIG_NUMA={values.get('CONFIG_NUMA', 'n')}; requested {numa}")
    selected = compressor
    if selected is None:
        default = values.get("CONFIG_MEMCOMP_DEFAULT_COMPRESSOR", "")
        match = re.fullmatch(r'"([a-z][a-z0-9_-]{0,63})"', default)
        if match is None:
            errors.append("missing/invalid CONFIG_MEMCOMP_DEFAULT_COMPRESSOR; "
                          "supply a valid configured default or --compressor")
        else:
            selected = match[1]
    if selected is not None:
        selected = compressor_name(selected)
        symbol = CODEC_CONFIG.get(selected)
        if symbol is None:
            errors.append(f"unmapped compressor {selected!r}; cannot verify its "
                          "built-in implementation from this config")
        elif values.get(symbol) != "y":
            value = values.get(symbol, "missing")
            errors.append(f"{symbol}={value}; compressor {selected!r} requires y "
                          "(built-in); the static initramfs does not load modules")
    if errors:
        raise ValueError("MTE config preflight failed:\n  " + "\n  ".join(errors))


def build_qemu_command(args: argparse.Namespace, case: str,
                       artifacts: dict[str, Path],
                       swap_image: Path | None = None) -> list[str]:
    """Construct argv without touching files or changing the kernel default."""
    if case not in CASES:
        raise ValueError(f"unknown MTE case: {case}")
    if args.numa not in ("n", "y"):
        raise ValueError("NUMA must be n or y")
    if args.memory_mb <= 0 or args.smp <= 0:
        raise ValueError("memory and SMP must be positive")
    if args.numa == "y" and (args.memory_mb % 2 or args.smp % 2):
        raise ValueError("NUMA=y needs even memory and SMP for two equal nodes")
    swap = case == "swap-on"
    if swap != (swap_image is not None):
        raise ValueError("swap image must be supplied only for the swap-on case")
    append = (f"console=ttyAMA0 panic=-1 rdinit=/init "
              f"mte_swap={'on' if swap else 'off'} mte_numa={args.numa}")
    if args.compressor is not None:
        append += f" memcomp.compressor={compressor_name(args.compressor)}"
    smp = (str(args.smp) if args.numa == "n" else
           f"{args.smp},sockets=2,cores={args.smp // 2},threads=1")
    command = [args.qemu, "-machine", "virt,mte=on", "-accel", "tcg,thread=multi",
               "-cpu", "max", "-m", str(args.memory_mb), "-smp", smp,
               "-nographic", "-nodefaults", "-no-reboot", "-kernel", str(args.kernel),
               "-initrd", str(artifacts["initramfs"]), "-append", append,
               "-serial", "mon:stdio"]
    if args.numa == "y":
        half = args.memory_mb // 2
        command += ["-object", f"memory-backend-ram,size={half}M,id=mem0",
                    "-object", f"memory-backend-ram,size={half}M,id=mem1",
                    "-numa", "node,nodeid=0,memdev=mem0",
                    "-numa", "node,nodeid=1,memdev=mem1",
                    "-numa", "cpu,node-id=0,socket-id=0",
                    "-numa", "cpu,node-id=1,socket-id=1"]
    if swap:
        command += ["-drive", f"if=none,format=raw,file={swap_image},id=swapdisk",
                    "-device", "virtio-blk-device,drive=swapdisk"]
    return command


def run_case(args: argparse.Namespace, case: str, work: Path,
             artifacts: dict[str, Path]) -> dict:
    if case not in CASES:
        raise ValueError(f"unknown MTE case: {case}")
    swap = case == "swap-on"
    swap_image = work / "swap.img" if swap else None
    command = build_qemu_command(args, case, artifacts, swap_image)
    if swap_image is not None:
        make_swap(swap_image, args.swap_mb)
    log = work / "guest.log"
    # Persist argv before launch, even if QEMU fails to start or times out.
    with (work / "command.json").open("x", encoding="utf-8") as stream:
        json.dump(command, stream, indent=2)
        stream.write("\n")
    started = time.monotonic()
    error = None
    # Exclusive creation is outside the OSError handler: never classify an
    # existing evidence file as a new failed run or alter its contents.
    with log.open("xb") as stream:
        try:
            result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT,
                                    timeout=args.timeout, check=False)
        except subprocess.TimeoutExpired:
            error = f"runner timeout after {args.timeout}s"
            result = subprocess.CompletedProcess(command, 124)
        except OSError as exc:
            error = f"runner could not start QEMU: {exc}"
            result = subprocess.CompletedProcess(command, 127)
    receipt = case_receipt(args, case, work, artifacts, command, log, result,
                           swap_image)
    receipt["duration_seconds"] = round(time.monotonic() - started, 3)
    receipt["qemu"]["command_file"] = str(work / "command.json")
    if error is not None:
        receipt["error"] = error
    elif result.returncode != 0:
        receipt["error"] = ("guest failed; tag mismatch and all guest failures "
                            "are hard failures")
    elif receipt["status"] == "FAIL":
        receipt["error"] = "; ".join(receipt["guest"]["markers"]["errors"])
    return receipt


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tree", type=Path, required=True,
                        help="kernel tree containing usr/gen_init_cpio")
    parser.add_argument("--kernel", type=Path, required=True,
                        help="arm64 Image; the runner never builds or modifies it")
    parser.add_argument("--config", type=Path,
                        help="matching kernel config; preflight built-in MTE, "
                             "memcomp, MGLRU and selected-codec prerequisites")
    parser.add_argument("--results", type=Path, required=True)
    parser.add_argument("--qemu", default="qemu-system-aarch64")
    parser.add_argument("--numa", choices=("n", "y"), default="n",
                        help="observed topology: n single node, y two equal nodes; "
                             "matching config is checked when supplied")
    parser.add_argument("--compressor", type=compressor_name, default=None,
                        help="explicit memcomp.compressor crypto name; omitted "
                             "by default so the kernel selects its own default")
    parser.add_argument("--memory-mb", type=int, default=512)
    parser.add_argument("--smp", type=int, default=2)
    parser.add_argument("--swap-mb", type=int, default=256)
    parser.add_argument("--timeout", type=int, default=300)
    args = parser.parse_args()
    args.tree = args.tree.resolve()
    args.kernel = args.kernel.resolve()
    args.results = args.results.resolve()
    if args.config is not None:
        args.config = args.config.resolve()
    require_file(args.kernel, "kernel Image")
    if args.config is not None:
        require_file(args.config, "kernel config")
        try:
            preflight_config(args.config, args.compressor, args.numa)
        except ValueError as exc:
            parser.error(str(exc))
    # Validate topology bounds before creating results or building guest files.
    try:
        build_qemu_command(args, "no-swap", {"initramfs": Path("unused")})
    except ValueError as exc:
        parser.error(str(exc))
    try:
        args.results.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        parser.error(f"results path already exists; choose a new path: {args.results}")
    build_out = args.results / "build"
    run([str(HERE / "build.sh"), str(args.tree), str(build_out)])
    artifacts = {"guest": build_out / "mte_guest",
                 "initramfs": build_out / "initramfs.gz"}
    for path, label in ((artifacts["guest"], "guest binary"),
                        (artifacts["initramfs"], "initramfs")):
        require_file(path, label)
    pending = {
        "schema": "memcomp-mte-v2", "status": "RUNNING",
        "tree": str(args.tree), "kernel": str(args.kernel),
        "config": None if args.config is None else str(args.config),
        "harness_source_sha256": source_hashes(),
        "qemu_contract": {"accelerator": "tcg", "machine": "virt,mte=on",
                          "no_reboot": True, "compressor": args.compressor,
                          "numa": args.numa, "config_numa_verified": args.config is not None},
        "cases": ["no-swap", "swap-on"],
    }
    with (args.results / "receipt.pending.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(pending, indent=2) + "\n")
    receipts = []
    for case in CASES:
        work = args.results / case
        work.mkdir(exist_ok=False)
        receipt = run_case(args, case, work, artifacts)
        receipts.append(receipt)
        with (work / "receipt.json").open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(receipt, indent=2) + "\n")
    final = dict(pending)
    final["status"] = "PASS" if all(item["status"] == "PASS" for item in receipts) else "FAIL"
    final["cases"] = receipts
    final["finished_at_epoch"] = int(time.time())
    with (args.results / "receipt.json").open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(final, indent=2) + "\n")
    (args.results / "receipt.pending.json").unlink(missing_ok=True)
    return 0 if final["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
