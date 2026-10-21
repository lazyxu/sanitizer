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

**实现方案与组件。** B 在 IR 层保留 mask/EVL，必要时分元素检查或生成有条件的范围检查；后端负责让检查与真实访存使用相同的有效集合。C/E 从机器指令谓词和目标执行模式恢复同一集合。A 在模型的真正访存回调上检查，不把被取消的地址计算当成访存。D/F 同样必须服从 mask，不能因为加入 bounds/tag 就无条件执行检查。被关闭 lane／元素中的无效或 poison 地址不得驱动额外 metadata 访存、分支或错误报告；本合法反例不对启用元素自身已有 poison／未定义行为作保证，安全的纯地址算术可以保留。

**修复、合法反例与预期报告。** 原例预期零错误、有效访问数为 2；`<=` 变体应只把 i=10 对应的 4 B 访问报告为 OOB。同时验证全零 mask、部分 mask 及 gather/scatter。EVL 合法对照可选“起始下标 8、向量长度 8、元素 mask 全真、EVL=2”，同样只能访问两个元素。此例也检验 race/init 插桩不能为关闭元素制造虚构的读写事件。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 关闭的元素不产生虚构访存 |
| QEMU 已有 GPU ISA 执行模型 | 合法反例；M/R/I 插桩正确性 |
| KMD | lane、predicate、元素 mask、EVL |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S22.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
