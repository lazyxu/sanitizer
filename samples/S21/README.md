# S21：Atomic Global越界

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S21 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S21 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `detect` | GPU原子访存同样有对象边界；ROCm是否插桩由实际目标决定。 |
| AMD HIP | AMD GPU AddressSanitizer | `conditional` | GPU原子访存同样有对象边界；ROCm是否插桩由实际目标决定。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

**本 README 是 S21 的唯一详细技术与验收规范。** [主研究 §18.21](../../docs/llvm-gpu-sanitizer-research.md#case-s21) 仅保留场景摘要与稳定入口。本页伪代码与可编译 CUDA/HIP 程序按**相同违规语义**对齐，不保证逐行一致；真机验收必须确认目标访存确实执行，不能把被优化消除的操作当成已检查。

### 18.21 S21：原子指令访问越界——原子性不保证地址有效

**场景与例子。** 对应能力 M2/M3。`bins` 是已清零的 64 个 `uint32_t`，共 256 B；只有线程 0 执行下列原子操作，因此没有其他线程竞争，也没有未初始化读取干扰。为确定验收真实GPU B/C设备检查器，夹具保证 `[base+256,base+260)` 位于已 poison 的尾部 redzone 或不可访问范围；落入另一存活对象的变体另用于验收 D1／具有对象来源信息的检查。

~~~cpp
// Host：bins = alloc(64 * sizeof(uint32_t)); memset_zero(bins, 256);
kernel void bad_bin(uint32_t* bins) {
    if (thread_id() == 0) {
        atomic_add_u32(&bins[64], 1);  // 错误：有效下标只有 0..63
    }
}
~~~

**哪里不对、应检测什么。** 原子操作触及 `[base+256, base+260)`，不在分配范围内；“多个线程更新不会丢失”与“地址合法”是不同条件。检查必须覆盖 atomic RMW、CAS 及目标特殊原子，而不能只匹配普通 load/store。CAS 即使比较失败，也执行了读取；其范围仍须检查，是否产生写事件则需按实际结果处理。访问宽度、对齐和作用域依据目标指令定义。[L05][N09]


**修复、合法反例与预期报告。** 将下标限制在 `[0,64)`；多个线程合法执行 `atomic_add_u32(&bins[0],1)`，在该原子作用域覆盖这些线程且无混合非原子访问的前提下，不应报本例的空间错误或把同类原子冲突直接报成数据竞争。错误报告应为 `atomic out-of-bounds`，给出 4 B、偏移 256、对象大小 256 和 PC；仅报告“发生竞争”没有指出本例根因。

**从原主报告归并的补充边界：**

**实现方案与组件。** B对真实GPU的LLVM atomic/目标intrinsic插入完整范围检查，设备运行库查询shadow；UMD提供真实逻辑对象范围。C/E在机器码atomic前插入真实GPU检查指令并保留返回值、谓词和scope/order。D1若保存原对象bounds还可检测地址落入另一live对象。 所有插桩都不得把应用原子拆成非原子读写。

## 实体GPU生产组件职责

**产品检测必须由实体GPU执行对应设备Checker或真实硬件机制，正式验收需要实卡设备报告。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | 编译/运行时GPU检查ISA、GPU设备运行库、或受支持硬件MMU/Tag/Bounds；必须真机执行及报告 |
| KMD（生产） | 原子故障及映射；不能用原子指令本身证明地址正确；原接口满足契约时不新增代码 |

## 验收需要留下什么证据？

在目标实体GPU运行错误与合法对照；保存硬件型号、驱动/Firmware、Compiler、最终目标ISA、设备原始PC与原始错误日志。发生在UMD/Host allocator API的错误仅能归类`HOST_API_DETECTED`；只有GPU内部实际执行并产生设备检测证据才能标记`GPU_DETECTED`。`GPU_NOT_RUN`、`UNSUPPORTED`、`PARTIAL_COVERAGE`、`TOOL_FAILURE`必须分别保留。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
