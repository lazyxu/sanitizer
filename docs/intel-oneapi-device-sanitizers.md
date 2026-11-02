# Intel oneAPI 设备端 Sanitizers：成熟实现、运行链路与支持边界

> **定位：B2类Compiler + Shadow/Runtime产品参考；不增加第四种基础空间参考配置。**
> 资料核验：2026-11-02；详细能力依据Intel oneAPI DPC++/C++ **2026.2**官方指南（页面日期2026-10-15）。[发布说明](https://www.intel.com/content/www/us/en/developer/articles/release-notes/oneapi-dpcpp/2026.html)确认2026.2.0于2026-10-22发布。此前2026.0证据保留在档案中；文档能力不等于本仓库实卡测试结果。

项目三种核心参考配置的唯一分类仍见 [核心方案](core-design-comparison.md)；自研 GPU 组件设计见 [真实设备组件实施](real-gpu-components-implementation.md)。本页仅记录 Intel 的成熟产品能力、构建与运行入口、已公开限制及对自研 GPU 的工程启示。

**厂商证据唯一登记：** [INTEL-DEVICE-2026-2 / INTEL-LIMITS-2026-2 / INTEL-UR及Profile](research-notes/vendors_sources.json)。2026.2能力表、Runtime命令与平台限制必须合起来阅读；UR规范不能单独证明所有Adapter均已支持。

## 1. 它是什么：Intel GPU，并非新的 Sanitizer 算法分类

Intel针对**SYCL**与**OpenMP C/C++ Offload**提供设备ASan、MSan、TSan：`icpx`用`-Xarch_device -fsanitize=...`生成设备插桩，SYCL/OpenMP Runtime协同**Unified Runtime (UR)**及受支持的**Level Zero**等后端，把设备诊断送回Host。[2026.2设备Sanitizer指南](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/device-side-compiler-sanitizers.html)。这套编译期产品没有为任意已有二进制提供NV-R式检查的公开承诺。

[UR规范](https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers)明确ASan Runtime层依赖后端Virtual Memory API及设备库Shadow Mapping。这足以把它作为**B2类产品实现参照**，但不能推定它与AMD每个地址空间的编码、GPUVA布局或XNACK/HMM条件相同；自研通用组件分工仍只在[组件指南](real-gpu-components-implementation.md)定义。

## 2. AddressSanitizer（ASan）：内存可访问性和资源状态

下表摘取[2026.2 Device-Side AddressSanitizer能力表](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/device-side-compiler-sanitizers.html)的支持范围；“支持”同时受§5的平台、并发与跳过检查限制约束：

| 检查项目 | OpenMP C/C++ | SYCL | 自研 GPU 参考价值 |
|---|---|---|---|
| Device Global 越界、Global USM 越界 | 支持 | 支持 | GPU Global 地址可访问性，与 AMD-B2 属于同类检查目标 |
| Local、Private 越界 | 支持（有规模限制） | 支持（有规模限制） | Address Space 分类与独立的对象布局及 Shadow/Bounds 逻辑 |
| Buffer 对象越界 | 不适用 | 支持 | 在 API/资源语义足够时可检查 Buffer 边界 |
| Use-after-free | 支持 | 支持 | 分配/释放生命周期登记与元数据同步 |
| 非对齐、NULL、无效参数、内存泄漏 | 支持 | 支持 | 分离设备指令、Runtime API 与 Leak 检查结论 |
| 多错误报告、内存开销统计 | 支持 | 支持 | Report Buffer、溢出统计及用户可读诊断 |
| bad-context、bad-free、double-free | 不适用 | 支持 | **SYCL 特定的** Context/资源释放检查，不能照搬到 OpenMP 或裸指针检测 |
| Kernel Filter | 不支持 | 支持 | 覆盖范围必须记录实际启用的 Kernel 集合 |

**保证边界：** 上表支持UAF并不意味着地址复用后还能识别旧对象身份；公开资料未建立S04的Original-Object Bounds或S09的Generation保证。因此它为B2提供产品工程参考，不能被用作D1/D2无条件成立的证据。Buffer/Context/Free检查还包含Runtime资源语义，必须与设备访存报告分开归因。

### 官方构建与运行入口

~~~bash
# SYCL ASan：Intel 2026.2官方示例；demo.cpp需为相应SYCL程序
icpx -fsycl -Xarch_device -fsanitize=address -g -O2 -o demo demo.cpp
ONEAPI_DEVICE_SELECTOR=level_zero:gpu ZE_AFFINITY_MASK=0 ./demo

# OpenMP C/C++ ASan：注意选择的是不同的运行时入口
icpx -fiopenmp -fopenmp-targets=spir64 -Xarch_device -fsanitize=address -g -O2 -o demo demo.cpp
LIBOMPTARGET_PLUGIN=unified_runtime UR_ENABLE_LAYERS=UR_LAYER_ASAN ZE_AFFINITY_MASK=0 ./demo
~~~

**不要混用设备选择器：** [Intel官方指南](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/device-side-compiler-sanitizers.html)指出，OpenMP GPU Offload不能照抄SYCL的`ONEAPI_DEVICE_SELECTOR=level_zero:gpu`。上面两个编译命令应各用对应语言的`demo.cpp`，不是把同一源码自动转成另一运行时；命令尚未在本仓库执行。

**实际覆盖配置：** Local/Private检测分别有编译期和Runtime开关；实验必须记录实际启用值及是否被跳过。[官方参数表](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/device-side-compiler-sanitizers.html)。这类开关决定本次检查范围，不增加D1原对象边界或D2身份保证。

**UR资料的适用范围：** 通用[UR规范](https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers)要求由语言Runtime自动启用Sanitizer Layer，而Intel 2026.2的OpenMP示例仍显式设置`UR_ENABLE_LAYERS`。这里保留产品版本的操作命令，不将两套说明拼接成适用于所有UR版本/Adapter的统一脚本。

## 3. MemorySanitizer（MSan）：未初始化值的使用

据[2026.2设备指南](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/device-side-compiler-sanitizers.html)，MSan自**2025.1**支持SYCL/OpenMP设备代码，以设备USM为重点；**2025.2**扩展Local/Private检测，但增加运行开销。

~~~bash
# SYCL MSan
icpx -fsycl -Xarch_device -fsanitize=memory -g -o demo demo.cpp
ONEAPI_DEVICE_SELECTOR=level_zero:gpu ZE_AFFINITY_MASK=0 ./demo

# OpenMP C/C++ MSan
icpx -fiopenmp -fopenmp-targets=spir64 -Xarch_device -fsanitize=memory -g -o demo demo.cpp
LIBOMPTARGET_PLUGIN=unified_runtime UR_ENABLE_LAYERS=UR_LAYER_MSAN ZE_AFFINITY_MASK=0 ./demo
~~~

公开选项包括 `-mllvm -msan-spir-locals=0|1`、`-mllvm -msan-spir-privates=0|1` 和 `UR_LAYER_MSAN_OPTIONS=recover:true`。ASan 的地址有效性 Shadow 与 MSan 的**值定义性/初始化状态**不是同一个判定；本项目 S13 的已写字节表只能视为受限 Initcheck，不应直接宣传为完整 MSan 的值级传播。

## 4. ThreadSanitizer（TSan）：设备数据竞争

据[2026.2设备指南](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/device-side-compiler-sanitizers.html)，TSan从**2025.2**支持SYCL/OpenMP，公开介绍重点是**设备USM上的数据竞争**。

~~~bash
# SYCL TSan
icpx -fsycl -Xarch_device -fsanitize=thread -g -o demo demo.cpp
ONEAPI_DEVICE_SELECTOR=level_zero:gpu ZE_AFFINITY_MASK=0 ./demo

# OpenMP C/C++ TSan
icpx -fiopenmp -fopenmp-targets=spir64 -Xarch_device -fsanitize=thread -g -o demo demo.cpp
LIBOMPTARGET_PLUGIN=unified_runtime UR_ENABLE_LAYERS=UR_LAYER_TSAN ZE_AFFINITY_MASK=0 ./demo
~~~

TSan 不等同于 NVIDIA `racecheck` 的 Shared Memory Hazard 检查，更不等同于 `synccheck` 的 Barrier / Warp 参与集合验证。不能从 TSan 有 device USM 支持，推出任意 Shared/Local、跨 Kernel、跨 Queue 场景都已具有完整检测保证。

## 5. GPU 支持配置和官方已知限制（不能省略）

以下均来自[oneAPI DPC++/C++ 2026.2 Compiler Sanitizers系统要求与限制](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/compiler-sanitizers.html)：

1. **平台范围：** Linux；该文档明确列举 **一张 Intel Ponte Vecchio GPU** 的 GPU 配置，并使用 `ZE_AFFINITY_MASK=0`。不能推定所有 Intel Arc、Xe 或多卡集群都获得相同支持。
2. **执行模型改变：** Device ASan 启用时，Kernel 并发会被强制串行化。不能用开启 ASan 后的运行结果，证明生产态多 Kernel 竞态已覆盖或性能无损。
3. **私有内存压力：** Device ASan 可能增加 Private Memory 使用量、降低可用的最大 Workgroup Size，触发 `UR_RESULT_ERROR_INVALID_WORK_GROUP_SIZE`。
4. **可能主动跳过部分检查：** GPU Workgroup 数量很大时，ASan 的 Local/Private 越界检查和 MSan 的 Local/Private 未初始化检查**可能被跳过**。因此退出码正常、无报告并不等于这些访问均经过检查。
5. **OpenMP执行范围：** 该章限定OpenMP C/C++设备Sanitizer在GPU执行；SYCL示例中的CPU选择器不能拿来证明OpenMP GPU检查已生效。

独立[UR规范](https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers)仍把MSan/TSan层标为*planned*，而2026.2产品指南已有运行示例。这里能确认的是各自文档口径，不能推出所有UR Backend都支持，亦不能反向否定Intel版本化产品示例。

对上述限制，实验结果应同时记录 **Compiler/Runtime 版本、GPU 型号、Backend、Selector、实际并发模式、被跳过的访问、Report Buffer 完整性和实际 GPU 设备日志**。文档中的 Supported 不等于本项目已经实机验证。

## 6. 与 NVIDIA、AMD 三核心方案的准确关系

| 对比维度 | Intel oneAPI 公开实现 | 本项目映射 |
|---|---|---|
| 空间 OOB | 设备编译期插桩 + Runtime/资源元数据协作 | **B2 类成熟参考**；有利于研究 USM、Local/Private、Buffer |
| 编译不可得的 Binary | 这套文档没有提供通用 NVIDIA NV-R 等价的任意已有二进制插桩保证 | 仍以 NVIDIA NV-R 为独立参考配置 |
| 原对象越过 A 落入存活 B | Intel 支持表不足以承诺 Original-Object Provenance | D1 仍主要参考 NVIDIA NV-C |
| 值初始化与竞争 | MSan、TSan 有独立设备功能与条件 | 独立于三种**基础空间**检查配置，不重复计数 |
| Runtime 分工 | SYCL / OpenMP + UR + Level Zero/Adapter | 借鉴分配、Context、Queue、设备元数据/诊断契约，而不是照搬自研 KMD |

## 7. 对自研 GPU 最有价值的工程借鉴

按重要性排序：**① 首先把编译期插桩与 UMD/Device Runtime 元数据发布作为可独立验收的契约；② 为 Global、Local/Private、USM/Buffer 等地址来源标明支持范围；③ 以实际最终 ISA 和已执行 Kernel 证明检查覆盖，不以编译成功代替；④ Runtime 对错误资源参数、内存状态及报告容量负责；⑤ 分别建设 ASan、MSan、TSan 的独立语义与回归用例；⑥ 在日志中标出强制串行和跳过检查，防止错误的检测率/性能结论。**

本仓库对应测试主要包括：ASan 的 S01/S02/S06/S07/S08/S12，MSan 的 S13，以及 TSan 参考的 S15/S16（**只作类别对照，非逐例保证检出**）。S04/S09 仍需要 D1/D2；S17/S18 不应借 TSan 冒充同步原语检查。

## 8. 官方来源（逐版本引用）

- [I01 Intel oneAPI DPC++/C++ 2026.2 — Device-Side Compiler Sanitizers](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/device-side-compiler-sanitizers.html)：ASan/MSan/TSan能力、参数及SYCL/OpenMP命令。
- [I02 Intel oneAPI DPC++/C++ 2026.2 — Compiler Sanitizers](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/compiler-sanitizers.html)：Linux/Ponte Vecchio单卡、ASan串行化和Local/Private跳过检查。
- [I03 oneAPI Unified Runtime — Sanitizers](https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers)：后端及Shadow Mapping依赖，独立于Intel产品版本维护。
- [I04 Intel 2026发布说明](https://www.intel.com/content/www/us/en/developer/articles/release-notes/oneapi-dpcpp/2026.html)：2026.2.0发布时间。
- [证据登记](research-notes/vendors_sources.json)保留2026.0历史来源，并登记2026.2来源及核验日期；[历史核验笔记](research-notes/vendors.md)仅作索引。

**状态：文档核验完成；未对 Intel 真机运行 `icpx`/设备 Sanitizer；不增加新的基础检测方案，也不改变自研 GPU 当前 `GPU_NOT_RUN` 状态。**
