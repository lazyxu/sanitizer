# S15：Global跨线程竞争

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S15 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S15 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `racecheck` | `unsupported` | 全局GPU数据竞争不是NVIDIA racecheck的普遍检测范围。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | 全局GPU数据竞争不是NVIDIA racecheck的普遍检测范围。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.15](../../docs/llvm-gpu-sanitizer-research.md#case-s15)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.15 S15：global同block、跨block竞争与合法atomic

**前提与错误。** `x`是已置0并完成发布的有效global `int`；两次错误测试分别重置并等待，互不叠加。普通`x++`包含读和写，多个线程的访问没有HB，即使最终计数偶尔正确也不合法。跨block原子反例限定同一device和适用的memory synchronization domain。[N08]

~~~c
__global__ void bad_increment(int *x) {
    int old = *x;                        // BAD：另一线程可能并发写
    *x = old + 1;                        // BAD：R/W及W/W冲突
}
reset_to_zero_and_wait(x);
bad_increment<<<1, 2>>>(x);              // 独立用例①：同block
wait_reset_to_zero_and_wait(x);
bad_increment<<<2, 1>>>(x);              // 独立用例②：跨block
__global__ void good_increment(int *x) {
    cuda::atomic_ref<int, cuda::thread_scope_device> a(*x);
    a.fetch_add(1, cuda::memory_order_relaxed); // OK：只要求计数更新原子性
}
wait_reset_to_zero_and_wait(x);
good_increment<<<2, 1>>>(x);              // 独立合法反例：完成后计数应为2
~~~

**实现路线与观测点。** 真实GPU Racecheck使用global规范对象和全grid线程身份维护冲突历史，消费真实同步scope、ordering和barrier事件；block barrier只向同block参与者提供同步。C可在可解码指令处提取load/store/atomic宽度及scope，再经runtime汇总HB；E结合编译器语义、缺失模块补丁和UMD launch事件。两者都必须另建设Racecheck，B的可寻址位和D1/D2对象检查只能确认访问对象，不能判定竞争。[P02][P03]

**修复、报告与边界。** `good_increment`用相同适用scope的原子操作访问同一计数器，不应报race；用block-scope原子跨block通信仍不足。relaxed仅保护该计数器：若用flag发布普通payload，仍须建立合适scope的release/acquire关系，且acquire读到对应发布值。[N08] 报告两端PC/block/thread、对象内字节、atomic属性和未成立的HB。未建模锁、跨domain同步、历史溢出或未插桩模块应降级；概率重复访问未报错不能作无竞争证明。[P08]

另保留普通读与原子写并发的混合访问错误，避免检查器只见到一端为atomic，就把同址的全部访问放行。

## 实体GPU生产组件职责

**只能由实体GPU实际执行设备检查器或已有硬件机制提供产品检测，实卡证据是验收依据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | 编译/运行时GPU检查ISA、GPU设备运行库、或受支持硬件MMU/Tag/Bounds；必须真机执行及报告 |
| KMD（生产） | context/映射与外部同步；不能用每次DMA发生先后代替语言HB；原接口满足契约时不新增代码 |

## 验收需要留下什么证据？

在目标实体GPU运行错误与合法对照；保存硬件型号、驱动/Firmware、Compiler、最终目标ISA、设备原始PC与原始错误日志。发生在UMD/Host allocator API的错误仅能归类`HOST_API_DETECTED`；只有GPU内部实际执行并产生设备检测证据才能标记`GPU_DETECTED`。`GPU_NOT_RUN`、`UNSUPPORTED`、`PARTIAL_COVERAGE`、`TOOL_FAILURE`必须分别保留。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
