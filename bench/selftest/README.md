# Independent memcomp kselftest receipt

`receipt.py` creates a fail-closed JSON receipt for the standalone
`memcomp_selftest` TAP output. It is intentionally separate from the E2E and
full-link runners.

## Command

```sh
python3 bench/selftest/receipt.py \
  --log /path/to/qemu-console.log.gz \
  --provenance /path/to/provenance.txt \
  --source /path/to/linux-source \
  --output /path/to/selftest-receipt.json
```

The provenance file uses the existing standalone format:

```text
head=<source commit SHA>
kernel=<sha256> <kernel image>
config=<sha256> <kernel .config>
selftest=<sha256> <memcomp_selftest binary>
```

The three artifact paths must exist and their computed SHA256 values must
match. A source path is optional only when the provenance `head` is present;
when supplied, a source file or deterministic directory manifest is hashed.
The complete log must contain the standalone marker, TAP plan `1..8`, exactly
eight numbered results, matching totals of `8 PASS`, `0 FAIL`, `0 SKIP`, and the
exact zero-exit completion marker. The BEGIN, numbered TAP, totals, child
exit and QEMU completion records must belong to one ordered execution. The
child must exit zero without a signal; QEMU must either have one legacy
`qemu_rc=0` record or the owned supervisor's zero exit, no timeout, verified
death and drained-group record. Duplicate/missing completion records and
TODO results, explicit guest failures and fatal kernel diagnostics prevent
a receipt. An explicit source SHA must agree with the provenance head. These checks do not prove that
an arbitrary kernel image was built from the declared source; the invoking
runner must independently bind its clean HEAD, build and stable inputs.
Gzip input is decompressed for `log.sha256` while
`log.input_sha256` preserves the exact supplied file bytes.

Missing inputs, stale paths, hash mismatches, malformed TAP, non-zero exit
markers, or any result other than 8/0/0 return non-zero and do not write a
receipt. The command never infers a PASS from a missing log or artifact.

The full eight-result plan is PAGEOUT, refault integrity, refault pagemap,
clean swap-cache eviction, dirty swap-cache rewrite, rewrite integrity,
swapoff integrity, and restoration of the selected swap area and priority.
A successful old five-result plan (`1..5`, 5 PASS) or rewrite-only plan
(`1..3`, 3 PASS) is rejected, even with valid artifact hashes and zero child
and QEMU exits. Neither is a substitute for the full selftest contract.

## Host regression tests

```sh
scratch=<work>/v46-20261017/selftest-receipt-contract
mkdir -p "$scratch/tmp"
TMPDIR="$scratch/tmp" TMP="$scratch/tmp" TEMP="$scratch/tmp" \
  MEMCOMP_TEST_TMP_ROOT="$scratch/tmp" PYTHONDONTWRITEBYTECODE=1 \
  python3 -B -m unittest -v bench.selftest.test_receipt
```

Run this command from the material repository root. Host parser/receipt tests
validate the host contract only: they do not execute a guest, build a kernel,
or establish guest acceptance. The tests explicitly reject old five-result and
rewrite-only three-result success logs, and retain ordering, duplicate-record,
artifact-hash, source/provenance and verified-supervisor-death checks.

Historical guest evidence in
`results/2026-10-15-selftest-current-5ac721/README.md` is not upgraded by these
host tests and does not replace a current full eight-result guest receipt.
