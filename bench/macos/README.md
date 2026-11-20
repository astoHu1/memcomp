# 原生 macOS ARM64 构建与 Apple Virtualization Framework 运行

本目录在 Apple Silicon macOS 主机上原生运行构建工具，生成 Linux
6.12.23 ARM64 内核及静态 AArch64 Linux 用户态，再通过 Apple
Virtualization Framework (VZ) 启动一次性 guest。它不是 Docker/Linux
构建容器，也不是 x86_64 KVM 路径；macOS 主机工具与 Linux guest 产物
不能混用。已复现的问题、修复及实测边界见 [DIAGNOSIS.md](DIAGNOSIS.md)。

## 测试入口

| 原测试 | macOS arm64 路径 |
| --- | --- |
| 有盘、无盘 E2E；MM selftest；KSM | `e2e.config`，`run.sh` 默认运行；`SWAP_MB=0` 不附加任何磁盘 |
| KSM；HMM 模块 | `RUN_KSM=1`；打包 `hmm.config` 内核后加 `RUN_HMM=1 HMM_MODULE=1` |
| MGLRU / classic LRU KUnit | 基础配置 / `classic-kunit.config`，用独立 guest 和 `KUNIT_ONLY=1` |
| 真实 swap budget KUnit | [实验性入口](../kunit-swap/README.md)，已观察到断言失败，不纳入默认验收 |
| 两节点 NUMA；MTE；授权 MM swapoff | 不在 VZ 跑；保留 [QEMU TCG](tcg.md) 实验性入口 |
| 独立 memcg tmpfs 回读 | [S08](../memcg/README.md) 可单独跑；匿名归属检查已有失败，不纳入默认验收 |
| compare 工作负载 | [VZ 功能跑法](compare.md)，不进入正式性能矩阵 |

Linux 6.12 arm64 的两处正向 soft-dirty 断言单独计为跳过，其余 compat、
完整 pagemap token 比较及 UFFD-WP 不跳过。VZ 单节点不算跨节点迁移覆盖，
MM selftest 的 swapoff 在 VZ 仍受原来的 QEMU 身份门禁限制；这不影响
独立的 E2E swapoff。Android A/B 仍需配套 ACK 内核及 Android 设备，
不能用普通 Linux guest 的结果代替。

## 依赖

需要支持 VZ 的 Apple Silicon Mac，以及提供 `/usr/bin/clang`、
`xcrun swiftc`、macOS SDK 和 `codesign` 的 Xcode/Command Line Tools。
runner 会检查 `VZVirtualMachine.isSupported` 并验证虚拟机配置；
`build-runner.sh` 使用本目录的 virtualization entitlement 进行临时签名。
如果预检报告当前进程无法使用 VZ，请在 macOS 本地终端运行
`build/macos-vz/vz-runner --check`。受限沙箱可能禁止使用 VZ，即使
同一台 Mac 能从本地终端启动 guest。

Homebrew 依赖为 LLVM、lld、GNU make、coreutils、GNU sed、findutils、
GNU grep、GNU tar、bison、flex、cmake，脚本还直接使用 ripgrep (`rg`)：

```sh
brew install llvm lld make coreutils gnu-sed findutils grep gnu-tar bison flex cmake ripgrep
```

日志门禁需要 Python 3；TCG 和 compare 的版本要求见各自说明。

`env.sh` 默认 `BREW_PREFIX=/opt/homebrew`，将上述 GNU 工具和 LLVM
放在 PATH 前面；非默认安装位置需显式导出 `BREW_PREFIX`。
guest 编译使用 Homebrew clang/LLVM 与独立 lld formula 的
`opt/lld/bin/ld.lld`；host 工具仍使用 Apple clang。
`compat/` 提供 Darwin host 兼容头，其中 `elf.h` 依赖本地 musl 源码。

## 源码与忽略规则

以下命令均从本仓库根目录执行。源码必须已在本地准备好；脚本不会
下载、clone、切换分支或自动应用补丁。

根据仓库根目录 `README.md` 的 Kernel series 一节，所需内核为：

- branch/ref：`memcomp/series-v1`，基于 Linux 6.12.23，含 8 个前置提交及
  16 个系列补丁；不是 `memcomp/android16-6.12` ACK 分支。
- 签名分支头：`07038a98738485a22e70da0b58e1873a5867ec0b`。
- 对应提交的 tree：`06b510f011f66cb14e8a5dd4d00d72a3a1a0a7a5`。

默认源码路径及实测版本如下：

| 路径 | 用途与实测版本 |
| --- | --- |
| `kernel-src/linux-mainline` | 上述内核分支，加本目录的 `kernel-fixes.patch` |
| `kernel-src/musl` | 静态 libc，`7813e9119eb5fe681b6750b8a033a0490bca0941` |
| `kernel-src/llvm-project` | compiler-rt builtins，`ca7933e47d3a3451d81e72ac174dcb5aa28b59d1`（`llvmorg-22.1.8`） |

运行前检查各源码树的版本和本地修改：

```sh
git status --short --untracked-files=normal
git -C kernel-src/linux-mainline status --short --branch
git -C kernel-src/linux-mainline rev-parse HEAD 'HEAD^{tree}'
git -C kernel-src/musl status --short --branch
git -C kernel-src/llvm-project status --short --branch
```

根 `.gitignore` 忽略 `/build/`、`/kernel-repos/`、`/kernel-src/` 和
`/results/macos-vz/`，因此根仓库状态不能替代源码树自己的状态检查，
运行结果也不会自动纳入版本管理。

实测内核包含 `bench/macos/kernel-fixes.patch` 中的测试桩隔离和 fixture
修复。对相同 base 的干净树，先检查再应用；已包含修复时不要重复应用。
本补丁不改写发布的 `patches/v1/`。

```sh
TREE=${TREE:-"$PWD/kernel-src/linux-mainline"}
git -C "$TREE" apply --check "$PWD/bench/macos/kernel-fixes.patch" &&
  git -C "$TREE" apply "$PWD/bench/macos/kernel-fixes.patch"
```

## 构建

```sh
export OUT="$PWD/build/macos-e2e"
CONFIG_FRAGMENT="$PWD/bench/macos/e2e.config" bash bench/macos/build.sh --configure
INITRAMFS_OUT="$OUT/guest" KERNEL_UAPI_OUT="$OUT/uapi" bash bench/macos/mkinitramfs.sh
bash bench/macos/build-runner.sh
export INITRAMFS="$OUT/guest/initramfs.gz"
```

`build.sh --configure` 从 `arm64-vz.config` 执行 `allnoconfig`，然后立即
构建 `Image`，不是仅生成配置。默认 `TREE=$PWD/kernel-src/linux-mainline`、
`OUT=$PWD/build/macos-arm64`、`JOBS=8`；这里的 `$PWD` 指仓库根目录，
脚本实际按自身位置计算根路径。需要覆盖时，在各条命令间保持一致地
导出 `TREE`、`OUT`、`JOBS`。musl 与 llvm-project 的源码路径及用户态
输出目录仍固定在仓库根目录下，不随 `TREE`、`OUT` 改变。

已有配置可用相同 `CONFIG_FRAGMENT` 增量构建；缺少 `.config` 时自动配置。
不同 profile 使用不同 `OUT`。`CONFIG_ONLY=1` 只检查配置，不生成 Image。
脚本检查生效的配置，并在 MEMCOMP 启用时拒绝 ZRAM/ZSWAP；构建后检查
`vmlinux` 是 AArch64 ELF，`Image` 是小端
ARM64、4 KiB 页内核，并打印内核 SHA-256。

| 步骤 | 主要产物 |
| --- | --- |
| `build.sh` | `build/macos-arm64/.config`、`vmlinux`、`arch/arm64/boot/Image` |
| `build-userspace.sh` | `build/aarch64-musl/` 的静态 libc 和启动文件 |
| `build-runtime.sh` | `build/compiler-rt/lib/linux/libclang_rt.builtins-aarch64.a` |
| `mkinitramfs.sh` | 导出 UAPI 至 `build/kernel-uapi/`；生成 `build/macos-userspace/initramfs.gz` |
| `build-runner.sh` | 已签名的 `build/macos-vz/vz-runner` |

initramfs 以 `bench/e2e/mcwb_init.c` 编译的程序作为 `/init`，包含
`/memcomp_selftest`、`/ksm_memcomp_test`，不提供交互 shell。选用
`hmm.config` 后，`build.sh` 还构建 `test_hmm.ko`，打包器将模块和原始
`hmm-tests` 纳入镜像。同一 profile 的 `TREE`、`OUT` 必须一致。
`mkinitramfs.sh` 会在静态 libc
或 builtins 缺失时调用对应构建脚本。改动内核、配置、E2E 源码或头文件
后，应重建相关产物；`run.sh` 不负责重建或判断产物是否陈旧。

### KUnit 的编译与运行边界

原 E2E 配置不编译 KUnit hooks，使用上面的 `e2e.config` 对齐；
`kunit.enable=0` 只关闭运行，不等于移除 hooks。独立 KUnit 使用基础配置，
或另建 `OUT` 并加 `CONFIG_FRAGMENT="$PWD/bench/macos/classic-kunit.config"`：

```sh
OUT="$PWD/build/macos-kunit" bash bench/macos/build.sh --configure
OUT="$PWD/build/macos-kunit" KUNIT_ONLY=1 bash bench/macos/run.sh
```

沿用前面生成的 `INITRAMFS`，KUnit-only 在 kernel poweroff 前不启动 `/init`，
所以 boot-time 的真实 swap suites 不会看到活动 swap。

## 运行与磁盘隔离

```sh
bash bench/macos/run.sh
SWAP_MB=0 bash bench/macos/run.sh
```

无 guest 参数时运行实测的 VZ 配置，包含 `RUN_KSM=1 RUN_SELFTEST=1`，
并要求 E2E 的三处预期跳过与日志一致。有盘默认配置为 1 GiB RAM、
2 GiB 私有 swap 盘，使用下节的写回压力参数；无盘只验证内存内路径。
有任意显式 guest 参数时，不补入这些参数或默认跳过数量；变更有盘
默认配置的 RAM 或盘大小时也须显式给出 guest 参数。
`run.sh [新结果目录] [guest 参数...]`
的第一个位置参数始终是结果目录；省略目录但要传 guest 参数时可用
`bash bench/macos/run.sh '' RUN_SELFTEST=1`。显式目录必须尚不存在，
脚本用 `mkdir` 防止复用；省略时在 `results/macos-vz/run.XXXXXXXX`
创建新目录。guest 参数须逐个传入，不要将整串参数合并成带空格的 token。

host 环境变量 `MEMORY_MB`、`CPUS`、`TIMEOUT`、`SWAP_MB` 默认分别为
1024 MiB、4、900 秒、2048 MiB。它们控制 VZ 资源和 host 超时；
`TEST_TIMEOUT` 等则作为 guest 命令行参数传入，二者不可混淆。

**E2E 只将本次结果目录里新建的私有 `swap.raw` 暴露给 guest。**
`SWAP_MB=0` 启动无盘 VM，并追加 `NO_SWAP=1`。E2E launcher 不挂载 host
目录，也不附加 host 原始磁盘。
guest `/init` 会对 `/dev/vda` 写 swap 头、执行 swapon，并在相关用例中
执行 swapoff。不得将真实磁盘、已有重要镜像、host swap 或其他共享盘
替换为该文件，也不要绕过脚本给 runner 传入这些对象。所有压力和
swapoff 操作只允许在这种可丢弃 guest 中执行，不在 macOS 主机执行。

## 有盘写回复现

不带 guest 参数的 `run.sh` 使用下列参数。原 x86 E2E 参数在 VZ 上
没有稳定触发写回，因此使用更大的填充量与自然 reclaim 压力；不关闭
任何 E2E 阶段。需要定制 guest 参数时，可以从这条等效命令开始：

```sh
E2E_SKIPS=3 MEMORY_MB=1024 SWAP_MB=2048 bash bench/macos/run.sh '' \
  RUN_KSM=1 RUN_SELFTEST=1 \
  FILL_MB=1792 PRESS_MB=64 CHUNK_MB=4 \
  PAGEOUT_LAG=0 PACE_US=3000 THP=never WB_WAIT_S=10 \
  CYCLES=1 CONC_SECS=3 SWAPOFF_MB=1792 SWAPOFF_PRESS_MB=64 \
  TEST_TIMEOUT=820 HEARTBEAT_S=15
```

`PAGEOUT_LAG=0` 只禁止 fill 阶段对旧 chunk 主动调用 `MADV_PAGEOUT`，
用超过 guest RAM 的填充触发自然内存压力；后续 fill+pressure 等阶段
仍有显式 pageout，不能称为整个流程完全没有 `MADV_PAGEOUT`。
`THP=never` 是 `/init` 设置的全局 THP 策略，不是编译关闭 THP，
也不表示所有单独设置策略的 mTHP 子测试都被移除。

这组参数只跑一轮 cycles、3 秒并发回读，并非与历史 KVM 运行配对。
三项计数跳过是两处 soft-dirty 和 MM selftest 通过部分 TAP 后报告的跳过。

### 可选 HMM 模块测试

HMM 使用单独的内核和 initramfs。下列无盘配置只跑 HMM 所需的
功能路径，不重复默认 E2E 的 churn、cycles 和并发压力阶段：

```sh
export OUT="$PWD/build/macos-hmm"
CONFIG_FRAGMENT="$PWD/bench/macos/hmm.config" bash bench/macos/build.sh --configure
INITRAMFS_OUT="$OUT/guest" KERNEL_UAPI_OUT="$OUT/uapi" bash bench/macos/mkinitramfs.sh
INITRAMFS="$OUT/guest/initramfs.gz" SWAP_MB=0 TIMEOUT=240 bash bench/macos/run.sh '' \
  RUN_HMM=1 HMM_MODULE=1 RUN_KSM=1 kfence.sample_interval=1 loglevel=7 \
  TEST_TIMEOUT=180 SKIP_CHURN=1 SKIP_CYCLES=1 SKIP_CONC=1
```

## 日志、覆盖与验收边界

每个结果目录保留 `console.log`、`host.log`、`vz-info.txt`、
`provenance.txt`、有盘时的私有 `swap.raw`，以及内核/工作区的 dirty diff、
status 和 `source-sha256.txt`。provenance 记录 HEAD/tree、产物与配置
散列、运行资源、完整 guest 命令行及状态快照散列；源码快照标明
`source_snapshot=run-time`，它是运行时快照，不是构建时来源证明。
若构建后又改源码，不能仅凭这份记录断言该 Image 包含那些改动。

VZ 正常关机只代表虚拟机停止；`run.sh` 还会调用本目录的
`validate-log.py`，分别验收 E2E、请求的 KUnit 与 MM selftest。请求
KSM 和 HMM 时还要求对应结果通过。原 KVM 验收入口不变。

- `coverage=PARTIAL` 必须伴随 `skips>0`：实际执行范围存在跳过。
  默认验证器可以返回零，但不得把它升级成 COMPLETE 或全套测试通过。
- `coverage=COMPLETE` 必须伴随 `skips=0`：仅表示此 E2E harness
  当前统计范围内没有计入的 coverage skip，且仍须满足其余验收条件。
  它不自动证明所有可选套件、架构或工作负载已覆盖。
- `COVERAGE: SKIP (expected)` 不增加 `g_skips`；未请求的可选套件也
  不一定计入 skips。即使字段是 COMPLETE，也须核对参数、预期 skip
  和独立套件日志。不要把 E2E 汇总当成 KUnit 或独立 selftest 的完整回执。

要求此验证器拒绝 PARTIAL 时，使用 `REQUIRE_COMPLETE=1`，例如在运行
命令前设置该 host 环境变量，或对已有日志执行：

```sh
REQUIRE_COMPLETE=1 bash bench/macos/run.sh
```

该设置只收紧验收，不会补跑跳过的用例。超时、崩溃、缺失 RESULT、
内核诊断或任何失败均应保留原始证据；不能由命令存在、构建完成、
runner 退出或旧结果推断这次通过。每次运行的结论只适用于其参数与
源码/产物快照，不自动扩展到其他配置或工作负载。

### Independent console gate

Pass only the raw guest `console.log`, after checking the VZ runner status.
Use the effective guest flags, not values inferred from which tests appear in
the log. The standalone gate does not change the KVM validator.

```sh
python3 bench/macos/validate-log.py "$RESULT/console.log" \
  --mode regular --kunit-enabled 1 --run-selftest 1 \
  --e2e-skips 5 --require-swapoff

python3 bench/macos/validate-log.py "$RESULT/console.log" \
  --mode diskless --kunit-enabled 0 --run-selftest 1

python3 bench/macos/validate-log.py "$RESULT/console.log" --mode kunit
```

- `regular` requires writeback and the single-page swap read witness.
  `--require-swapoff` additionally requires the E2E swapoff witness. `diskless`
  requires the `NO_SWAP=1` skip sentinels and zero swap transport counters.
- `--e2e-skips N` checks an exact count (`0` requires `COMPLETE`). Omit it to
  verify and report the observed count without prescribing it. Both paths
  reconcile the summary with the counted skip records; expected diskless skips
  are not counted, and a partially skipped MM selftest counts once in E2E.
- Requested MM selftest must have a complete eight-case TAP stream between its
  `BEGIN` and `END` records. `--selftest-skips N` overrides the expected TAP
  skips (regular: 2; diskless: 5).
- KUnit pins the following leaf totals and skips for the 6.12 VZ profiles:
  `swap-memcomp-budget=5/5`, `swap-memcomp-packed-budget=5/5`,
  `memcomp=484/7`, and `memcomp-codec-scope=1/0`. For a different kernel or
  config, repeat `--kunit-suite NAME=TOTAL/SKIPS` for the entire expected
  suite set. Standalone `--mode kunit` requires complete KTAP and a power-down
  record without guest init; it does not require or credit E2E or MM selftest.

Exit status is 0 when all requested gates pass, 1 for rejected/unreadable logs,
and 2 for invalid arguments. A successful gate still reports partial coverage
and disabled suites separately. Run the host-only regressions with
`python3 -B -m unittest discover -s bench/macos/tests -v`.
