# S28：大量GPU越界与日志限额

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S28 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S28 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | 许多GPU线程同时OOB，测试报告聚合；不能从应用证明工具metadata OOM。 |
| AMD HIP | AMD GPU AddressSanitizer | `conditional` | 许多GPU线程同时OOB，测试报告聚合；不能从应用证明工具metadata OOM。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


**当前实验子范围限制：** 目前只模拟大量GPU访问违规，尚未注入Sanitizer内部metadata OOM与日志缓冲耗尽。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.28](../../docs/llvm-gpu-sanitizer-research.md#case-s28)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.28 S28：日志满、metadata 不足与错误终止——检查器必须说明检查是否完成

**场景与例子。** 这是工具可靠性场景，而非新增的应用错误类别；它决定所有能力的结果是否可信。p 指向恰好 n 个 `int32_t` 的有效分配，n≥1，尾部已 poison；block 至少两个线程，全部属于 barrier 的参与集合。下例只有线程 0 触发越界，其余线程会执行 barrier；本例选择固定到达计数且不会自动退订退出线程的目标同步原语。若目标会按规定调整参与集合，必须另按其规则验收，不能把所有提前 return 一概视为非法。

~~~cpp
kernel void error_then_barrier(int32_t* p, int n) {
    if (thread_id() == 0) p[n] = 1;  // 原始应用错误
    counted_barrier(expected_arrivals=block_size());
    // 如果检查器只让线程0 return，其余线程可能永远等不到正确参与集合。
}
// 日志故障注入：容量=1且预填未消费记录，再触发本例；或直接注入“已满”。
// 另用至少两个独立诊断事件核对丢失计数。
// shadow分配失败、clock耗尽分别独立运行，不要求同时触发。
~~~

**哪里不对、应检测什么。** sanitizer 发现错误后的动作也是工具语义的一部分。只退出一个参与线程、静默丢掉唯一错误或把 metadata 分配失败当作“所有地址可访问”，都会使结果失真；单纯等待超时也无法说明是应用 barrier 误用还是工具引入死锁。

**实现方案与组件。** Compiler/device runtime 设置统一错误状态，采用目标可保证前进性的报告协议；UMD 与已有 KMD/固件取消能力配合执行受控 dispatch/context abort，并保留完成状态。没有安全恢复但有可靠 abort 时，终止受影响执行；连可靠终止能力也缺失时，应将依赖该能力的模式标为不支持，并在启动前拒绝相应配置，不能承诺任意 lane 局部恢复。A 可注入队列、缓冲和生命周期故障校验状态机；C/E 的补充路径也必须遵守同一错误处理 ABI。报告可聚合重复事件，但应保留“已经检测到错误”的持久标志及记录丢失计数；shadow/clock 不足则标记检查不完整，按配置拒绝启动或安全终止。具体错误动作需要结合目标能力设计。[N02]

**修复、合法反例与预期报告。** 修复应用下标后，在工具资源充足时应正常完成；保持应用合法但注入 metadata 失败时，应报告 `tool-resource-exhausted`，不能伪装成应用 OOB。错误程序即使日志满，也应保留失败状态并报告记录丢失；触发 abort 后确认其他任务的影响范围符合声明。验收分别检查资源耗尽、日志丢失和 barrier 终止，不能只用“进程退出了”作为通过证据。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 错误、覆盖不足与工具失败可区分 |
| QEMU 已有 GPU ISA 执行模型 | 所有 checker 的可靠性 |
| KMD | 丢失计数、资源状态、abort 协议 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S28.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
