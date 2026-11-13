# SPDX-License-Identifier: GPL-2.0-only
"""W1 v4.8 first-pass denominator, distinct from the later read snapshot."""
from decimal import Decimal, InvalidOperation

FIRST_PASS_FIELDS = ("first_pass_nonpresent_pages", "first_pass_nonpresent_fraction",
                     "drain_additional_nonpresent_pages")
PAGEOUT_CONTRACT = {"denominator": "first_pass_nonpresent_pages",
                   "sample": "pagemap_only_after_timed_pageout_before_drain_retry",
                   "read_classification": "post_drain_snapshot"}


def first_pass_counts(row):
    try:
        requested = int(row["requested_pages"])
        first = int(row["first_pass_nonpresent_pages"])
        final = int(row["nonpresent_pages"])
        added = int(row["drain_additional_nonpresent_pages"])
        fraction = Decimal(row["first_pass_nonpresent_fraction"])
    except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
        raise ValueError("missing/malformed W1 first-pass denominator") from exc
    if (requested <= 0 or not 0 <= first <= requested or not 0 <= final <= requested
            or added != final - first or not fraction.is_finite()
            or abs(fraction - Decimal(first) / requested) > Decimal("1e-9")):
        raise ValueError("inconsistent W1 first-pass fraction/count/drain delta")
    return first, fraction, added
