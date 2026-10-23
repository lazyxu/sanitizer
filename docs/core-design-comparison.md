# GPU Sanitizer 核心设计分类与能力对照（v2.0）

**2026-10-22｜现行口径与唯一分类总表｜状态：设计阶段；自研GPU代码未实施，GPU_NOT_RUN。**

本文是全仓**方案计数、厂商来源与能力声明的规范入口**。详细算法参照[S01厂商指南](S01-vendor-implementation-guide.md)、[主研究](llvm-gpu-sanitizer-research.md)、[AMD无XNACK适配](amd-gpu-asan-hardware-fallback.md)。厂商、检查谓词、插桩方式、集成策略和条件性硬件**不应混在同一维度计数**。

## 1. 唯一结论：2家主要厂商、3种核心参考配置，而不是5种检测算法

| 口径 | 数量 | 解释 |
|---|---:|---|
| 重点成熟厂商 | **2** | NVIDIA、AMD；Intel和摩尔线程作为能力及接口参照，不凭功能表再造算法 |
| **基础空间检查参考配置** | **3** | **① NV-R** 运行时二进制检查当前地址，**② AMD-B2** LLVM Shadow/Redzone，**③ NV-C** LLVM编译期Original-Object Bounds |
| 基础**判定语义** | **2层** | 当前地址的Addressability，与原指针所属对象的Provenance Bounds；**3种参考配置并非3种完全不同的数学谓词** |
| 时间安全扩展 | **D2** | 指针Allocation ID/Epoch的跨存储/ABI一致传播，不属于前三种基本空间检查 |
| 工程交付职责 | **B/C/D/E/F** | **5个工程标签，不是5套互不相关的独立检查算法**：B编译地址检查、C运行时重写、D来源/时间身份、E混合集成、F实体Tag/Bounds硬件（条件性） |
| 不应独立计数 | **B1(I)、软件Tag与条件性F1/F2** | B1是当前Live区间表实现变体；软件Tag归D2，硬件Tag/Bounds只有实硅片才归F1/F2 |

**工程完整性门槛**：所声明支持的每条真实GPU访存执行前的检查，设备可用元数据与allocation生命周期发布，覆盖/未知opcode处理，GPU报告及错误后的安全终止，合法/非法实卡验证。**本项目当前完整自研产品交付数为0**，不能把完整“设计”写成已实现。

## 2. 三种参考配置：参考谁、是否完全相同

| 配置 | 成熟来源和公开证据 | 自研GPU判定链 | 是否与厂商一模一样？ |
|---|---|---|---|
| **① NVIDIA NV-R** | [NVIDIA Compute Sanitizer默认Runtime Binary Instrumentation](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compile-time-patching)、[NVIDIA技术文章](https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/) | 普通Binary → UMD独立Runtime Binary Rewriter插检查stub → GPU检查当前合法Allocation地址范围 → 报告 | **不是**。参考公开插桩原理/能力等级。NVIDIA私有重写器、设备元数据索引布局未完整公开；自研ISA译码、控制流、报告需要自己实现 |
| **② AMD-B2** | [ROCm 7.2.4 GPU ASan](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)、[LLVM ASan](https://clang.llvm.org/docs/AddressSanitizer.html) | LLVM插桩 → GPU按访问宽度读取已发布Shadow与Redzone Poison → 报告 | **基本算法同源，不是整套复制**。目标GPUVA、Shadow布局、Device Runtime、UMD/KMD异步顺序和故障处理需适配 |
| **③ NVIDIA NV-C** | [CUDA 13.1+ Compile-Time Patching Base-and-Bounds](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#base-and-bounds-analysis)、[NVIDIA技术文章](https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/)、[cuCatch论文](https://doi.org/10.1145/3591225) | 编译器识别指针Root并保存Original base/size，经过SSA和ABI传播 → GPU检查**原对象**范围 | **不是**。借鉴fat-pointer/原对象Bounds思想；自研实现SSA、参数/内存/调用ABI、unknown fallback和Device Checker |

**命名约束**：NV-C指**NVIDIA编译期参考配置**；仓库“路线C”指**Runtime Binary Rewriter**，两者不能混为一谈。

官方[Compute Sanitizer说明](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)区分默认运行时与编译期memcheck：后者在指针来源已知时可以发现跨入相邻存活Allocation；分析失效时回落普通Allocation Bounds。**NV-R与AMD-B2在基础空间合法性上同属当前地址Addressability检查等级**，但注入位置、元数据、覆盖及部署不一样。不能断言NVIDIA内部就是一张Live Allocation有序表，也不能说AMD与NV-R在内部实现上完全一样。

**版本限定**：NVIDIA [2026.3 Release Notes](https://docs.nvidia.com/compute-sanitizer/ReleaseNotes/index.html)特别提示：同一模块中编译插桩与未插桩函数混用，不等于未插桩函数会得到Runtime Binary补充；E不得承诺未证明的函数级全覆盖。

## 3. 基础空间能力对比（不是厂商全部Checker矩阵）

✅ 支持范围内能检出；△ 另需相应对象/生命周期/指令支持；— 此机制自身无法保证。必须真实GPU运行，且unknown/资源失败不可误记CLEAN。

| 目标场景 | ① NV-R地址检查 | ② AMD-B2 Shadow | ③ NV-C/D1原对象Bounds |
|---|---|---|---|
| S01 Global越界进入无效区 | ✅ | ✅ | ✅ |
| S02宽访问跨界 | ✅ 要检查整个width | ✅ 跨granule检查 | ✅ 已知原对象base/size |
| S04 A派生指针完全落入**live B** | — | — | ✅ **来源已知时** |
| S05子分配/逻辑view越界 | △ 需子对象元数据 | △ 需子对象poison/view语义 | △ 需shape/stride来源语义 |
| S08 free后尚未复用UAF | △ 依赖free撤销 | △ poison生效时 | △ 另需生命周期判断 |
| S09 相同GPUVA复用后的旧指针 | — | — | — **只有D1 bounds不够** |
| 已有不可重编译Binary | ✅ 支持的ISA/模块 | — | — |
| 需要重编译应用 | 不需要 | 需要 | 需要 |
| 需要LLVM来源分析 | 不需要 | 基础B2不需要 | **需要** |
| Shadow | 不必采用特定布局 | **必需** | 不一定 |
| 无XNACK的**自研目标** | △ GPU设备patch/元数据可行 | △ 预映射设备Shadow可行 | △ 原对象base/limit可传GPU |
| 检测开销 | 目标实卡测量 | 目标实卡测量 | 目标实卡测量 |

**XNACK 与 Shadow 内存策略不是新的算法分类：** XNACK是可恢复GPU缺页后重试访存；无XNACK的自研B2可选择全量物理Shadow、按对象分配且直接映射、按需分配加安全索引。三者在物理显存占用与访问开销间取舍，详细说明和边界在[AMD硬件适配文档第2节](amd-gpu-asan-hardware-fallback.md)，不新增第四种基础空间参考配置。

**决定性反例**：A=[0x1000,0x1040)，B=[0x1040,0x1080)，A派生指针访问0x1040开始的4字节，B仍live。NV-R基础地址检查与AMD-B2可能认为B有效；D1需要携带expected A base/size才能拒绝。相反，一个实际跨越A/B交界的**宽访存**，若基础Checker确实要求访问的完整范围属于同一对象，仍可能被检出，不应误称所有跨界都必漏。

## 4. B1、C、D2、E、F是什么？为什么不能重复计数？

| 标签 | 实际作用 | 来源/保证边界 |
|---|---|---|
| **B1（旧I）** | **编译期**插桩GPU当前Live Allocation区间查询、**无Shadow** | 当前地址有效性等级与NV-R默认路径类似，**只是把查询插在LLVM**，不具有D1 provenance；不能声称NVIDIA内部索引表完全相同 |
| **B2** | AMD/LLVM式设备Shadow/Redzone（**推荐首版**） | XNACK不是自研Shadow算法必要硬件；仍要真实GPU访存检查与预驻留的设备metadata |
| **C** | 运行时重写Binary插设备检查stub，生产Compiler可零改 | 参考NV-R的插桩/交付方式，但**C不是新的地址判定算法**，必须选择B1/B2或受支持的其他checker |
| **D1** | 原对象source/base/size传播 | 参考NV-C和cuCatch，解决S04；未知pointer来源降级 |
| **D2** | Allocation ID/Epoch**跟随指针值**传播（包括store/load/copy/atomic/ABI） | 参考cuCatch、[HWASan Tag设计](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html)，目标身份协议属自研；不能保证任意外部代码全覆盖 |
| **E** | 将B/D编译检查与C部分Binary补充整合为单一产品 | 参考[NVIDIA Hybrid Instrumentation](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#hybrid-instrumentation)；E是**集成与覆盖策略**，不是新增访存检查谓词 |
| **F1 / F2** | 真实GPU硬件Tag / Bounds/Metadata | 参考HWASan、GPUArmor与硬件研究；**只有实际RTL/ISA/固件与芯片具备时才可用**。F1有Tag碰撞/Granule边界，F2若只看当前地址也不会凭空获得D1 |

## 5. Intel与摩尔线程的正确位置

- **Intel oneAPI**：详细机制、支持范围和运行命令见[独立 Intel 技术专题](intel-oneapi-device-sanitizers.md)。[2026.0 Device-Side Compiler Sanitizers](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/device-side-compiler-sanitizers.html)公开GPU端ASan、MSan、TSan与global/local/private/UAF等能力；[系统要求与限制](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/compiler-sanitizers.html)明确部分GPU/运行模式限制，包括设备ASan下强制顺序执行的并发影响。**参考其编译器、Unified Runtime/设备库与功能覆盖**，不凭功能表推导全新第四种基础算法。
- **摩尔线程 MUSA Compute Sanitizer**：[官方用户指南](https://docs.mthreads.com/mooreperf/mooreperf-doc-online/musa_compute_sanitizer/user_guide/)公开global越界、MUSA API错误、需单独开启的显存泄漏。**具体内部Shadow/Runtime patch/来源分析未披露，必须标未知**，参考其对外诊断类型与报告质量，不另增基础算法。
- **Initcheck / Racecheck / Synccheck**：是不同检查目标（定义性、HB/冲突、barrier/mask/phase），**不由任一基础Address Bounds Checker自动提供**。

## 6. 唯一建议实施顺序

1. 公共P＋**AMD-B2**，在真实GPU构建LLVM检查ISA、可读/预驻留Shadow、UMD poison与KMD fence、GPU错误报告，完成S01合法与非法实卡验证。没有XNACK可自研该路径，不等于官方ROCm无需xnack+。
2. 加入**NV-C风格D1 Original-Object Bounds**，专项S04 A→live B，并明确unknown来源与跨模块降级。
3. 需要复用地址后旧指针识别时再做**D2**，覆盖S09/S25及指针身份原子一致性。
4. 确有闭源/JIT缺口再做**NV-R风格C**，以**E**整合模块能力和统一报告。
5. 先有软件真机性能与硬件契约证据，再考虑F1/F2条件性芯片研发。

**验收状态**：本仓库是研究文档、示意代码和28组CUDA/HIP厂商工具对照样例。没有目标GPU Compiler/UMD/KMD产品补丁、实卡报告或真实性能；GPU_NOT_RUN。工程预算保持[v1.8估算](estimates-v1.8.json)历史兼容，但**五个预算包不是五种独立算法**。
