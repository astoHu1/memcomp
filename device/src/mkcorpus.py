# SPDX-License-Identifier: GPL-2.0
"""Build a 32 MiB page corpus meant to compress about like a phone's anon
memory (zstd ~3.5-4x): C source, stripped arm64 modules, object-heap-like
pages, partly used pages, zero pages and incompressible pages, shuffled."""
import glob, os, random, struct, subprocess, sys
PG = 4096
out, tree = sys.argv[1], sys.argv[2]
r = random.Random(1)
def pages(data):
    return [data[i:i + PG] for i in range(0, len(data) - PG + 1, PG)]
kod = b""
os.makedirs("corpus_src", exist_ok=True)
for m in sorted(glob.glob(f"{tree}/out/**/*.ko", recursive=True))[::7]:
    t = "corpus_src/strip.ko"
    subprocess.run(["llvm-objcopy", "--strip-all", m, t], check=True)
    kod += open(t, "rb").read()
    if len(kod) > 8 << 20:
        break
src = b""
for f in sorted(glob.glob(f"{tree}/mm/*.c") + glob.glob(f"{tree}/kernel/**/*.c", recursive=True)):
    src += open(f, "rb").read()
    if len(src) > 12 << 20:
        break
srcp, kop = pages(src), pages(kod)
r.shuffle(srcp); r.shuffle(kop)
classes = [0x70000000 + 0x40 * i for i in range(40)]
base = 0x7a3c000000
def heap_page():
    b = bytearray()
    while len(b) < PG:
        n = r.choice((2, 3, 4, 6, 8))
        b += struct.pack("<II", r.choice(classes), 0)
        for _ in range(n - 1):
            k = r.random()
            if k < 0.35:
                b += struct.pack("<Q", 0)
            elif k < 0.65:
                b += struct.pack("<Q", base + r.randrange(1 << 22) * 8)
            elif k < 0.85:
                b += struct.pack("<Q", r.randrange(256))
            else:
                b += struct.pack("<II", r.choice(classes), r.randrange(1 << 16))
    return bytes(b[:PG])
def partial(p):
    keep = r.randrange(PG // 20, PG // 2)
    return p[:keep] + bytes(PG - keep)
N = 8192
p = srcp[:int(N * .30)] + kop[:int(N * .14)]
p += [heap_page() for _ in range(int(N * .24))]
p += [partial(r.choice(kop + srcp)) for _ in range(int(N * .22))]
p += [bytes(PG)] * int(N * .07)
p += [r.randbytes(PG) for _ in range(N - len(p))]
r.shuffle(p)
open(out, "wb").write(b"".join(p))
print(len(p), "pages")
