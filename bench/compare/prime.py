# SPDX-License-Identifier: GPL-2.0-only
"""W1 prime observation contract; integrity is not a real-guest smoke verdict."""
import re

PRIME_SOURCE = "pagemap_after_prime_drain_before_readback"
PRIME_FIELDS = (
    "prime_source", "prime_swap_type", "prime_memcomp_type",
    "prime_requested_pages", "prime_observed_pages", "prime_real_swap_pages",
    "prime_memcomp_pages", "prime_present_pages", "prime_other_pages",
    "prime_drain_rounds", "prime_drain_remaining_swapcache_pages",
    "prime_drain_remapped_pages", "prime_drain_timed_out")
PRIME_CONTRACT = {
    "source": PRIME_SOURCE, "content": "random",
    "real_swap_identity": "COMPARE_SWAP_TYPE_bound_to_workload_spec.active_swap_types",
    "record": "one_rewrite_batch_per_inner_round_including_warmup",
    "smoke_verdict": "report_only_pending_realguest_review_no_numeric_threshold",
}


def prime_counts(row, spec):
    """Bind counts to the controlled guest's swap identities and exact mapping.

    Real swap means a nonpresent swapped PTE of the declared active type,
    whether or not its folio remains in swapcache. Memcomp tokens are separate.
    No count/fraction here decides whether the swapcache path was exercised.
    """
    if row.get("mode") != "rewrite" or row.get("record") != "batch":
        if any(row.get(name) for name in PRIME_FIELDS):
            raise ValueError("prime fields outside rewrite batch")
        return None
    if row.get("prime_source") != PRIME_SOURCE:
        raise ValueError("missing/mismatched prime snapshot source")
    values = {}
    for name in PRIME_FIELDS[1:]:
        text = row.get(name, "")
        minimum = -1 if name in ("prime_swap_type", "prime_memcomp_type") else 0
        if not re.fullmatch(r"-?\d+", text or "") or int(text) < minimum:
            raise ValueError("missing/invalid " + name)
        values[name] = int(text)
    if not isinstance(spec, dict):
        raise ValueError("prime requires workload_spec source binding")
    active = spec.get("active_swap_types")
    memcomp = spec.get("memcomp_swap_type")
    if (not isinstance(active, list) or len(active) > 1 or
            any(type(t) is not int or not 0 <= t <= 31 for t in active) or
            (memcomp is not None and (type(memcomp) is not int or not 0 <= memcomp <= 31)) or
            memcomp in active):
        raise ValueError("prime requires distinct verified guest swap identities")
    if (values["prime_swap_type"] != (active[0] if active else -1) or
            values["prime_memcomp_type"] != (memcomp if memcomp is not None else -1)):
        raise ValueError("prime swap identity differs from workload_spec")
    pages = spec.get("pages")
    if (type(pages) is not int or pages <= 0 or
            values["prime_requested_pages"] != pages or
            values["prime_observed_pages"] != pages):
        raise ValueError("prime requested/observed mapping coverage mismatch")
    population = sum(values[name] for name in (
        "prime_real_swap_pages", "prime_memcomp_pages", "prime_present_pages", "prime_other_pages"))
    if (population != pages or (not active and values["prime_real_swap_pages"]) or
            (memcomp is None and values["prime_memcomp_pages"])):
        raise ValueError("prime population/identity accounting mismatch")
    rounds = values["prime_drain_rounds"]
    remaining = values["prime_drain_remaining_swapcache_pages"]
    remapped = values["prime_drain_remapped_pages"]
    # W1 also retries PAGEOUT for present pages. Rounds count those retries;
    # remapped counts only cached-nonpresent pages, so it can stay zero.
    if (rounds > 4 or remaining > values["prime_real_swap_pages"] or remapped > 4 * pages or
            (rounds == 0 and remapped != 0) or (remaining and rounds != 4) or
            values["prime_drain_timed_out"] not in (0, 1)):
        raise ValueError("invalid prime bounded drain diagnostics")
    return values
