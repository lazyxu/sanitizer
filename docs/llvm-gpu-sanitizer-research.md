
# 自研 LLVM GPU 后端 Sanitizer 技术调研与实施方案

**文档版本：v2.9 · 2026-11-02 · 用途：技术选型、编译器/驱动联合评审与工作量拆分。**

本稿详细比较**NV-R、B1、B2、NV-C、cuCatch、CuSafe六条软件Memcheck路线**；Intel ASan作B2产品对照。现行[核心分类](core-design-comparison.md)仍为两家主要厂商、三种成熟空间参考配置、两层基础空间判定；B/C/D/E/F保留工程责任与历史预算含义，D2是时间身份扩展、E是集成策略。F只说明既有硬件契约/成本边界，不扩大芯片或图形验证方案选型。

**当前事实：** 仓库提供研究文档、伪代码、28组CUDA/HIP样例和CPU静态检查；目标GPU检查器尚无实现及性能证据，GPU_NOT_RUN。厂商工具结果不能代替自研验收。v1.8数字预算保持不变，既有估算不是本次新增报价。

正文采用四类证据：**【官方事实】**限定产品版本和配置；**【论文结果】**限定论文平台、样本和统计口径；**【工程设计/推导】**说明本项目应如何实现与验证；**【估算】**是待平台审计收敛的规划。来源编号在§23，厂商条件由[版本化登记](research-notes/vendors_sources.json)维护，历史改动见[修订记录](revision-history.md)。

## 目录

按“决策 → 证据与判定模型 → 公共契约 → 方案实现 → 成本 → 验收”组织，保留原章节号和场景深链接。会议不必逐页朗读：先审§1、[核心比较](core-design-comparison.md)、§16、§19、§22；争议再回到对应机制和来源。

| 阅读阶段 | 原有章节及职责 |
|---|---|
| 决策与范围 | [1. 推荐路线](#review-decisions)；[2. 目标与假设](#background)；[3. 比较口径](#comparison-rules) |
| 证据与算法基础 | [4. 厂商公开机制](#vendor-deep-dive)；[5. 论文及性能证据](#paper-evidence)；[6. 检查模型](#memory-model)；[7. GPU执行语义](#gpu-semantics) |
| 公共执行契约 | [8. 生命周期与组件接口](#common-architecture)；[9. 部署准入](#hardware-deployment-gate) |
| 六条软件路线 | [10. B1/B2](#software-route-details)；[11. NV-R/C](#runtime-route)；[12. NV-C/D1、cuCatch、CuSafe及D2](#identity-routes) |
| 集成和范围边界 | [13. E混合集成](#hybrid-integration)；[14. F条件硬件边界](#hardware-boundary)；[15. Init/Race/Sync独立目标](#other-checkers) |
| 投入与测量 | [16. 组件任务及预算](#component-estimates)；[17. 性能/显存模型](#performance-design) |
| 实验与落地 | [18. 28个场景](#detection-scenarios)；[19. 验收](#validation)；[20. 研发阶段](#roadmap)；[21. 风险](#review-risks)；[22. 评审待决项](#platform-parameters) |
| 追溯 | [23. 参考来源与证据分歧](#references)；[样例执行入口](#sample-execution) |

<a id="review-decisions"></a>

## 1. 决策结论与推荐路线

### 1.1 推荐结论

**【工程建议】优先建立统一的分配／生命周期登记和诊断协议，以LLVM编译期生成的GPU指令及UMD发布的真实资源状态，在实体GPU上实施基础内存检查，随后增加对象边界与时间身份传播。**

推荐的建设顺序是：

1. 建设公共底座 P：统一对象登记、异步生命周期、模块能力声明、错误报告和符号化。
2. **先实现B真实GPU可执行的LLVM访存检查、设备Shadow与UMD生命周期**，通过实体GPU的S01错误与合法对照验证PC、lane、宽度、报告和可靠终止。
3. 扩展 B 的地址空间和指令覆盖；增加 D1 的编译器对象 bounds 分析，弥补“越界后落入另一合法分配”的漏检。
4. 对明确需要地址复用后 UAF 检测的场景，再做 D2 的完整指针时间身份传播；与仅查询分配表的方案分别命名、分别验收。
5. 当闭源库、已有二进制、JIT 模块构成实际覆盖缺口时，增加 E 的二进制补充。C 的工程成本与已有解码、重编码和 patch 基础设施高度相关。
6. Racecheck、Initcheck、Synccheck 作为独立检查器迭代；如果软件方案的真实瓶颈足够集中，再评估 F 的硬件辅助。

这一路线要求编译器与UMD可扩展，KMD提供实际缺失的映射、完成、故障和终止契约；已有契约充分时可以复用，并不要求每条路线都新增KMD代码。第一版不必同时完成四类检查器、全部CUDA类功能或完整二进制重写器。

### 1.2 三个生产软件组件与实体GPU

一个LLVM Pass可以插入设备检查，但不能独自提供资源生存期、特权映射、队列完成或可靠终止。本报告以Compiler、KMD、UMD三个生产软件责任包和运行在实体GPU上的Device Runtime/硬件检查器组织实施、预算与验收。

| 顶层组件 | 定义与职责 | 本报告的子模块归属 |
|---|---|---|
| 编译器 | 前端选项、中端分析、后端访存覆盖、对象来源与模块契约 | Pass/后端/MC；随工具链交付的GPU设备检查库分别列项 |
| KMD（Kernel-Mode Driver，内核态驱动） | 特权资源、BO/GPUVA映射、提交/完成、fault、context及可用终止/reset | 只承担目标驱动确有的内核接口与状态；硬件/固件前提另列 |
| UMD（User-Mode Driver，用户态驱动） | 逻辑资源与命令、loader/JIT/patch、元数据调度、API诊断和报告 | SDK/Host runtime、用户态driver、CLI/符号化分层实现并归入同一预算组 |

**归属不等于运行位置。** 设备检查库在GPU执行，不能因其预算归入编译器包就称为编译期检查；CLI和Host SDK也不必嵌入底层UMD库。这样分组用于满足三组件比较口径，不要求搬动用户现有源码目录；团队调整归属时保持总量去重。

**真正的产品准入依据是实体GPU实际执行检查**：必须有最终ISA中可运行的插桩指令，或目标芯片真实存在的MMU/Tag/Bounds能力。编译器IR、Host静态检查、API报错均不得代替实体GPU对访存或同步事件的判断。

### 1.3 三项必须坚持的能力区分

**第一，地址有效性不等于对象边界正确。** 某个地址属于当前存活分配，并不能证明从对象 A 推导出来的指针有权访问对象 B。ASan 式 redzone 和分配区间查询都可能漏掉这种情况。[L02][N04]

**第二，分配表 generation 不等于完整时间安全。** 地址重用后，新旧裸指针可以具有完全相同的位值；只有当前对象表而没有指针自身的历史身份，就无法区分两者。D2 必须解决指针身份的生成、传播、存取和跨模块边界。

**第三，单次实卡执行不等于所有输入和调度均安全。** 需结合输入覆盖、静态分析及不同实际并发调度的正反例，明确受支持的范围。[P07]

### 1.4 立项时最实用的选择表

| 项目现实条件 | 优先选择 | 主要原因 |
|---|---|---|
| ISA／UMD频繁变化但已有实体GPU硬件 | B最小设备插桩原型＋实卡S01正反例 | 必须先证明实际GPU二进制可执行检测指令 |
| 有真机和稳定 LLVM 后端，用户可重编译 | P＋B，逐步引入 D1 | 能较快形成实际可用工具并测量真机开销 |
| 大量对象越界落入另一合法 buffer | D1，必要时 D2 | 需要保留原对象身份，单纯地址查询不足 |
| 高频异步内存池、地址复用和指针缓存 | D2＋完整生命周期事件 | 当前分配状态不能完整辨别旧指针身份 |
| 闭源库覆盖是近期交付硬要求，且已有成熟 patch 框架 | C 或 E | 以最终二进制为入口补充覆盖 |
| 下一代芯片可修改，软件原型已找到明确瓶颈 | F | 有证据地设计 metadata cache／检查指令 |

## 2. 项目背景、建设目标与研究范围

<a id="background"></a>


### 2.1 项目背景

Kernel数值输出正确或Host同步点收到Fault，都不足以回答哪次设备访问违反了哪个对象/生命周期/同步约定。错误写入可能稍后才表现为另一个Kernel异常；落入合法映射内的其他对象甚至不触发硬件Fault。项目需要在违规操作附近记录设备证据，并关联分配、来源、命令和后续症状。[N02][N08]

| 责任视角 | 需要回答的问题 | 应保留的证据 |
|---|---|---|
| 应用/算子 | 哪个访问违反何种约束，如何复现修复？ | Source/PC、实际范围、线程、对象与合法对照 |
| Compiler/Device | 检查是否覆盖最终访存，优化是否改变语义？ | IR/MIR/ISA、宽度/Mask、资源用量和来源传播 |
| UMD/KMD | 逻辑对象、底层映射、Free与执行何时生效？ | Allocation/Mapping/Queue ID、Fence、故障和终止范围 |
| 测试/性能 | 错误被触发且被检查了吗？成本由什么造成？ | 版本化输入、覆盖矩阵、原始报告及共同基线 |

LLVM/ASan可提供分析和插桩基础，GPU仍需独立确定Shared/Private身份、设备元数据、队列顺序、SIMT前进性和停止策略。CPU运行库、页映射或API拦截均不能单独承担这些职责。[L01][L05][L06]

<a id="goals"></a>

### 2.2 建设目标

首版交付目标为**显式Global分配的地址/宽度检查、生命周期接入、设备报告及可靠失败路径**；每项都以§19的错误版/合法版证据验收。随后按实际缺口增加原对象D1、受控D2、更多空间/指令及独立Init/Race/Sync。全部能力编号和盲区只在§2.4定义，避免另维护一份同义目标矩阵。

| 优先级 | 建议交付 | 退出证据与后续门槛 |
|---|---|---|
| P0 | 公共P＋B2（或经论证的B1）、S01/S02、生命周期与报告 | 真实设备执行Check、非法操作受控处理、合法对照不误报；工具失败/未知覆盖可见 |
| P1 | 多空间/指令、D1来源、受控D2、Pool/View | A→Live B、同VA复用及ABI边界分别验证；不把D1与D2合并宣布完成 |
| P2 | 需求明确的C/E、独立检查器与性能改进 | 闭源覆盖有实际收益；同覆盖同输入测性能，硬件投入另审 |

质量要求与功能同时验收：有PC/对象/线程证据；未支持指令与丢来源可见；活动Mask和原子语义正确；日志满、元数据不足及错误终止不造成假Clean或死锁；Compiler/Device/UMD/KMD协议有版本兼容。运行时间、显存峰值和寄存器长尾在首轮设备基线后设门槛，不以论文平均值代替。

首版不承诺任意第三方ABI的完整时间安全、完整值级MSan、跨CPU/GPU通用TSan、所有输入/调度的安全证明或近零开销。项目定位为开发与CI调试，未将其定义为恶意Kernel隔离边界。

### 2.3 本报告采用的项目假设

目标GPU实际实现仓库、ISA、GPUVA布局、编译器分支和KMD/UMD接口清单尚待确认。方案与预算采用以下可替换假设：

- 已有能编译并执行计算 kernel 的 LLVM GPU 后端，已有基本调试行信息。
- 已有可执行kernel的Host runtime/UMD与KMD基本栈；KMD是否满足检查器必需的映射、报告、错误处理和完成契约仍应核查。
- 至少支持全局内存、共享／局部数据存储和线程私有／栈地址语义；具体 address-space 编号以目标定义为准。
- GPU 支持基本原子操作和可由驱动管理的设备缓冲；不假设具有 CPU 式虚拟内存缺页能力、Top-Byte Ignore、128-bit 原子或可靠线程级恢复。
- 第一阶段面向开发和 CI 调试，不将 sanitizer 当作恶意 kernel 的隔离边界。
- 代码量预算只针对现有工具链和真实GPU软硬件栈上的Sanitizer建设，不包含从零开发LLVM后端、完整UMD/KMD或整颗GPU；未来RTL/DV和新增固件单独估算。

如果这些假设不成立，应调整对应模块预算；不必因此推迟本报告提出的能力分层和接口设计。

### 2.4 检测能力不宜压缩成一个“支持 sanitizer”开关

| 能力编号 | 检查内容 | 核心所需信息 | 典型漏检边界 |
|---|---|---|---|
| M1 | 地址未分配、不可访问、跨映射边界 | 地址空间、映射、权限 | 落入另一合法对象 |
| M2 | 对齐和访存范围 | 实际地址、访问宽度、指令对齐约束 | 不能替代对象身份 |
| M3 | 分配对象级越界 | 原对象 base／size 与实际地址 | 未注册子分配、来源丢失 |
| M4 | 子对象／逻辑分区越界 | 子对象边界或用户注解 | 结构体内部、tensor view 语义未登记 |
| M5 | 释放后使用、地址复用后的旧指针 | 指针时间身份、对象生存期 | 只有地址表时无法完整判断 |
| M6 | invalid free、double free、泄漏 | allocator 事件与对象状态 | 未拦截的分配器、进程退出策略 |
| I1 | 从未写入的内存读取 | 初始化位图、copy／memset／store 事件 | 值级未定义传播需另做 MSan 类机制 |
| R1 | 数据竞争 | 冲突访问和 happens-before／同步语义 | 未执行路径、未建模同步 |
| S1 | barrier／mask／异步同步误用 | 参与集合、到达次数、phase 和作用域 | 单次实体GPU运行不能证明所有调度都正确 |
| A1 | API、资源和异步顺序错误 | Host API 与命令依赖图 | 只看 API 返回时刻不能判定设备时序 |

这里将 M1～M6 拆开，是为了让测试结果可以清楚地说明检测保证。实现 M1/M2/M6 的工具可以很有价值，但不应据此声称已经实现对象级或时间安全。

### 2.5 地址空间及其身份

| 空间／资源 | 元数据身份建议 | 必须注意 |
|---|---|---|
| Global／device heap | context、规范对象 ID、对象内偏移、generation | 同一物理分配可以具有多个 GPUVA |
| Shared／LDS | launch、block／cluster、共享对象 ID | 不同 block 中相同数值地址不是同一对象 |
| Private／local／stack | launch、logical thread、frame、对象 ID | scratch backing 的物理布局不等于源级对象边界 |
| Constant | module、符号／对象、访问权限 | 合法读取与非法写入需要分开 |
| Image／texture／surface | 资源描述符、维度、格式、坐标和层级 | 只按原始指针范围检查不完整 |
| Unified／managed／host-mapped | 规范分配 ID、别名、驻留／权限状态 | CPU 写入、迁移和一致性都会影响检查 |
| Peer／imported／IPC | 物理对象身份与各上下文映射 | 不能将导入对象当成新的独立所有权 |

这是本报告的接口建议。各厂商对 shared、local 等词的命名存在差异，移植时必须按语义映射，不能照抄 address-space 数字。[L05][L06]


<a id="comparison-rules"></a>

## 3. 比较口径与评审规则

比较方案时固定下列独立维度，再谈优劣。厂商版本与支持条件以对应专题及原始来源为准。

| 维度 | 必须给出的答案 | 判断错误会导致什么 |
|---|---|---|
| 注入阶段 | 编译期、装载时还是运行时；用户Binary能否重编译 | 把插桩入口误当检测强度，或低估ISA重写工作 |
| 判定谓词 | 当前地址、同一对象完整范围、原来源、还是时间身份 | 用一个“OOB支持”遮住S02/S04/S09的不同盲区 |
| 元数据 | Base/Size、Shadow、Tag或Epoch在哪里，谁发布/退休 | 检查数据陈旧、未映射或把新对象误当旧对象 |
| 覆盖与失败 | 哪些空间/指令/ABI被检查，未知时降级还是拒绝 | 只运行成功却没有检查，或误报合法外部对象 |
| 统计口径 | 设备/版本、样本、输入、基线、计时范围、均值类型及超时 | 把不同实验的19%与13%直接排成性能胜负 |
| 工程工作 | 公共底座、独有实现、兼容、验证和条件接口缺口 | 重复计费，或把论文原型行数当产品工作量 |

本项目的B1要求完整访问落入一个当前Live对象；B2保证可表示布局中的各字节可访问。二者同属当前地址层，但跨紧邻对象的宽访问、对象内部Poison等场景可能不同。D1依靠原对象来源增强空间保证；D2依靠指针自身历史身份增强时间保证。只有地址表Generation不提供后者，软Tag也不意味着新增硬件。[L02][L04][N02]

所有比较先满足相同Context/空间、实际Lane/Element Mask、宽度、权限和生命周期顺序；未知覆盖、元数据故障、未触发错误和设备未运行分别记录。某方案的更快结果若来自少检查、串行化或排除超时，应作为范围变化解释，不能当成无条件优化。

## 4. 友商产品与上游实现：机制、实现接线及可复用边界

**从实际编码角度阅读S01：** 先阅读[S01按厂商实现与组件改动的独立指南](S01-vendor-implementation-guide.md)，按 NVIDIA-R（二进制插桩）、NVIDIA-C（编译期bounds）、AMD LLVM Shadow 三种真实产品路径逐组件拆解，再用本章横向比较其可公开证实的机制和证据范围。

<a id="vendor-deep-dive"></a>

**本章采用“证据分级”：官方明确公开的算法／接口＝【官方事实】；从接口推导的内部数据结构＝【工程推断】，不是厂商源码事实；本项目推荐的实现＝【自研设计】。** 不能把同样具有“越界检测”能力的产品推断为用了相同的 shadow、BST 或二进制重写器，也不能把论文实验原型说成产品的开源实现。每段提供官方链接与限长原文节选；真正可抄的只是接口/开源代码在许可允许范围内的部分，GPU ISA、地址空间、KMD及失败处理均需重做。[N01][N02][N03][A01][I01][M01][L01]


### 4.1 NVIDIA Compute Sanitizer：运行时插桩、编译期 bounds 与多 checker 不是同一种算法

**厂商版本和证据统一入口：** [厂商历史及版本化索引](research-notes/vendors.md)、[机器可读 Profile](research-notes/vendors_sources.json) `nvidia-compute-sanitizer`。NV-R/NV-C 具体编译和执行参照 [S01 厂商指南](S01-vendor-implementation-guide.md)。仅把公开机制与自研工程设计对齐，不推测 NVIDIA 未公开的内存索引结构。[N01][N02][N03][N04][N05][N07]

#### 4.1.1 Memcheck 的两条入口与可观测数据

默认 Memcheck 采用**运行时二进制插桩**，判断设备实际访问的当前地址合法性；可选**编译期 Memcheck** 通过已知来源的 Base-and-Bounds 分析保留指针原对象 A，在其地址落入存活 B 时仍可能检出 A 越界。来源追踪失败会降级为普通分配地址范围，不等于保持 D1 保证。[N02][N04]

这两种机制的不同在于插桩位置与 Provenance 可见性；并不意味着 NVIDIA 私有 Runtime Checker 使用了可以被公开复刻的特定树或 Shadow Layout。版本配套、JIT、混合模块边界以及部分 Memory Footprint/Launch 丢追踪风险均由 `NV-MANUAL`、`NV-RELEASE` 和 `NV-NVCC` 等证据记录，不在主稿复制第二份支持清单。

#### 4.1.2 API 的 callback／patch／memory 三通路

官方 [Compute Sanitizer API](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html) 把 Callback（资源、API、Launch）、Patching（设备访存点与 PC/地址/宽度）和 Memory（工具内存操作）分成不同入口。[N03] **Host API 回调不能直接证明对应 GPU Load/Store 已执行**；公开 Patching API 不等于 NVIDIA 内部 Binary Rewriter 已完整开源。

借鉴 NV-R 时自研的实际机器码 Decode/Relocate、原 PC 映射和设备 Checker 都归[组件实施指南](real-gpu-components-implementation.md)，生产 Compiler 源码可保持零修改。

#### 4.1.3 Racecheck、Initcheck、Synccheck 的内部语义差异

`memcheck` 检内存地址访问有效性；`initcheck` 检已声明支持的地址空间中的未初始化读取；`racecheck` 主要是 Shared 范围的数据 Hazard 检查，**不是**任意 Global/跨 Kernel 的完整 TSan；`synccheck` 检测受支持的 Barrier/Mask 同步误用。[N01][N02] 工具启用方式、各地址空间及特殊指令的具体支持范围由对应厂商手册的版本定义，自研 Init/Race/Sync 的设计另见本报告 §15，不能把这些 Checker 当成三种空间方案之外的新空间算法。


### 4.2 AMD ROCm GPU AddressSanitizer：LLVM IR 检查＋Shadow／Redzone＋异构 Runtime

**版本化事实入口：** AMD `amd-rocm-gpu-asan` Profile 与固定版本ROCm 7.2.4机制文档[A01]及10.1发行条件[A05]。成熟 AMD-B2 由 LLVM/Clang 在 GPU Device Load/Store/Atomic 前插入设备 Shadow/Poison 检查，Allocator Redzone 与运行时元数据保持对象有效性；并不是仅靠 KMD MMU 页映射判断逻辑对象边界。[L02][L03]

**不要混淆官方支持与自研可行性。** 官方 GPU ASan 有 `xnack+`、Linux HMM 与匹配的 ASan 版 HIP/HSA Runtime 前提；自研 GPU 即使没有 XNACK，也可显式建立/初始化设备 Shadow，但需要在实际设备访存前确保 Shadow 地址确已映射。[A01] XNACK 重试、HMM 的不同角色、三种 Shadow 物理映射取舍与 Sparse GPUVA 全部以 [AMD 硬件适配专题](amd-gpu-asan-hardware-fallback.md) 为准。Compiler/Device/UMD/KMD 的通用伪代码只维护在 [组件指南](real-gpu-components-implementation.md)。

**已知范围：** 有限 Redzone 可能被跨过；引用版本内某些 Private/Stack 不插桩，Shared/LDS 可能重映射为 Global。不能把 LLNL 单独的 rocmcc 7.0.0 现场 Known Issues 或 7.13.0 预览缺陷直接升级为 ROCm 7.2.4 所有部署均有的已知问题。[A01][A02]


### 4.3 Intel oneAPI：设备侧 Compiler Sanitizers 与 Unified Runtime 层的责任边界

**完整版本化能力、SYCL/OpenMP 构建、UR 接口与限制，唯一参照 [Intel Device Sanitizers 专题](intel-oneapi-device-sanitizers.md)**；证据 `intel-oneapi-device-sanitizers` Profile 保留 DPC++/C++ 2026.2 与独立 UR 规范的不同维护状态。[I01][I02][I03]

成熟 Intel 体系的关键是 Compiler 设备插桩、SYCL/OpenMP Runtime、Unified Runtime/Level Zero 的层次协作。ASan 负责内存可访问性和资源状态；MSan 负责初始化定义性；TSan 负责支持范围内的竞争，三者不能合并为单个 Shadow 检查函数。官方 GPU 条件是 Linux 与列举的 Ponte Vecchio 单卡；ASan 会使 Kernel 串行化，Workgroup 规模过大时某些 Local/Private 检查可能跳过。因此报告必须注明支持范围及实际检查覆盖，不能把“运行正常”认作并发检测通过。[I01][I02]

UR规范和Intel版本化产品指南的启用方式、Adapter支持及发布节奏分别核对；不能用通用UR层存在来推断所有设备都支持。产品命令与已知限制以[Intel专题](intel-oneapi-device-sanitizers.md)为准。[I01][I02][I03]


### 4.4 MUSA Compute Sanitizer：可证实的公开行为与不可推断的内部实现

摩尔线程公开 MUSA 工具资料证实 Device Global OOB、API Error 和独立启用的显存泄漏诊断等功能，但尚不能从产品公开功能表推断其内部是否使用 AMD 式 Shadow、NVIDIA 式 Runtime Patch 或原对象 Bounds。[M01][M02] 版本、硬件要求和未公布的内部机制分别记录在 `moore-threads-musa-compute-sanitizer` Profile 与官方来源条目。

它可作报告设计和设备工具体验参考，**并不构成已经证实的第四种通用 GPU 空间内存检查算法**。

### 4.5 LLVM 上游 ASan／HWASan：能够直接借鉴的算法与仍缺的 GPU 适配

**经典ASan**：[Clang AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html)、[AddressSanitizerAlgorithm](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm)、[LLVM AddressSanitizer.cpp](https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html) 给出了“编译器访问点插桩＋GPU可用Shadow/Poison＋运行时分配管理”的核心结构。[L01][L02][L03]

~~~c
// 算法示意：只有Shadow映射已受控且状态有效才能读取
idx = gpuva >> 3;                         // 常见1:8 granule
s = safe_shadow_lookup(context, idx);     // 查不到≠有效
check_all_active_bytes(s, gpuva, width);  // 全部字节，而非只查首字节
~~~

ASM／特殊设备指令可能在IR pass后出现，必须用目标后端的指令清单复核插桩。CPU compiler-rt 的线程局部、系统调用、mmap、异常机制及符号化工具，不能不加处理地假设 GPU 设备端存在。

**HWASan**：[LLVM Hardware-Assisted AddressSanitizer Design](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html) 定义 pointer tag／memory tag 比较和short granule路径。[L04] 它不是“任何越界都会检出”的确定性对象bounds算法：需要可用tag位或独立携带方式、granule语义、tag重选和碰撞模型，还要处理tag与地址运算。已有CPU专用Top Byte Ignore不能推导自研GPU自动具备同样硬件功能。

**上游与自研分工：** Compiler 借鉴LLVM访存识别、检查插桩、DebugLoc、ignorelist/优化交互；UMD提供对象/分配信息、Shadow资源、设备报告；KMD只补缺失的映射/异常/终止。GPU真实执行语义、异步依赖和错误终止必须通过实卡验证。


### 4.6 原始研究与产品证据的对应

cuCatch提供后端Base/Tag传播和驱动协同的原型证据，CuSafe提供带内Bounds、指针编码和VA分配的另一种软件设计；两者分别在§12.7/§12.8展开。NV-C产品的功能和限制只按当前官方文档确认，不能从论文直接外推。BARRACUDA、HiRace和SuperCollider保留作§15独立竞争检查的背景，不参加本轮Memcheck性能排名。[P01][P02][P03][P08][P10]

### 4.7 以S01的一次Global Store对齐检查机制

| 进入设备检查时的信息 | NV-R/B1 | B2 | NV-C/D1 | cuCatch/CuSafe |
|---|---|---|---|---|
| 地址、宽度、空间、活动Mask、PC | 来自机器Stub或LLVM检查点 | 来自LLVM检查点 | 同左并传来源 | 来自论文的编译插桩 |
| 判定依据 | 当前对象/权限；自研具体索引另选 | 对所有实际字节的Shadow状态 | 原对象Base/Length及生命周期 | 各自的Bounds/Tag/布局和有限时间机制 |
| 失败后的动作 | 记录并Guard原操作或可靠停止 | 同左 | 同左，Unknown另报覆盖 | 按支持模型诊断；自研停止契约仍须补齐 |

页/BO已映射不证明逻辑对象边界正确；Host回调只能提供资源事件，不能证明每次Store已被检查。S01详细代码和验收由[唯一案例规范](../samples/S01/README.md)维护。

### 4.8 三核心参考配置对应的B/C/D工程方式与其他扩展来源

<a id="route-provenance-matrix"></a>

| 工程标签 | 参考与实际任务 | 不可混淆的边界 |
|---|---|---|
| B1/B2 | LLVM插桩框架；当前区间或AMD式Shadow查询 | B1不是AMD Shadow；两者都不自动有原来源 |
| C | NVIDIA公开Runtime Patch入口；目标重写器由UMD实现 | 生产Compiler可零修改；C仍需一个实际Checker |
| D1/D2 | NV-C、cuCatch、CuSafe及HWASan的不同来源/身份机制 | 表中当前Epoch不是指针预期身份；论文与产品不能互换 |
| E | 按模块共享对象/报告协议，整合B/D/C | 集成不产生比参与模块更强的来源保证 |
| F | 保留现有Tag/Bounds硬件契约和历史预算边界 | 本轮不新增芯片路线比较，也不把软件Tag当RTL |

“自研”表示目标ISA、ABI、资源和失败协议需实现，不是学术首创声明。公开源码只有在许可与接口可复用时才能计入复用量；私有产品和论文伪代码都不能按已有完整模块估算。

### 4.9 对每种产品机制最有价值的源码／官方证据入口

**全部一手来源、版本化条件和厂商支持声明的唯一登记**：[厂商版本化证据索引](research-notes/vendors.md) 和 [证据来源 JSON](research-notes/vendors_sources.json) 的 `vendor_profiles` / `sources[].id`。每个 Profile 统一提供产品版本、硬件与 Runtime 条件、检测能力、已知限制、原始来源、详细专题和硬件验收状态，主研究不复制第二张逐项 URL 表。

NVIDIA NV-R/NV-C 机制与命令参见 [S01 厂商指南](S01-vendor-implementation-guide.md)，AMD XNACK/HMM 与官方构建命令参见 [AMD 硬件适配专题](amd-gpu-asan-hardware-fallback.md)，Intel ASan/MSan/TSan 的 SYCL/OpenMP 配置参见 [Intel 专题](intel-oneapi-device-sanitizers.md)。自研 Compiler/UMD/KMD 的具体实现契约则只维护在 [组件实施指南](real-gpu-components-implementation.md)。

### 4.10 厂商版本、运行命令与可复制的实验入口

版本表由 `vendor_profiles[].version_baseline` 和 `sources[].version` 唯一维护。实际运行必须使用所选厂商**确实支持的 GPU、编译工具、驱动与配套 Sanitizer Runtime**；不可混用不同版本开关并据此推断通用结果。完整命令归各厂商专题，不在主研究再维护可漂移的第二份命令块。

厂商工具检测成功只说明该厂商工具在所选条件下报告了错误；它不等于本仓库自研 GPU Checker 已实施。设备命中、仅有 Host API/CPU ASan、未知覆盖、工具失败及 `GPU_NOT_RUN` 都应保留独立状态。


<a id="paper-evidence"></a>

## 5. 核心论文、原理与效果证据

### 5.1 论文在本轮评审中的作用

cuCatch与CuSafe用于比较软件Memcheck实现；其他研究只补充检测目标或测试方法。机制直接进入原有§10～12，原始全文位置、特殊参数和证据分歧集中在[论文笔记](research-notes/papers.md)，本章只回答研究结果能支持什么决策。

### 5.2 cuCatch

PLDI2023原型证明编译传播、Bounds和有限Tag可在特定GPU/工作负载上取得较低开销；不是独立于Allocator/驱动的纯Pass。过程内Root恢复、240直接索引与15种Shadow Tag等条件决定具体覆盖，详见§12.7。公开资料没有给自研移植行数或人月。[P01]

### 5.3 BARRACUDA

其从Fat Binary提取PTX并在JIT前插桩、记录访问/同步后进行HB分析。用于理解事件采集和竞争分析分工，不能充当任意最终机器码重写器或当前ISA完整同步模型。[P02]

### 5.4 HiRace

其状态机、Scoped Clocks和原子更新是§15中规则同步场景的候选方法；同步子集、状态容量和粒度决定保证。Race指标不与Memcheck空间/时间指标混排。[P03]

### 5.5 CuSafe

USENIX Security2026原型把指针编码与带内精确Bounds结合，并用栈Epoch和随机VA增强时间检查。它减少部分独立元数据维护，却增加对象布局、地址资源和兼容约束；详见§12.8。作者原型LOC及试验集检出不是本项目完整产品成本或安全证明。[P10]

### 5.6 SuperCollider

重复读取/随机延迟与地址碰撞观察用于竞争检测，仍有同值、ABA和观测窗口限制。此处保留作独立Racecheck的算法对照，实验数字不参加六路线Memcheck排行。[P08][P09]

### 5.7 基准与输入覆盖

GMSBench的149个CUDA用例可补充分空间/对象/时间的测试分类；SCuBA提醒输入依赖和大分配内部逻辑边界。二者都是指定版本研究材料，本文未在目标设备复现；不同Setup/触发/适用分母不能合成一个总体准确率。[P06][P07]

<a id="paper-performance"></a>

### 5.8 量化效果：哪些数字可以比较

| 来源与模式 | 作者报告/可核实结果 | 实验口径及排除项 | 本次采用的结论 |
|---|---|---|---|
| cuCatch默认，PLDI2023 | 176个workload，几何平均1.19×，即增加19% | RTX2080Ti；Samples/PolyBench与独立Kernel的计时口径不同；分配拦截与BST/Shadow管理不计，独立Kernel不计Launch；仅5项达到240个活跃分配 | 有编译优化潜力；不是目标端到端19%承诺 |
| cuCatch Shadow-BB-only | 几何平均1.25×、最大3.22× | 同论文专门禁用直接索引收益的模式 | 可用于分析路径代价；**3.22×不能移给默认模式** |
| cuCatch空间/时间用例 | 作者表内40/56检出 | 人工用例；直接索引、概率Tag和未支持路径条件不同，部分竞争工具项是推估 | 用来设计反例，不能当成厂商总体检测率 |
| CuSafe，USENIX2026 | 作者报告套件44项；普通程序时间平均增加13%（1.13×）、最大83%（1.83×），LLM另计吞吐 | RTX5090 32 GiB、CUDA12.8、Driver570.144、LLVM21；每项五次；设备事件计时不含初始化和Host/Device拷贝；跨项均值类型及运行时均值分母未明确 | 记为作者报告；不与cuCatch跨平台比较快慢 |
| CuSafe的LLM | 吞吐平均下降11%，LLaMA3下降18% | 单独吞吐指标，非运行时慢速倍数 | 与前行分列，不能把吞吐降幅直接称运行时开销 |
| CuSafe安全程序 | 作者33/33检出 | 15空间＋18时间的自建程序；cuCatch等若干对照按文献推估而非重跑 | 不声称共同平台证明所有竞争工具劣于CuSafe |
| AMD GPU ASan机制文档 | 部分工作负载可能3×或更高 | 文档经验描述，无共同样本/完整统计协议 | 提醒成本风险，不作为与论文同条件的均值 |
| NVIDIA NV-R/NV-C、Intel产品、自研B1/B2/D1 | 未取得可用于该比较的统一同条件性能数据 | 官方配置与私有实现不同；自研GPU_NOT_RUN | 保留未知；用§17/§19协议采集 |

数据依据[P01]（§5～6）、[P10]（§6）、[A01]与[N02][N04]。慢速倍数r=T检查/T基线，额外开销为(r−1)×100%；两者不能混用。

**原文分歧不能消失在表格里。** CuSafe正文/摘要称44项，但Table4分类数19＋19＋6＋2合计46；正文/摘要使用约16.5 MiB与0.3%，Table6平均格为0.5MB/0.01%，统计范围未解释。结构上可确认固定栈Epoch数组16.5 MiB＋每对象8 B头，其他占用另计，故不采用“0.3%”作预算保证。其对Compute Sanitizer的超时剔除与单项数字口径也有不清处，本稿不据此建立工具排行榜。[P10]

任何“某方案比另一方案快”的结论都须固定支持集合、输入、GPU、编译/工具版本、并发模式与计时范围；同时报告未支持、超时和失败项。公开论文只提供可检验的机制线索，目标设备的性能排序仍待测量。

<a id="memory-model"></a>

## 6. 内存检查的基本模型

### 6.1 地址有效性：shadow／redzone

经典 ASan 将应用内存压缩映射到 shadow，一个 shadow 字节描述一组应用字节是否可访问；分配器以 redzone 和释放后的 poison 提高越界、UAF 的可见性。[L02]

常见模型可写为：

~~~text
shadow_address = (application_address >> 3) + shadow_offset
~~~

1:8只是基础Shadow映射比例，不是完整显存成本；对象描述符、报告缓冲及资源管理也要计入。

**【工程建议】** 自研实现应把有效性检查设计成对整个访问区间的判断。只检查首字节或最后一字节，都可能漏掉跨 granule、跨页、宽向量访问中间的无效部分。常量长度采用可证明正确的展开，动态长度走区间逻辑。

经典的单 shadow byte 适合表达 8 字节 granule 中的全有效、全无效或有效前缀；如果自定义 allocator 在同一 granule 内放置不规则子对象，原始编码无法同时表示任意有效位分布。可选方案是调整对齐／redzone、增加精细位图，或使用独立的对象／子对象表；三者会改变布局、内存成本或检查成本。

### 6.2 为什么落入另一合法对象可能漏检

考虑两个 live 对象：

~~~text
A: [0x1000, 0x1040)
B: [0x1040, 0x1080)
指针来源：A
实际访问：0x1048，宽度 4 字节
~~~

地址查询会得到“0x1048 位于合法 B”；shadow 也可能判定这 4 字节可访问。但从 A 导出的访问已经越界。**要判断它属于 A 的错误，检查器必须保留 A 的身份。**

这个示例是地址模型说明，不是鼓励依赖 C/C++ 未定义行为的源码测试。实际测试要确认目标指令确实存在，没有被优化器消除或转化。[L05]

### 6.3 对象 bounds：采用防整数溢出的检查

假设已经获得正确对象的 base、length，访问地址为 addr、长度为 n：

~~~text
if n == 0:
    无实际访存，按对应指令语义处理
else:
    valid = addr >= base
         && n <= length
         && addr - base <= length - n
~~~

先确认减法前提，避免直接计算 addr+n 或 base+length 时溢出。该条件只证明访问落入已指定对象；“指定对象是否正确”由 provenance 提供，不能用当前地址反查出一个对象后再循环论证。

对齐检查应独立进行：有的指令允许非对齐访问，有的指令要求严格对齐，不能一律按数据类型大小判错。

### 6.4 时间安全：身份必须随指针保留

~~~text
时刻 1：对象 A 分配于地址 X，身份 (id=41, epoch=7)
时刻 2：保存旧指针 p = X
时刻 3：A 被释放
时刻 4：对象 B 在 X 创建，身份 (id=92, epoch=1)
时刻 5：通过 p 访问 X
~~~

仅有地址 X 时，p 与合法指向 B 的新指针不能区分。查询“X 当前 generation=1”没有提供 p 原本期望的 generation。

**【工程建议】** D2 的检查至少需要“指针预期身份”与“对象当前身份”两侧信息。可以通过显式 fat pointer、隐藏参数／寄存器、指针 shadow 或 tag 携带；每种方法都必须定义 spill、reload、memcpy、atomic pointer、跨函数和外部接口的传播规则。

单纯查当前Allocation状态不能解决地址复用后旧指针身份丢失；D2必须保存并比较指针的预期对象身份。

### 6.5 Tagging 的概率属性

若有 b 位独立均匀 tag，且新旧对象的 tag 独立重选，单次错误恰好 tag 相等的概率约为 1/2^b。这只是简化模型；保留值、tag 分配策略、对象重用相关性和 wraparound 都会影响结果。

因此必须说明 tag 位宽和碰撞条件，不能把随机 tag 方案写成确定性、无限生命周期的 UAF 保证。若需要强保证，可使用足够宽的对象 ID／epoch 和受控回收协议，但会增加传递与存储成本。HWASan 文档是理解 granule、tag 和尾部小对象处理的入门材料。[L04]

### 6.6 Initcheck 与值级 MSan 不相同

“内存是否被写过”可以由初始化 bitmap 回答；“这个被写入的值是否仍含未定义来源”需要在寄存器和运算间传播定义性状态。

例如，从未初始化的 x 读出数据后写入 y：简单 write bitmap 会把 y 标为已写，值级 MSan 仍可能认为 y 的数据未定义。项目第一版宜明确命名为“未初始化内存读取检查”，不要宣传成完整 MemorySanitizer。

### 6.7 Racecheck 与 Synccheck 不相同

竞争检查需要判断冲突访问之间是否存在相应同步关系。同步检查则验证 barrier 本身的参与者、mask、phase、等待或退出是否符合指令语义。

barrier 用法合法的 kernel 仍可能存在访问竞争；没有观测到竞争也不能证明没有 barrier 死锁。二者应共享事件格式，分别拥有分析状态和验收集合。

<a id="gpu-semantics"></a>

## 7. GPU 特有的语义和实施难点

以下是**自研实施要求**；LLVM 指令语义和目标地址空间定义是规则来源，具体数据结构由项目选择。[L05][L06]

### 7.1 访存清单必须覆盖最终执行语义

| 指令／路径 | 应观察的内容 | 常见漏项 |
|---|---|---|
| scalar load／store | 地址、字节数、读写权限、活动 lane | 未插桩目标 intrinsic |
| vector load／store | 每 lane 的向量长度、对齐和跨界范围 | 只检查一个元素 |
| gather／scatter | 每个启用元素的独立地址 | 把非连续地址当连续区间 |
| masked／VP 操作 | SIMD lane mask、vector element mask、有效向量长度 | 对屏蔽元素求值造成额外 fault |
| atomicrmw／cmpxchg | 原子访问范围、成功／失败路径和 scope | 将原子拆成非原子访问；失败 CAS 的写语义 |
| memcpy／memmove／memset | 源／目标区间、长度、重叠语义 | lowering 后新生成操作未覆盖 |
| async copy／DMA／bulk tensor copy | 描述符、源目标范围、提交和完成时刻 | 只检查普通 load/store |
| image／texture／surface | 坐标、层、格式、边界模式与资源身份 | 将 clamp 等合法语义误报为裸地址 OOB |
| matrix／tensor 指令 | 隐含内存操作、descriptor 和异步依赖 | 只看显式寄存器操作 |
| backend spill／frame lowering | 新增 scratch 访问及对象映射 | IR Pass 完成后出现未覆盖的最终访存 |
| inline assembly／外部库 | 已声明语义或运行时补充 | 默认为完整覆盖 |

每一个“未支持”都必须进入 module capability manifest 和最终报告。测试中遇到未知指令时宁可明确标记覆盖缺口，也不要输出无条件的 clean。

### 7.2 两种 mask 不能混在一起

一个 wave 中可能只有部分硬件 lane 活跃；每个活跃 lane 内的一条 LLVM 向量指令又可能只启用部分向量元素。

检查参与条件应概念上取：

~~~text
participates(lane, element) =
    logical_lane_active(lane)
    && instruction_predicate(lane)
    && element_mask(lane, element)
    && element < effective_vector_length
~~~

whole-wave／quad-wave 执行时，当前硬件 EXEC 可能不等于应用的逻辑参与集合，必须保留后者。被屏蔽元素不得触发额外应用／shadow 访存，也不能让只存在于该元素中的 poison 驱动新增分支或报告；安全的纯地址计算可以存在。实际 lowering 不应随意把检查 hoist 到控制流外。

### 7.3 SIMT 中的锁和失败路径

在 GPU 上增加 per-address spinlock 并不自动安全：一个 lane 持锁，其他 lane 自旋，若调度或分支执行方式阻止持锁 lane 前进，检查器可以制造死锁。

优先考虑无锁／有限重试、warp 聚合和按 launch 分片的结构；必须用实体GPU验证前进性。即便使用 CAS，热点竞争仍会显著改变调度，不能把“无显式锁”解释成“无性能或进展风险”。

### 7.4 优化器与插桩时机

早期 IR 保留源对象信息，但之后的合并、向量化、地址空间转换和 intrinsic lowering 可能改变实际访存。很晚的插桩接近硬件，却已失去一些对象语义。

建议采用三段式：

1. 在对象信息尚可用时收集／传播语义。
2. 在访存 lowering 边界产生稳定的检查描述或 pseudo。
3. 在寄存器／指令语义明确的阶段展开，并在最终机器码上审计覆盖。

插桩会增加寄存器活跃区间与分支。必须防止自己的元数据访存被再次插桩，避免不合适的属性使检查被优化掉，也避免不必要地禁止全部优化。

### 7.5 检测错误后如何继续

**推荐第一版默认选择可控地终止 kernel 或 context，并保留首个有效诊断。** 具体粒度由现有固件／驱动能力决定。

若提供 recover 模式，需明确：错误 store 可以被屏蔽，错误 load 可能返回替代值，但后续结果不再具有原程序语义；让单个线程提前退出可能使其他线程永远等待 barrier。不得为“多报几个错误”隐式承诺结果可信。

若硬件不能在错误访存前阻止操作，工具只能报告已发生错误，并说明可能已经破坏应用或检测元数据。

### 7.6 真机检测与可观测性边界

Sanitizer 的判定必须运行在**实体GPU上执行的检查指令**中，或由实体GPU已具备的硬件检查机制产生。IR审计、CPU侧静态测试和驱动故障注入均不能独立证明设备内存访问已经被检查。真实执行需保存设备型号、ISA/module hash、访问宽度、lane/element mask、context、queue、PC及source map，并明示检查器覆盖缺口。
<a id="common-architecture"></a>

## 8. 跨组件公共架构与接口

### 8.1 三组件协作架构

~~~mermaid
flowchart TD
    C["编译器与设备检查库"] --> B["目标代码与manifest"]
    B --> U["UMD用户态资源和命令"]
    U --> K["KMD映射提交与故障接口"]
    K --> G["GPU执行与检查库"]
    U --> M["对象与生命周期元数据"]
    M --> G
    G --> R["诊断缓冲与fault"]
    R --> K
    K --> U
    R --> U
    U --> T["用户态报告与CI"]
~~~

UMD负责应用逻辑对象，KMD负责其所管理的底层资源与执行契约；二者通过规范对象、BO/映射和命令ID关联，不能把每个用户态子分配当作一个新的KMD BO。设备检查可以通过诊断缓冲返回，硬件fault则经过KMD；两类错误通道需要关联和去重。


### 8.2 对象描述符：示意协议

**唯一的对象元数据字段/ABI 扩展入口**是[通用组件指南 §1.1](real-gpu-components-implementation.md) 的 `SanObject` / `SanAccessEvent` / `SanReport`。Allocation ID/Epoch、逻辑 Base/Size、Owner/State 属于基础信息；Suballocation/View、Backing BO、Mapping/Peer、权限、Allocation Site 是可选扩展。**同一 BO 或 GPUVA 不能自动代表同一不可复用的逻辑对象身份。**

第 17 章的 **32 B 热描述符与 64 B 完整诊断描述符**保留为显存预算示例，不表示目标 ABI 的真实 `sizeof`；最终字段、Alignment、版本和元数据缓存必须由实际设备实现验证。


### 8.3 统一事件集

记录逻辑Alloc/子分配、Free生效、Map/Unmap、Copy/Memset完成、Submit/Complete/Fault、Barrier/Atomic、Module Load/Unload和终止事件。用Operation ID把高层请求与底层子调用关联，分配身份、映射身份和执行身份分开；一次API引出的多层回调不能登记为多个分配。事件真值及接口缺口集中在§8.7，字段和代码只维护在[组件指南](real-gpu-components-implementation.md)。

### 8.4 异步分配／释放是实际关键路径

“Host 已提交 free”“free 在设备命令序列中开始生效”“释放及相关观察已经完成”是三个不同节点。以下是针对流序分配／释放的建议状态机；自研 API 必须另行明确其精确生效契约。

~~~mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Live: 分配命令生效
    Live --> FreeQueued: 释放命令进入依赖图
    FreeQueued --> Dead: free开始生效
    Dead --> Retired: 释放完成且相关观察结束
    Retired --> Reusable: 元数据安全回收
    Reusable --> Pending: 新身份分配
~~~

**FreeQueued 不等于所有队列立刻不可访问；Retired 也不是应用访问才开始失效的时刻。** 检查必须结合访问命令与 free 命令的依赖关系。同队列排在 free 之前的合法访问仍可完成；跨队列缺少依赖则可能产生生命周期竞争。CUDA 流序分配规范要求所有使用先于 free 操作开始，free 开始后的访问非法；不能等到释放完成或物理内存实际复用才判定 UAF。[N08]

图中的 Retired／Reusable 表示检查器可以回收相关描述符、历史及报告资源的阶段。实际存储复用可以有独立策略，但新对象必须获得新身份，旧身份必须按检测承诺保留；映射尚在或物理页未重用，都不证明旧对象仍可访问。若平台不能观测精确开始时刻，应依靠队列依赖、执行侧标记或明确收紧支持范围，不能用 Host 时间戳猜测。S20 给出跨队列的错误及合法对照。

实现要点：

- 对异步 API，Host 返回不等于设备命令完成；元数据 poison／unpoison 必须按设备命令顺序可见。同步 API 则使用其实际完成保证。
- 事件等待形成跨队列依赖；不能以 CPU wall-clock 顺序代替 happens-before。
- 设备尚在执行时，不应原地回收它可能使用的描述符、模块 debug map 或报告缓冲。
- graph capture 保存可重放依赖；每轮 replay 使用独立执行实例／launch ID。仍 live 的对象跨 replay 保留 allocation identity；只有 allocation/free 节点建立新生存期或地址复用时才更换 allocation epoch。
- 不支持 device-side launch 时明确标记；支持时不能依赖每次 launch 都经过 Host 回调。


### 8.5 指针参数与外部 ABI

公开Kernel参数可以保留裸指针，用受控Sidecar或私有Launch Descriptor传元数据；但元数据必须来自可信对象引入点/生产方的传播或显式注册。UMD只拿已经偏移后的裸地址查询当前Live对象，不能证明它原本来自哪个对象，更不能恢复旧Epoch。

顶层参数也不是全部可达对象集合：设备内Pointer数组、结构体、间接加载、外部函数和嵌套容器都可能继续传指针。D1/D2需声明闭合的存取/调用范围；来源未知时记录降级。元数据发布可以建立工具内部依赖，但不得自动补上应用缺失的跨队列数据依赖，否则会改变待检程序并掩盖生命周期或竞争问题。

### 8.6 错误协议与覆盖状态

最小报告建议包括：checker、错误类型、device/context、launch/queue、module build ID、原始 PC、source location、线程／block／lane、地址空间、访问宽度、对象范围／身份、allocation/free site，以及是否截断或降级。

至少区分以下结果：

| 状态 | 含义 |
|---|---|
| CHECKED_NO_ERROR | 已声明覆盖的范围内，本次执行未报告错误 |
| ERROR_DETECTED | 检查器确认错误，保留具体证据 |
| PARTIAL_COVERAGE | 存在未插桩模块、未知指令或丢失来源 |
| TOOL_RESOURCE_EXHAUSTED | shadow、报告或元数据资源不足 |
| TOOL_INTERNAL_ERROR | 检查器／协议自身失败 |
| APPLICATION_FAILED | 应用失败，但未必能归因于已检测内存错误 |

日志达到打印上限与检查停止是两种不同状态。可以限制详细报告，但必须保留总数、丢失计数和“是否仍在检查”的标志。

<a id="component-contracts"></a>


### 8.7 编译器、KMD、UMD的支持契约

**必须支持**是方案成立所需契约；**必须新增**是现有平台尚缺的实现，两者分开审计。Compiler/Device执行目标检查，UMD管理逻辑对象与命令，KMD实施底层映射、完成与故障。Linux DRM/GPUVM、Fence和Reset文档提供职责参照，并不证明目标平台已有这些能力。[K01][K02][K03]

| 事件/真值 | 责任和边界 | 需要留存的验收证据 |
|---|---|---|
| 逻辑对象、Pool/View | Allocator/框架提供子对象，UMD登记；KMD的BO不理解Tensor切分 | 同BO多子对象、失败分配、子对象Free和未登记语义 |
| GPUVA/权限/Backing | KMD实施Map/Unmap，UMD维护逻辑关联 | 正常/失败映射、初始化可见性、别名与重映射顺序 |
| Free生效与资源退休 | API定义逻辑失效点，UMD将其接入设备命令；Fence只证明对应操作完成 | Free前合法访问、Free后非法访问、跨队列缺依赖、旧描述符读者退休 |
| 真实访存与覆盖 | Compiler/Stub提供活动Mask、宽度、空间和原PC；KMD不提供常规全访存流 | 原始/插桩ISA、正反例设备报告、未知Opcode/来源统计 |
| 提交、完成与失败 | UMD关联Launch与Job；KMD/设备返回接受、完成或错误 | 提交失败、正常/错误Fence、Reset后旧通知和Context状态 |
| 停止与回收 | 设备/固件/KMD提供实际停止范围，UMD消费终态 | Queue取消、运行中Dispatch/Context停止、设备Reset分别验证；报告可读且无残留DMA |
| 模块与诊断 | Compiler/Loader绑定Code Hash、ISA、ABI、PC Map和覆盖 | 并发加载卸载、Patch Cache失配、报告解析晚于执行完成 |

Linux调度器的Queued Cancel与运行中停止不是同一能力；请求Abort不等于已停止。只有收到对应终态证据，才能完成失败Fence或回收相关资源。[K01][K02] DMA-BUF缓存一致性操作也不等于设备之间的互斥或完整Happens-Before。[K03]

**身份去重。** Allocation ID/Epoch表示逻辑生存期，BO表示Backing，Mapping ID表示映射；Context/地址空间限定数值地址的解释。一个BO可有多个子对象和VA别名，导入并不自动增加拥有权，Host指针与GPUVA不能直接互查。若扩展到虚拟化/外部设备，需要显式地址域和映射证据；没有证据的关联标为Unknown，不影响本轮单GPU显式分配基线。[K04]

**平台记录只有三种结论：** “已联测可复用”“拟复用待验”“确有缺口”。最后一种写明缺失API、固件/硬件前提及受阻模式，再按§16.8计增量。不能因接口名字存在就记满足，也不能默认每条路线都要新增KMD代码。

### 8.8 各生产组件的真实GPU可执行修改代码

Compiler/LLVM、Device Runtime、UMD Allocator/Loader/Binary Rewriter、KMD、Firmware、实体 GPU RTL 与 CLI 的**唯一完整伪代码、通用 ABI 和模块职责矩阵**统一见[逐组件实施指南](real-gpu-components-implementation.md)。本章 §8.7 说明事件真值、跨 Guest/Host 地址域和 KMD 能力缺口的判定依据。

**纯 C Runtime Binary 路线**可不修改生产 Compiler；UMD 自行 Decode/Patch/Relocate。**KMD** 在既有映射、完成及安全终止契约充分时亦可零新增。均不表示免于源码级契约审计或实体设备验证。

<a id="hardware-deployment-gate"></a>

## 9. 真实GPU部署准入


### 9.1 必须由实体GPU作出访问检查判定

检查须位于真实GPU执行的访存路径并在原操作前生效；IR中有调用、Host API报错或映射成功都不能替代设备证据。未知指令、元数据失败和未运行设备不能算检查通过。

### 9.2 准入门槛与责任

| 门槛 | 需要证明的事实 | 不满足时的处理 |
|---|---|---|
| 指令覆盖 | 实际Opcode/空间/Mask/宽度对应已执行的Check | 拒绝或PARTIAL_COVERAGE |
| 元数据可用 | 可读、已初始化、版本一致，按队列及时更新 | TOOL_FAILURE，不能默认有效 |
| 来源等级 | 声明D1/D2的路径确有可信来源/历史身份 | 显式降级或拒绝所请求保证 |
| 非法操作控制 | 失败后原访问受Guard，停止/跳过符合目标语义 | 不承诺可靠阻止模式 |
| 报告与终态 | PC/线程/对象关联真实，停止有完成证据 | 区分应用失败、工具失败和超时 |
| 资源回收 | 所有设备读者、DMA和报告读者安全退休 | 不提前释放元数据或复用身份 |

### 9.3 研发与验收顺序

P+B先在Global S01/S02闭环，再按来源/时间边界推进D1/D2，有明确Binary缺口才做C/E。F仅在真实Tag/Bounds硬件与相关失效/故障契约存在时可用，不纳入本轮软件选型默认前提。具体实现见[组件代码](real-gpu-components-implementation.md)，验收与排期分别见§19测试矩阵和§20阶段计划。

<a id="software-route-details"></a>

## 10. 路线 B：编译期Addressability实现（B2 Shadow / B1 当前Live区间）

**本章分别评价B1和B2，§11评价NV-R，§12评价NV-C、cuCatch、CuSafe。** 六条路线统一回答：GPU上怎样检查、状态从哪里来、能发现什么、何时漏检/误报、部署条件、成本来源、要开发什么及如何验收。通用设备ABI与代码只维护在[组件指南](real-gpu-components-implementation.md)，本章的算法推导不冒充厂商内部源码。

### 10.1 B1：LLVM插桩与当前Live对象区间查询

**原理与执行链【自研设计】。** 编译器取得实际地址、访问宽度、空间、权限和活动Mask，在原访存前插入设备Checker；Checker在GPU可读索引中定位候选逻辑对象，再用§6.3的无溢出谓词检查完整访问属于同一个Live对象。UMD在分配、子分配、Free和映射事件处维护索引，按设备队列顺序发布；不在每次GPU访问时同步回CPU查表。索引可用有序数组、Radix或其他结构，选择属于目标设计，不声称NVIDIA使用同一数据结构。

**元数据生命周期。** 索引条目至少区分Context/空间、逻辑Base/Length、访问权限和对象状态；BO范围不能代替池内逻辑范围。读者必须持有一致快照或版本化目录，更新后的页先可读再发布根；Free生效后撤销可访问性，等所有相关读者和报告退休后才回收旧索引。仅在Host函数返回时立即删除条目，可能误报同队列中排在Free之前的合法访问。

**效果与能力差异。** 可检查未分配、已失效和整次宽访问跨对象边界。在A/B紧邻且都Live时，要求“同一个对象包含全部字节”能拒绝跨A/B的一次宽访问；但从A派生后完全落入B的地址仍会命中B，因此没有原来源保证。外层大Allocation仍Live、内部片段已失效时，只有外层区间也会漏检；要登记子对象/权限洞，不能默默把这类能力写成已有。

**约束与失败分类。** 必须重编译所声明覆盖的Kernel；最终ISA新产生的访存、Async/Atomic、特殊资源操作另做覆盖审计。没有登记的外部内存应标覆盖未知，不能一概报应用OOB；已登记对象的索引缺页/版本损坏是工具失败。查询通过后也必须遵循对象的设备生效顺序，缓存Live状态不能跨越可能释放/复用的边界。

**性能成本【推导】。** 有序数组的二分查询通常有O(log N)次比较，GPU时间还取决于随机Load、Warp分歧、Cache和描述符布局；换成近似常数层数的目录也不等于常数延迟。描述符空间主要随Live对象数N增长，粗略可按N×EntrySize另加目录与保留旧版本估计。连续宽访问可复用一次范围判定，不能借此跳过不连续Gather的各有效元素。目标时间/内存均未测。

**开发工作量。** 独有工作是GPU索引、并发发布/退休及范围查询；LLVM访存覆盖、报告、分配账本可与B2共用。v1.8只有P+B家族预算（核心25–48、手写测试21–39 kLOC），没有独立B1数字，不能把该范围当成B1/B2各一份报价。主要验证成本在大量小对象、跨队列更新、子对象和查询失败，而非只有二分查找函数。

### 10.2 B2：LLVM插桩与Shadow/Redzone/Poison

**原理与执行链【官方算法参考＋自研适配】。** LLVM识别访存后插入设备Shadow查询；Allocator在对象周围设保护区，并对尚未分配、已释放或显式失效范围Poison；Checker检查实际触及的全部字节，合法才执行原操作。经典一字节Shadow描述8字节应用范围：0为全有效，1～7为有效前缀长度，其他Poison编码表示不可访问。UMD负责对象事件、Shadow映射和更新顺序，设备库负责安全查找和报告。[L01][L02][L03][A01]

**粒度是语义约束。** 例如对象长度10 B且按8 B起点对齐，前两项Shadow为0和2；从偏移8访问2 B合法，访问4 B应拒绝。非对齐宽访问可能触及多个Granule，不能只检查首尾Shadow而漏掉中间Poison。1:16或1:32的单字节有效前缀编码也可表示尾部，但要求每个Granule保持全有效、全无效或单个有效前缀；同一Granule中的“有效—无效—有效”孔洞不能表达。对象起点对齐只是必要布局条件之一，栈、Shared、子分配和权限变化也要满足；对象长度本身不必为G的倍数。

| 比例（1 B Shadow:G B应用） | 理论逻辑Shadow占比 | 需额外评估的代价 |
|---|---:|---|
| 1:8 | 12.5% | 经典前缀编码与布局基础；仍要保护区和驻留页 |
| 1:16 | 6.25% | 更大对齐/保护区、同Granule子对象表达限制 |
| 1:32 | 3.125% | 小对象浪费可能更大，宽访存跨Granule检查并未消失 |

这些是容量推导，不是已验证的GPU产品模式，也不是完整显存占比。降低Shadow比例可能被对齐、页粒度及Allocator额外占用抵消。

**效果与能力差异。** Poison覆盖的头尾越界、Free生效到复用前的UAF、可表达的内部失效区域可检出。有限保护区可能被远距离越界跳过；A派生地址完整落入未Poison的Live B仍可能通过。若紧邻A/B均Unpoison，一次跨两者的宽访问也可能全部有效；B1的单对象包含条件与B2的逐字节条件不能标成数学等价。地址复用后重新Unpoison会使旧裸指针再次看似合法，需D2或其他时间身份机制增强。

**部署与错误路径。** 官方AMD GPU ASan有版本化的设备、xnack+、HMM及配套运行库要求；自研B2可以不依赖可恢复缺页，但Shadow Load之前必须证明元数据地址已映射、驻留并初始化。查不到映射不等于访问合法。三种物理Shadow取舍及Safe Lookup条件见[AMD专题§2](amd-gpu-asan-hardware-fallback.md)；代码唯一入口见[组件指南§3.1](real-gpu-components-implementation.md)。Private/Stack/LDS的厂商实现限制不能推广成Shadow算法在理论上不支持该空间。[A01][A05]

**性能和开发工作量。** 热路径增加地址计算、Shadow读取、分支和寄存器，规则访问可能改善元数据局部性，但取决于GPU缓存与布局。UMD还承担Poison Kernel/Copy、对象事件和映射成本，必须计入端到端时间。AMD旧版机制文档提示部分负载3×或更高，不能作为目标GPU预测。[A01] 独有实现是ShadowManager、保护区布局、顺序Poison与Safe Lookup；§16中的P+B没有再细分B2独立数字。

### 10.3 B1/B2共同的编译覆盖与优化前提

Load/Store只是起点：Atomic/RMW、失败CmpXchg、Masked/VP、MemIntrinsic、Generic Pointer、Async Copy、Target Intrinsic以及后端新增访存都要登记。设备检查库本身不可递归插桩；Compiler还要保留DebugLoc、原始PC映射、资源声明与检查对控制流的支配关系。Shared/Private若改变布局，必须同步修改Frame/Shared资源用量。

优化只能在证明空间、对象状态和实际访问集合未变化时进行；不能因几条指令数值地址接近，就把不同Lane或带孔洞的访问合成无条件范围。插桩改变Register/Spill/Occupancy后，应重新核验最终ISA与性能，而非只看IR中存在一个Check调用。[L05][L06]

### 10.4 组件修改、复用和维护

Compiler/Device负责检查和最终访存覆盖；UMD提供对象真值、索引或Shadow及更新命令；KMD复用GPUVA/权限、提交完成、错误与停止契约，缺失部分才新增。已有KMD契约满足时可以零新增，但仍须联测。二者共用对象/队列/报告底座P，切换查询后端不要求维护两套产品协议。

B1维护成本主要随索引/Allocator并发语义与对象规模增加；B2主要随地址空间布局、Shadow映射和Poison规则增加。选择应由目标接口和代表性负载决定，不能从“无Shadow”推断整体简单，也不能从“有LLVM上游代码”推断无需设备适配。

### 10.5 能力边界与退出条件

| 验收问题 | B1应证明 | B2应证明 |
|---|---|---|
| 基础合法/非法、访问宽度 | 同一对象完整包含，含溢出边界 | 全部活动字节/Granule均检查 |
| A/B紧邻 | 跨界宽访问拒绝，完整落B边界公开 | 全有效跨界可漏检；Poison对照应拒绝 |
| 释放与复用 | 同队列Free前合法、Free生效后撤销 | Poison/Unpoison按实际命令顺序生效 |
| 工具元数据异常 | 缺索引、坏版本、内存不足不记Clean | 未映射/未驻留Shadow不触发无归因Fault后假Clean |
| 性能扩展性 | 对象数、索引层数、分歧与峰值 | 覆盖范围、对象大小、Shadow驻留与更新成本 |

各项在真实GPU上留下错误版、合法版、最终指令和报告；B阶段不把复用后的旧指针漏检伪装成测试通过。性能阈值待第一轮目标实测设定，当前GPU_NOT_RUN。

<a id="route-b-platforms"></a>

### 10.6 来源及选型

B2借鉴LLVM/AMD的Shadow/Redzone；B1是本项目的当前Live区间实现，不是已公开的NVIDIA内部索引。可重编译、LLVM/UMD可控且Shadow布局可实现时优先B2；若Shadow地址资源受限而区间索引可控，可选择B1，仍需证明本章所有边界。Intel设备ASan仅作为B2产品化/运行时协作对照，具体平台与串行限制见[Intel专题](intel-oneapi-device-sanitizers.md)。[L01][A01][I01][I02]


<a id="runtime-route"></a>

## 11. 路线 C：运行时二进制插桩（借鉴NV-R的插桩方式，非独立检查谓词）

### 11.1 NV-R原理、执行链与元数据

**官方事实。** NVIDIA默认Memcheck使用Runtime Binary Instrumentation；其公开Patching API描述访存检查点、PC和参数，但未公开整个私有重写器和对象查询实现。[N02][N03] 因此本项目的区间索引或Shadow后端是自研选择，不能写成已还原NVIDIA算法。

**自研执行链。** UMD取得Module/JIT产物 → 按目标ISA解码并建立控制流 → 为支持的Load/Store/Atomic等插入设备Stub → 修复分支/调用/重定位和资源声明 → 正规加载改写版本 → 在GPU访问前查询已发布的对象状态 → 合法执行原指令，非法记录并进入已验证的停止路径。Stub只写日志而继续执行非法Store，不能实现阻止错误写入的保证。

对象、权限和Free状态仍由UMD维护；Patch并不会自动知道池内子对象或旧指针身份。原始Binary、改写Binary、工具版本、ISA版本和PC映射应绑定；模块卸载必须等待执行与报告解析退休。BARRACUDA的PTX提取/JIT研究可提供事件采集参考，但不是任意最终机器码重写能力的证明。[P02]

### 11.2 检测效果与覆盖边界

NV-R的价值是无需重编译用户Binary即可在受支持指令上检查访问。NVIDIA完整Memcheck公开支持若干空间的OOB、非对齐、部分硬件错误与泄漏；这些还依赖产品特有空间模型、Runtime和错误解析，不能全部归因于一个区间谓词。[N02]

基础当前地址检查能发现落入无效区域的访问，以及对象失效已生效且地址未复用的UAF；A派生地址完整落入Live B、地址复用后旧裸指针均无原身份保证。完整宽访问是否跨对象要看具体查询规则；自研若采用B1可按单对象包含条件保证，不能据此推断NVIDIA私有实现。未知Opcode、未支持模块、外部Allocator或Async指令均须显式降级/拒绝。

### 11.3 部署、性能与维护约束

**部署。** 生产Compiler源码可以零修改；用现有编译器构建设备Patch库与“修改生产编译器”不同。需要完整ISA语义、Decoder/Encoder、合法代码装载、资源核算及停止能力。Opaque代码格式、加密/签名装载、目标不允许重写等限制需在接入前确认，不默认可绕开。

**性能。** 首次Load/JIT增加解码、分析和Patch成本，稳定缓存命中可摊薄启动成本；每次动态访存仍增加Checker与寄存器保存/恢复。代码膨胀、分支、Scratch和Occupancy台阶可能主导成本。缓存键至少包含原Code Hash、ISA、Checker ABI和工具版本；不能为追求复用加载旧语义的Patch。没有可移用于自研的统一NV-R慢速倍数。

**维护。** ISA更新、控制流编码、Scoreboard、谓词、Memory Scope和特殊访存变化都要求回归。一个LLVM可识别的新IR操作不自动让Binary Rewriter支持对应新Opcode。与B1/B2相比，C减少用户重编译要求，却把更多验证负担转移到最终指令透明性。

### 11.4 开发工作量与退出条件

P+C为核心31–60、手写测试27–51 kLOC的既有规划，前提是已有成熟解码/编码/Patch基础；其缺失条件项另列15–35/15–40 kLOC，必须剔除重复的控制流与Patch管理工作。纯Runtime C的生产Compiler新增为0，设备Stub与Rewriter在UMD/工具包交付；KMD仅补确实缺失的正规代码接口、映射、完成与停止。[预算](estimates-v1.8.json)

工作包应分别交付：指令语义清单；控制流/重定位；资源核算；检查后端与对象生命周期；缓存/卸载；原PC诊断；合法程序差分与非法程序报告。评审最关键的退出证据是：改写后合法程序语义不变、活动谓词不变、非法访问被实际Guard、特殊/未知指令不静默跳过，以及错误后没有Barrier挂死或提前回收。

若核心应用都能重编译且没有成熟重写设施，先做B2/D1更符合当前目标；只有闭源/JIT缺口成为明确交付要求，才投入C或以E集成受控子集。

<a id="route-c-platforms"></a>

### 11.5 官方产品与自研对应

[S01厂商指南](S01-vendor-implementation-guide.md)维护实际NV-R使用条件；[组件指南§5](real-gpu-components-implementation.md)维护唯一重写伪代码。本项目参考NVIDIA公开插桩方式、独立实现目标ISA与运行时，**不是**完整复制私有产品。相同插桩点接B1/B2只改变查询后端，不会凭空得到D1/D2原身份。

<a id="identity-routes"></a>

## 12. 路线 D：D1原对象Bounds（借鉴NV-C）与D2时间身份扩展


### 12.1 NV-C/D1：原对象边界与D2时间身份必须分开

**NV-C官方机制。** NVIDIA把编译期Memcheck接入NVCC/PTXAS，分析指针对应的分配Base/Bounds，以捕获来源A越界后落入相邻Live B的访问；分析失败会回到普通分配范围检查。PTX编译器与Compute Sanitizer须属于同一Toolkit，来源定位还需行信息。公开资料没有证明它是可直接取得的LLVM Pass，也没有承诺任意指针存储/外部ABI的确定性时间安全。[N02][N04][N07]

**自研D1执行链。** Compiler在可信对象引入点建立Base/Length或Descriptor引用，随GEP、phi/select、受控调用和存取传播；GPU在访问前将实际范围与该原对象比较，再组合当前生命周期/权限检查。UMD提供对象登记和可信参数Sidecar，不能仅用派生后的裸地址查到Live B，就把B称为原来源A。来源未恢复时可降级B1/B2，但报告必须保留Unknown和实际检查等级。

**效果。** D1的增益是S04原对象越界；它不自动理解大Allocation内部的Tensor View或Struct Field，除非额外提供子对象语义。仅保存原Base/Length也不证明对象仍Live；Free后未复用需生命周期检查，复用后若新旧对象范围完全相同，则需要指针携带的历史身份。增加当前表Generation而不传播指针预期Generation无法解决这一问题。

| 层次 | 空间/时间效果 | 必要信息 | 不可省略的边界 |
|---|---|---|---|
| D1 | 在来源已知范围内拒绝A→Live B | 原对象Base/Length及正确传播 | Call/Load/整数变换的来源丢失；生命周期另查 |
| D2 | 在闭合身份链内区分同地址新旧指针 | 指针预期ID/Epoch与当前对象身份 | Store/Load/Copy/Atomic/ABI、身份回绕和描述符回收 |
| NV-C产品 | 官方规定的Base-and-Bounds增强与回退 | 特定Toolkit的编译/运行协议 | 不等于本项目完整D1/D2设计已交付 |

**性能与开发量。** 来源元数据可能常驻寄存器并复用检查，减少重复对象查找；代价是更长活跃区间、Spill、隐藏参数及Pointer Shadow。须同时测未知来源比例与最终Register/Occupancy，不能因为少了Lookup就断言更快。独有任务包括SSA/MIR传播、ABI、来源失效/降级、对象与时间检查；P+D核心46–89、测试39–74 kLOC是包含受控D1/D2的总规划，**没有D1单独报价**。NV-C厂商未给通用平均性能承诺。[预算](estimates-v1.8.json)

**验收。** A派生指针完整落入Live B必须与B合法指针对照；Call、Phi、Pointer Load、Partial Copy、Atomic Pointer和Unknown各自出具覆盖证据。D2再加入同VA/同Size复用、ABA和Epoch回绕专项。D1受控原型可以先交付，但不能因此把完整D2列为完成。

### 12.2 可供选择的表示方法

| 表示 | 公开 ABI／布局 | 热路径 | 主要局限 |
|---|---|---|---|
| SSA／寄存器 companion metadata | 可保持公开裸指针 ABI | pointer 运算同时传播 identity/bounds | spill、函数边界和内存 reload 需要补充 |
| 显式 fat pointer | 往往改变对象／参数布局 | 信息直接可得 | 库兼容、结构体布局和原子宽度 |
| pointer shadow | 可保留应用槽位大小 | pointer slot 旁路保存身份 | 每次指针存取成本、原子一致性、外部 writer |
| pointer 高位 tag／索引 | 取决于 ISA 和接口 | 可减少 lookup 成本 | 可用位、地址规范化、碰撞、外部传递 |
| 不可变 descriptor handle | 改变内部表示或接口 | 原子发布 handle 后读取一致描述符 | 间接层、回收和兼容适配 |

**【工程建议】** 先以 companion metadata＋受控边界完成 D1。D2 优先针对已有明确结构的 kernel 参数和指针数据结构，再扩展通用语言／库边界。

### 12.3 编译器需要处理的传播

| IR／机器操作 | 身份处理规则建议 |
|---|---|
| GEP／普通 offset 计算 | 保留同一对象身份；改变访问偏移 |
| phi／select | 值与身份一起选择，不能取多个对象的简单范围并集代替身份 |
| pointer load／store | 与 pointer slot 的元数据协议结合 |
| memcpy／memmove | 正确复制对应 pointer metadata；考虑未对齐和部分覆盖 |
| ptrtoint／inttoptr | 只对受控可逆模式传播；一般算术或外部值标为 unknown |
| call／return | 隐藏参数、返回 sidecar、instrumented clone 或边界摘要 |
| addrspacecast | 保留语义身份并验证目标空间，不是只保留数值地址 |
| spill／reload | 数据与 companion 的活跃期、frame 布局和恢复配套 |
| atomic pointer | 值与身份来自同一次原子发布 |
| device allocation | 创建并发布新对象身份；free 触发受控退休 |

不要把所有来源丢失都当错误；对合法低级代码可能无法静态恢复。合理的结果是降低该访问的保证等级，并在覆盖报告中计数；若来源缺失就悄悄用当前地址反查，则不应再声称拥有原对象级保证。

### 12.4 原子指针是高风险边界

考虑另一个线程更新 atomic pointer，检查器分别读取“值”和“shadow identity”。即使两次读取各自都是 atomic，也可能拿到不同写入的组合。仅多读一次指针还可能遇到 ABA。

可选实现包括：

- 硬件支持的加宽原子，将值与身份整体更新。
- 原子发布不可变 descriptor handle；读者通过 handle 取得一致元组，并安全回收旧描述符。
- 对**所有**读写方实施经验证的配对协议；必须覆盖未插桩 writer、部分写、内存序及 SIMT 前进性。

若只能控制部分参与者，不能声称 D2 在该边界保持精确时间身份。使用 seqlock 或锁也不自动解决未受控 writer、ABA 或 GPU 前进性问题。

### 12.5 编译器、KMD、UMD支持与修改

**D1** 的 Compiler 需要在闭合来源范围内传播 Original Object Base/Bounds，UMD 提供对象 Sidecar；**D2** 增加指针 Value/ID/Epoch 的存储、拷贝、Atomic 和 ABI 一致性。KMD 可以提供相同 BO、Mapping、完成与资源退休契约，不能自动从复用后的 GPUVA 反推出旧指针身份。

具体 PointerMeta、Overflow Safe Bounds、[LLVM 外部状态指针](https://llvm.org/docs/LangRef.html#pointers-with-external-state)与 Atomic Identity 的唯一实施说明在[组件指南 §2.1～§6](real-gpu-components-implementation.md)。本章 §12.2～§12.4 保留为何表示方案和一致性协议不同的研究依据。

### 12.6 子对象和 tensor view

大块 GPU allocation 可能被框架划分成多个 tensor、workspace 和临时区。只检查最外层 allocation 无法判断逻辑 view 越界。推荐提供可选的 suballocation／view 注册接口，字段包括 parent、offset、length、stride、element size 和 lifetime。

第一版支持连续子范围即可；非连续 strided view 需要选择逐维 bounds 检查或明确只提供外包范围检查。后者不能声称完整 tensor 索引安全。


### 12.7 cuCatch：编译传播、BST与有限时间Tag

**原理【论文结果，PLDI2023】。** cuCatch在后端恢复访存依赖的Reaching Base，将Base、Size、Tag以寄存器Companion形式传播，访问时检查Bounds与身份，再按要求处理Tag执行原访存；对象元数据来自修改后的CUDA分配/驱动链。BST在此是**Base and Size Table**，不能误写为Binary Search Tree。论文保持内存中指针宽度和公开ABI，但内部仍维护类似Fat Pointer的额外信息。[P01]（§3～4）

| 路径 | 元数据与读取方式 | 能力与限制 |
|---|---|---|
| Global直接索引 | 高8位中的16～255用作240个直接BST索引，取得Base/Size/Tag | 热路径可减少Shadow读取；索引复用仍须处理时间别名 |
| Global Shadow路径 | 1～15为非零随机时间Tag；按地址查Shadow取得完整32位BST索引，32 B区域对应4 B索引 | 仅15种随机Tag，存在碰撞；不是统一“8位随机Tag、1/256漏检率” |
| CPU可见共享分配 | 为兼容CPU地址解释保留无Tag路径 | 不能据此保证地址复用后的UAF |
| Local | 每线程31项BST，5位Frame索引与3位随机Tag | 栈Frame重用有碰撞，表耗尽退回整栈范围；源级栈对象并非全部精确 |
| Shared | 静态符号Bounds；动态Shared按合并池解释 | 动态池的内部逻辑划分仍可能无对象边界 |

**生命周期与效果。** Alloc/Free更新BST和Shadow，Free清空相关身份，论文Runtime还处理元数据可见性与Cache刷新。已知Base的访问可做原对象空间检查；直接索引变化或Tag不匹配可增强时间检出。但其分析主要是过程内：Pointer Load、Call以及含多个Base的运算可能成为新的Root。新Root可能已经是Interior Pointer，重新查地址不必然恢复最初Allocation；Shadow路径丢来源后不能承诺确定性S04。[P01]（§4～5）

**约束。** 不支持任意Struct字段内越界、隐藏Allocator子对象、动态Shared内部划分或任意CPU可见旧指针；原型还排除设备malloc、动态并行、未插桩预编译库等情况。错误Free部分依赖原Allocator报错，不能归为设备Bounds的新保证。论文56项人工用例报告40项检出，含不同条件/概率路径；这个分母不是实际应用错误全集，更不是现行NV-C产品检测率。

**性能。** 直接索引、元数据Hoist、检查合并与循环优化共同影响结果；176个工作负载中只有5个达到240个及以上活跃分配，所以直接索引路径占优的工作集分布是解释性能的条件。论文固定BST与一级目录约32 MiB＋128 MiB，二级Shadow按需增加，对使用该路径的区域约12.5%；局部BST、寄存器和Spill另计。时间数据与排除项集中在§5.8，不与当前NV-C或CuSafe直接排名。[P01]（§6）

**开发工作量【工程判断】。** 移植需重做目标后端Reaching-Base分析、Tag位/去Tag规则、BST/Shadow布局、Allocator事件、元数据可见性、诊断和失败路径。不能只替换一个Checker函数，也不能把私有驱动配合假定为已可复用。论文没有给自研移植kLOC/人月；以§16的D相关工作包逐项审计，不照搬完整P+D数字充当cuCatch报价。

**可评审的退出证据。** 分别验证直接索引和Shadow路径、索引复用/Tag碰撞、Pointer Load后Root变化、CPU可见对象、Local表耗尽、栈重用与未插桩调用。每项报告其实际保证，不能把所有模式合并成“强时间安全”。论文启发NV-C编译方向，但2023实验数据和内部结构不自动适用于现行产品。[P01][N04]

<a id="route-d-platforms"></a>

### 12.8 CuSafe：指针编码、带内精确边界与地址布局

**原理【论文结果，USENIX Security2026】。** CuSafe使用LLVM变换和CUDA API拦截库，把对象放进按2的幂分组的虚拟地址大小类；指针中的6位编码表示对齐/大小类，不是6位生命周期Tag。每对象前置8 B精确长度。设备Checker按编码定位对象头，并检查实际偏移和访问宽度是否位于精确长度内，因此不只是粗粒度大小类Bounds。[P10]（§4～5）

**地址与算术检查。** Global指针把对齐信息编码在VA的[46:41]位，依靠CUDA VMM/别名将选择的有效VA映射到实际物理页，不能假设硬件自动忽略Tag高位。Local/Shared采用编译器可控的Tag表示，并在真实访问或外部调用边界按协议清理。指针算术检查该对象对齐区间所允许的低位Offset之外的高位是否变化，越界时留下无效位，实际Dereference再报告；大小类内的越界由对象头精确长度检查。Global映射只需为使用的物理范围提供Backing，不等于每个VA大小类都占满物理显存。

**空间效果与反例。** 在受支持且算术全覆盖的指针路径上，可检测较大跨对象跳跃和类内精确越界；但它不是所有语言变换下的来源证明。先跨出大小类再算回合法地址会保留无效标记，论文承认可能误报；未插桩的指针变换、兼容性边界也不具备同一保证。Struct Field内部越界没有独立字段Bounds，仍可能漏检。原型不支持动态Shared、Kernel内malloc及JIT，不能把静态对象结果扩展到这些场景。[P10]（§8）

**时间效果。** Global Free清除长度并通过随机VA减少旧地址再次对应新对象的机会；随机空间随大小类变化，约为2^(41−A)，其中大小类为2^A。16 B类有37位选择空间，1 MiB类21位，8 GiB类仅8位，不能宣传成所有对象都有同样的37位时间保证。Local栈使用5位Depth＋5位Generation辨认Frame，支持祖先Frame引用但同Depth的Generation在32轮后会回绕。两类机制都增强检出，却不是无限期确定身份。[P10]（§4.3～4.4）

**成本与部署。** 每个受检访问增加边界解码/对象头Load，算术操作增加不变量检查；大小类对齐影响地址资源和小对象布局，VA/别名管理影响分配路径。论文设计的固定栈Epoch数组为16.5 MiB，每对象再加8 B头，另有对齐/映射和运行期资源。其平均内存百分比存在原文分歧，不能据此宣称自研产品只有0.3%成本。Global带Tag VA可用于某些未插桩库并不表示该库访问也被检查；Local/Shared去Tag兼容同样不等于保留全部检查保证。

**开发工作量【工程判断】。** 原型披露2,964行C/C++，包括LLVM21 Pass与LD_PRELOAD API库，这是原型规模，不包含本项目完整驱动集成、Allocator矩阵、并发/Graph/外部ABI、错误恢复和产品验证。自研必须交付：大小类/带内布局、地址编码和溢出处理、目标VMM别名、Free失效/随机VA策略、栈Epoch、跨模块适配、资源失败与诊断。软件路线本身不要求新RTL；若目标GPUVA、权限或VMM不支持其布局，则须改设计并重新估算，不凭论文“易移植”承诺工作量。

**选择与验收。** 相比cuCatch，CuSafe省去分离BST及应用地址到BST的后备映射，把成本转向对象头读取、对象布局和VA管理；两者都须维护自身有限身份与兼容条件。应先实测小/大对象类、VA耗尽、临时越界后返回、未插桩调用、32轮栈复用、Global地址碰撞及元数据OOM，再决定吸收哪些机制。论文33个安全程序均检出是该测试集结果，竞争方案部分结果由作者按文献估计，不是共同平台重跑；详细性能与证据冲突见§5.8和[论文笔记](research-notes/papers.md)。自研状态仍为GPU_NOT_RUN。

<a id="hybrid-integration"></a>

## 13. 路线 E：编译与运行时检查的混合集成（非新空间算法）

### 13.1 推荐的组合形态

编译可控的代码走 B＋D；不可重编译的模块使用 C 的受控子集补充；无法处理的指令或模块明确报覆盖缺口。所有模式共用 P 的对象、queue 和错误协议。

NVIDIA 的混合模式证明此类产品形态可行，但其官方限制同样说明：混合粒度必须由模块协议保证，不能假定任意函数自动补齐。[N02][N05]

### 13.2 核心接口

每个模块声明：

~~~text
build_id
target_isa_revision
sanitizer_abi_version
checker_set
instrumentation_mode
covered_address_spaces
covered_instruction_classes
provenance_level
requires_runtime
~~~

UMD 依据这些字段选择原代码、已插桩代码、patch 后代码或拒绝加载。相同 kernel 不能同时被编译期与运行期重复插桩。

### 13.3 混合边界的真实保证

| 场景 | 可提供的保证 |
|---|---|
| 全链路已有 D2 身份的指针 | 在支持语义内执行对象与时间身份检查 |
| D1 模块内可恢复来源的访问 | 检查可恢复的原对象 bounds |
| C fallback 模块内的裸地址 | 检查地址有效性和已登记区间 |
| 从未插桩库返回的裸指针 | 来源可能 unknown；可恢复信息必须注明来源及保证等级 |
| 完全未支持模块 | 报告未检查，不能计入 checked kernels |

普通二进制 fallback 不能凭空为一个裸地址恢复它曾经属于哪个旧对象。

### 13.4 编译器、KMD、UMD支持与修改

E 把 B/D 编译期检查与 C Runtime Patch 按模块集成，Compiler 生产能力 Manifest，UMD/Loader 协商 ABI 并保持统一对象与 Report，KMD 复用实际所需的底层契约。**同模块未插桩函数不保证自动获得检查，C Fallback 也不能将 Raw Pointer 升级为 D1/D2。**

完整 Module Mode 选择和 Loader 伪代码在[组件指南 §5～§6](real-gpu-components-implementation.md)，P＋E 预算仍由 §16 与 [预算 JSON](estimates-v1.8.json) 唯一计算，共享底座不重复计费。

<a id="route-e-platforms"></a>


### 13.5 集成成本与验收

E不创造新的空间谓词。独有工作是Module Manifest/ABI协商、统一对象和报告、重复插桩防护、跨模式来源降级及缓存版本。热路径取决于实际被选中的B/D/C，不能给E一个独立于模式分布的平均慢速倍数；另测装载协商和跨边界开销。

验收必须包含编译模块→未插桩模块→编译模块、同模块未插桩函数、JIT变更和版本失配；不能因使用Hybrid就升级裸指针Fallback。P+E核心69–132、测试59–113 kLOC按§16.6去重，公共底座和设备库只计一次。[N02][N05]

<a id="hardware-boundary"></a>


## 14. 路线 F：保留的硬件契约与预算边界

### 14.1 本轮范围

本轮详细选型只比较六条软件Memcheck路线。F1/F2保留为历史预算和组件接口边界，不展开新的芯片方案，也不把软件Tag、GPUVA映射或普通指令Bounds检查称为已有硬件加速。

### 14.2 必需条件

只有实体芯片已有或另行交付Tag/Bounds指令、元数据失效/一致性、准确的错误处理与Context状态，F才成立。Compiler选择真实指令，UMD发布对象，KMD/Firmware接通实际资源；缺失时标UNSUPPORTED/FUTURE_HARDWARE。接口以[组件指南§8～9](real-gpu-components-implementation.md)为准。

### 14.3 重新立项的依据

先用软件实卡数据证明瓶颈来自检查/元数据，给出工作集、Cache命中和释放一致性成本，再决定是否值得芯片投入。不能凭论文平均数字或软件原型推断硬件收益。

### 14.4 历史软件预算的解释

P+F核心23–46、测试22–42 kLOC仅覆盖既有硬件能力的软件使能，来自v1.8；不包含新ISA、RTL、DV、硅片和大幅新增固件。没有相应硬件不能用这个数字报价完整F，也不能据其低于其他软件范围就认定最便宜。

<a id="route-f-platforms"></a>

### 14.5 与本轮决策的关系

首轮P+B/D不依赖F。未来若重开硬件评审，须独立提交能力、收益、交付时间及完整成本；本次不添加新硬件方案计数或预算。

<a id="other-checkers"></a>

## 15. Initcheck、Racecheck 与 Synccheck 的独立设计

### 15.1 Initcheck：先定义“初始化”

推荐第一版 I1 采用 per-byte 定义性位图；每次有效 store 标记写入的 byte enable，读取检查对应范围。copy 传播来源定义性，memset 定义目标，allocation 初始化为未定义。

不同应用字节可能共用一个 metadata word，更新时须使用原子 bit 合并或经验证的聚合协议；否则合法的并发 byte store 也可能使检查器自身丢 bit、制造误报。

Host→device copy 有两个模式：

- **边界假设模式**：把来源 Host 缓冲视为已定义；在报告中声明不会检测 Host 未初始化值。
- **跨主机传播模式**：读取 Host sanitizer 提供的定义性元数据，需要 CPU checker 协作。

async copy 在**完成且满足可见性条件**时才对依赖读取构成定义；不能在 Host 提交时立即设置全部目标位。

原子操作既读又写时需按实际语义检查旧值和新值，失败 cmpxchg 不能被当作成功写入。是否传播值级 poison 另立 MSan 扩展，不混入简单 bitmap 预算。

### 15.2 Racecheck：至少有三类算法选择

| 算法路线 | 保存内容 | 优点 | 主要成本和限制 |
|---|---|---|---|
| Epoch／vector-clock HB | 冲突访问历史、同步关系 | 可给成对访问和较明确的语义判断 | 元数据、复杂同步、scope 与扩展成本 |
| 层次 FSM（HiRace 类） | 压缩状态、线程关系、scoped clocks | 适合 bulk-synchronous 模型，便于设备端实现 | clock 上限、非层次同步和适用模型 [P03] |
| 重复访问／碰撞（SuperCollider 类） | 局部观测状态、延迟及值 | per-address 历史需求小，适合补充并发范围 | 同值／ABA／窗口漏检，较弱的对端归因 [P08] |

**【工程建议】** 第一版选择 shared memory＋明确 barrier／atomic 子集的 HB 或 FSM。之后单独扩展 global、跨 block、跨 kernel、跨 device。若引入重复访问模式，应在命令名和报告中清楚区分概率观测与历史建模。

### 15.3 数据竞争判定的最低语义

对两个访问，至少要判断：是否涉及同一语义内存字节、是否至少一方写入、是否原子或兼容的混合访问、是否存在适用作用域的同步顺序。

不要把 GPU 实际调度中“恰好先执行”当成语言内存模型中的 happens-before；也不要因为两个操作都是 atomic，就忽略对周围非原子数据的发布／获取关系。

实现还需要明确：

- 同线程顺序、warp／block barrier、memory fence、atomic ordering 和 scope。
- kernel completion、stream wait、event、graph 依赖是否构成跨 kernel 边。
- barrier ID 复用、phase、动态参与者、线程退出及 clock wraparound。
- metadata 更新使用的原子不得反过来给应用增加原本不存在的同步证明。

### 15.4 GPU 检查器自己的数据结构

设备内的状态表应按 kernel、空间或对象分片；shared 的 shadow 绑定 block／cluster 身份，全局的 shadow 绑定规范对象及 byte 范围。

对于不同访问粒度，不应把一个 4-byte word 内两个独立 byte 的写入误报成冲突；可以采用 byte mask，或明确更粗粒度带来的保守报告策略。

达到 clock 容量、记录数上限或无法识别同步时，必须输出覆盖降级；不能悄悄复位为“未发生访问”。

### 15.5 Synccheck：验证参与集合和阶段

建议覆盖：

1. barrier 初始化与销毁时机。
2. 到达／等待计数、phase 和 token 对应关系。
3. mask 中声明的线程是否按指令要求参与。
4. 提前退出、非一致控制流和无效 barrier 复用。
5. async copy 的 commit／wait／完成事件及受保护数据的可见性。
6. cluster／cooperative grid 等扩展同步的作用域。

判错必须基于目标指令语义，不能一律要求全部硬件 lane 参与：某些部分 mask 是合法的。超时只证明没有在预算时间内完成，若没有更强证据，不应直接宣称已证明程序死锁。

### 15.6 Initcheck、Racecheck、Synccheck 的生产职责

Device runtime在真实GPU上执行具体检查与状态更新；Compiler负责访存、atomic、mask、barrier、async等目标ISA语义的覆盖；UMD负责对象、copy、launch、queue/event/graph及报告；KMD仅补充真实GPU映射、提交/完成、fault/abort接口缺口。三类检查器须独立报告能力、失败、性能与覆盖。

| 检查器 | Compiler / GPU设备检查库 | UMD/Host Runtime | KMD |
|---|---|---|---|
| Initcheck | 实际load/store/copy定义性bitmap，逐字节并发更新 | allocator、memset/copy和async完成时机 | DMA/执行完成及实际错误契约 |
| Racecheck | 真实GPU的冲突访问、atomic、scope/order及HB/FSM | queue/event/graph跨kernel依赖和CPU共享资源 | 实际fence与共享映射；不能伪造设备内HB |
| Synccheck | 真机参与mask、phase、token和等待状态检测 | launch参数、graph依赖与用户报告 | 硬件真实终止/错误通知契约 |
### 15.7 Init/Race/Sync各自来源与本项目原创边界

- **I1 Initcheck：** 检查目标来自NVIDIA initcheck[N02]及Intel device MSan[I01]；本项目实现逐字节已写状态、copy/async完成更新，**不把位图误称完整值级MSan**。
- **R1 Racecheck：** NVIDIA shared hazard检测[N02]＋BARRACUDA的HB[P02]＋HiRace层次FSM[P03]＋SuperCollider重复观测[P08]作为可选研究机制；本项目按目标ISA真实scope/barrier和调度语义自行选择、适配和验收，不把概率观察当确定性race-free。
- **S1 Synccheck：** 借鉴NVIDIA工具的barrier/sync分类[N02]；具体参与mask、phase/token、GPU指令级容错和报告是目标ISA的自主状态机。GPU设备端Synccheck实际参与集合与故障终止必须在实体GPU验证。

<a id="component-estimates"></a>

## 16. 组件改动与代码量估算

### 16.1 口径和适用前提

**全章沿用[v1.8预算JSON](estimates-v1.8.json)，数字未改；它是未实测设计估算，不是目标源码审计结果、厂商内部LOC或工期承诺。** 单位kLOC，1 kLOC约1,000行新增或实质修改的手写代码；空行、注释、生成代码与未改第三方依赖不计，核心/手写测试分开，同一行不重复收费。区间是规划包络，不是统计置信区间。

基线假设已有LLVM GPU后端、可执行GPU栈与实体设备，KMD映射/完成/错误/所选停止契约足够。Compiler责任包含GPU设备库，但设备库在GPU执行；UMD包含SDK/Host Runtime/Loader/Allocator/CLI的用户态工作；KMD只计确实新增的内核契约。固件、RTL/DV及从零构建GPU栈不在基线内。

### 16.2 公共底座P只计一次

| 默认交付归属 | 核心kLOC | 手写测试kLOC | 包含内容 |
|---|---:|---:|---|
| Compiler/Device | 2–4 | 2–4 | 选项、Manifest、Context、报告缓冲和设备ABI；Pass/后端与Device库各1–2 |
| UMD | 6–11 | 5–8 | 资源/命令/对象接入，Loader、CLI及符号化 |
| KMD | 0 | 0 | 已有契约满足；仍需集成验证 |
| 合计 | 8–15 | 7–12 | 公共对象、队列、报告、版本协议 |

**纯Runtime C例外是责任迁移，不是删掉工作：** P中的工具/设备库与C的Stub都可以归UMD/运行时工具，用现有Compiler构建，不新增生产Compiler源码。历史“Compiler包”数字不能被解读成必须改LLVM，也不能迁移后再算第二遍。

### 16.3 B/C独有实现与工作量

以下均为P之外的增量；B1/B2尚未单独估价。C按实际纯Runtime归属列示，历史分组保存在JSON供追溯。

| 包/责任 | 核心/测试kLOC | 实施工作 | 主要验证成本 |
|---|---|---|---|
| B Compiler/Device | 12–22 / 10–18 | 最终访存覆盖、Mask/Atomic、空间布局；B1区间或B2安全Shadow查询 | 优化、宽/非对齐、异常、Shared/Private与SIMT前进性 |
| B UMD | 5–11 / 4–9 | 索引或Shadow资源、生命周期发布、Context/JIT接入、覆盖报告 | Alloc/Free并发、元数据可见性、映射失败、日志满与资源回收 |
| C生产Compiler | 0 / 0 | 使用既有工具链构建设备Stub，无强制源码变更 | Compiler不新增产品实现；Stub透明性由Runtime测试承担 |
| C UMD/设备Stub | 23–45 / 20–39 | 运行时重写、重定位、资源重算、Patch Cache、原PC报告 | 分支/谓词/调用、并发装卸、未知编码拒绝、原始与改写代码差分 |
| B/C KMD | 0 / 0 | 拟复用既有契约，缺口按§16.8另计 | 既有契约联测已计用户态/设备集成验证 |

B1独有索引发布/并发退休，B2独有Shadow布局/Poison；两者即使共用Compiler前端与对象服务，也不能据家族区间断言成本相同。C增量23–45/20–39由历史设备Patch6–13/5–11与UMD17–32/15–28转列合成，不改总额。

### 16.4 D/E/F独有实现与工作量

| 包/责任 | 核心/测试kLOC（不含P） | 实施与验证重点 |
|---|---|---|
| D Compiler/Device | 28–54 / 23–45 | 原对象/身份传播、Pointer Load/Store/Copy/Atomic、受控ABI；验证Phi、Spill、Partial Copy、ABA、Unknown |
| D UMD | 10–20 / 9–17 | ID/Epoch、可信Sidecar、外部注册、元数据退休；验证复用/跨模块/未插桩Writer |
| E Compiler/Device | 40–76 / 33–63 | B完整＋D独有＋C受控子集、Manifest和边界；防重复插桩、降级与版本失配 |
| E UMD | 21–41 / 19–38 | 模式协商、共同对象表与报告、JIT/Graph缓存及兼容验证 |
| F Compiler/Device | 9–18 / 9–18 | **已有硬件**的指令选择/ABI、Tag/Bounds使能；不含新增ISA/RTL |
| F UMD | 6–13 / 6–12 | 已有硬件资源初始化、失效和诊断；Context切换/资源失败验证 |
| D/E/F KMD | 0 / 0 | 基线契约可复用；确有缺失时选条件包 |

D区间包含受控D1/D2，不是任意C++ ABI、所有外部Writer或无限生命周期的保证。P/D设备检查库与Compiler拆分、UMD/CLI子项均保留在预算JSON，不再在正文重复三套同义表。

### 16.5 从Sanitizer零起点建设的总量

“零起点”仅指Sanitizer，平台基础设施仍按§16.1假设。下表直接按JSON的生产组件口径核对，单位kLOC。

| 责任包（均含P一次） | Compiler核心/测试 | UMD核心/测试 | KMD核心/测试 | 核心合计 | 测试合计 | 核心＋测试 |
|---|---:|---:|---:|---:|---:|---:|
| P＋B | 14–26/12–22 | 11–22/9–17 | 0/0 | 25–48 | 21–39 | 46–87 |
| P＋C | 0/0 | 31–60/27–51 | 0/0 | 31–60 | 27–51 | 58–111 |
| P＋D | 30–58/25–49 | 16–31/14–25 | 0/0 | 46–89 | 39–74 | 85–163 |
| P＋E | 42–80/35–67 | 27–52/24–46 | 0/0 | 69–132 | 59–113 | 128–245 |
| P＋F | 11–22/11–22 | 12–24/11–20 | 0/0 | 23–46 | 22–42 | 45–88 |

纯C的Compiler为0；表中UMD包含P、Rewriter和GPU Patch库全部工作。KMD为0表示无新增源码，不表示无依赖或免验证。F前提是实际硬件已存在/另交付，其总量不适合与完整软件路线直接比便宜。

### 16.6 E共享代码的去重

| 组成 | 核心kLOC | 测试kLOC |
|---|---:|---:|
| 公共P | 8–15 | 7–12 |
| B完整增量 | 17–33 | 14–27 |
| D在B上独有的身份/ABI | 27–51 | 22–42 |
| C受控Binary子集 | 9–18 | 8–17 |
| 跨模式接口/覆盖边界 | 8–15 | 8–15 |
| 合计P＋E | 69–132 | 59–113 |

不能把P+B、P+C、P+D整包相加得到E；对象表、Shadow、设备报告、元数据映射和停止接口只计一次。按Compiler/UMD汇总应与§16.5完全一致。

### 16.7 独立检查器的增量

| 额外能力 | Compiler核心/测试 | UMD核心/测试 | 核心合计 | 测试合计 |
|---|---|---|---:|---:|
| I1逐字节定义性 | 5–10 / 4–8 | 3–6 / 3–6 | 8–16 | 7–14 |
| S1 Barrier/Mask/Phase | 5–10 / 5–10 | 2–4 / 2–4 | 7–14 | 7–14 |
| R1 Shared HB/FSM | 11–22 / 11–22 | 3–7 / 3–7 | 14–29 | 14–29 |
| R1 Global/跨Kernel扩展 | 8–16 / 8–17 | 5–11 / 5–12 | 13–27 | 13–29 |
| 重复访问/碰撞模式 | 6–12 / 5–10 | 2–4 / 2–5 | 8–16 | 7–15 |

基于公共事件和报告ABI的设计范围；不重计P。初始化位图不等于完整MSan，Shared Race不等于跨CPU/GPU通用TSan，概率观测另报精度。

### 16.8 实际缺口才触发的条件工作

| 条件包 | 核心/测试kLOC | 边界和关键验证 |
|---|---|---|
| K-M 元数据映射 | 3–7 / 3–8 | 既有GPUVM有限扩展；映射权限、容量、发布与退休，不从零建内存管理器 |
| K-E 错误/完成证据 | 2–4 / 2–5 | Fault与完成关联、错误Fence、Reset后旧通知；不重复UMD报告格式 |
| K-A KMD停止接线 | 2–5 / 2–5 | 硬件/固件已有停止能力；取消/完成竞态、残留DMA、隔离和回收 |
| K-X 正规代码接口 | 1–3 / 1–3 | 既有加载接口补版本/权限/资源校验；不重计Decoder与重定位 |
| K-F 已有F专属状态 | 3–7 / 3–8 | Tag/Bounds Context与失效，不含K-M/E/A或RTL/DV |
| 缺成熟机器码基础 | 15–35 / 15–40 | Decoder/Encoder/基础语义；剔除C已含CFG/Patch管理 |
| 额外固件停止接线 | 2–5 / 2–5 | 与K-A合计仍为原4–10/4–10；若硬件不能停止，该范围不适用 |

B/D可能需要K-M/E/A，C/E再加K-X，F可能再加K-F。对应包恰好全缺时核心/测试分别为B/D 7–16/7–18，C/E 8–19/8–21，F 10–23/10–26；**不是默认附加费**，按源码证据取缺失子集并去重。

多GPU/IPC/外部内存、通用D2、完整MSan/跨CPU-GPU TSan或新硬件没有可套用的统一增量，应按新增语义单独分解。新ISA/RTL/DV/硅片与大幅固件变化均不包含在软件表。

### 16.9 六路线如何形成可执行任务单

| 软件路线 | 独有交付物 | 能复用的部分 | 实施难点与阶段退出证据 |
|---|---|---|---|
| B1 | 版本化GPU区间索引、权限/子对象解析、退休协议 | LLVM覆盖、对象账本、报告 | 先单队列Global，再扩对象数/并发；证明跨相邻对象宽访问和索引失败 |
| B2 | 保护区布局、Safe Shadow Lookup、ShadowManager/Poison命令 | LLVM覆盖、对象/队列/报告 | 先固定布局Global，再扩空间/Granule；证明驻留/未映射/Free顺序 |
| NV-R | ISA重写器、资源与原PC映射、装卸Cache | 设备Checker后端、对象/报告 | 先有限Opcode/Kernel，再扩控制流；证明合法Binary透明与非法操作Guard |
| NV-C/D1 | 来源分析、Companion与可信ABI、Unknown分级 | B设备覆盖、对象描述符/报告 | 先局部及受控参数，再扩存取/调用；S04与来源丢失成对验收 |
| cuCatch | Base/Tag/BST的目标化实现及驱动一致性 | P和D相关分析，不是假定可复用私有驱动 | 分直接索引/Shadow/Local路径验收，明确碰撞与表耗尽；缺独立移植预算 |
| CuSafe | 带内头/大小类、Tag算术、VA别名/随机分配、栈Epoch | LLVM检查/报告及可用VMM接口 | 先验证目标地址布局/ABI，再扩Allocator；复用/回绕/兼容边界验收；2,964行不能代替产品任务单 |

维护成本同样进入任务单：B1对象索引和Allocator，B2布局/映射，NV-R每代ISA，D1传播/ABI，cuCatch驱动/位编码，CuSafe地址布局/外部兼容。每项指定代码所有者、输入/输出契约、受影响平台、正反例、失败注入及更新回归；只有这些明确后才能估可靠工期。

### 16.10 人力、工期与复核规则

§20保留既有团队/阶段规划，不能从kLOC除以每日产出推导人月；也不能把完整D预算当D1或cuCatch单独排期。M0应先取得目标Opcode/空间清单、Allocator/API数量、模块ABI、驱动契约、可复用源码及设备资源上限，再按“实现＋兼容＋验证＋联调”拆工作包。

汇总规则为：所选生产包（含P一次）＋实际缺失契约＋额外Checker＋另行立项硬件/固件；核心与测试分别相加。B1/B2替代或共享时重新去重，D1/D2按真实边界裁剪，E用§16.6公式。保留未知报价比填入没有依据的精确人月更适合评审；待决项集中在§22，不妨碍本轮选定首个技术验证目标。

<a id="performance-design"></a>

## 17. 性能、显存和可观测性设计

### 17.1 内存成本必须分项

**【推导】** 若应用有 80 GiB 实际需要 byte-addressability 状态，1:8 shadow 本身约需 10 GiB；应用与 shadow 已约 90 GiB，尚未加入任何 redzone 或其他元数据。80 GiB 显存的设备不能把 80 GiB 全部留给应用后，再无成本打开 sanitizer。

| 元数据 | 示意参数 | 推导占用 |
|---|---|---:|
| 1:8 validity shadow | 80 GiB 应用范围 | 10 GiB |
| 逐 byte 的 init bit | 80 GiB，每 byte 1 bit | 10 GiB |
| 紧凑对象表 | 1,000,000 对象，32 B/entry | 约 30.5 MiB |
| §8.2 完整对象表 | 1,000,000 对象，64 B/entry | 约 61.0 MiB |
| 1 bit/16 B 的附加 bitmap | 80 GiB | 0.625 GiB |
| race shadow 示例 | 每 4 B 受监控数据配 8 B metadata | 数据大小的 200%，未含目录 |
| pointer shadow 示例 | 每个 8 B 指针槽配 16 B metadata | 指针密集区域的 200%，不等于全部应用占用 |

表中的数值是参数化计算，不是任何厂商实测。不同 checker 的元数据能否合并需要协议设计；不要默认 validity 与 init 的两个 12.5% 可以自动只付一次。

### 17.2 虚拟地址保留不等于物理显存占用

若完整 48-bit 字节地址空间都采用 1:8 线性映射，理论 shadow 范围是 2^45 字节，即 32 TiB。

这不表示必须分配 32 TiB 物理显存，但要求可以安排相应虚拟布局、稀疏映射或重新选择设计。某些 GPUVA 布局还要容纳 special apertures、runtime 和应用映射，不能只看指针是 64-bit 就认为地址空间充足。

### 17.3 真正需要测的开销

| 指标 | 为什么必须测 |
|---|---|
| kernel slowdown 几何平均、p50、p95、最大值 | 平均值会掩盖寄存器溢出或热点 atomic 的长尾 |
| 应用端到端时间 | 包含 allocation、launch、JIT、copy、日志和同步 |
| compile／link／JIT／module load 时间 | 编译期优化与运行期缓存改变开发体验 |
| registers／thread、spill、occupancy | 插桩可能使资源用量跨过 occupancy 台阶 |
| metadata bytes／access、cache misses | 区分算法与内存系统瓶颈 |
| shadow resident、reserved GPUVA、peak VRAM | 区分地址保留、实际驻留和峰值 |
| Host RSS、日志流量、错误丢失 | 设备够内存不代表主机分析不会耗尽 |
| 检查覆盖、跳过 kernel、timeout | 不能以少检查换来的速度宣称优化 |

### 17.4 优化优先级

推荐依次处理：

1. 编译器可以证明安全的访问，取消冗余检查并保存证明依据。
2. 同一对象、同一支配范围内复用不可变 bounds 和 descriptor 定位；live／epoch 只有在证明不会退休或复用时才可缓存，否则按释放失效协议重新验证。
3. 合并连续访问的区间检查；保持每个活动 lane 和 byte 的正确性。
4. 将冷错误路径移出热路径，使用 warp 聚合报告。
5. 将描述符的热字段与诊断字段拆开，减少不必要的全局访存。
6. 采用按对象／launch 分片，控制热点 shadow 原子竞争。
7. 最后才考虑采样、串行化或缩小覆盖；一旦使用，明确记录模式变化。

“把所有 kernel 串行化后更稳定”可能改变本来要检测的竞争行为。串行模式可以作为诊断辅助，但不能用其无错误结果证明原始并发执行安全。

### 17.5 推荐的性能报告模板

~~~text
版本：compiler / device-check-library / KMD / UMD / firmware / ISA
执行环境：实体GPU原生；设备/固件/驱动版本、二进制hash和真实kernel证据
编译：优化级别、sanitizer模式、debug配置、LTO、编译时间
工作负载：名称、输入hash、数据大小、launch数、并发队列
基线：未插桩；诊断NOP；完整检查（分别记录）
结果：kernel与端到端时间、p50/p95/max、重复次数
资源：寄存器、spill、VRAM峰值、shadow、Host RSS
覆盖：checked/skipped kernels、unknown provenance、unsupported ops
异常：errors、tool failure、overflow、timeout、sampled/dropped事件
~~~

诊断NOP基线只解释编译和资源扰动，不能替代未插桩基线。比较完整工具时还需包括分配、元数据更新和装载成本；论文排除的部分必须在报告中单列。

<a id="detection-scenarios"></a>

## 18. 检测场景与实例分析

本章保留**28项场景的摘要与稳定锚点**；每个案例的触发前提、错误/合法代码、检测算法、设备组件要求、可执行 CUDA/HIP 样例及验收要点，由对应的 [Sxx README](../samples/README.md) **唯一维护**，工具预期以 [manifest](../samples/manifest.json) 为准。S01～S28 是场景编号；M1～M6、I1、R1、S1、A1 是第2章的检测能力编号，与第20章里程碑 M0～M6 不同。

**例子与证据口径。** 各案例 README 的受控 allocator / ISA 伪代码不一定逐行等同可执行样例，也不意味着目标自研 GPU 已具有对应接口或硬件检测能力；真实验收须核对最终 ISA、合法反例和原始 GPU 报告。

未另外说明时，分配成功、对象已发布且具有例中声明的访问权限；用于隔离单一错误的初始化和队列同步必须执行。一个场景里的错误版、修复版和其他变体应独立运行，不能让前一次非法操作污染后一次判断。访问范围统一使用半开区间 `[addr, addr+width)`；实际实现使用防整数溢出的等价比较，不直接信任可能回绕的 `addr+width`。

**本版组件口径。** Device Runtime 执行 GPU 检查，SDK/Host Runtime/Loader/CLI 由 UMD 管理，KMD 承担资源和故障契约。按类别的责任见[§18.29](#scenario-platforms)，逐案例细节见 Sxx README，通用代码见[组件指南](real-gpu-components-implementation.md)。

**厂商核心参照配置、判定语义、工程包与Checker类别是四个不同维度。** 主要厂商是NVIDIA、AMD；基础空间参考配置是NV-R、AMD-B2、NV-C，其中前两者同属当前地址有效性等级。B/C/D/E/F仅是工程实施与预算标签，E不增加独立访存判定算法，F需要实体芯片支持。Init/Race/Sync还需要真正运行的设备事件与算法，不会由Address Shadow自动提供Happens-Before。

### 场景索引

| 场景 | 具体要检查的约束 | 能力或验收属性 | 决定性信息 |
|---|---|---|---|
| [S01](#case-s01) Global 头／尾越界 | 真实访问是否超出有效范围 | M1/M2/M3 | 地址、宽度、边界 |
| [S02](#case-s02) 宽访问跨界 | 首字节合法但末端非法 | M2/M3 | 完整访问足迹 |
| [S03](#case-s03) 对齐错误 | 是否满足该指令的对齐要求 | M2 | opcode 与对齐契约 |
| [S04](#case-s04) A 的指针进入 live B | 地址有效但原对象越界 | M3 | 原对象身份与 bounds |
| [S05](#case-s05) 子分配／view | 外层 allocation 合法，逻辑子边界非法 | M4 | 子对象、维度、stride |
| [S06](#case-s06) Shared 越界 | block 的共享对象边界 | M2/M3 | launch/block 与共享布局 |
| [S07](#case-s07) Private／stack | 栈对象边界与 frame 生命周期 | M3/M5 | 线程、frame、对象身份 |
| [S08](#case-s08) 未复用地址上的 UAF | free 生效后仍访问旧对象 | M5 | 生命周期与 poison 状态 |
| [S09](#case-s09) 地址复用后的 UAF | 新旧裸地址相同但身份不同 | M5 | 指针预期 epoch 与当前 epoch |
| [S10](#case-s10) Invalid free | 传给释放接口的身份／所有权合法 | M6/A1 | allocator、原始基址、owner |
| [S11](#case-s11) Double free | 同一生命周期被重复释放 | M6 | 对象状态和释放记录 |
| [S12](#case-s12) Leak | 约定检查点仍有应释放对象 | M6 | 活跃账本与所有权策略 |
| [S13](#case-s13) 未初始化读取 | 被读的每个 byte 已有定义性来源 | I1 | 初始化位图与复制传播 |
| [S14](#case-s14) Shared race | 冲突访问之间存在合法顺序 | R1 | 访问身份、HB、barrier |
| [S15](#case-s15) Global race | 跨线程／block 的读写与原子语义 | R1 | 规范地址、order/scope |
| [S16](#case-s16) 跨 kernel／queue race | launch 间依赖是否覆盖冲突 | R1/A1 | queue/event 依赖图 |
| [S17](#case-s17) Block barrier／phase | 参与集合、到达代次符合约定 | S1 | barrier ID、phase、线程集合 |
| [S18](#case-s18) Warp／subgroup mask | 调用者与命名参与者满足同步规则 | S1 | lane ID、mask、目标语义 |
| [S19](#case-s19) Async copy | 消费前完成对应复制及同步 | S1/I1/R1 | copy group、完成、phase |
| [S20](#case-s20) Async free | 所有使用都先于释放生效 | M5/A1 | 提交、free 开始与回收证据 |
| [S21](#case-s21) Atomic 越界 | 原子性不能替代空间合法性 | M2/M3 | 原子实际宽度和范围 |
| [S22](#case-s22) Mask／EVL 合法尾部 | 关闭的元素不产生虚构访存 | 合法反例；M/R/I 插桩正确性 | lane、predicate、元素 mask、EVL |
| [S23](#case-s23) 权限／资源坐标 | 只读属性、维度及寻址模式 | M1/M2/M3/A1 | 资源描述符和访问模式 |
| [S24](#case-s24) CPU/GPU 与别名 | 可访问性、一致性和跨处理器顺序 | R1/A1/M5 | 系统 scope、规范对象、映射 |
| [S25](#case-s25) Atomic pointer 身份 | 指针值与身份来自同次发布 | 合法反例；D2 正确性 | 原子配对、发布序号、epoch |
| [S26](#case-s26) 库／JIT 覆盖 | 真实访问是否进入检查范围 | 覆盖完整性 | 模块 manifest、opcode 与 ABI |
| [S27](#case-s27) Graph replay | 重放不能复活已失效的指针 | M5/A1 | 捕获对象身份与执行实例 |
| [S28](#case-s28) 工具资源与终止 | 错误、覆盖不足与工具失败可区分 | 所有 checker 的可靠性 | 丢失计数、资源状态、abort 协议 |

跨页、负偏移、整数回绕、稀疏访问、peer／IPC 及特殊 opcode 等变体归对应案例 README；28 例不是全部 ISA 与语言错误的穷举，扩展时先维护样例与 manifest，再更新本章索引。

S01～S12除另行声明外，读入数据已初始化、输出对象独立有效，单个Kernel仅由指定线程执行目标访问。**所有检测能力必须来自B/C/D/E的真实GPU可执行检查代码或已存在的真实F类硬件。**

<a id="case-s01"></a>

### 18.1 S01：Global 对象头部与尾部越界

**关键判定：** 真实访问是否超出有效范围；需要的核心信息是地址、宽度、边界（M1/M2/M3）。

**唯一详细案例及运行入口：** [S01 错误与合法对照、检测方法、组件职责及验收](../samples/S01/README.md) · [CUDA 完整源码](../samples/S01/cuda.cu) · [AMD HIP 完整源码](../samples/S01/amd.hip.cpp) · [三种厂商参考技术路径](S01-vendor-implementation-guide.md)。

<a id="case-s02"></a>

### 18.2 S02：访问首地址合法，宽访问的末端越界

**关键判定：** 首字节合法但末端非法；需要的核心信息是完整访问足迹（M2/M3）。

**唯一详细案例及运行入口：** [S02 错误与合法对照、检测方法、组件职责及验收](../samples/S02/README.md) · [CUDA 完整源码](../samples/S02/cuda.cu) · [AMD HIP 完整源码](../samples/S02/amd.hip.cpp)。

<a id="case-s03"></a>

### 18.3 S03：范围正确但违背指令对齐约束

**关键判定：** 是否满足该指令的对齐要求；需要的核心信息是opcode 与对齐契约（M2）。

**唯一详细案例及运行入口：** [S03 错误与合法对照、检测方法、组件职责及验收](../samples/S03/README.md) · [CUDA 完整源码](../samples/S03/cuda.cu) · [AMD HIP 完整源码](../samples/S03/amd.hip.cpp)。

<a id="case-s04"></a>

### 18.4 S04：从对象 A 越界，恰好落入仍存活的对象 B

**关键判定：** 地址有效但原对象越界；需要的核心信息是原对象身份与 bounds（M3）。

**唯一详细案例及运行入口：** [S04 错误与合法对照、检测方法、组件职责及验收](../samples/S04/README.md) · [CUDA 完整源码](../samples/S04/cuda.cu) · [AMD HIP 完整源码](../samples/S04/amd.hip.cpp)。

<a id="case-s05"></a>

### 18.5 S05：内存池子分配、granule 孔洞与 tensor view 边界

**关键判定：** 外层 allocation 合法，逻辑子边界非法；需要的核心信息是子对象、维度、stride（M4）。

**唯一详细案例及运行入口：** [S05 错误与合法对照、检测方法、组件职责及验收](../samples/S05/README.md) · [CUDA 完整源码](../samples/S05/cuda.cu) · [AMD HIP 完整源码](../samples/S05/amd.hip.cpp)。

<a id="case-s06"></a>

### 18.6 S06：静态／动态 shared 越界与 block 身份

**关键判定：** block 的共享对象边界；需要的核心信息是launch/block 与共享布局（M2/M3）。

**唯一详细案例及运行入口：** [S06 错误与合法对照、检测方法、组件职责及验收](../samples/S06/README.md) · [CUDA 完整源码](../samples/S06/cuda.cu) · [AMD HIP 完整源码](../samples/S06/amd.hip.cpp)。

<a id="case-s07"></a>

### 18.7 S07：Private／stack 越界与返回后使用指针

**关键判定：** 栈对象边界与 frame 生命周期；需要的核心信息是线程、frame、对象身份（M3/M5）。

**唯一详细案例及运行入口：** [S07 错误与合法对照、检测方法、组件职责及验收](../samples/S07/README.md) · [CUDA 完整源码](../samples/S07/cuda.cu) · [AMD HIP 完整源码](../samples/S07/amd.hip.cpp)。

<a id="case-s08"></a>

### 18.8 S08：Free 已生效、地址尚未复用的 UAF

**关键判定：** free 生效后仍访问旧对象；需要的核心信息是生命周期与 poison 状态（M5）。

**唯一详细案例及运行入口：** [S08 错误与合法对照、检测方法、组件职责及验收](../samples/S08/README.md) · [CUDA 完整源码](../samples/S08/cuda.cu) · [AMD HIP 完整源码](../samples/S08/amd.hip.cpp)。

<a id="case-s09"></a>

### 18.9 S09：同一 GPUVA 被新对象复用后的旧指针

**关键判定：** 新旧裸地址相同但身份不同；需要的核心信息是指针预期 epoch 与当前 epoch（M5）。

**唯一详细案例及运行入口：** [S09 错误与合法对照、检测方法、组件职责及验收](../samples/S09/README.md) · [CUDA 完整源码](../samples/S09/cuda.cu) · [AMD HIP 完整源码](../samples/S09/amd.hip.cpp)。

<a id="case-s10"></a>

### 18.10 S10：Invalid free——内部指针与错误所有权

**关键判定：** 传给释放接口的身份／所有权合法；需要的核心信息是allocator、原始基址、owner（M6/A1）。

**唯一详细案例及运行入口：** [S10 错误与合法对照、检测方法、组件职责及验收](../samples/S10/README.md) · [CUDA 完整源码](../samples/S10/cuda.cu) · [AMD HIP 完整源码](../samples/S10/amd.hip.cpp)。

<a id="case-s11"></a>

### 18.11 S11：同一分配实例被释放两次

**关键判定：** 同一生命周期被重复释放；需要的核心信息是对象状态和释放记录（M6）。

**唯一详细案例及运行入口：** [S11 错误与合法对照、检测方法、组件职责及验收](../samples/S11/README.md) · [CUDA 完整源码](../samples/S11/cuda.cu) · [AMD HIP 完整源码](../samples/S11/amd.hip.cpp)。

<a id="case-s12"></a>

### 18.12 S12：在约定检查点仍未释放的显存对象

**关键判定：** 约定检查点仍有应释放对象；需要的核心信息是活跃账本与所有权策略（M6）。

**唯一详细案例及运行入口：** [S12 错误与合法对照、检测方法、组件职责及验收](../samples/S12/README.md) · [CUDA 完整源码](../samples/S12/cuda.cu) · [AMD HIP 完整源码](../samples/S12/amd.hip.cpp)。

<a id="case-s13"></a>

### 18.13 S13：未初始化读取、部分字节写入与copy传播

**关键判定：** 被读的每个 byte 已有定义性来源；需要的核心信息是初始化位图与复制传播（I1）。

**唯一详细案例及运行入口：** [S13 错误与合法对照、检测方法、组件职责及验收](../samples/S13/README.md) · [CUDA 完整源码](../samples/S13/cuda.cu) · [AMD HIP 完整源码](../samples/S13/amd.hip.cpp)。

<a id="case-s14"></a>

### 18.14 S14：shared RAW、WAR与WAW竞争

**关键判定：** 冲突访问之间存在合法顺序；需要的核心信息是访问身份、HB、barrier（R1）。

**唯一详细案例及运行入口：** [S14 错误与合法对照、检测方法、组件职责及验收](../samples/S14/README.md) · [CUDA 完整源码](../samples/S14/cuda.cu) · [AMD HIP 完整源码](../samples/S14/amd.hip.cpp)。

<a id="case-s15"></a>

### 18.15 S15：global同block、跨block竞争与合法atomic

**关键判定：** 跨线程／block 的读写与原子语义；需要的核心信息是规范地址、order/scope（R1）。

**唯一详细案例及运行入口：** [S15 错误与合法对照、检测方法、组件职责及验收](../samples/S15/README.md) · [CUDA 完整源码](../samples/S15/cuda.cu) · [AMD HIP 完整源码](../samples/S15/amd.hip.cpp)。

<a id="case-s16"></a>

### 18.16 S16：跨kernel／queue竞争与event正确反例

**关键判定：** launch 间依赖是否覆盖冲突；需要的核心信息是queue/event 依赖图（R1/A1）。

**唯一详细案例及运行入口：** [S16 错误与合法对照、检测方法、组件职责及验收](../samples/S16/README.md) · [CUDA 完整源码](../samples/S16/cuda.cu) · [AMD HIP 完整源码](../samples/S16/amd.hip.cpp)。

<a id="case-s17"></a>

### 18.17 S17：block barrier缺失live参与者与phase错配

**关键判定：** 参与集合、到达代次符合约定；需要的核心信息是barrier ID、phase、线程集合（S1）。

**唯一详细案例及运行入口：** [S17 错误与合法对照、检测方法、组件职责及验收](../samples/S17/README.md) · [CUDA 完整源码](../samples/S17/cuda.cu) · [AMD HIP 完整源码](../samples/S17/amd.hip.cpp)。

<a id="case-s18"></a>

### 18.18 S18：warp／subgroup无效mask

**关键判定：** 调用者与命名参与者满足同步规则；需要的核心信息是lane ID、mask、目标语义（S1）。

**唯一详细案例及运行入口：** [S18 错误与合法对照、检测方法、组件职责及验收](../samples/S18/README.md) · [CUDA 完整源码](../samples/S18/cuda.cu) · [AMD HIP 完整源码](../samples/S18/amd.hip.cpp)。

<a id="case-s19"></a>

### 18.19 S19：async copy缺wait、等待了错误批次或phase

**关键判定：** 消费前完成对应复制及同步；需要的核心信息是copy group、完成、phase（S1/I1/R1）。

**唯一详细案例及运行入口：** [S19 错误与合法对照、检测方法、组件职责及验收](../samples/S19/README.md) · [CUDA 完整源码](../samples/S19/cuda.cu) · [AMD HIP 完整源码](../samples/S19/amd.hip.cpp)。

<a id="case-s20"></a>

### 18.20 S20：async free跨queue缺依赖与释放生效边界

**关键判定：** 所有使用都先于释放生效；需要的核心信息是提交、free 开始与回收证据（M5/A1）。

**唯一详细案例及运行入口：** [S20 错误与合法对照、检测方法、组件职责及验收](../samples/S20/README.md) · [CUDA 完整源码](../samples/S20/cuda.cu) · [AMD HIP 完整源码](../samples/S20/amd.hip.cpp)。

<a id="case-s21"></a>

### 18.21 S21：原子指令访问越界——原子性不保证地址有效

**关键判定：** 原子性不能替代空间合法性；需要的核心信息是原子实际宽度和范围（M2/M3）。

**唯一详细案例及运行入口：** [S21 错误与合法对照、检测方法、组件职责及验收](../samples/S21/README.md) · [CUDA 完整源码](../samples/S21/cuda.cu) · [AMD HIP 完整源码](../samples/S21/amd.hip.cpp)。

<a id="case-s22"></a>

### 18.22 S22：尾部 mask／EVL——被屏蔽的非法地址不得产生误报

**关键判定：** 关闭的元素不产生虚构访存；需要的核心信息是lane、predicate、元素 mask、EVL（合法反例；M/R/I 插桩正确性）。

**唯一详细案例及运行入口：** [S22 错误与合法对照、检测方法、组件职责及验收](../samples/S22/README.md) · [CUDA 完整源码](../samples/S22/cuda.cu) · [AMD HIP 完整源码](../samples/S22/amd.hip.cpp)。

<a id="case-s23"></a>

### 18.23 S23：只读对象与 image／surface——检查权限、坐标及寻址模式

**关键判定：** 只读属性、维度及寻址模式；需要的核心信息是资源描述符和访问模式（M1/M2/M3/A1）。

**唯一详细案例及运行入口：** [S23 错误与合法对照、检测方法、组件职责及验收](../samples/S23/README.md) · [CUDA 完整源码](../samples/S23/cuda.cu) · [AMD HIP 完整源码](../samples/S23/amd.hip.cpp)。

<a id="case-s24"></a>

### 18.24 S24：CPU/GPU共享、peer／IPC别名的身份与同步

**关键判定：** 可访问性、一致性和跨处理器顺序；需要的核心信息是系统 scope、规范对象、映射（R1/A1/M5）。

**唯一详细案例及运行入口：** [S24 错误与合法对照、检测方法、组件职责及验收](../samples/S24/README.md) · [CUDA 完整源码](../samples/S24/cuda.cu) · [AMD HIP 完整源码](../samples/S24/amd.hip.cpp)。

<a id="case-s25"></a>

### 18.25 S25：atomic pointer 的值与身份不一致——合法程序也可能被错误插桩误报

**关键判定：** 指针值与身份来自同次发布；需要的核心信息是原子配对、发布序号、epoch（合法反例；D2 正确性）。

**唯一详细案例及运行入口：** [S25 错误与合法对照、检测方法、组件职责及验收](../samples/S25/README.md) · [CUDA 完整源码](../samples/S25/cuda.cu) · [AMD HIP 完整源码](../samples/S25/amd.hip.cpp)。

<a id="case-s26"></a>

### 18.26 S26：未插桩库与 JIT——没有观测到访问不能当作检查通过

**关键判定：** 真实访问是否进入检查范围；需要的核心信息是模块 manifest、opcode 与 ABI（覆盖完整性）。

**唯一详细案例及运行入口：** [S26 错误与合法对照、检测方法、组件职责及验收](../samples/S26/README.md) · [CUDA 完整源码](../samples/S26/cuda.cu) · [AMD HIP 完整源码](../samples/S26/amd.hip.cpp)。

<a id="case-s27"></a>

### 18.27 S27：graph replay捕获已释放对象与执行ID／allocation epoch

**关键判定：** 重放不能复活已失效的指针；需要的核心信息是捕获对象身份与执行实例（M5/A1）。

**唯一详细案例及运行入口：** [S27 错误与合法对照、检测方法、组件职责及验收](../samples/S27/README.md) · [CUDA 完整源码](../samples/S27/cuda.cu) · [AMD HIP 完整源码](../samples/S27/amd.hip.cpp)。

<a id="case-s28"></a>

### 18.28 S28：日志满、metadata 不足与错误终止——检查器必须说明检查是否完成

**关键判定：** 错误、覆盖不足与工具失败可区分；需要的核心信息是丢失计数、资源状态、abort 协议（所有 checker 的可靠性）。

**唯一详细案例及运行入口：** [S28 错误与合法对照、检测方法、组件职责及验收](../samples/S28/README.md) · [CUDA 完整源码](../samples/S28/cuda.cu) · [AMD HIP 完整源码](../samples/S28/amd.hip.cpp)。

<a id="scenario-platforms"></a>

### 18.29 28个场景的真实GPU生产职责与验收映射

本表只说明**跨案例的检查类别与组件责任**，不是第二份逐场景技术规范。各 Sxx 的具体前提、Compiler/Device Runtime/UMD/KMD 需求和错误报告以独立 [案例 README](../samples/README.md) 为准；通用代码接口见[组件实施指南](real-gpu-components-implementation.md)。

| 场景组 | 覆盖案例（本章锚点） | 生产组件核心职责 |
|---|---|---|
| 地址、指令宽度、Mask 与访问权限 | [S01](#case-s01)、[S02](#case-s02)、[S03](#case-s03)、[S06](#case-s06)、[S07](#case-s07)、[S21](#case-s21)、[S22](#case-s22)、[S23](#case-s23) | Compiler/设备Checker按真实访存宽度、地址空间、谓词与资源权限检查；UMD登记边界，KMD保证映射与故障语义 |
| 对象来源、UAF、指针身份和重放 | [S04](#case-s04)、[S05](#case-s05)、[S08](#case-s08)、[S09](#case-s09)、[S20](#case-s20)、[S25](#case-s25)、[S27](#case-s27) | D1/D2保留原对象Bounds/epoch；UMD保障分配、异步释放、原子发布及Graph生命周期 |
| 分配器API与泄漏 | [S10](#case-s10)、[S11](#case-s11)、[S12](#case-s12) | UMD/设备Allocator管理free和所有权并在约定时间快照；Host API错误不计GPU Kernel设备检出 |
| 定义性检测 | [S13](#case-s13) | 设备读写更新定义性；UMD发布Memcpy/Memset及完成依赖 |
| Race与跨处理器Happens-Before | [S14](#case-s14)、[S15](#case-s15)、[S16](#case-s16)、[S24](#case-s24) | GPU真实访存与HB事件，加上UMD/Host Queue、Event、Peer/IPC别名信息 |
| Barrier/Subgroup/Async Copy | [S17](#case-s17)、[S18](#case-s18)、[S19](#case-s19) | GPU真实参与集合、phase、mask、copy group/wait；不能以超时当作精确检测证据 |
| 模块覆盖与工具自身可靠性 | [S26](#case-s26)、[S28](#case-s28) | 未插桩、报表丢失、资源故障和abort要明确标记，不能记为CLEAN |

仅在**真实设备检查指令或硬件机制**执行后方可登记 GPU 检测结论；Host API/分配器错误须另行分类。未知覆盖为 `PARTIAL_COVERAGE`、不支持为 `UNSUPPORTED`，未运行目标 GPU 为 `GPU_NOT_RUN`；任何状态不得自动转为 `CLEAN`。

---

<a id="sample-execution"></a>

### 18.30 CUDA/AMD HIP 真实设备样例与运行入口

**详尽场景、各厂商预期与源代码的唯一维护入口：** [S01～S28 样例导航](../samples/README.md)、[机器可读工具及预期矩阵](../samples/manifest.json)。每个案例 README 均提供 CUDA/HIP 完整源文件和 `--good` / `--bad` 对照；并非所有 `--bad` 都能被两个厂商工具检测，危险路径及不受支持路径由 Runner 按 manifest 控制。

~~~bash
# 仅能在具备对应编译工具、厂商Sanitizer及真实GPU时执行
python3 scripts/run_samples.py --backend cuda --case S01 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S01 --mode both --strict
./scripts/run-all.sh cuda  # 或 rocm；按manifest控制适用/危险的bad
~~~

S19、S23、S26、S28 等**当前可执行程序小于完整概念场景**的限制，均见对应案例 README。真实设备尚未执行自研 GPU Checker，CPU 静态测试通过不能作为其检测证据。

<a id="validation"></a>

## 19. 验证基准与验收标准

### 19.1 分四层验证

| 层 | 内容 | 检查重点 |
|---|---|---|
| V0：编译语义 | LLVM IR、MIR、反汇编和覆盖清单 | 检查没有被消除，新增目标访存可见 |
| V1：最小错误与合法反例 | 每个能力一个确定触发样例及无错对应样例 | 误报／漏报、边界和整数溢出 |
| V2：实体GPU生产驱动端到端 | 正反例在GPU实际执行，KMD真实传递完成与故障 | 不凭Host合成异常推断设备已检出 |
| V3：真实应用与规模 | kernel库、框架内存池、长时间并发 | 性能、内存、时序、诊断可操作性 |

公开 benchmark 用来补充 V1/V3，不取代目标 ISA 的语义验证。[P03][P06]

### 19.2 最小测试矩阵

第18章提供具体种子实例；下表要求把它们扩展为正反例、边界和覆盖退化测试。实例是测试设计输入，是否真正触发及是否检出仍须由目标执行证据确认。

| 类别 | 必须包含的测试 | 合法反例／重要边界 | 对应实例 |
|---|---|---|---|
| Global bounds | 前越界、尾越界、跨页、远距离跳越 redzone | 最后一个有效 byte、零长度操作 | [S01](#case-s01)、[S02](#case-s02) |
| 对象来源 | A 的指针落入 live B | B 的合法新指针 | [S04](#case-s04) |
| 子分配 | 同一个 pool 内两个相邻逻辑对象 | 合法跨 view 范围的显式 API | [S05](#case-s05) |
| Granule | 同 granule 的孔洞和多个子对象 | 编码可表达的有效前缀 | [S05](#case-s05) |
| 宽访问 | 16／32／64 B跨边界、非对齐 | 指令明确允许的非对齐 | [S02](#case-s02)、[S03](#case-s03) |
| Vector／mask | lane掩码、元素掩码、EVL、gather/scatter | masked-off非法地址不得误报／fault | [S22](#case-s22) |
| Atomics | 不同宽度、CAS成功／失败、混合访问 | 不同 scope 下合法同步 | [S21](#case-s21)、[S25](#case-s25) |
| Shared | static、dynamic、跨变量、block身份复用 | 不同 block 的相同偏移 | [S06](#case-s06)、[S14](#case-s14) |
| Private／stack | frame边界、递归／调用、返回后访问 | 相邻合法frame、spill地址 | [S07](#case-s07) |
| Constant／resource | 只读权限、资源描述符、维度 | 合法clamp／border等地址模式 | [S23](#case-s23) |
| Lifetime | free后访问、free排队但尚未生效 | 同queue中free之前的合法访问 | [S08](#case-s08)、[S20](#case-s20) |
| 地址重用 | 同GPUVA旧指针与新指针 | 新epoch正确传播 | [S09](#case-s09) |
| Pointer传播 | phi/select、spill/reload、memcpy、call/return | 指针值与identity成对一致 | [S09](#case-s09)、[S25](#case-s25)、[S26](#case-s26) |
| Atomic pointer | 并发CAS、ABA、value与metadata错配 | 所有writer遵循同一协议 | [S25](#case-s25) |
| API | invalid/double free、失败分配、泄漏 | 对象仍被合法持有／导入但不拥有 | [S10](#case-s10)、[S11](#case-s11)、[S12](#case-s12) |
| 初始化 | byte部分写、copy传播、async完成 | 有序memset和store后读取 | [S13](#case-s13) |
| Shared race | RAW/WAR/WAW、同warp／跨warp | 正确barrier后的访问 | [S14](#case-s14) |
| Global race | 同block、跨block、跨kernel | queue event和发布／获取 | [S15](#case-s15)、[S16](#case-s16)、[S24](#case-s24) |
| 同步 | 无效mask、缺失参与、phase错误、退出 | 合法子组同步和早退语义 | [S17](#case-s17)、[S18](#case-s18)、[S19](#case-s19) |
| 模块混合 | 已插桩→未插桩→已插桩 | 正确标记unknown，不伪造身份 | [S26](#case-s26) |
| 特殊执行 | graph replay、JIT、device launch | 执行实例独立；仍live对象保留allocation epoch | [S26](#case-s26)、[S27](#case-s27) |
| 外部内存 | alias、peer、IPC、host-mapped | 同一对象不同VA不可被重复拥有 | [S24](#case-s24) |
| 工具资源 | 报告满、shadow不足、clock溢出 | 覆盖降级明确且应用不会被隐瞒 | [S28](#case-s28) |
| 终止 | 一lane错误但其他线程等barrier | dispatch/context可控终止 | [S28](#case-s28) |

### 19.3 数据集与规模建议

**【工程建议】** 小用例做确定性诊断；规模用例覆盖 10^3／10^5／10^6 活跃对象或描述符、不同实际数据占用比例、1／多个 queue，以及长时间重复分配／释放。

具体建议包括：

- 算力密集：GEMM、卷积、attention／reduction，观察寄存器与 occupancy。
- 带宽密集：copy、scan、stencil、稀疏矩阵，观察 shadow 带宽。
- 指针／图结构：链表、hash、图遍历，观察 provenance 和 metadata lookup。
- allocator密集：小对象池、频繁free/reuse、多stream缓存。
- 特殊功能：async copy、tensor descriptor、graph和JIT。

100万对象测试可以采用小 allocation 或描述符模型，不必强行申请100万个大 buffer；应同时记录实际显存，以免把 API／驱动的原有限制误认为 sanitizer 回归。

### 19.4 检测率的正确分母

至少分开统计：

- 已生成的错误测试总数。
- 满足setup条件且实际执行到目标操作的测试数。
- checker声明支持的适用测试数。
- 在这些条件下的检出、漏报和合法样例误报。
- 未支持、编译失败、工具内部错误、timeout和未触发路径。

GMSBench 中不同 checker 使用不同适用分母，HiRace 的 code/input 组合也存在“错误未被输入触发”的情况。不能把这类不同口径合成一个总体 accuracy。[P03][P06]

### 19.5 建议验收标准

以下是拟议项目门槛，不是当前已经达成的测量：

1. 对声明支持的确定性错误集，要求全部预期诊断出现；有意概率检测单列，不按确定性要求评价。
2. 对合法反例集，要求无已知误报；所有 suppressions 必须有解释和独立记录。
3. 不允许 unsupported／tool OOM／记录丢失静默变成 clean。
4. 诊断必须至少给出 kernel／PC、空间、宽度、线程／block、对象信息；具备lineinfo时给源定位。
5. 按声明的保证等级验证地址重用、atomic pointer、mask、async free和barrier终止：B阶段记录其复用UAF边界，D2阶段要求身份链支持范围内的真实检出。
6. 性能阈值在完成第一轮真机基线后确定；每次优化都同时给前后覆盖和资源数据。

### 19.6 性能实验流程

固定频率／功耗配置并记录环境；先 warm-up 和单独记录首次 JIT，再做多轮重复。每轮同时收集 kernel 时间与端到端时间；长尾 workload 单独展示。

对两种方案只在相同输入、相同支持范围、相同并发模式下比较。超时不从总表消失；共同可完成子集可以提供单独表，但必须显示其占原始集合多少。

误报和漏报调查应先缩小输入／opcode，再使用 实体GPU运行日志、反汇编和时间线定位。不能仅因为“加同步后错误消失”就认定原检测器正确。


### 19.6a 样例与执行证据分级

源码齐全、CPU结构检查、GPU实际编译、错误版设备诊断和合法版无误报是不同状态。当前只有文档/源码/静态验证材料，自研目标GPU_NOT_RUN；具体状态与Runner操作由[GPU CI说明](gpu-actions-ci.md)维护，不在主报告复制另一套会漂移的配置表。

Docker准备工具链，不创造GPU。厂商GPU报告必须核对设备、错误类型、源PC/地址等具体证据；Host ASan、API异常或通用非零ERROR SUMMARY均不足以证明目标访存被检出。[N02][A01]

### 19.6b GitHub Actions与结果归档

[CPU工作流](../.github/workflows/sample-structure.yml)验证结构/语法/链接/预算与合成日志分类；[GPU工作流](../.github/workflows/gpu-hardware.yml)需要实际设备和已配置的可信Runner。Skipped不升级为GPU通过。配置、容器、危险用例与Artifact路径只以[CI专题](gpu-actions-ci.md)及实际Workflow为准。

### 19.7 实体GPU端到端验收补充

§19.1的V0～V3是唯一分层。本节只补跨组件异常：元数据/报告映射失败、异步Alloc/Free依赖、运行中Abort、Context销毁、旧报告读者及资源耗尽。请求停止不等于停止完成，Free逻辑失效不等于可回收全部元数据，内部Metadata Ready等待也不能替应用补齐缺失的同步。

每次验收绑定Commit、ISA、Compiler/Runtime/驱动/固件版本、Binary Hash、输入、原始日志、错误/合法结果和覆盖；设备故障注入、Host合成日志与真实违规访存分别标识。D2还要保留对未来可能加载的旧指针的拒绝能力，不能仅等本轮Kernel结束就重用同一身份。

<a id="roadmap"></a>

## 20. 分阶段研发计划和团队安排

### 20.1 推荐交付阶段

下表保留既有阶段规划，是有经验团队、已有硬件/基础栈前提下的【估算】，不是六条路线分别验证过的排期。跨度指主动开发日历时间，不含等待硬件或长期平台故障，也不能简单串行相加；M0取得实际接口后须重新核准。

| 阶段 | 范围 | 建议投入 | 预计跨度 | 退出证据 |
|---|---|---|---|---|
| M0 能力摸底 | 三组件生产契约、GPUVA/ISA/abort、allocator、真实硬件错误通道 | 2–3人 | 2–3周 | 实际接口清单、硬件限制、预算修订 |
| M1 窄原型 | 最小P＋B Global设备插桩＋S01真实GPU合法/非法用例 | 3–4人 | 6–10周 | 真机报错、确认B的地址reuse边界、首轮性能 |
| M2 内存检查内测版 | P＋B完善、KMD契约回归、shared/private、API、诊断与覆盖 | 5–7人 | 再10–16周 | 支持矩阵闭合、应用样例、资源可控 |
| M3 对象语义 | D1先行，再做受控D2与pool/view | 5–8人 | 16–28周 | 相邻对象／旧指针／atomic边界通过 |
| M4 独立checker | init、sync、shared race逐项推进 | 3–6人/工作流 | 每项8–16周量级 | 各自覆盖、误报和性能报告 |
| M5 生态补充 | E中binary fallback与JIT/外部库 | 4–6人 | 10–18周 | 混合边界无静默漏检 |
| M6 硬件研究 | 实卡profiling＋ISA/RTL/DV/固件与新芯片计划 | 跨架构团队 | 单独排期 | 相同工作负载下的真实芯片收益数据 |

M1是**真实GPU代码生成能力的窄原型**，不能将工期作为完整P＋B交付承诺。首先在实体GPU执行S01插桩ISA并获得Device Runtime报告、映射生效次序与可靠失败/终止证据。M3/M4可以部分并行。

### 20.2 团队职责建议

| 角色 | 核心责任 |
|---|---|
| LLVM负责人 | 插桩时机、优化交互、目标指令覆盖、provenance |
| 编译器包内设备检查库负责人 | shadow／对象表、SIMT安全、错误通道和性能 |
| KMD负责人 | BO/GPUVA、提交完成、fault、abort/reset、上下文与内核接口回归 |
| UMD/runtime负责人 | 逻辑生命周期、queue/graph、模块、外部资源及用户态报告 |
| 测试与性能负责人 | 反例、真实workload、可复现数据和回归 |
| 工具链集成人员 | CLI、符号化、安装、CI与用户文档 |
| 实体GPU架构/RTL/Firmware负责人 | 实卡MMU/Tag/Bounds能力、设备故障、上下文恢复、RTL/DV与实际硅片验收 |

人数少时可以兼任，但不宜让一个人同时独自实现检查器并独自定义全部正确性 oracle。D2、异步生命周期和错误终止都值得交叉评审。

### 20.3 每个阶段必须留下什么

每阶段交付包括版本化文档、源码、测试清单、运行命令、输入hash、原始性能数据、已知限制及复现报告。公共 ABI 和 module capability manifest 的变更应记录兼容策略。

设计、接口、任务和实验结果随对应Commit维护。改变支持集合或协议后，应更新覆盖矩阵与已知限制，并使旧报告仍能识别其产生版本。

<a id="review-risks"></a>

## 21. 主要风险与决策条件

| 风险 | 后果 | 对策与决策条件 |
|---|---|---|
| 指针身份在ABI或atomic边界丢失 | 时间错误漏检或误报 | D1/D2分级；unknown显式计数；不闭合就不宣称强保证 |
| GPUVA无法满足所选Shadow元数据映射 | 工具启动或运行失败 | 核对GPU资源；可选择B1，不能静默放行未检查访存 |
| UMD按Host时间更新生存期 | 合法访问误报、真实race漏检 | 以queue/event和实际生效顺序为准 |
| 插桩导致SIMT死锁 | 工具把应用挂住 | 无锁／有限等待、目标前进性测试、统一abort |
| metadata和日志OOM | 检查中途失效 | 预算、显式降级、CI失败分类 |
| 二进制重写覆盖特殊指令不全 | 假clean或错误结果 | opcode清单、拒绝未知指令、差分执行 |
| 性能优化偷偷降低覆盖 | 看似加速但工具退化 | 性能与覆盖同时验收，采样模式单列 |
| 实际ISA与设备运行时语义不一致 | 漏检、误报或错误完成 | 实体GPU ISA核验、memory-order litmus与设备日志交叉审查 |
| KMD只取消未运行job，却承诺运行中abort | 错误后挂死或扩大reset影响 | 分别验证queued cancel、dispatch/context abort和device reset [K01][K02] |
| GPUVA/Host指针及设备backing混用 | 错误映射、权限越界、上下文误关联 | 显式GPUVA/BO/Host域及版本，实体GPU与KMD契约核对 |
| 把论文原型当可直接移植软件 | 计划低估、关键组件不可获得 | 核验artifact、实现开放范围和依赖 [P09] |
| 追逐最低平均overhead | 忽略长尾和实际业务 | 使用自己的应用分布与端到端基线 |
| 过早设计硬件 | 芯片投入与真正瓶颈错位 | 软件profile先证明需求 |

<a id="platform-parameters"></a>


## 22. 架构评审时应确认的项目参数

本节将未知平台事实转为可讨论的决策，不把它们隐藏进性能或工期数字。**会议建议先批准目标保证、首轮范围和退出证据；具体平台条件由对应负责人补齐后锁定实现/预算。** 表内“建议”不表示已获得平台确认。

| 评审项 | 本稿建议/当前未知 | 需提供的事实与责任 | 对决策的影响 |
|---|---|---|---|
| 首版用途与保证 | 建议开发/CI；Global显式分配的B2地址检查先行，预留D1 | 技术负责人确认必检错误、可接受漏检边界及必须重编译范围 | 是否需要立即做D1/D2或C，避免把所有Checker同时立项 |
| Compiler与最终ISA | 未提供目标分支/Opcode清单 | Compiler负责人列LLVM版本、IR/MIR链、Load/Store/Atomic/Async/Tensor覆盖 | 插桩位置、工作量与无法覆盖的指令 |
| 地址与元数据 | 未提供GPUVA有效位/特殊Aperture、驻留/VMM能力 | UMD/KMD给地址布局、权限、页面粒度、共享/别名与上限 | B2物理策略；B1替代可行性；CuSafe布局是否成立 |
| 指针/对象语义 | 建议先受控参数/Allocator；目标ABI未知 | Compiler/Runtime列Pointer存取、整数变换、调用、Pool/View、设备malloc | D1来源闭合度，D2身份方案，论文机制能否移植 |
| 并发/前进性 | 不假设128-bit原子或线程级恢复 | 架构/Compiler列Wave大小、Whole-Wave、Scope/Order、Barrier和原子宽度 | Atomic身份一致性、Checker锁/报告及错误路径 |
| 生命周期和模块 | 同步/异步Alloc/Free、Graph/JIT/外部库需求待定 | Runtime提供API集合、原有依赖图、模块覆盖和分配成功语义 | 失效发布、可重编译比例、C/E是否值得投入 |
| 故障与停止 | 既有KMD契约拟复用但未联测 | KMD/Firmware分别证明Queued Cancel、运行中停止、Context/Device Reset和报告保留 | 是否能交付可靠阻止模式；K-A/E等增量是否触发 |
| 性能与容量 | 无目标基线，不能设论文式平均承诺 | 性能负责人选代表性负载、显存上限、超时和覆盖门槛 | 决定测量协议、资源预算、后续优化收益 |
| 人力/时间/复用 | v1.8范围未做目标源码审计 | 项目负责人确认可用团队、设备、可复用库和发布窗口 | 重估§16工作包与§20关键路径 |

**可形成的会议决议。** 记录所选路线及备用条件、第一批支持空间/指令/Allocator、每个Unknown的处置方式、S01/S02/S04/S09等退出证据、责任人和平台事实补齐日期。缺少某项事实时做条件批准并明确受阻能力，不把未知填成“无需改动/零开销/完全支持”。

<a id="references"></a>


## 23. 参考文献、阅读路径与核验记录

### 23.1 官方产品、工具链与接口

首版取证为2026-10-20～23；本轮2026-11-02复核了本次采用的NVIDIA、AMD、Intel条件及两篇Memcheck论文。完整来源日期、版本、适用范围和历史记录在[厂商登记](research-notes/vendors_sources.json)，未复核的条目不冒称新日期。动态页面以实际版本为准，访问日不是发布日期。

| ID/来源 | 采用范围与版本边界 |
|---|---|
| [N01] NVIDIA Compute Sanitizer产品页 | 工具体系总览；精确能力以手册/发行说明为准 |
| [N02] Compute Sanitizer User Manual | Runtime/Compile-Time、Base-and-Bounds、Hybrid、错误动作和内存条件 |
| [N03] Compute Sanitizer API Guide | Callback/Patching/Memory职责；不是私有重写器完整源码 |
| [N04] NVIDIA编译期插桩技术文章，2025-12-10 | 相邻对象、Fat-Pointer思路及CUDA13.1起的产品方向；不替代当前限制 |
| [N05] Compute Sanitizer Release Notes，核对2026.3 | 同模块Hybrid、Shared/HMM/JIT等版本条件 |
| [N06] CUDA-MEMCHECK 11.8归档 | 历史报告分类，不作当前支持矩阵 |
| [N07] NVCC在线13.4文档 | 设备Memcheck编译选项及配套Toolkit约束 |
| [N08] CUDA Programming Guide | 内存模型、Stream-Ordered Alloc/Free、Graph/同步；目标ISA需另核 |
| [N09] PTX ISA在线规范 | 宽度/对齐、Atomic/Scope/Mask与资源访问 |
| [N10] CUDA Driver VMM，13.4.2归档 | GPUVA Reserve/Map/Access；不等同逻辑对象Checker |
| [A01] ROCm docs-7.2.4 GPU ASan | 固定版本机制、运行库和已知限制，非最新发行号 |
| [A02] LLNL El Capitan调试指南 | 特定部署环境说明，不推广成所有ROCm限制 |
| [A03] HIP VMM文档 | 设备VMM能力与粒度条件 |
| [A04] ROCm Docker说明，7.2.3归档 | 容器设备/工具链接入，不证明ASan支持 |
| [A05] ROCm10.1.0 ASAN安装与发行条件 | gfx942/gfx950包、独立安装和HMM/XNACK前提；不证明所有7.2.4缺陷已修复 |
| [I01] Intel2026.2 Device-Side Compiler Sanitizers | ASan/MSan/TSan、SYCL/OpenMP产品入口 |
| [I02] Intel2026.2 Compiler Sanitizers | Linux/Ponte Vecchio、单卡、串行化及检查跳过条件 |
| [I03] Unified Runtime Sanitizers规范 | 通用Layer/Adapter职责；与产品命令分别核对 |
| [M01] MUSA Compute Sanitizer指南 | 已公开OOB/API/Leak与报告能力；内部算法未知 |
| [M02] MUSA v1.1.1发行说明 | 具体环境与版本修复范围 |
| [L01] Clang AddressSanitizer | 通用Compiler＋Runtime结构；CPU性能不转作GPU性能 |
| [L02] 原项目AddressSanitizerAlgorithm | Shadow前缀编码、Poison和分配管理 |
| [L03] LLVM AddressSanitizer.cpp | 上游插桩实现入口；落地须固定Commit |
| [L04] LLVM HWASan设计 | Tag/Granule及有限碰撞模型，不推定GPU有CPU高位忽略硬件 |
| [L05] LLVM LangRef | Pointer、Atomic、Masked/VP、External State和优化语义 |
| [L06] AMDGPU后端指南 | 地址空间/内存模型参照，不照抄目标编号 |
| [K01] Linux DRM/GPUVM/Scheduler | BO/映射、提交与Queued Cancel边界 |
| [K02] Linux GPU Device Reset接口 | KMD/UMD故障和Context状态传播 |
| [K03] Linux DMA-BUF/Fence | 完成/共享/一致性，不能单独证明无竞争 |
| [K04] Linux VFIO | 地址域与I/O映射参照；不提供GPU对象来源 |
| [C01] NVIDIA CUDA镜像标签 | 选型须与实际宿主驱动/设备匹配 |

### 23.2 原始论文与复现材料

| ID/文献 | 正式记录与采用边界 |
|---|---|
| [P01] Ziad等，cuCatch | PACMPL7, PLDI, Article111, 2023；DOI [10.1145/3591225](https://doi.org/10.1145/3591225)。§3～7的元数据/编译/实验/限制；未在本项目复现 |
| [P02] Eizenberg等，BARRACUDA | PLDI2017；DOI [10.1145/3062341.3062342](https://doi.org/10.1145/3062341.3062342)。PTX/JIT与HB历史参考 |
| [P03] Jacobson等，HiRace | SC2024；DOI [10.1109/SC41406.2024.00042](https://doi.org/10.1109/SC41406.2024.00042)。FSM与支持同步子集 |
| [P06] Singh等，GMSBench | arXiv2609.08871v1，2026-09-08；149项研究基准，未运行，不称行业标准 |
| [P07] Ghosh等，SCuBA | arXiv2601.21552v1，2026-01-29；输入依赖与逻辑分区，未核后续正式版本 |
| [P08] Stephenson等，SuperCollider | PACMPL10, PLDI, Article261, 2026；DOI [10.1145/3808339](https://doi.org/10.1145/3808339)。概率竞争观察机制 |
| [P09] SuperCollider复现包 | Zenodo v1，DOI [10.5281/zenodo.19058944](https://doi.org/10.5281/zenodo.19058944)；可下载不等于全部Runtime开源，本文未执行 |
| [P10] Lu等，CuSafe | USENIX Security2026，2745–2762；[会议记录](https://www.usenix.org/conference/usenixsecurity26/presentation/lu-hongyi)。§4～8的机制/实现/实验/限制；作者全文与官方PDF交叉核对 |

历史硬件研究不参加本轮软件比较，已移除正文性能排名与重复论述；旧修订仍可从Git历史追溯。论文逐节证据、实验平台和原文分歧见[论文笔记](research-notes/papers.md)。

### 23.3 来源能支持什么

官方手册支持特定产品/版本的公开行为，不能证明私有数据结构；原论文支持其原型及实验，不能证明现行厂商产品完全相同；公开Linux/LLVM接口支持职责推导，不能证明目标GPU已有同等能力。自研建议和估算均需目标源码/接口与设备验证。

### 23.4 证据复核方法

对方案关键机制、支持条件和量化结论回查官方文档或原始全文；记录版本、章节/表格、平台、基线、排除项及未知。主稿删除旧搜索过程、引擎名称和重复阅读日志，保留足以复核结论的来源与分歧，不虚构检索数量或已复现实验。

### 23.5 保留的分歧与限制

- CuSafe样本数44/46、内存16.5 MiB/0.3%与Table6平均值，以及超时统计口径未完全一致；§5.8逐项披露，不据此排性能胜负。
- cuCatch自身两种Global Tag/索引路径与3-bit Local Tag不同，不能用另一篇论文概括的“256个随机Tag”替代原文。
- 新AMD安装包支持范围不自动证明旧机制文档中的每个限制是否修复；Intel产品指南与通用UR规范的命令不合并成万能配置。
- NVIDIA总览与细分手册可能更新不同步；采用实际Toolkit对应手册和Release Notes，保留来源丢失及Hybrid限制。
- 预算没有审计目标GPU源码；没有目标硬件性能、检测率或已实现Checker证据。GPU_NOT_RUN。

### 23.6 评审完成与产品完成

本稿的交付是可追溯的方案比较、组件契约、预算边界、场景和验收设计。会议可据§22确认首个目标与平台待决项；进入实现后，只有真实设备及对应版本证据才能升级产品状态。文档完善、CPU检查通过和厂商论文结果都不能替代这一判断。

[N01]: https://developer.nvidia.com/compute-sanitizer
[N02]: https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html
[N03]: https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html
[N04]: https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/
[N05]: https://docs.nvidia.com/compute-sanitizer/ReleaseNotes/index.html
[N06]: https://docs.nvidia.com/cuda/archive/11.8.0/cuda-memcheck/index.html
[N07]: https://docs.nvidia.com/cuda/cuda-compiler-driver-nvcc/index.html
[N08]: https://docs.nvidia.com/cuda/cuda-programming-guide/index.html
[N09]: https://docs.nvidia.com/cuda/parallel-thread-execution/index.html
[N10]: https://docs.nvidia.com/cuda/archive/13.4.2/cuda-driver-api/cuda_driver_api/group__CUDA__VA.html
[A01]: https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html
[A02]: https://hpc.llnl.gov/documentation/user-guides/using-el-capitan-systems/using-el-capitan-systems-debugging-tools
[A03]: https://rocm.docs.amd.com/projects/HIP/en/latest/how-to/hip_runtime_api/memory_management/virtual_memory.html
[A04]: https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.3/how-to/docker.html
[A05]: https://rocm.docs.amd.com/en/docs-10.1.0/install/asan.html
[I01]: https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/device-side-compiler-sanitizers.html
[I02]: https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-2/compiler-sanitizers.html
[I03]: https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers
[M01]: https://docs.mthreads.com/mooreperf/mooreperf-doc-online/musa_compute_sanitizer/user_guide/
[M02]: https://docs.mthreads.com/mooreperf/mooreperf-doc-online/musa_compute_sanitizer/releasenote/v1.1.1_release_note/
[L01]: https://clang.llvm.org/docs/AddressSanitizer.html
[L02]: https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm
[L03]: https://llvm.org/docs/doxygen/AddressSanitizer_8cpp_source.html
[L04]: https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html
[L05]: https://llvm.org/docs/LangRef.html
[L06]: https://llvm.org/docs/AMDGPUUsage.html
[K01]: https://docs.kernel.org/gpu/drm-mm.html
[K02]: https://docs.kernel.org/gpu/drm-uapi.html#device-reset
[K03]: https://docs.kernel.org/driver-api/dma-buf.html
[K04]: https://docs.kernel.org/driver-api/vfio.html
[C01]: https://gitlab.com/nvidia/container-images/cuda/blob/master/doc/supported-tags.md
[P01]: https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf
[P02]: https://mansky-cs.red.uic.edu/wp-content/uploads/sites/862/2021/08/4.-barracuda.pdf
[P03]: https://userweb.cs.txstate.edu/~mb92/papers/sc24.pdf
[P06]: https://arxiv.org/html/2609.08871v1
[P07]: https://arxiv.org/html/2601.21552v1
[P08]: https://mtarekibnziad.onrender.com/files/preprint_PLDI26_SuperCollider.pdf
[P09]: https://zenodo.org/records/19058944
[P10]: https://hongyi.lu/papers/cusafe-usenix26.pdf
