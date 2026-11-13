#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-only
"""Strict, offline smoke memory diagnostics; no backend-exclusive attribution."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import struct

import telemetry as telem

LIMIT = 500000  # Decimal bytes, not 0.5 MiB.
LRU = ("Active(anon)", "Inactive(anon)", "Active(file)", "Inactive(file)", "Unevictable")
DIAGNOSTICS = ("AnonPages", "Cached", "Buffers", "SwapCached")
ISOLATED = ("nr_isolated_anon", "nr_isolated_file")
MEMINFO = ("MemFree", "Slab", "SReclaimable", "SUnreclaim", "AnonPages", "Cached",
           "Buffers", "SwapCached", "PageTables", "Percpu", "VmallocUsed", "KernelStack") + LRU
TERMS = ("Slab", "PageTables", "Percpu", "VmallocUsed") + LRU
POOL = {"memcomp": (telem.MEMCOMP + "stat", 2, 3, True),
        "zram": (telem.ZRAM + "mm_stat", 2, 3, False),
        "zswap": (telem.ZSWAP + "pool_total_size", 0, 1, True)}
RAW = {"/proc/meminfo", "/proc/slabinfo", "/proc/zoneinfo", "/proc/vmstat", "/proc/swaps", "/proc/cmdline"}
RAW.update(item[0] for item in POOL.values())
RAW.update((telem.MEMCOMP_ACCOUNT, telem.MEMCOMP_OCCUPANCY))
RAW.update(telem.MEMORY_RAW + side + "/" + name for side in ("before", "after")
           for name in ("buddyinfo", "zoneinfo"))
SWAP_LAYOUT = "/compare/swap_layout"
# Accounting/collector identity only; never rewrite the original kernel variant.
BACKEND_ALIASES = {"zswap-crosscheck": "zswap"}


def residual_gate(value):
    if type(value) is not int:
        return "undefined"
    return "PASS" if abs(value) <= LIMIT else "FAIL"


def source_evidence(source_root, config, family=None):
    """Recognize the inspected 6.12 accounting implementation, fail closed otherwise."""
    out = {"issues": [], "files": {}, "vmap_stack": None, "memcg": None, "rules": {}}

    def read(path):
        try:
            data = Path(path).read_bytes()
            out["files"][str(path)] = hashlib.sha256(data).hexdigest()
            return data.decode()
        except (OSError, UnicodeError) as exc:
            out["issues"].append(f"source/config unavailable: {path}: {exc}")
            return ""

    text = read(config) if config else ""
    if not config:
        out["issues"].append("build config missing")
    for key, field in (("CONFIG_VMAP_STACK", "vmap_stack"), ("CONFIG_MEMCG", "memcg")):
        values = re.findall(rf"^{key}=(y|n)$|^# {key} is not set$", text, re.M)
        if len(values) != 1:
            out["issues"].append(key + " missing/ambiguous")
        else:
            out[field] = values[0] == "y"
    if not re.search(r"^CONFIG_X86_64=y$", text, re.M):
        out["issues"].append("inspected layout/stack proof currently requires CONFIG_X86_64=y")
    patterns = {
        "fs/proc/meminfo.c": (
            r'"VmallocUsed:\s*",\s*vmalloc_nr_pages\(\)',
            r'"Percpu:\s*",\s*pcpu_nr_pages\(\)',
            r'cached\s*=.*?NR_FILE_PAGES.*?-\s*total_swapcache_pages\(\)\s*-\s*i\.bufferram',
            r'"AnonPages:\s*",\s*global_node_page_state\(NR_ANON_MAPPED\)',
            r'pages\[lru\]\s*=\s*global_node_page_state\(NR_LRU_BASE\s*\+\s*lru\)',
            r'"PageTables:\s*",\s*global_node_page_state\(NR_PAGETABLE\)',
            r'"Slab:\s*",\s*sreclaimable\s*\+\s*sunreclaim',
            r'NR_SLAB_RECLAIMABLE_B', r'NR_SLAB_UNRECLAIMABLE_B') + tuple(
                re.escape('"' + name + ':') + r'\s*",\s*pages\[' + index + r'\]'
                for name, index in zip(LRU, ('LRU_ACTIVE_ANON', 'LRU_INACTIVE_ANON',
                                             'LRU_ACTIVE_FILE', 'LRU_INACTIVE_FILE', 'LRU_UNEVICTABLE'))),
        "include/linux/mmzone.h": (r'enum lru_list\s*\{.*?LRU_INACTIVE_ANON.*?LRU_ACTIVE_ANON.*?LRU_INACTIVE_FILE.*?LRU_ACTIVE_FILE.*?LRU_UNEVICTABLE',),
        "include/linux/mm_inline.h": (
            r'if \(folio_test_unevictable\(folio\)\)\s*return LRU_UNEVICTABLE',
            r'folio_is_file_lru\(folio\) \? LRU_INACTIVE_FILE : LRU_INACTIVE_ANON',
            r'__mod_lruvec_state\(lruvec,\s*NR_LRU_BASE \+ lru,\s*nr_pages\)'),
        "mm/vmscan.c": (r'update_lru_size\(lruvec,\s*lru,\s*zid,\s*-nr_zone_taken\[zid\]\)',
                         r'__mod_node_page_state\(pgdat,\s*NR_ISOLATED_ANON \+ file,\s*nr_taken\)',
                         r'__mod_node_page_state\(pgdat,\s*NR_ISOLATED_ANON \+ file,\s*-nr_taken\)'),
        "mm/vmstat.c": (r'"nr_isolated_anon"', r'"nr_isolated_file"'),
        "mm/slub.c": (r'alloc_pages_node_noprof\(node,\s*flags,\s*order\).*?lruvec_stat_mod_folio\(folio,\s*NR_SLAB_UNRECLAIMABLE_B,\s*PAGE_SIZE << order\)',),
        "mm/vmalloc.c": (r'atomic_long_add\(area->nr_pages,\s*&nr_vmalloc_pages\)',),
        "mm/percpu-vm.c": (r'alloc_pages_node\(', r'vmap_pages_range_noflush\('),
        "kernel/fork.c": (r'CONFIG_VMAP_STACK', r'__vmalloc_node_range\(THREAD_SIZE',
                          r'alloc_pages_node\(node,\s*THREADINFO_GFP,\s*THREAD_SIZE_ORDER\)'),
        "mm/swap_cgroup.c": (
            r'struct swap_cgroup\s*\{\s*unsigned short\s+id;\s*\}',
            r'SC_PER_PAGE\s*\(PAGE_SIZE/sizeof\(struct swap_cgroup\)\)',
            r'DIV_ROUND_UP\(max_pages,\s*SC_PER_PAGE\)',
            r'vcalloc\(length,\s*sizeof\(void \*\)\)', r'alloc_page\(GFP_KERNEL'),
        "mm/swapfile.c": (r'bytes\s*=\s*K\(si->pages\)',
                          r'nr_good_pages\s*=\s*maxpages\s*-\s*1',
                          r'swap_cgroup_swapon\(si->type,\s*maxpages\)'),
        "include/linux/swap.h": (r'bootbits\[1024\]',
                                 r'__u32\s+version;\s*__u32\s+last_page;\s*__u32\s+nr_badpages;'),
    }
    pool_patterns = {
        "memcomp": {"mm/memcomp.c": (r'zs_get_total_pages\(memcomp_pools\[i\]\) << PAGE_SHIFT',
                                         r'stat_show\(.*?memcomp_pool_total_size\(\)')},
        "zram": {"drivers/block/zram/zram_drv.c": (r'mem_used = zs_get_total_pages\(zram->mem_pool\)',
                                                      r'mem_used << PAGE_SHIFT')},
        "zswap": {"mm/zswap.c": (r'total \+= zpool_get_total_pages\(pool->zpool\)',
                                     r'\*val = zswap_total_pages\(\) \* PAGE_SIZE')},
    }
    patterns.update(pool_patterns.get(family, {}))
    if family in ("memcomp", "zram"):
        patterns["mm/zsmalloc.c"] = (r'page = alloc_page\(gfp\)', r'__SetPageZsmalloc\(page\)',
                                       r'inc_zone_page_state\(page,\s*NR_ZSPAGES\)',
                                       r'return atomic_long_read\(&pool->pages_allocated\)')
    for rel, expected in patterns.items():
        content = read(Path(source_root) / rel) if source_root else ""
        # Comments cannot serve as implementation evidence.
        content = re.sub(r'/\*.*?\*/|//[^\n]*', lambda m: '\n' * m[0].count('\n'), content, flags=re.S)
        out["rules"][rel] = [{"pattern": pattern,
            "line": content[:match.start()].count("\n") + 1 if (match := re.search(pattern, content, re.S)) else None}
            for pattern in expected]
        if any(item["line"] is None for item in out["rules"][rel]):
            out["issues"].append("unrecognized accounting implementation: " + rel)
        if rel == "mm/percpu-vm.c" and "nr_vmalloc_pages" in content:
            out["issues"].append("Percpu/VmallocUsed independence not proven")
    out["accounting_basis"] = {
        "LRU": "meminfo five disjoint LRU lists + vmstat isolated anon/file physical pages; mm_inline classifies a folio in one list and vmscan removes isolated pages from LRU sizes",
        "diagnostics_only": "AnonPages/Cached/Buffers/SwapCached overlap LRU and possibly each other; never additive",
        "Slab": "NR_SLAB_RECLAIMABLE_B + NR_SLAB_UNRECLAIMABLE_B; includes inspected large kmalloc page allocations, no extra workspace addend",
        "pool": "inspected backend physical pool pages; named caches remain within global Slab",
        "PageTables": "NR_PAGETABLE, separate physical page tables",
        "Percpu": "pcpu_nr_pages, percpu-vm pages not added to nr_vmalloc_pages",
        "VmallocUsed": "vmalloc_nr_pages physical pages, includes VMAP stacks and vcalloc metadata",
        "KernelStack": "separate alloc_pages only if CONFIG_VMAP_STACK=n; otherwise not added",
        "swapcg_direct_data": "alloc_page per SC_PER_PAGE max_pages; vcalloc pointer array is already vmalloc"}
    out["binding"] = "caller must supply source/config of the actual smoke build; hashes record evidence, not image identity"
    return out


def block(s, path):
    b, error = telem.payload(s, path)
    if not error and (b.begin < s.ns or b.end < b.begin):
        error = "sample clock unavailable/out of order"
    if error:
        raise ValueError(f"{s.phase}: {path}: {error}")
    return b


def meminfo(s):
    lines = block(s, "/proc/meminfo").lines
    out = {}
    for name in MEMINFO:
        matches = [line for line in lines if line.startswith(name + ":")]
        match = re.fullmatch(rf"{re.escape(name)}:\s+([0-9]+)\s+kB", matches[0]) if len(matches) == 1 else None
        if not match:
            raise ValueError(f"{s.phase}: meminfo {name} missing/duplicate/malformed")
        out[name] = int(match[1]) * 1024
    if out["Slab"] != out["SReclaimable"] + out["SUnreclaim"]:
        raise ValueError(f"{s.phase}: inconsistent Slab/SReclaimable/SUnreclaim snapshot")
    return out


def isolated(s, page_bytes):
    b = block(s, "/proc/vmstat")
    values, error = telem.key_values(b, "")
    if error:
        raise ValueError(s.phase + ": " + error)
    out = {}
    for name in ISOLATED:
        if name not in values or values[name] < 0:
            raise ValueError(s.phase + ": missing/invalid isolated-page gauge " + name)
        out[name] = values[name] * page_bytes
    return out


def swaps(s, page_bytes):
    lines = block(s, "/proc/swaps").lines
    if not lines or lines[0].split() != ["Filename", "Type", "Size", "Used", "Priority"]:
        raise ValueError(f"{s.phase}: malformed /proc/swaps header")
    out = {}
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split()
        if (len(fields) != 5 or fields[0] in out or fields[1] not in ("file", "partition") or
                not all(re.fullmatch(r"[0-9]+", v) for v in fields[2:4]) or
                not re.fullmatch(r"-?[0-9]+", fields[4])):
            raise ValueError(f"{s.phase}: malformed/duplicate swap row")
        size, used = (int(v) * 1024 for v in fields[2:4])
        if not size or size % page_bytes or used % page_bytes or used > size:
            raise ValueError(f"{s.phase}: invalid swap size/used/page alignment")
        out[fields[0]] = (fields[1], size // page_bytes)
    return out


def pool(s, family, page_bytes):
    if family == "swap":
        return 0  # Proven backend kind, not a missing pool field filled with zero.
    path, index, count, exact = POOL[family]
    b = block(s, path)
    values, error = telem.numbers(b, "", count, exact)
    if error or values[index] % page_bytes:
        raise ValueError(f"{s.phase}: physical pool missing/malformed/not page-aligned")
    return values[index]


def swap_layouts(log, path, guest_source=None):
    """Numeric guest-formatter evidence; no disk retention or synthesized header."""
    records = [(s, name) for s in log.snapshots for name in s.blocks if name.startswith(SWAP_LAYOUT)]
    proof = {"status": "not_captured", "issues": []}
    if not records:
        return {}, proof
    layouts = {}
    try:
        metadata_path = Path(path).parent / "metadata.json"
        metadata = json.loads(metadata_path.read_text())
        expected = metadata["workload_source_hashes"]["guest.c"]
        producer = Path(guest_source) if guest_source else Path(__file__).with_name("guest.c")
        data = producer.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected) or actual != expected:
            raise ValueError("guest.c producer hash does not match smoke metadata")
        text = data.decode()
        text = text[text.index("static int format_and_swapon("):text.index("static int verify_zswap(")]
        if not all(token in text for token in ("calloc(1, page_bytes)", "pages = bytes / page_bytes;",
                "value = (uint32_t)(pages - 1);", "memcpy(header + 1028, &value, sizeof(value));",
                "pwrite(", "fsync(", "swapon(", "swap_entries(path)",
                "path=/compare/swap_layout%s", "nr_badpages 0")):
            raise ValueError("unrecognized guest swap-layout producer")
        proof.update(status="verified", guest_source=str(producer), guest_sha256=actual,
                     metadata=str(metadata_path), note="hash bound to workload_source_hashes; actual successful formatter layout, not requested disk capacity")
        starts = [s.ns for s in log.snapshots if s.phase == "workload_start"]
        for snap, path in records:
            b = block(snap, path)
            if snap.phase != "before_backend_setup" or len(starts) != 1 or b.end >= starts[0]:
                raise ValueError("swap-layout evidence outside successful setup window")
            fields = {}
            for line in b.lines:
                if not line.strip():
                    continue
                parts = line.split()
                if len(parts) != 2 or parts[0] in fields:
                    raise ValueError("malformed/duplicate swap-layout field")
                fields[parts[0]] = parts[1]
            keys = {"device", "page_bytes", "version", "last_page", "nr_badpages"}
            if set(fields) != keys or any(not re.fullmatch(r"\d+", fields[k]) for k in keys - {"device"}):
                raise ValueError("missing/malformed numeric swap-layout field")
            device = fields.pop("device")
            values = {k: int(v) for k, v in fields.items()}
            if (device not in ("/dev/vda", "/dev/zram0") or path != SWAP_LAYOUT + device or device in layouts or
                    values["version"] != 1 or values["nr_badpages"] or
                    values["page_bytes"] != 4096 or not 15 <= values["last_page"] <= 0xffffffff):
                raise ValueError("unsupported/duplicate/inconsistent swap-layout identity or geometry")
            layouts[device] = dict(values, raw_source=path, producer_sha256=actual,
                                   begin=b.begin, end=b.end)
    except (OSError, UnicodeError, ValueError, KeyError, TypeError) as exc:
        proof["status"] = "undefined"
        proof["issues"].append("swap-layout proof: " + str(exc))
        layouts = {}
    return layouts, proof


def swapcg_bytes(device, pages, headers, page_bytes, layouts=None):
    """Verify max_pages against a real swap header, not requested disksize."""
    if page_bytes != 4096:
        raise ValueError("swap_cgroup header/layout proof currently supports 4096-byte pages only")
    layout = (layouts or {}).get(device)
    if layout is not None:
        if layout["last_page"] != pages or layout["page_bytes"] != page_bytes:
            raise ValueError("guest swap-layout/proc usable-page mismatch for " + device)
        max_pages = layout["last_page"] + 1
        data_pages = (max_pages + page_bytes // 2 - 1) // (page_bytes // 2)
        return data_pages * page_bytes, dict(layout, device=device, usable_pages=pages,
            max_pages=max_pages, data_pages=data_pages, sizeof_swap_cgroup=2,
            evidence_kind="guest_numeric_layout_no_retained_disk")
    if device not in headers:
        raise ValueError("swap max_pages proof missing for " + device)
    path = Path(headers[device])
    try:
        with path.open("rb") as stream:
            header = stream.read(page_bytes)
    except OSError as exc:
        raise ValueError(f"swap header unavailable for {device}: {exc}") from exc
    if len(header) != page_bytes or header[-10:] != b"SWAPSPACE2":
        raise ValueError("invalid/truncated SWAPSPACE2 header for " + device)
    version, last, bad = struct.unpack_from("<III", header, 1024)
    if version != 1 or bad or last != pages:
        raise ValueError("swap header/proc usable-page mismatch, bad pages or unsupported header for " + device)
    max_pages = last + 1
    data_pages = (max_pages + page_bytes // 2 - 1) // (page_bytes // 2)
    return data_pages * page_bytes, {"device": device, "usable_pages": pages,
        "max_pages": max_pages, "data_pages": data_pages, "sizeof_swap_cgroup": 2,
        "header_path": str(path), "header_sha256": hashlib.sha256(header).hexdigest()}


def swapcg_delta(a, b, page_bytes, proof, cmdline, headers, layouts=None):
    left, right = swaps(a, page_bytes), swaps(b, page_bytes)
    if left == right:
        return 0, []
    if proof["memcg"] is False:
        return 0, [{"not_applicable": "CONFIG_MEMCG=n"}]
    if cmdline is None:
        raise ValueError("MEMCG runtime state requires captured /proc/cmdline")
    disabled = any(token.startswith("cgroup_disable=") and
                   {"memory", "all"} & set(token.split("=", 1)[1].split(","))
                   for token in cmdline.split())
    if disabled:
        return 0, [{"not_applicable": "captured cgroup_disable=memory/all"}]
    if proof["memcg"] is not True:
        raise ValueError("MEMCG state undefined")
    total, evidence = 0, []
    for device in sorted(left.keys() | right.keys()):
        if left.get(device) == right.get(device):
            continue
        for sign, table in ((-1, left), (1, right)):
            if device in table:
                value, item = swapcg_bytes(device, table[device][1], headers, page_bytes, layouts)
                total += sign * value
                evidence.append(dict(item, sign=sign))
    return total, evidence


def window(a, b, family, page_bytes, proof, cmdline, headers, scope, layouts=None):
    result = {"start": a.phase, "end": b.phase, "scope": scope, "gate": "undefined",
              "observed_loss_bytes": None, "classified_bytes": None, "residual_bytes": None,
              "components_bytes": {}, "issues": list(proof["issues"]), "named_cache_details": {}}
    try:
        if a.memory or b.memory:
            if (not a.memory or not b.memory or
                    telem.memory_snapshot_protocol((a, b)) is None):
                raise ValueError("mixed memory protocols in accounting window")
            result["free_basis"] = "buddy_plus_PCP_after_v411"
            result["snapshot_status"] = {s.phase: s.memory.get("status", "ERROR") for s in (a, b)}
            if any(s.memory.get("status") == "ERROR" for s in (a, b)):
                raise ValueError("memory snapshot protocol/IO/parse ERROR")
            if any(s.memory.get("status") == "INVALID" for s in (a, b)):
                if not result["issues"]:
                    result["gate"] = "INVALID"
                result["note"] = "ten inconsistent snapshots; memory window unmeasurable, not residual FAIL"
                return result
        if a.ns >= b.ns:
            raise ValueError("snapshot window out of order")
        x, y = meminfo(a), meminfo(b)
        ix, iy = isolated(a, page_bytes), isolated(b, page_bytes)
        pcp = []
        for s in (a, b):
            source = block(s, "/proc/zoneinfo")
            pages, basis, error = telem.zoneinfo_pcp(source, "")
            if error:
                raise ValueError(s.phase + ": " + error)
            pcp.append((pages, basis))
        if pcp[0][1] != pcp[1][1]:
            raise ValueError("PCP node/zone/CPU coverage changed")
        if a.memory:
            if a.memory["basis"] != b.memory["basis"]:
                raise ValueError("buddy/PCP endpoint coverage changed")
            result["observed_loss_bytes"] = (a.memory["free_pages"] - b.memory["free_pages"]) * page_bytes
        else:
            result["observed_loss_bytes"] = x["MemFree"] - y["MemFree"] + (pcp[0][0] - pcp[1][0]) * page_bytes
        paths = {"/proc/meminfo", "/proc/zoneinfo", "/proc/vmstat", "/proc/swaps"}
        if family in POOL:
            paths.add(POOL[family][0])
        for path in sorted(paths):
            if block(a, path).end > block(b, path).begin:
                raise ValueError("endpoint source clock overlap/regression: " + path)
        components = result["components_bytes"]
        components["pool"] = pool(b, family, page_bytes) - pool(a, family, page_bytes)
        result["pool_pages_delta"] = components["pool"] // page_bytes
        components.update((name, y[name] - x[name]) for name in TERMS)
        components.update((name, iy[name] - ix[name]) for name in ISOLATED)
        result["lru_partition_delta_bytes"] = sum(components[name] for name in LRU + ISOLATED)
        result["diagnostics_only_bytes"] = {name: {"start": x[name], "end": y[name], "delta": y[name] - x[name],
                                                     "not_additive": True} for name in DIAGNOSTICS}
        if proof["vmap_stack"] is False:
            components["KernelStack"] = y["KernelStack"] - x["KernelStack"]
        elif proof["vmap_stack"] is True:
            result["KernelStack_not_added"] = "CONFIG_VMAP_STACK=y: already inside physical VmallocUsed"
        else:
            result["issues"].append("KernelStack/VmallocUsed overlap undefined")
        components["swapcg_direct_data"], result["swapcg_evidence"] = swapcg_delta(
            a, b, page_bytes, proof, cmdline, headers, layouts)
        for name in ("SReclaimable", "SUnreclaim"):
            result.setdefault("Slab_subdivision_bytes", {})[name] = y[name] - x[name]
        # Per-cache pages are a subdivision of Slab only, never another addend.
        cache_samples = {}
        for s in (a, b):
            details = telem.slab_memory(s, page_bytes)
            cache_samples[s.phase] = details
            result["named_cache_details"][s.phase] = {
                name: {"value": c.value, "unit": c.unit, "status": c.quality, "reason": c.reason,
                       "note": c.note, "source": c.source, "basis": c.basis}
                for name, c in details.items()}
        result["named_cache_delta_bytes_within_Slab_only"] = {}
        for name in cache_samples[a.phase].keys() | cache_samples[b.phase].keys():
            if name.endswith(".allocated_bytes"):
                xcache, ycache = cache_samples[a.phase].get(name), cache_samples[b.phase].get(name)
                known = (xcache is not None and ycache is not None and xcache.value is not None and
                         ycache.value is not None and xcache.basis == ycache.basis)
                result["named_cache_delta_bytes_within_Slab_only"][name] = {
                    "delta": ycache.value - xcache.value if known else None,
                    "not_additive": True,
                    "note": "family totals and actual cache names are overlapping views within global Slab"}
        if not result["issues"]:
            result["classified_bytes"] = sum(components.values())
            result["residual_bytes"] = result["observed_loss_bytes"] - result["classified_bytes"]
            result["gate"] = residual_gate(result["residual_bytes"])
    except (ValueError, KeyError, TypeError) as exc:
        result["issues"].append(str(exc))
    result["attribution"] = "global non-atomic observations, not exclusive backend cost; no other/residual filler"
    return result


def summed(setup, payload):
    out = {"scope": "setup_plus_W1_payload_not_contiguous", "gate": "undefined",
           "issues": list(setup["issues"]) + list(payload["issues"]), "components_bytes": {},
           "observed_loss_bytes": None, "classified_bytes": None, "residual_bytes": None}
    if setup["gate"] == "undefined" or payload["gate"] == "undefined":
        return out
    if "INVALID" in (setup["gate"], payload["gate"]):
        out["gate"] = "FAIL" if "FAIL" in (setup["gate"], payload["gate"]) else "INVALID"
        out["note"] = "unmeasurable constituent; no invented residual or cancellation"
        return out
    for key in ("observed_loss_bytes", "classified_bytes", "residual_bytes"):
        out[key] = setup[key] + payload[key]
    for key in setup["components_bytes"].keys() | payload["components_bytes"].keys():
        out["components_bytes"][key] = setup["components_bytes"].get(key, 0) + payload["components_bytes"].get(key, 0)
    # Do not let opposite undefined costs cancel into a fake closed total.
    out["gate"] = "PASS" if setup["gate"] == payload["gate"] == "PASS" and abs(out["residual_bytes"]) <= LIMIT else "FAIL"
    out["note"] = "sum of two windows; excludes workload_start->fill_begin; component failures cannot cancel"
    return out


THP_POLICY_PATH = re.compile(
    r"/sys/kernel/mm/transparent_hugepage/(?:hugepages-[0-9]+kB/)?(?:enabled|shmem_enabled)")


def is_thp_policy_event(snapshot):
    """Recognize raw-only setup policy records, never a memory boundary."""
    # retain() records the full source inventory before dropping unused raw
    # blocks. A THP record mixed with an unknown source is not policy-only.
    sources = getattr(snapshot, "thp_policy_raw_sources", tuple(snapshot.blocks))
    return (not snapshot.explicit and not snapshot.memory and
            not snapshot.memory_attempts and not snapshot.limits and
            snapshot.phase in ("thp_policy_never", "thp_policy_absent") and
            bool(sources) and
            all(THP_POLICY_PATH.fullmatch(path) for path in sources))


def analyze(path, source_root=None, config=None, headers=None, guest_source=None,
            raw_limit=telem.LOG_RAW_LIMIT, decoded_limit=telem.LOG_DECODED_LIMIT,
            line_limit=telem.LOG_LINE_LIMIT):
    layout_only, supplemental = set(), []

    def retain(s, log):
        s.thp_policy_raw_sources = tuple(s.blocks)
        # Classify before reducing paths: a foreign/core source must not be
        # hidden to make a mixed implicit record look like layout-only evidence.
        if not s.explicit and not s.limits and s.blocks and all(name.startswith(SWAP_LAYOUT) for name in s.blocks):
            layout_only.add(id(s))
            supplemental.append({"phase": s.phase, "ns": s.ns, "raw_sources": sorted(s.blocks),
                                 "role": "supplemental_layout_not_measurement"})
        # Keep complete original blocks, including all meminfo/slabinfo lines;
        # telemetry.retain_snapshot would discard fields needed for accounting.
        s.blocks = {name: b for name, b in s.blocks.items()
                    if name in RAW or name.startswith((telem.SLAB_SYSFS, SWAP_LAYOUT)) or
                    THP_POLICY_PATH.fullmatch(name)}

    log = telem.parse_log(path, retain, raw_limit=raw_limit,
                          decoded_limit=decoded_limit, line_limit=line_limit)
    measurements = [s for s in log.snapshots if id(s) not in layout_only]
    layouts, layout_proof = swap_layouts(log, path, guest_source)
    issues = list(log.issues) + layout_proof["issues"]
    request = log.requests[0] if len(log.requests) == 1 else {}
    page_bytes = telem.positive_marker([request.get("page_bytes", "")])
    if not telem.valid_page_bytes(page_bytes):
        issues.append("guest page_bytes missing/invalid/ambiguous")
    elif page_bytes != 4096:
        issues.append("inspected x86_64 build evidence requires 4096-byte pages")
    backend = request.get("backend", "")
    accounting_backend = BACKEND_ALIASES.get(backend, backend)
    family = ("memcomp" if accounting_backend in ("memcomp-swap", "memcomp-noswap") else
              "zram" if accounting_backend in ("zram", "zram-writeback") else accounting_backend)
    if family not in (*POOL, "swap"):
        issues.append("backend missing/unknown")
    proof = source_evidence(source_root, config, family)
    proof["swap_layout_producer"] = layout_proof
    if layouts and telem.valid_page_bytes(page_bytes):
        observed_swaps = {}
        try:
            for s in log.snapshots:
                if "/proc/swaps" not in s.blocks:
                    continue
                for device, (_, pages) in swaps(s, page_bytes).items():
                    observed_swaps.setdefault(device, set()).add(pages)
            for device, layout in layouts.items():
                if (layout["page_bytes"] != page_bytes or
                        observed_swaps.get(device) != {layout["last_page"]}):
                    issues.append("guest swap-layout/proc usable-page mismatch or device never activated: " + device)
        except (ValueError, TypeError) as exc:
            issues.append(str(exc))
    label = request.get("workload", "")
    if label not in ("w1", "w2", "w3", "w4"):
        issues.append("workload missing/unknown")
    commands = []
    for s in log.snapshots:
        if "/proc/cmdline" in s.blocks:
            try:
                commands.append(" ".join(block(s, "/proc/cmdline").lines))
            except ValueError as exc:
                issues.append(str(exc))
    cmdline = commands[0] if len(commands) == 1 else None
    by_phase = {}
    for s in measurements:
        by_phase.setdefault(s.phase, []).append(s)
    named_issues, named_last = [], {}
    for s in measurements:
        required = {name for name in telem.required_metrics({"label": label, "meta": {"backend": accounting_backend}}, s)
                    if name.startswith("slabinfo.")}
        if not required:
            continue
        metrics = telem.slab_memory(s, page_bytes)
        for name in sorted(required):
            c = metrics.get(name)
            reason = "missing" if c is None else c.reason or (
                "required cache unavailable" if c.value is None or c.quality != "ok" else "")
            if not reason and (c.begin is None or c.end is None or c.begin < s.ns or c.end < c.begin):
                reason = "required cache sample clock invalid"
            previous = named_last.get(name)
            if not reason and previous is not None:
                if (c.semantics, c.unit, c.basis) != (previous.semantics, previous.unit, previous.basis):
                    reason = "required cache source definition/target changed"
                elif c.begin < previous.end:
                    reason = "required cache sample clock decreased"
            if reason:
                named_issues.append(s.phase + ": " + name + ": " + reason)
            else:
                named_last[name] = c
    report = {"input": str(path), "backend": backend, "workload": label, "page_bytes": page_bytes,
              "accounting_backend": accounting_backend, "accounting_family": family,
              "threshold_bytes": LIMIT, "gate": "undefined", "issues": issues, "evidence": proof,
              "windows": [], "totals": [], "not_backend_exclusive": True,
              "supplemental_layout_records": supplemental,
              "gate_scope": "strict named-cache evidence plus nonoverlap global arithmetic, never backend attribution",
              "metadata_approximation": {"zram_table_entry_bytes_per_slot": 16, "swap_map_bytes_per_slot": 1,
                  "swap_cgroup_bytes_per_slot": 2, "zram_approx_bytes_per_slot": 19,
                  "proof_status": "stock layout approximation only; actual build evidence required",
                  "note": "excludes allocator rounding and other costs; never a residual addend"}}

    def add(left, right, scope):
        if (len(by_phase.get(left, [])) != 1 or len(by_phase.get(right, [])) != 1 or
                not by_phase[left][0].explicit or not by_phase[right][0].explicit):
            issues.append("missing/duplicate/nonexplicit window: " + left + "->" + right)
            return None
        if not telem.valid_page_bytes(page_bytes) or family not in (*POOL, "swap"):
            return None
        item = window(by_phase[left][0], by_phase[right][0], family, page_bytes, proof,
                      cmdline, headers or {}, scope, layouts)
        report["windows"].append(item)
        return item

    setup = add("before_backend_setup", "workload_start", "setup_global_observation")
    if label == "w1":
        rounds = sorted({int(m[1]) for name in by_phase if (m := re.fullmatch(r"r(\d+)_seq_(fill_begin|read_begin)", name))})
        if not rounds:
            issues.append("no W1 seq fill/read windows")
        for r in rounds:
            payload = add(f"r{r}_seq_fill_begin", f"r{r}_seq_read_begin", "W1_payload_global_observation")
            if setup is not None and payload is not None:
                report["totals"].append(dict(summed(setup, payload), round=r))
    elif label == "w2":
        add("workload_start", "workload_end", "W2_whole_workload_not_pure_backend")
    elif label in ("w3", "w4"):
        add("prepared", "read_begin", label.upper() + "_pressure_window_not_pure_backend")
    all_windows = report["windows"] + report["totals"]
    if not issues and all_windows and all(item["gate"] != "undefined" for item in all_windows):
        report["gate"] = "PASS" if all(item["gate"] == "PASS" for item in all_windows) else "FAIL"
    memory_snapshots = [dict(phase=s.phase, **s.memory) for s in measurements if s.memory]
    if memory_snapshots:
        statuses = [s["status"] for s in memory_snapshots]
        snapshot_status = "ERROR" if "ERROR" in statuses or issues else "INVALID" if "INVALID" in statuses else "CONSISTENT"
        report.update(memory_snapshot_protocol=telem.memory_snapshot_protocol(measurements),
                      memory_snapshot_status=snapshot_status,
                      memory_snapshot_validity={"CONSISTENT": "VALID", "INVALID": "INVALID", "ERROR": "ERROR"}[snapshot_status],
                      memory_snapshot_count=len(memory_snapshots), memory_snapshots=memory_snapshots,
                      free_basis="buddy_plus_PCP_after_v411")
        if (not issues and not proof["issues"] and all_windows and
                all(item["gate"] != "undefined" for item in all_windows)):
            report["gate"] = "FAIL" if any(item["gate"] == "FAIL" for item in all_windows) else "INVALID" if snapshot_status == "INVALID" else "PASS"
        if snapshot_status == "ERROR":
            report["gate"] = "undefined"
    report["residual_gate"] = report["gate"]
    report["named_cache_required_gate"] = "undefined" if named_issues else "PASS"
    report["named_cache_issues"] = named_issues
    if named_issues:
        report["gate"] = "undefined"
    if family == "memcomp":
        report["cluster_metadata"] = cluster_accounting(measurements, page_bytes)
        cluster = report["cluster_metadata"]
        if cluster["integrity_gate"] != "PASS":
            report["gate"] = "undefined"
        elif cluster["teardown_gate"] == "FAIL":
            report["gate"] = "FAIL"
    report["gate_scope"] = "strict backend evidence and objcg teardown plus nonoverlap global residual threshold; never a boot-validity relabel"
    return report, log


def metadata_density_gate(metadata_bytes, nonzero_pages):
    """Exact 10.5 B/object comparison; zero denominators remain undefined."""
    if (type(metadata_bytes) is not int or metadata_bytes < 0 or
            type(nonzero_pages) is not int or nonzero_pages <= 0):
        return "undefined"
    return "PASS" if 2 * metadata_bytes <= 21 * nonzero_pages else "FAIL"


def cluster_accounting(snapshots, page_bytes):
    """Independent of the global residual: metadata is already in its terms."""
    out = dict(source=telem.MEMCOMP_ACCOUNT, integrity_gate="PASS", teardown_gate="undefined",
               density_limit_bytes_per_nonzero_page=10.5, snapshots=[], issues=[],
               note="density gate applies to the cohort-v4 512 MiB merged sample; select/aggregate externally; "
                    "pending included once, retired is a subset, owned xarray added once; not a residual addend")
    seen, final, last_end, layout = set(), None, None, None
    for s in snapshots:
        if s.phase == "boot" or is_thp_policy_event(s):
            continue
        try:
            if s.memory.get("version") == telem.MEMORY_PROTOCOL:
                block(s, telem.MEMCOMP_OCCUPANCY)
            captured = block(s, telem.MEMCOMP_ACCOUNT)
            if last_end is not None and captured.begin < last_end:
                raise ValueError("metadata sample clock decreased")
            last_end = captured.end
            account = telem.memcomp_accounting(s, page_bytes)
            values, owners = account["values"], account["objcgs"]
            current_layout = tuple(values[k] for k in ("version", "page_bytes", "cluster_header_bytes", "xa_node_bytes"))
            if layout is not None and layout != current_layout:
                raise ValueError("metadata layout changed within boot")
            layout = current_layout
            if seen - owners.keys():
                raise ValueError("objcg inventory lost IDs/tombstones: " + ",".join(sorted(seen - owners.keys())))
            seen.update(owners)
            pages = values["stored_nonzero_pages"]
            out["snapshots"].append(dict(phase=s.phase, **account,
                bytes_per_nonzero_page=account["total_bytes"] / pages if pages else None,
                density_gate=metadata_density_gate(account["total_bytes"], pages)))
            if s.phase == "workload_end":
                if final is not None:
                    raise ValueError("duplicate workload_end metadata")
                final = account
        except ValueError as exc:
            out["issues"].append(s.phase + ": " + str(exc))
    if not out["snapshots"]:
        out["issues"].append("no cluster metadata samples")
    if final is None:
        out["issues"].append("missing workload_end metadata/objcg teardown evidence")
    if out["issues"]:
        out["integrity_gate"] = "undefined"
    elif final is not None:
        out["teardown_gate"] = "PASS" if all(
            row["payload_bytes"] == row["metadata_bytes"] == 0
            for row in final["objcgs"].values()) else "FAIL"
    return out


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path, help="real smoke run1.log.gz (never an R3 performance input)")
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--config", type=Path, help="actual smoke build .config")
    parser.add_argument("--guest-source", type=Path, help="exact captured guest.c producer (default sibling guest.c); hash must match adjacent metadata.json")
    parser.add_argument("--swap-header", action="append", default=[], metavar="GUEST_DEVICE=HOST_IMAGE")
    parser.add_argument("--output", type=Path, help="JSON output, default stdout")
    args = parser.parse_args(argv)
    headers = {}
    for value in args.swap_header:
        pair = value.split("=", 1)
        if len(pair) != 2 or not all(pair) or pair[0] in headers:
            parser.error("--swap-header requires unique GUEST_DEVICE=HOST_IMAGE")
        headers[pair[0]] = pair[1]
    report, _ = analyze(args.log, args.source_root, args.config, headers, args.guest_source)
    text = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.write_text(text)
    else:
        print(text, end="")
    return {"PASS": 0, "FAIL": 1, "undefined": 2, "INVALID": 3}[report["gate"]]


if __name__ == "__main__":
    raise SystemExit(main())
