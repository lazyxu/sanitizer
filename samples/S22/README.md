# S22：Masked-off地址不能误报

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S22 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S22 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `clean` | 故意构造inactive lane中的非法地址但无设备访问，正确结果应无报告。 |
| AMD HIP | AMD GPU AddressSanitizer | `clean` | 故意构造inactive lane中的非法地址但无设备访问，正确结果应无报告。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.22](../../docs/llvm-gpu-sanitizer-research.md#case-s22)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.22 S22：尾部 mask／EVL——被屏蔽的非法地址不得产生误报

**场景与例子。** 对应 M1–M3 的合法反例及插桩语义验收。分配 10 个 `float`，使用宽度 8 的示意执行组处理第二批元素；基准实例中 8 个逻辑 lane 均活跃，只有边界谓词关闭 i=10…15。下列地址计算采用 ISA 风格整数地址，避免把超出源级数组的指针构造规则混入这个用例。错误变体的尾后 4 B 保持 poison，以验证基础 shadow 的确定检出。

~~~cpp
// out 只有 40 B；本执行组 lane = 0..7，对应 i = 8..15。
i = 8 + lane;
addr = out_base_integer + 4 * i;
active = logical_lane_active && (i < 10);
masked_store_f32(addr, 1.0f, active);  // 合法：实际只有 i=8、9 写内存
// 错误变体：把 i < 10 改成 i <= 10，i=10 就成为真实越界访问。
~~~

**哪里不对、应检测什么。** 原例程序没有空间错误；若检查器对所有 8 个地址无条件访问 shadow 并报警，是检查器引入的错误。LLVM masked memory 的关闭元素不执行对应访存，VP 操作还要结合显式向量长度 EVL；不能用“访问完整向量后 select”替代原语义。[L05] 若一个 GPU lane 内还有向量元素，实际检查集合应同时考虑逻辑 lane、指令谓词、元素 mask 和 `element_index < EVL`，不能把硬件 EXEC 当成全部逻辑条件。

**实现方案与组件。** B在LLVM IR中保留mask/EVL，按真实GPU活动元素插入检查；C/E从目标机器码谓词和执行模式恢复同一集合。D及真正存在的F硬件同样必须服从mask，不能无条件检查被屏蔽元素。**模型只能对照这些真实GPU检查行为**，不能在解释器回调中自作判定并宣布产品通过。被关闭lane的非法地址不应触发额外设备metadata访问或错误报告。

**修复、合法反例与预期报告。** 原例预期零错误、有效访问数为 2；`<=` 变体应只把 i=10 对应的 4 B 访问报告为 OOB。同时验证全零 mask、部分 mask 及 gather/scatter。EVL 合法对照可选“起始下标 8、向量长度 8、元素 mask 全真、EVL=2”，同样只能访问两个元素。此例也检验 race/init 插桩不能为关闭元素制造虚构的读写事件。

## 生产方案与两类模型辅助验证职责

**只有可在真实GPU执行的设备检查/实体硬件检查才能作为本场景Sanitizer；cmodel及QEMU仅验证。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成可部署GPU ISA设备检查器或实体GPU硬件检查单元生效；真机记录对应错误 |
| 独立功能级cmodel（验证） | 只观察实际启用lane/元素；模型提供测试证据，不提供新增硬件检测能力 |
| **现有QEMU GPU ISA解释器（验证）** | 检查与GPU访存mask一致，避免QEMU桥接层强行全向量访问；不把模型自有检查hook计入产品 |
| KMD（生产） | 无需逐元素新接口；通常不拥有向量EVL语义；原有接口足够时零改动 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S22.*.log`日志及`summary.json`。CPU ASan、Host API错误或QEMU模型独有检测都不能冒充真实GPU检查器；GPU硬件fault不代表具备完整逻辑对象边界信息。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
