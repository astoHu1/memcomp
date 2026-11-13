# mcbench source

bench_init.c and mkcorpus.py are copied unchanged from the existing
mcbench benchmark. BUILD-mcbench.txt records the source, compiler,
options and output hashes, and SHA256SUMS covers the distributed files.
The supplied binary is a static AArch64 build.

Generate a corpus on the host with mkcorpus.py, as described in its
source, and use the same corpus, with the same SHA256, on both arms. Do
not commit the corpus.

bench_init.c still contains code for running as PID 1. Run it only as an
ordinary program, never as init. run.sh refuses to start if
/knob_toggle.ko exists and unsets inherited knob and huge-page options,
so it never loads a test module.
