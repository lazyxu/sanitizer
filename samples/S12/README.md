# S12：GPU显存泄漏

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S12 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S12 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | NVIDIA --leak-check=full可报告未释放GPU内存；ROCm GPU ASan不是相同设备漏检工具。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | NVIDIA --leak-check=full可报告未释放GPU内存；ROCm GPU ASan不是相同设备漏检工具。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.12](../../docs/llvm-gpu-sanitizer-research.md#case-s12)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.12 S12：在约定检查点仍未释放的显存对象

**前提与错误代码。** 一个独立检查 scope 进行三次 256 B 拥有型分配；每次 kernel 只在 thread 0 写该对象前 4 B，且等待完成。scope 约定结束时无应保留业务对象，只有 `k==1` 的分配没有释放，也没有把指针交给其他拥有者：

~~~cpp
begin_checked_scope(context);
for (int k = 0; k < 3; ++k) {
    uint8_t *p = owner_alloc(context, 256);
    touch_first_u32<<<1,32>>>(p);  // 合法写 [p,p+4)
    wait_all();
    if (k != 1) owner_free_and_wait(context, p);
    // 错误：k==1 时丢失最后一个业务引用，256 B 仍为 live。
}
report_leaks_before_context_teardown(context);
~~~

**哪里不对、怎样检。** 错误是拥有型对象在约定检查点仍未释放，不要求GPU非法访存。**真实UMD/Host runtime的allocation账本**在检查点扫描尚未成功free的对象，并由工具报告；它属于生产Host资源诊断，并不等于GPU内部访存检测。仅“账本未释放”不能在任意业务中自动证明对象已不可达；本例提供明确scope/引用丢失前提。[N02][L01]

**组件观测。** compiler／loader 提供分配调用栈与模块定位；deviceRT 补充设备堆分配、失败结果及释放事件；UMD 等待相关命令完成，在 context 自动回收前取快照。工具自己的 shadow、quarantine 和报告缓冲须与应用账本分开。

**合法反例、修复及报告。** 无条件释放每次 `p` 即可；若确实要跨 scope 保留，则把拥有权显式移交并按约定报告。合法 allocator 缓存、仍由调用者持有的对象及非拥有导入映射，不能一律报成应用泄漏。预期为 1 个、256 B 未释放拥有型对象，指出 `k==1` 对应分配位置与检查点。异常退出前未形成快照、漏接设备堆事件或只观察驱动大 pool，都会使该结论不完整。

## 实体GPU生产组件职责

**只能由实体GPU实际执行设备检查器或已有硬件机制提供产品检测，实卡证据是验收依据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成并使GPU执行设备检查ISA；或实体GPU硬件检查单元/MMU触发，附真实设备报告 |
| KMD（生产） | 实际BO/context清理；OS自动回收不抹去程序检查点的逻辑泄漏；已充分支持的接口不需改源码 |

## 验收需要留下什么证据？

在实体GPU运行`--good`/`--bad`并保存硬件/驱动/编译器/模块build ID、真实设备ISA、PC/lane/宽度/对象/queue及原始报告。Host API资源/释放错误只能标记`HOST_API_DETECTED`，不能冒充设备内`GPU_DETECTED`。未运行记`GPU_NOT_RUN`，硬件能力不支持记`UNSUPPORTED`；部分覆盖、日志丢失或工具OOM必须标为`PARTIAL_COVERAGE`/`TOOL_FAILURE`而不是clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
