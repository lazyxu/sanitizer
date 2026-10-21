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

**哪里不对、怎样检。** 错误是拥有型对象在约定检查点仍未释放，不要求GPU非法访存。**真实UMD/Host runtime的allocation账本**在检查点扫描尚未成功free的对象，并由工具报告；它属于生产检查器的一部分，不是模型事件重放产品方案。仅“账本未释放”不能在任意业务中自动证明对象已不可达；本例提供明确scope/引用丢失前提。[N02][L01]

**组件观测。** compiler／loader 提供分配调用栈与模块定位；deviceRT 补充设备堆分配、失败结果及释放事件；UMD 等待相关命令完成，在 context 自动回收前取快照；cmodel 以独立实例 ID 对账，不因两次分配恰好同地址而合并。工具自己的 shadow、quarantine 和报告缓冲须与应用账本分开。

**合法反例、修复及报告。** 无条件释放每次 `p` 即可；若确实要跨 scope 保留，则把拥有权显式移交并按约定报告。合法 allocator 缓存、仍由调用者持有的对象及非拥有导入映射，不能一律报成应用泄漏。预期为 1 个、256 B 未释放拥有型对象，指出 `k==1` 对应分配位置与检查点。异常退出前未形成快照、漏接设备堆事件或只观察驱动大 pool，都会使该结论不完整。

## 生产方案与两类模型辅助验证职责

**模型及QEMU仅验证实际GPU设备检查产物/真实硬件机制，不可作为独立Sanitizer产品方案。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成并使GPU执行设备检查ISA；或实体GPU硬件检查单元/MMU触发，附真实设备报告 |
| 独立功能级cmodel（验证） | 明确检查点和逻辑活跃账本；只用于预验证真实checker产物和oracle差分 |
| **现有QEMU GPU ISA解释器（验证）** | 在约定检查点、退出／context自动回收前保存UMD逻辑账本；退出／销毁后另核对内核资源清理；不能用模型独有访存hook代替真实设备检查 |
| KMD（生产） | 实际BO/context清理；OS自动回收不抹去程序检查点的逻辑泄漏；已充分支持的接口不需改源码 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S12.*.log`日志及`summary.json`。CPU ASan、Host API错误或模型独有hook不能冒充真实GPU设备检测；GPU MMU fault限于页级证据。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
