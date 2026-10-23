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


**当前实验子范围限制：** 当前仅提供大量GPU违规访问的测试源码；尚未实卡执行，也没有注入Sanitizer内部metadata OOM与日志缓冲耗尽。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

**本 README 是 S28 的唯一详细技术与验收规范。** [主研究 §18.28](../../docs/llvm-gpu-sanitizer-research.md#case-s28) 仅保留场景摘要与稳定入口。本页伪代码与可编译 CUDA/HIP 程序按**相同违规语义**对齐，不保证逐行一致；真机验收必须确认目标访存确实执行，不能把被优化消除的操作当成已检查。

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


**修复、合法反例与预期报告。** 修复应用下标后，在工具资源充足时应正常完成；保持应用合法但注入 metadata 失败时，应报告 `tool-resource-exhausted`，不能伪装成应用 OOB。错误程序即使日志满，也应保留失败状态并报告记录丢失；触发 abort 后确认其他任务的影响范围符合声明。验收分别检查资源耗尽、日志丢失和 barrier 终止，不能只用“进程退出了”作为通过证据。

**从原主报告归并的补充边界：**

**实现方案与组件。** Compiler/device runtime 设置统一错误状态，采用目标可保证前进性的报告协议；UMD 与已有 KMD/固件取消能力配合执行受控 dispatch/context abort，并保留完成状态。没有安全恢复但有可靠 abort 时，终止受影响执行；连可靠终止能力也缺失时，应将依赖该能力的模式标为不支持，并在启动前拒绝相应配置，不能承诺任意 lane 局部恢复。真实生产路径必须处理队列、报告缓冲与生命周期资源失败；C/E补充路径必须遵守同一错误处理ABI。报告可聚合重复事件，但应保留“已经检测到错误”的持久标志及记录丢失计数；shadow/clock 不足则标记检查不完整，按配置拒绝启动或安全终止。具体错误动作需要结合目标能力设计。[N02]

## 实体GPU生产组件职责

**实体GPU设备Checker或已有硬件机制必须实际产生检测与故障报告。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成可部署GPU ISA设备检查器或实体GPU硬件检查单元生效；真机记录对应错误 |
| KMD（生产） | 可靠的终止范围和完成语义；只有queued-job cancel不够；原有接口足够时零改动 |

## 验收需要留下什么证据？

在目标实体GPU运行错误与合法对照，保存硬件/固件/驱动/Compiler版本、实际ISA、模块build ID、device PC、lane/范围/对象/queue及原始日志。GPU内部真实检查成功记`GPU_DETECTED`；仅UMD/Host API识别的资源错误记`HOST_API_DETECTED`。未运行记`GPU_NOT_RUN`，不支持记`UNSUPPORTED`；未覆盖指令/模块、buffer丢失、tool OOM等记`PARTIAL_COVERAGE`或`TOOL_FAILURE`，不能算clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
