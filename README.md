# LLVM GPU Sanitizer 技术调研

本仓库保存面向自研GPU和LLVM后端的sanitizer技术调研与架构实施建议。生产软件按**编译器、KMD（Kernel-Mode Driver，内核态驱动）、UMD（User-Mode Driver，用户态驱动）**分工；功能级cmodel与基于QEMU的全系统虚拟平台分别分析。

**当前版本1.3（2026-10-21）：用户确认QEMU全系统平台已具有目标GPU ISA执行模型；厂商/LLVM机制深挖及所有路线的来源／融合／原创工作已补齐。设计文稿已更新，实际Sanitizer实现及用户平台验证尚未进行。**

## 阅读入口

- [正式调研与实施方案](docs/llvm-gpu-sanitizer-research.md)：23章、40项参考来源，包含详细背景与目标、六条技术路线、组件契约、工程预算和28个检测场景。
- [项目背景](docs/llvm-gpu-sanitizer-research.md#background)与[建设目标](docs/llvm-gpu-sanitizer-research.md#goals)：建设动因、现有调试盲区、使用者、功能／质量目标、验收证据和优先级。
- [厂商/LLVM详细机制](docs/llvm-gpu-sanitizer-research.md#vendor-deep-dive)与[各方案来源／融合／原创对照](docs/llvm-gpu-sanitizer-research.md#route-provenance-matrix)：NVIDIA runtime/compile-time、AMD GPU ASan、Intel UR、MUSA公开能力、LLVM shadow/tag与cuCatch/HiRace/GPUArmor等研究。
- [三组件支持契约](docs/llvm-gpu-sanitizer-research.md#component-contracts)：编译器、KMD、UMD分别生产哪些信息、依赖什么接口，以及现有支持与新增修改的区别。
- [功能级cmodel／QEMU能力边界](docs/llvm-gpu-sanitizer-research.md#model-platforms)与[全系统接入](docs/llvm-gpu-sanitizer-research.md#qemu-integration)：区分GPU执行、模型检查、Guest驱动路径和真机直通。
- 逐路线双平台分析：[A cmodel](docs/llvm-gpu-sanitizer-research.md#route-a-platforms)、[B shadow](docs/llvm-gpu-sanitizer-research.md#route-b-platforms)、[C binary](docs/llvm-gpu-sanitizer-research.md#route-c-platforms)、[D bounds／identity](docs/llvm-gpu-sanitizer-research.md#route-d-platforms)、[E hybrid](docs/llvm-gpu-sanitizer-research.md#route-e-platforms)、[F hardware](docs/llvm-gpu-sanitizer-research.md#route-f-platforms)。各路线正文也有三组件支持与修改表。
- [工程量估算](docs/llvm-gpu-sanitizer-research.md#component-estimates)与[本项目QEMU已确认事实和预算数据](docs/estimates-v1.3.json)：核心／手写测试分开；三组件、cmodel、条件KMD缺口及QEMU接入分别计算。
- [S01七条具体机制与平台落地](docs/llvm-gpu-sanitizer-research.md#case-s01)：模型oracle、区间表、Shadow、二进制patch、原对象bounds、hybrid、硬件辅助。
- [检测场景与实例分析](docs/llvm-gpu-sanitizer-research.md#detection-scenarios)：S01～S28逐例说明错误位置、检测方案、组件配合、修复／合法对照、报告与限制；[逐场景KMD和平台对应表](docs/llvm-gpu-sanitizer-research.md#scenario-platforms)补充每例的执行与观测条件。
- [验证基准与验收矩阵](docs/llvm-gpu-sanitizer-research.md#validation)、[研究进度](docs/research-progress.md)、[修订记录](docs/revision-history.md)、[文稿验证记录](docs/validation-report.json)。
- [归档研究笔记](docs/research-notes/)：架构、论文及友商资料的过程记录与来源链接。

## 1.3版：现有QEMU GPU ISA执行模型＋厂商机制溯源

**确认的项目事实：QEMU全系统虚拟平台已具备目标GPU ISA执行模型（Q2）。** 不再将“新建QEMU GPU ISA解释器”列为开发工作；直接利用已有模型在实际GPU load/store之前增加检查钩子，并打通Guest UMD/KMD逻辑对象、Shadow与诊断回传。还未知的部分（模型是否与独立cmodel共用、具体访存opcode/Guest桥/Shadow已有程度）必须源码审计，不能臆测成已完成或未完成。

第4章深入比较NVIDIA memcheck的运行时/编译期、base-and-bounds及hybrid，AMD GPU ASan的Shadow/Redzone/device runtime，Intel oneAPI与Unified Runtime，MUSA公开产品能力，以及LLVM源码/HWASan；各机制附官方直达链接和限长原文摘录，明确已知事实与未公开实现的区别。

A～F六路线与S01变体现在逐一标注**厂商/研究来源、融合的算法与自研GPU必须自行设计的接口/逻辑**。所谓原创仅指本项目工程方案，不宣称ASan、bounds、tag等底层算法学术首创。预算保留历史规划区间，但Q2基础和接入仅按真实缺口追加；[v1.2原始预算](docs/estimates-v1.2.json)保留作对照。

## 1.2版历史主要更新

每条A～F路线分别给出编译器、KMD、UMD必需支持和新改代码的判断条件；D进一步分为D1对象边界与D2时间身份。设备检查库在GPU执行，归入编译器交付包；SDK/Host runtime、用户态driver和CLI按层实现，预算归入UMD责任包。该归属不要求改变现有代码仓库或运行边界。

功能级cmodel能否运行插桩代码，与是否实现独立检查oracle分开。QEMU配置按Q0/Q1/Q2/Q3比较，**本项目已确认属于Q2**；模型执行与真机、TCG CPU回调的观测能力不同，QEMU内部是否复用cmodel待审计。

代码量保留1.1版六路线基础参考包合计，重新组织三组件与模型分项；KMD新增映射、故障／完成、终止、代码及硬件状态契约按缺口另计。Q2一次性系统基础和逐路线接线分别估算，复用的cmodel检查引擎只计一次；Guest KMD适配也有明确归属。

新增Linux kernel与QEMU官方来源9项。原28个详细实例保留，并补上全部场景的KMD／双平台对应和分层验收，区分真实检测、故障注入、模型证据与真机证据。

## 文稿状态与后续实施

**正式判断以主稿为准。** 归档研究笔记保留1.0版调研过程，可能包含早期候选结论、旧章节编号或尚未统一的措辞。文稿检查验证结构、引用、场景映射和预算算术；没有编译或运行示例，没有执行用户GPU、cmodel、QEMU、KMD或UMD实现测试。

从[第22章平台参数](docs/llvm-gpu-sanitizer-research.md#platform-parameters)核实ISA/GPUVA、LLVM分支及三组件契约，**定位已有QEMU GPU模型的访存hook和Guest系统接线**，再选择可验收原型。依据真实执行证据收敛覆盖、组件修改和预算，文献性能数字不代表自研实现性能。

仓库保存原创概括与公开来源链接，不包含第三方论文PDF或完整原文提取。后续继续工作时先读取master最新版本，通过正常提交保留历史。
