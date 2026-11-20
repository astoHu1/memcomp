# macOS ARM64 / VZ 排查记录

日期：2026-11-18。基线为 Linux 6.12.23 memcomp series-v1，HEAD
`07038a98738485a22e70da0b58e1873a5867ec0b`，加本地修复。
内核与 Linux 静态用户态均由 macOS 原生工具构建；运行后端为 Apple
Virtualization Framework，不是 Linux 构建 VM、QEMU 或 KVM。

## 主要故障与修复

### KUnit 测试桩泄漏到正常 swap 路径

`mm/memcomp.c` 中，`CONFIG_MEMCOMP_KUNIT_TEST=y` 原本使
`mc16_kunit_real_swap` 默认为 false。此时 `memcomp_slot_dup/put/count`
和预取槽位归还使用模拟计数或空操作。`kunit.enable=0` 只关闭测试执行，
不会移除这些编译进去的路径；测试执行后的 cleanup 也会将模式置回 false。

实际 writeback 因此没有为 memcomp token 取得应有的真实 swap 引用。
swap cache 释放后，token 仍可能指向已释放的槽位。
`__read_swap_cache_async()` 对未使用槽位可以返回 NULL，并不一定是
内存分配失败；memcomp 将这个 NULL 转成 `-ENOMEM`，最终形成缺页 OOM
无限重试。

修复前的直接证据在
`results/macos-vz/arm64-fault-diag-20261118/console.log`：

- 匿名页同一地址持续从 `do_swap_page` 返回 `VM_FAULT_OOM`。
- 原始 PTE 为 `0x64b784`，ARM64 解码为 memcomp type 30、token `0x64b`。
- writeback 已达 60,584 页，但 `swapped_read_io=0`；空闲内存恢复至约
  76 MiB 后仍不前进，30 秒超时，`oom_kill=0`。

修复使真实 swap 成为默认；仅 KUnit case 生命周期内进入 synthetic
模式，最后在 fixture、worker 与 RCU 清理结束后恢复真实模式。
三个真实 swap fixture 保存并恢复外层模式；两个独立 budget suite
也使用同一 case init/cleanup。没有修改 writeback 门槛或关闭 MGLRU。

隔离边界：synthetic 模式仍是全局的，以覆盖测试 worker 和 RCU 清理。
这些操作全局 memcomp 状态的 KUnit 用例只应串行运行在无业务 reclaim
流量的一次性 guest 中，不应在承载真实负载的系统上动态执行。

### KUnit 目标 folio 缺少 memcg charge

consumer 和 async fixture 原先把未 charge 的目标 folio 交给公共
load 路径；真实 `workingset_refault()` 因此触发 DEBUG_VM 警告。
修复在 fixture 中执行真实 charge，补充调用契约断言，并在未入 LRU
的目标最终释放前清 active。生产 charge/refault 逻辑没有因此被弱化。

### ARM64 zero-token 测试误判

ARM64 此配置没有 swap soft-dirty bit。E2E zero-token 测试将正向
soft-dirty 断言计为跳过，但仍要求 bit 55 为零。完整 token 一致性、
MOVE、hole、swap 和 UFFD WP 检查保留。

### writeback 压力条件

旧的主动 pageout 工作负载使空闲内存回升；pool 超过门槛时可能没有
新的合格 kswapd 高压 scope，因而不能要求 writeback 自动增长。
定向回归在 fill 阶段使用 `PAGEOUT_LAG=0` 产生自然压力。
`writeback_pressure_scopes` 是瞬时活动数，累计进入次数应看 `events`。
普通 `pswpout` 不能单独证明 memcomp writeback。

## 已完成验证

修复后内核 Image SHA-256：
`67105787a9b8444e92893e932a5c49704ef570d89ad5e5abb34a7d7d1f214d73`。
配置保留 MEMCG、DEBUG_VM、MEMCOMP_KUNIT_TEST 与 MGLRU，禁用 ZRAM/ZSWAP。
增加 PROC_SYSCTL、MAGIC_SYSRQ 以支持超时诊断；临时缺页日志已移除。
原生构建成功；未修改的 `mm/ksm.c` 有一条 clang uninitialized 警告，
因此不是零编译警告声明。

以下三轮均为 1 GiB RAM、4 vCPU、2 GiB 私有 swap 的 VZ guest，
`FILL_MB=1792 PRESS_MB=64 CHUNK_MB=4 PAGEOUT_LAG=0 PACE_US=3000 THP=never`。
完整参数、源码 diff、产物散列与 VZ 后端信息保存在各结果目录。

| 结果目录（相对 `results/macos-vz/`） | KUnit 执行 | E2E 结果 | writeback 页 | swap 回读页 |
| --- | --- | --- | --- | --- |
| `arm64-slotfix-no-kunit-20261118` | 关闭执行，仍编译 | PASS / PARTIAL，7 skips，25 秒 | 144176 | 98001 |
| `arm64-slotfix-kunit-20261118` | 启动时执行 | PASS / PARTIAL，7 skips，25 秒 | 156860 | 97979 |
| `arm64-slotfix-extended-20261118` | 启动时执行 | PASS / PARTIAL，5 skips，85 秒 | 514797 | 447858 |

三轮均 `mismatches=0`、`oom_kill=0`，日志验证器通过，无内核
WARNING/BUG/Oops，未再出现 OOM 重试循环。独立 mincore 单页见证检查
确认 token 查询不触发读入，随后的单页磁盘回读内容正确。

日志验证器还会拒绝本次故障特有的 `Huh VM_FAULT_OOM`、
`pagefault_out_of_memory` 和临时 `memcomp-diag:` 诊断行；因此旧的
`arm64-fault-diag-20261118` 日志会被明确判为失败，而不是因为缺少最终
`RESULT` 才间接失败。验证器只检查 guest `console.log`，不替代对
`host.log`、VZ 信息和 provenance 散列的核对。

第二轮 KUnit 总计行（含参数化子测试）为 477 pass、0 fail、7 skip。
7 项跳过涉及 soft-dirty 和缺少第二 NUMA 内存节点；两个额外真实 swap
budget suite 在 boot 时尚未 swapon，各跳过 5 项，不能宣称它们通过。
KUnit 注入失败会改变累计 debug counters；判断正常负载错误时必须与
负载 baseline 比较，不能把启动时注入量当作压力测试新错误。

第三轮另外设置 `CYCLES=1 CONC_SECS=3 SWAPOFF_MB=1792
SWAPOFF_PRESS_MB=64 RUN_SELFTEST=1`，仅保留
`SKIP_CHURN=1 SKIP_RETAIN=1 SKIP_UFFD=1 SKIP_COMPAT=1` 四个跳过开关；
`TEST_TIMEOUT=300 HEARTBEAT_S=5`，host `TIMEOUT=360`。
它完成 refault/重写、并发 pageout/refault、munmap 残留检查及带映射
swapoff。swapoff 恢复 20,612 个 memcomp 条目，读取 20,612 次，失败增量
为零，最后 `swapped_entries=0`，映射内容校验通过。

第三轮独立 MM selftest 为 6 pass、2 skip、0 fail。破坏性 selftest 的
QEMU 身份/显式 boot 授权保护在这轮 VZ 命令下未满足，因而其 swapoff
与 swap restoration 子项没有执行；没有移除安全检查或伪装 QEMU。
这与上述 E2E harness 已实际执行的 swapoff 是两个不同的覆盖项。
selftest 的 skip 再计为一类 E2E 跳过，故第三轮合计 5 skips。

这些是早期定向回归，不是全覆盖、性能对比或稳定性认证。上述各轮未跑
的用例不能从这些日志得出结论。后续运行入口见 [README.md](README.md)。
