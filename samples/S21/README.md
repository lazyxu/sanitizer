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

以下章节直接对应[主调研文档 §18.21](../../docs/llvm-gpu-sanitizer-research.md#case-s21)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.21 S21：原子指令访问越界——原子性不保证地址有效

**场景与例子。** 对应能力 M2/M3。`bins` 是已清零的 64 个 `uint32_t`，共 256 B；只有线程 0 执行下列原子操作，因此没有其他线程竞争，也没有未初始化读取干扰。为确定验收 B/C/A0，夹具保证 `[base+256,base+260)` 位于已 poison 的尾部 redzone 或不可访问范围；落入另一存活对象的变体另用于验收 D1／具有对象来源信息的检查。

~~~cpp
// Host：bins = alloc(64 * sizeof(uint32_t)); memset_zero(bins, 256);
kernel void bad_bin(uint32_t* bins) {
    if (thread_id() == 0) {
        atomic_add_u32(&bins[64], 1);  // 错误：有效下标只有 0..63
    }
}
~~~

**哪里不对、应检测什么。** 原子操作触及 `[base+256, base+260)`，不在分配范围内；“多个线程更新不会丢失”与“地址合法”是不同条件。检查必须覆盖 atomic RMW、CAS 及目标特殊原子，而不能只匹配普通 load/store。CAS 即使比较失败，也执行了读取；其范围仍须检查，是否产生写事件则需按实际结果处理。访问宽度、对齐和作用域依据目标指令定义。[L05][N09]

**实现方案与组件。** B 在 LLVM atomic 指令或目标 intrinsic 前执行完整范围检查，device runtime 查询 shadow；UMD 提供真实 allocation 范围。C/E 在解码后的原子指令前补检查，必须正确保存返回值、谓词和内存顺序。A 在模型的原子语义入口检查同一范围。D1 可进一步用原对象 bounds 识别原子地址落入另一存活对象的情况；硬件 fault/F 可提供补充，但页内越界不一定触发映射故障。所有路线均应保持应用原子的 scope/order，不得通过拆成非原子读写来插桩。

**修复、合法反例与预期报告。** 将下标限制在 `[0,64)`；多个线程合法执行 `atomic_add_u32(&bins[0],1)`，在该原子作用域覆盖这些线程且无混合非原子访问的前提下，不应报本例的空间错误或把同类原子冲突直接报成数据竞争。错误报告应为 `atomic out-of-bounds`，给出 4 B、偏移 256、对象大小 256 和 PC；仅报告“发生竞争”没有指出本例根因。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 原子性不能替代空间合法性 |
| QEMU 已有 GPU ISA 执行模型 | M2/M3 |
| KMD | 原子实际宽度和范围 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S21.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
