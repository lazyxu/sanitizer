# Intel oneAPI 设备端 Sanitizers：成熟实现、运行链路与支持边界

> **定位：成熟厂商的设备端 Compiler + Runtime 体系参考，不是第四种基础 GPU 空间越界算法。**
> 资料核验：2026-10-23；主要依据 Intel oneAPI DPC++/C++ **2026.0** 官方文档（页面日期 2026-04-28）。文档能力是厂商声明，不等于本仓库的 Intel GPU 实卡测试结果。

项目三种核心参考配置的唯一分类仍见 [核心方案](core-design-comparison.md)；自研 GPU 组件设计见 [真实设备组件实施](real-gpu-components-implementation.md)。本页仅记录 Intel 的成熟产品能力、构建与运行入口、已公开限制及对自研 GPU 的工程启示。

## 1. 它是什么：Intel GPU，并非新的 Sanitizer 算法分类

Intel oneAPI DPC++/C++ Compiler 提供面向 **SYCL** 与 **OpenMP C/C++ Offload** 的设备侧 ASan、MSan、TSan。软件层包括 `icpx` / LLVM 编译期设备插桩、SYCL/OpenMP Runtime、**Unified Runtime (UR)** Sanitizer 支撑以及 Intel **Level Zero** 等设备后端。它既不是一块特殊的 Sanitizer GPU，也不能因支持 ASan/MSan/TSan 就认为其内部实现了 NVIDIA 的私有 Binary Rewriter。

~~~text
SYCL / OpenMP C/C++
   ↓ Intel icpx 编译、-Xarch_device -fsanitize=...
设备侧检查逻辑 + 目标设备程序（相关示例采用 spir64）
   ↓
SYCL / OpenMP Runtime → Unified Runtime / Sanitizer Layer
   ↔ 分配对象、USM / Buffer、Context、队列、Device Metadata
   ↓ Level Zero 等受支持的 Backend
真实 Intel GPU 运行 → 设备诊断 → Host 报告
~~~

**能确认的机制边界：** UR 官方规范公开说明了 ASan 运行时层以及 Shadow Mapping 与 Virtual Memory API 的后端依赖；这为设备端 Compiler/Runtime 分工提供了依据。**不能**由此推断 Intel 设备 ASan 在所有地址空间采用与 AMD 完全相同的 1:8 编码、完全相同的 GPUVA 结构，或同一种 XNACK/HMM 运行条件。

## 2. AddressSanitizer（ASan）：内存可访问性和资源状态

Intel oneAPI 2026.0 的 *Device-Side AddressSanitizer* 功能表区分了 OpenMP 与 SYCL：

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

**元数据策略：** 编译期为设备访存插入检查；设备运行时和 Runtime 需要正确关联合法分配、标记不可访问区域并在访问前查询相应元数据。UR 规范对 Shadow Memory 映射和后端虚拟内存接口提出限制。这与自研方案 **B2** 的大方向相近，**但不足以证明 Intel 与 AMD 的内部 Shadow 算法逐项相同**。Intel ASan 本身不自动解决原对象 A 派生指针完全落入仍存活对象 B 的情形（本仓库 S04 的 D1 目标）。

### 官方构建与运行入口

~~~bash
# SYCL ASan：Intel 2026.0 官方示例
icpx -fsycl -Xarch_device -fsanitize=address -g -O2 -o demo demo.cpp
ONEAPI_DEVICE_SELECTOR=level_zero:gpu ZE_AFFINITY_MASK=0 ./demo

# OpenMP C/C++ ASan：注意选择的是不同的运行时入口
icpx -fiopenmp -fopenmp-targets=spir64 -Xarch_device -fsanitize=address -g -O2 -o demo demo.cpp
LIBOMPTARGET_PLUGIN=unified_runtime UR_ENABLE_LAYERS=UR_LAYER_ASAN ZE_AFFINITY_MASK=0 ./demo
~~~

**不要混用设备选择器：** Intel 官方特别指出，OpenMP GPU Offload 不能照抄 SYCL 的 `ONEAPI_DEVICE_SELECTOR=level_zero:gpu`，否则可能报错。SYCL 与 OpenMP 的环境变量以各自的版本化指南为准。

Intel 文档公开的 ASan 运行参数还包括：`UR_LAYER_ASAN_OPTIONS=redzone:32`、`detect_locals`、`detect_privates`、`detect_leaks`、`detect_kernel_arguments`、`print_stats`、`halt_on_error`。这些参数体现了保护区大小、设备地址空间检查、设备参数校验与诊断报告的运行时责任；不能当成自研 GPU 自动支持的选项。

## 3. MemorySanitizer（MSan）：未初始化值的使用

Intel 设备 MSan 自 **2025.1** 支持 SYCL/OpenMP 设备代码，重点是设备 USM 的未初始化使用；**2025.2** 扩展 Local/Private 检测，但会增加运行时开销。

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

Intel 设备 TSan 在 oneAPI **2025.2** 引入面向 SYCL 与 OpenMP 的支持，官方介绍重点是**设备 USM 上的数据竞争**。

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

以 **oneAPI DPC++/C++ 2026.0** 的 *Compiler Sanitizers* 总章为准：

1. **平台范围：** Linux；该文档明确列举 **一张 Intel Ponte Vecchio GPU** 的 GPU 配置，并使用 `ZE_AFFINITY_MASK=0`。不能推定所有 Intel Arc、Xe 或多卡集群都获得相同支持。
2. **执行模型改变：** Device ASan 启用时，Kernel 并发会被强制串行化。不能用开启 ASan 后的运行结果，证明生产态多 Kernel 竞态已覆盖或性能无损。
3. **私有内存压力：** Device ASan 可能增加 Private Memory 使用量、降低可用的最大 Workgroup Size，触发 `UR_RESULT_ERROR_INVALID_WORK_GROUP_SIZE`。
4. **可能主动跳过部分检查：** GPU Workgroup 数量很大时，ASan 的 Local/Private 越界检查和 MSan 的 Local/Private 未初始化检查**可能被跳过**。因此退出码正常、无报告并不等于这些访问均经过检查。
5. **运行时版本差异：** 当前 UR 通用规范在 Sanitizers 章节仍把 MSan/TSan 层标成 *planned*，但 Intel 2026.0 编译器文档列出了 MSan/TSan 的可执行示例。这反映不同文档和集成路径的进度口径，不能简单宣布所有 UR Backend 都已支持相同的设备 Sanitizer。

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

- [I01 Intel oneAPI DPC++/C++ 2026.0 — Device-Side Compiler Sanitizers](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/device-side-compiler-sanitizers.html)：ASan/MSan/TSan 能力表、编译选项、SYCL/OpenMP 运行示例。
- [I02 Intel oneAPI DPC++/C++ 2026.0 — Compiler Sanitizers](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/compiler-sanitizers.html)：Linux/Ponte Vecchio 单卡前提、串行化及 Private/Local 检查跳过限制。
- [I03 oneAPI Unified Runtime — Introduction / Sanitizers](https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers)：UR 设备 Sanitizer Runtime 层、后端和 Shadow Mapping 依赖。该页面属独立维护的规范，状态不能机械等同于 C/C++ 2026.0 编译器产品文档。
- [证据登记](research-notes/vendors_sources.json)（`INTEL-DEVICE`、`INTEL-LIMITS`，以及新增的 UR 来源）；[厂商历史核验笔记](research-notes/vendors.md)。

**状态：文档核验完成；未对 Intel 真机运行 `icpx`/设备 Sanitizer；不增加新的基础检测方案，也不改变自研 GPU 当前 `GPU_NOT_RUN` 状态。**
