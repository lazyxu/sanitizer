# GPU Sanitizer文稿修订记录

## v2.7 · 2026-10-23 · 架构历史笔记与第8～17章实施表单源化

- [历史架构笔记](research-notes/architecture.md) 约19.0 KB→6.9 KB，保留12节、原20个标题和R1～R9一手来源；完整方案表、预算数值、设备ABI和重复执行代码不再单独维护。
- [主研究 §8～17](llvm-gpu-sanitizer-research.md) 保留23章、28例与原有锚点，去除重复 ObjectDesc、B/C/D/E/F逐层通用组件表及§16.9第二份软件预算总表；Guest/Host GPUVA身份、KMD真实支持条件、异步Free语义和性能设计继续保留。
- [组件指南](real-gpu-components-implementation.md) 统一Compiler/Device Runtime/UMD/KMD/Firmware/RTL伪代码和报告ABI；预算数值唯一登记在 [estimates-v1.8.json](estimates-v1.8.json)。
- 新增架构笔记、R1～R9、主研究23章/28例、稳定锚点及实现单源防回归。**所有结果仍仅文档/CPU静态检查，自研目标GPU状态为GPU_NOT_RUN**。
## v2.6 · 2026-10-23 · 厂商官方证据版本化与历史笔记去重

- [厂商来源登记](research-notes/vendors_sources.json) 新增统一的 `vendor_profiles`：NVIDIA、AMD、Intel、MUSA 均具备产品版本、核验日期、GPU/OS 条件、插桩/运行库、Checker 范围、限制、证据 ID 与目标 GPU 状态；LLNL 现场版本与 AMD Technology Preview 严格分开。
- [历史厂商笔记](research-notes/vendors.md) 变为版本化索引及纠正记录，不再复制完整厂家功能矩阵。主研究 §4.1～4.4 保留机制对比和重要条件，§4.9～4.10 转为官方来源/专题索引，不再重复机器码/支持版本与构建命令。
- 统一 README、三种核心方案和 NVIDIA/AMD/Intel 专题的证据链接，增加结构化校验、官方 source ID 引用及“公开产品能力≠自研 GPU 实卡结果”防回归。
- 主研究保留 23 章和 28 场景、全部已知锚点；未修改 GPU 程序、驱动或检测器实现，真实硬件状态仍为 `GPU_NOT_RUN`。

## v2.5 · 2026-10-23 · AMD硬件适配与通用设备代码单源化

- [AMD硬件适配专题](amd-gpu-asan-hardware-fallback.md)完整保留官方ROCm `xnack+`/HMM要求、可恢复缺页与HMM差异、三种Shadow物理映射取舍、Sparse GPUVA约束及无故障重试测试矩阵。
- 通用Compiler/Device Checker/UMD/KMD/固件与报告伪代码归 [唯一组件实施指南](real-gpu-components-implementation.md)；设备运行库新增 `safeShadowLookupNoFault` 设计，先核实元数据页状态，再执行Shadow Load。
- [S01厂商机制指南](S01-vendor-implementation-guide.md)保留AMD机制/版本与选型，但官方完整构建命令统一引用AMD硬件专题；根入口与文档导航同步澄清唯一职责。
- 新增CPU静态回归：12个章节、XNACK/HMM/Shadow三取舍、软件元数据缺页安全、重复代码禁止和ABI跨文档引用；不改变GPU测试代码、实际硬件能力或 `GPU_NOT_RUN`。

## v2.4 · 2026-10-23 · S01厂商机制与通用组件单源化

- 瘦身[S01 厂商指南](S01-vendor-implementation-guide.md)：保留 NVIDIA NV-R/NV-C、AMD-B2 官方机制与条件、XNACK 边界、B1/E/F 归属及 S01 选型；去掉重复的 Binary Rewriter/LLVM/Shadow/对象报告伪代码和第二份案例验收矩阵。
- [通用组件指南](real-gpu-components-implementation.md)作为唯一的 SanAccessEvent / SanObject / SanReport / PointerMeta 及安全 CFG 检查、模式协商实现入口；CPU 静态校验新增职责分离防回归。
- [S01 唯一案例 README](../samples/S01/README.md)仍负责错误/合法代码、实际 CUDA/HIP 样例、厂商预期和验收说明；主研究保持 23 章和 28 场景不变。
- 仅文档与校验/审计字段更新，不改变任何 GPU 编译器、驱动、程序或真实硬件验证状态。

## v2.3 · 2026-10-23 · 第18章单一详细案例规范及瘦身

- 主研究保留23章、28个 `18.x` 子标题及 `#case-sxx` 稳定锚点；移除重复的错误代码、合法反例、设备责任与厂商预期表，改为紧凑摘要及一对一源码链接。
- 每个 `samples/Sxx/README.md` 成为该案例的**唯一详细技术与验收规范**；原主报告少量更完整的实现说明已并入相应案例，保持被迁移的重要技术边界。
- §18.29保留跨场景7类生产职责映射；§18.30只链接样例导航及机器可读manifest。S01厂商指南专注成熟算法实施，不再复制64B场景参数表。
- 增加自动化一致性检查：23章、28个标题及锚点、双向README、两种设备源码、唯一场景说明、场景分组和厂商预期，保留 `GPU_NOT_RUN` 的真实验收状态。

## v2.2 · 2026-10-23 · Intel专题及统一文档导航

- 新增 [文档导航](README.md)：说明核心分类、厂商证据、组件实施、样例和历史笔记各自责任，并给出渐进式整理建议。
- 新增 [Intel 设备 Sanitizers 专题](intel-oneapi-device-sanitizers.md)：核对 C/C++ 2026.0 的 ASan/MSan/TSan、Unified Runtime、SYCL/OpenMP 命令、Ponte Vecchio 支持范围、Kernel 串行与检查跳过等条件。
- 更新核心分类表、主研究与厂商旧笔记的交叉引用，不改变三种基础空间方案及已有预算体系。
- 增加 CPU 静态文档防回归校验，保留 GPU_NOT_RUN 的自研实现状态。

## v2.1 · 2026-10-22 · 分类精简与删除历史冗余技术目录

- 依据用户确定的三种基础空间参考配置，精简所有相关研究、组件、能力、预算和样例说明；移除已撤销的非核心独立方案及对应演示代码、失效链接与结构测试依赖。
- 仅保留 **NVIDIA NV-R**、**AMD-B2 LLVM设备Shadow/Poison** 与 **NVIDIA NV-C Original-Object Bounds** 的基础空间配置分类；B1是当前Live区间表实现、D2时间身份扩展、E为集成、F1/F2需要实体硬件。
- GPUVA/metadata映射、队列生命周期、错误报告及设备Poison是上述核心检查器的内部必需工程契约，不从子部件另增加独立产品路线。
- 更新文档一致性防回归，执行CPU静态CI；保留GPU_NOT_RUN与目标Compiler/UMD/KMD尚未实施的真实状态。

## v2.0 · 2026-10-22 · 统一2家厂商、3种基础参考配置与能力口径

- 新增[统一分类与能力对照](core-design-comparison.md)。NV-R和AMD-B2均主要判断当前地址是否有效；NV-C在来源可知范围内判断原对象Bounds。
- B/C/D/E/F是交付预算责任包，不是5种独立地址检查谓词。厂商内部数据结构未公开的部分仅借鉴原理，不能宣称实现完全一致。
- 同步[S01](S01-vendor-implementation-guide.md)、[主研究](llvm-gpu-sanitizer-research.md)、[逐组件](real-gpu-components-implementation.md)、[预算](estimates-v1.8.json)、Intel/MUSA辅助能力参考和28组样例覆盖。
- 未交付自研GPU完整检查器与真实设备结果。

## v1.8～v1.10 · 2026-10-21 · 原研究成果

- 统一实体GPU的Compiler/Device Runtime、UMD、KMD与按需Firmware/RTL责任，明确C二进制重写不要求修改生产Compiler。
- 建立主研究23章、S01～S28的CUDA/HIP错误与合法对照、CPU静态CI和可选GPU自托管Runner。
- 维护B/C/D/E/F软件估算及复用公共P和已有KMD合同的预算规则。
- 对自研GPU上无XNACK的设备可用Shadow实现、错误终止和来源/时间身份检测作出工程设计，但尚无目标GPU实测。
