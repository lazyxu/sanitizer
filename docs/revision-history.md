# GPU Sanitizer文稿修订记录

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
