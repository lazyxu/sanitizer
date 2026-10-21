# LLVM GPU Sanitizer 技术调研

本仓库保存面向自研GPU和LLVM后端的sanitizer技术调研与架构实施建议。生产软件按**编译器、KMD（Kernel-Mode Driver，内核态驱动）、UMD（User-Mode Driver，用户态驱动）**分工；功能级cmodel与基于QEMU的全系统虚拟平台分别分析。

**当前版本1.7（2026-10-21）：撤销仅在cmodel/QEMU中生效的独立检测产品路线。正式候选只保留可在真实GPU执行检查指令或由真实硬件实施的B/C/D/E/F，模型只用于辅助验证。同步修正整个仓库的组件职责、实施顺序、样例和预算；真实GPU硬件检测仍未完成。**

## 阅读入口

- **[全仓库真实GPU可部署性与职责审计](docs/hardware-deployability-audit.md)**：逐项说明被删除的模型独立检查产品方案、五个可部署候选、Compiler/KMD/UMD真实改动与需要完成的硬件证据。
- **[当前B～F硬件候选工程量预算](docs/estimates-v1.7.json)**：模型/QEMU验证投入独立列示；旧预算中的模型单独产品路线已作废。
- [正式调研与实施方案](docs/llvm-gpu-sanitizer-research.md)：23章、44项参考来源，包含详细背景与目标、五条真实GPU可部署候选路线、组件契约、工程预算和28个检测场景。
- [项目背景](docs/llvm-gpu-sanitizer-research.md#background)与[建设目标](docs/llvm-gpu-sanitizer-research.md#goals)：建设动因、现有调试盲区、使用者、功能／质量目标、验收证据和优先级。
- [厂商/LLVM详细机制](docs/llvm-gpu-sanitizer-research.md#vendor-deep-dive)与[各方案来源／融合／原创对照](docs/llvm-gpu-sanitizer-research.md#route-provenance-matrix)：NVIDIA runtime/compile-time、AMD GPU ASan、Intel UR、MUSA公开能力、LLVM shadow/tag与cuCatch/HiRace/GPUArmor等研究。
- [三组件支持契约](docs/llvm-gpu-sanitizer-research.md#component-contracts)：编译器、KMD、UMD分别生产哪些信息、依赖什么接口，以及现有支持与新增修改的区别。
- [功能级cmodel／QEMU能力边界](docs/llvm-gpu-sanitizer-research.md#model-platforms)与[全系统接入](docs/llvm-gpu-sanitizer-research.md#qemu-integration)：区分GPU执行、模型检查、Guest驱动路径和真机直通。
- 逐路线双平台分析：[真实GPU产品准入条件](docs/llvm-gpu-sanitizer-research.md#hardware-deployment-gate)、[B shadow](docs/llvm-gpu-sanitizer-research.md#route-b-platforms)、[C binary](docs/llvm-gpu-sanitizer-research.md#route-c-platforms)、[D bounds／identity](docs/llvm-gpu-sanitizer-research.md#route-d-platforms)、[E hybrid](docs/llvm-gpu-sanitizer-research.md#route-e-platforms)、[F hardware](docs/llvm-gpu-sanitizer-research.md#route-f-platforms)。各路线正文也有三组件支持与修改表。
- [工程量估算](docs/llvm-gpu-sanitizer-research.md#component-estimates)与[当前真实GPU候选预算](docs/estimates-v1.7.json)：核心／手写测试分开；三组件、cmodel、条件KMD缺口及QEMU接入分别计算。
- **[S01：NVIDIA/AMD/其他方案，逐组件实施指南](docs/S01-vendor-implementation-guide.md)**：区分 NVIDIA 运行时二进制插桩和 CUDA13.1+ 编译期Base/Bounds，AMD LLVM+Shadow/Redzone，逐个给出 Compiler/KMD/UMD/cmodel/现有QEMU模型改动与核心伪代码，并附厂商官方原文。
- [S01真实GPU检测与验证边界](docs/llvm-gpu-sanitizer-research.md#case-s01)：设备区间表、Shadow、Binary Patch、原对象Bounds、Hybrid、真实硬件MMU/Tag，以及QEMU验证（非独立方案）。
- [检测场景与实例分析](docs/llvm-gpu-sanitizer-research.md#detection-scenarios)：S01～S28逐例说明错误位置、检测方案、组件配合、修复／合法对照、报告与限制；[逐场景KMD和平台对应表](docs/llvm-gpu-sanitizer-research.md#scenario-platforms)补充每例的执行与观测条件。
- [验证基准与验收矩阵](docs/llvm-gpu-sanitizer-research.md#validation)、[研究进度](docs/research-progress.md)、[修订记录](docs/revision-history.md)、[文稿验证记录](docs/validation-report.json)。
- [归档研究笔记](docs/research-notes/)：架构、论文及友商资料的过程记录与来源链接。

## v1.7 生产硬件准入原则

**Sanitizer的检查必须在实体GPU运行。** 允许的检测方案：Compiler生成的GPU ISA检查/Shadow、UMD运行期重写的GPU二进制检查、对象/时间身份设备检测、混合检测，以及目标实体GPU已支持的MMU/Tag/Bounds硬件。**仅在cmodel或QEMU GPU解释器中新增访存前回调，不会影响真实GPU，是无效的独立产品方案，现已从路线和预算中撤销。**

推荐先交付B的真实GPU可执行LLVM设备检查器、UMD Shadow和KMD映射/完成契约，随后用已有QEMU ISA执行器**运行同一份插桩代码**进行预验证，最后实卡验收。C纯Runtime的生产Compiler可以不改；KMD需要支持但已有接口足够时也可以零新增。

## v1.6.1 澄清：纯运行时二进制插桩不要求修改编译器

**NVIDIA-R风格的Runtime Binary Instrumentation：** 原始Kernel仍然使用未改动的既有编译器/已存在机器码。UMD的独立Runtime Binary Rewriter负责识别访存指令、机器码decode/encode、插入设备Patch、CFG重定位、资源统计和报告。可只读复用现有LLVM MC工具，但**没有强制修改生产Compiler的任务**；编译工具自己的设备Patch模块也不等于“修改编译器”。若缺少decoder，应在UMD插桩项目内自研并计费。旧§16中的C“Compiler”成本主要是历史GPU设备库的预算归属，不代表修改编译器；§16.5a已给出纯runtime路径的归属重分类。

详细见[S01 NVIDIA-R纠正后的组件表](docs/S01-vendor-implementation-guide.md)及[主报告§11/§16](docs/llvm-gpu-sanitizer-research.md)。

## v1.6 厂商优先的 S01 研发路线

新版不再把 S01 首先列成抽象的 A/B/C；优先讲 NVIDIA 默认运行时补丁、NVIDIA 编译期原对象分析和 AMDGPU LLVM ASan，再讨论必须在GPU执行的区间表插桩、GPU Guard Page、实体GPU已支持的Tag/硬件Bounds和Hybrid。推荐先实施**借鉴AMD/LLVM的真实GPU设备Shadow**，再用已有QEMU执行**相同的插桩Binary**做预验证，最后补充**NVIDIA式编译期原对象Bounds**；只有闭源覆盖缺口值得投入时才建设运行时机器码patch框架。详见[独立S01实施指南](docs/S01-vendor-implementation-guide.md)。

## 新增：真实 GPU Docker + GitHub Actions 验证

- **[28组独立 CUDA/AMD HIP 设备样例](samples/README.md)**：每组有源码、详细中文注释、README、合法对照及预期覆盖等级；源码加入不等于厂商GPU工具必检出。
- **[Docker一键配置](scripts/setup-docker.sh)**：`./scripts/setup-docker.sh cuda` / `ROCM_ARCH=gfx90a:xnack+ HSA_XNACK=1 ./scripts/setup-docker.sh rocm`，进入容器后 `./scripts/run-all.sh cuda|rocm`。
- **[GitHub Actions GPU 硬件工作流](.github/workflows/gpu-hardware.yml)**：可信`master`、可选变量`GPU_CI_ENABLED=true`、专用NVIDIA/AMD GPU Runner、`smoke / compile-all / safe-all`模式；结果原始日志与JSON上传Artifact。
- **[GPU Runner部署和完整验收说明](docs/gpu-actions-ci.md)**：GPU Runner标签、宿主驱动、Docker设备透传、GitHub仓库变量、安全隔离，以及GPU报告/CPU ASan/API错误的严格区分。
- **[CPU 静态 CI](.github/workflows/sample-structure.yml)** 只检查脚本结构与合成GPU日志分类；不得标称GPU检出成功。
- **[真实GPU Guard Page实验](samples/experiments/gpu-guard-page/README.md)**：采用GPUVA/VMM页级保护，不使用CPU`mprotect`，不承诺任意小对象的字节级边界检出。

**现状：** GPU CI所需Runner和仓库变量尚未由本会话配置；因此GPU任务`Skipped`只是**未运行**。28种错误并非两家工具都具备检测能力；本轮不宣称全部样例已在真实GPU编译或检出。

## 1.3版：现有QEMU GPU ISA执行模型＋厂商机制溯源

**确认的项目事实：QEMU全系统虚拟平台已具备目标GPU ISA执行模型（Q2）。** 不再将“新建QEMU GPU ISA解释器”列为开发工作；直接利用已有模型**执行B/C/D/E生成的GPU检查代码**、核对Shadow与Guest UMD/KMD报告；模型本身的访存检查钩子不列为产品实现。还未知的部分（模型是否与独立cmodel共用、具体访存opcode/Guest桥/Shadow已有程度）必须源码审计，不能臆测成已完成或未完成。

第4章深入比较NVIDIA memcheck的运行时/编译期、base-and-bounds及hybrid，AMD GPU ASan的Shadow/Redzone/device runtime，Intel oneAPI与Unified Runtime，MUSA公开产品能力，以及LLVM源码/HWASan；各机制附官方直达链接和限长原文摘录，明确已知事实与未公开实现的区别。

旧版曾将模型独立检测错误列为产品路线，现已撤销；当前B～F路线与S01变体逐一标注**厂商/研究来源、融合的算法与自研GPU必须自行设计的接口/逻辑**。所谓原创仅指本项目工程方案，不宣称ASan、bounds、tag等底层算法学术首创。预算保留历史规划区间，但Q2基础和接入仅按真实缺口追加；[v1.2原始预算](docs/estimates-v1.2.json)保留作对照。

## 1.2版历史主要更新

当前每条B～F真实硬件候选路线分别给出编译器、KMD、UMD必需支持和新改代码的判断条件；D进一步分为D1对象边界与D2时间身份。设备检查库在GPU执行，归入编译器交付包；SDK/Host runtime、用户态driver和CLI按层实现，预算归入UMD责任包。该归属不要求改变现有代码仓库或运行边界。

功能级cmodel能否运行插桩代码，与是否实现独立检查oracle分开。QEMU配置按Q0/Q1/Q2/Q3比较，**本项目已确认属于Q2**；模型执行与真机、TCG CPU回调的观测能力不同，QEMU内部是否复用cmodel待审计。

代码量历史版本曾包含已撤销的模型-only路线；当前预算仅保留五条真实GPU候选，重新组织三组件与辅助验证分项；KMD新增映射、故障／完成、终止、代码及硬件状态契约按缺口另计。Q2一次性系统基础和逐路线接线分别估算，复用的cmodel检查引擎只计一次；Guest KMD适配也有明确归属。

新增Linux kernel与QEMU官方来源9项。原28个详细实例保留，并补上全部场景的KMD／双平台对应和分层验收，区分真实检测、故障注入、模型证据与真机证据。

## 文稿状态与后续实施

**正式判断以主稿为准。** 归档研究笔记保留1.0版调研过程，可能包含早期候选结论、旧章节编号或尚未统一的措辞。文稿检查验证结构、引用、场景映射和预算算术；没有编译或运行示例，没有执行用户GPU、cmodel、QEMU、KMD或UMD实现测试。

从[第22章平台参数](docs/llvm-gpu-sanitizer-research.md#platform-parameters)核实ISA/GPUVA、LLVM分支及三组件契约，**定位已有QEMU GPU模型的访存hook和Guest系统接线**，再选择可验收原型。依据真实执行证据收敛覆盖、组件修改和预算，文献性能数字不代表自研实现性能。

仓库保存原创概括与公开来源链接，不包含第三方论文PDF或完整原文提取。后续继续工作时先读取master最新版本，通过正常提交保留历史。
