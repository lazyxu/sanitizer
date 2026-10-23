# 自研 LLVM GPU 后端 Sanitizer 技术调研与实施方案

**版本：2.0 · 资料核验日期：2026-10-22 · 文档语言：中文**

**用途：技术立项、编译器／驱动／架构联合评审、研发任务拆分与预算讨论。**

**现行统一分类**：[两家主要厂商、三种空间检查参考配置与能力比较](core-design-comparison.md)是唯一分类总表：①NVIDIA NV-R默认Runtime Binary插桩＋当前地址有效性；②AMD-B2 LLVM Shadow/Redzone；③NVIDIA NV-C LLVM Original-Object Bounds。①②的基础空间谓词都主要关注当前地址，③才追踪原对象；**3种参考配置不等于3种独立数学谓词**。B/C/D/E/F是五个工程责任包，**不是五种彼此独立的检测算法**；D2是时间身份扩展、E是集成、F真实Tag/Bounds硬件是条件性研发，版本更改记录另见[修订历史](revision-history.md)。

本文回答：在自研 GPU、LLVM 后端、KMD（Kernel-Mode Driver，内核态驱动）、UMD（User-Mode Driver，用户态驱动）及验证环境的边界下，怎样建设可用于真机调试的 sanitizer；不同路线能检测什么、需要修改哪些组件、投入多少、如何验证，以及什么情况下值得进一步投入二进制插桩或硬件支持。

资料采用有针对性的技术调研与批判性比较，优先官方文档、原始论文及作者公开材料。本文不是声称穷尽全部文献的系统综述。正文中的方括号编号对应末尾可直接访问的参考文献；资料版本、证据范围和检索记录见附录。

**证据标识：**

- **【官方事实】**：产品文档、上游源码或厂商技术说明直接支持的结论。
- **【论文结果】**：特定论文的实现、实验或作者主张，限定于其版本、平台和实验范围。
- **【工程建议】**：针对自研 GPU 的设计判断，需要在实际 ISA、驱动和应用上验证。
- **【估算】**：本报告提出的工程预算，不是厂商内部代码量，也不是对现有仓库逐文件审计后的报价。

---

## 目录

1. 决策结论与推荐路线
2. [项目背景、建设目标与研究范围](#background)
3. 对参考材料的关键修正
4. 友商产品与上游实现
5. 核心论文、原理与效果证据
6. 内存检查的基本模型
7. GPU 特有的语义和实施难点
8. 跨组件公共架构与接口
9. [实体GPU部署准入](#hardware-deployment-gate)
10. 路线 B：LLVM ASan 式影子内存
11. 路线 C：运行时二进制插桩
12. 路线 D：对象边界与时间身份传播
13. 路线 E：编译插桩与运行时补充的混合方案
14. 路线 F：硬件辅助
15. Initcheck、Racecheck 与 Synccheck 的独立设计
16. [组件改动与代码量估算](#component-estimates)
17. 性能、显存和可观测性设计
18. [检测场景与实例分析](#detection-scenarios)（[S01按厂商实现指南](S01-vendor-implementation-guide.md)）
19. [验证基准与验收标准](#validation)（含[可运行样例与结果口径](#sample-execution)）
20. [分阶段研发计划和团队安排](#roadmap)
21. 主要风险与决策条件
22. [架构评审时应确认的项目参数](#platform-parameters)
23. [参考文献、阅读路径与核验记录](#references)

---

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

**建设动因与问题边界。** 自研 GPU 的调试需要回答：某次访问是否违反对象边界、生命周期或同步约定；违规发生在哪个 kernel、指令和线程；相关资源由谁创建、释放和传入。仅验证 kernel 能编译、执行并给出预期数值，无法同时回答这些问题。**【工程建议】** 应把 sanitizer 建设为贯通编译器及设备检查库、KMD、UMD及诊断工具的动态检查能力，使应用与基础软件团队可以围绕同一份证据定位问题。

**【官方事实】** NVIDIA 将内存访问、初始化、竞争和同步检查分为不同工具，且区分能够归因到指令及线程的精确访问错误与归因能力有限的硬件异常。[N02] **【工程建议】** 本项目应沿用这种能力拆分，避免用一个“支持 sanitizer”的标签掩盖不同保证。下述风险属于立项需要验证的场景，不表示用户平台已经存在相应缺陷；实际 ISA、GPU执行语义、故障精度和工作负载仍待确认。

**为何偶发输出错误或延迟 fault 不足以定位根因。** 设 kernel A 的一次错误写入破坏了随后被 kernel B 使用的索引：可见症状可能是 B 的错误结果，也可能是 B 再次访存时的 fault。若 Host 在后续同步点才收到异常，报错 API 也未必对应最初的错误写入。这是需要用事件关联解决的工程场景，不能据此预设本平台的异常传递机制。反之，错误地址若仍处于已映射内存，甚至落入另一个存活分配，程序可能继续运行；NVIDIA 手册明确说明相邻分配之间的越界可能逃过基础地址检查。[N02] 因此，诊断应尽量在违规操作处留下证据，并关联后续症状；“最早被观察到的错误”仍不等于已证明是唯一根因。

**错误可能跨越的四层。** 下表为【工程建议】的排查分解；sanitizer 报告证明某项约束被违反，具体责任仍需结合源码、IR、机器指令与命令记录判断。

| 层次 | 应纳入调研的错误来源 | 为归因保留的上下文 |
|---|---|---|
| Host 资源与命令 | 分配大小、copy 范围或参数传递不一致；内存池子分配未登记；跨队列使用与释放缺少依赖 | 分配／释放位置、对象身份、queue／event 和生效顺序 |
| LLVM 表达与变换 | 源程序索引错误；地址空间或对象来源丢失；向量化、intrinsic lowering 与插桩次序不一致 | 源位置、优化前后 IR、检查点及目标指令映射 |
| 设备 ISA 访存 | 最终指令的宽度、对齐、byte enable、原子或异步访问足迹与检查模型不一致 | 实际地址、空间、opcode、有效访问范围和权限 |
| 调度与线程映射 | 活跃 lane、元素 mask、block／线程身份、barrier 参与集合或作用域解释不一致 | launch／block／thread／lane、mask、同步 phase 和顺序 |

**现有调试手段的作用与盲区。** 以下是选型时需要验证的工程边界，不是对用户现有工具能力的审计结论。

| 手段 | 能提供什么 | 单独使用时仍缺什么 |
|---|---|---|
| 单元测试与数值对拍 | 验证指定输入的结果与回归行为 | 未触发路径和调度；错误写入被覆盖、未影响最终输出时的访问证据 |
| GDB／设备调试器 | 在受支持目标上查看断点、变量和线程状态 | 自动遍历所有访问及对象历史；目标支持、watchpoint 资源与停顿影响须实测 |
| 硬件 fault／崩溃日志 | 捕获硬件可报告的异常及现场 | 合法映射内的对象越界、未初始化读取、生命周期与同步语义 |

**CPU ASan 不能只改目标名后直接移植。** **【官方事实】** ASan 本身由编译插桩与 runtime 配合组成；ROCm GPU ASan 还要求相应运行库配合，其文档明确列出设备地址空间覆盖及 shadow 维护的限制。[L01][A01] **【工程建议】** 可复用 LLVM 分析与 shadow／redzone 思路，但必须重新确定设备可访问的元数据布局、shared/private 身份、最终 ISA 覆盖、异步生命周期、设备报告通道和错误终止协议。SIMT 下的锁、分支和退出路径还须单独验证前进性，不能假设 CPU 线程的处理方式自然成立。

**面向的使用者与工作流。** 下表为拟议交付用途。

| 使用者 | 典型工作流与所需结果 |
|---|---|
| 应用／算子开发者 | 选择 checker 和保留行信息的构建方式，运行输入，获得访问位置、对象边界及分配／释放证据，缩小复现并回归修复 |
| 编译器工程师 | 将最小源码依次对照 IR、MIR／反汇编与覆盖清单，区分源程序违规、错误 lowering 和漏插桩 |
| UMD／runtime 工程师 | 对照逻辑分配、copy、launch、free，核查元数据与设备命令生效顺序 |
| KMD 工程师 | 核查BO/GPUVA映射、提交完成、fault、abort/reset和上下文清理，区分设备要求与现有接口 |
| CI／测试工程师 | 批量运行正反例及应用回归，按结构化状态区分错误、未覆盖、超时与工具失败，保留可复现版本和输入 |

<a id="goals"></a>

### 2.2 建设目标

**总目标。** **【工程建议】** 在明确声明的地址空间、指令、模块和执行模式内，形成“检查违规—保留证据—定位复现—验证修复”的工作流。本节给出未来研发的验收目标；本次调研交付的是方案、场景实例与验证设计，不代表检查器已经实现或已在用户硬件上验证。

**功能目标与完成证据。** 能力编号沿用后文定义；每行均须列明支持集合与限制，不能用另一行的完成情况替代。

| 能力 | 建设目标 | 可测的完成证据 |
|---|---|---|
| M1 | 识别未分配、无权限和跨映射范围访问 | 无效范围触发诊断；合法映射及权限对照通过 |
| M2 | 按真实访问宽度和指令约束检查范围、对齐 | 宽访问跨边界可检出；指令允许的非对齐不误报 |
| M3 | 保留原分配对象边界 | A 的派生指针进入存活 B 时仍报错；B 的合法指针通过 |
| M4 | 检查已登记的子分配、view 或逻辑分区 | 同一大 allocation 内跨子边界可检出；未登记语义明确列为限制 |
| M5 | 分别覆盖未复用窗口 UAF 与地址复用后旧指针 | B 路线声明 poison 窗口；D2 在身份传播闭合范围区分同地址新旧指针 |
| M6 | 检查 invalid free、double free 与约定观察点的泄漏 | 正确识别 allocator、所有权和对象状态，不误报仍被合法持有的资源 |
| I1 | 按已支持空间追踪内存初始化 | 部分写、copy／memset／异步完成的正反例通过；不宣称值级 MSan |
| R1 | 在明确声明支持的GPU内存与同步语义内识别冲突访问 | 无序冲突被报告，合法同步对照通过；概率方案另报重复试验结果 |
| S1 | 检查参与 mask、到达集合、phase 与作用域 | 非法同步被诊断；合法子组与目标允许的退出方式通过 |
| A1 | 检查 API、资源身份与异步依赖关系 | 同队列和跨队列生命周期正反例通过；Host 提交顺序不替代设备依赖 |

**共同质量目标。** 以下均为【工程建议】，与功能检出同时验收。

1. **可定位、可观察。** 报告关联 checker、错误类别、build ID、kernel／PC、访问范围、空间及线程身份；可获得时补充源行、对象边界、分配／释放栈和 queue／event。缺少符号时保留原始 PC 与模块信息；报告截断、丢失数量和检查是否继续必须可见。复杂问题提供相关事件时间线，不凭结果猜测责任组件。
2. **覆盖可核验。** 维护“能力×地址空间×指令族×模块／执行路径”的支持矩阵，关联插桩清单和实际运行记录。未知 opcode、未插桩模块、来源丢失及工具资源不足必须产生明确状态。已覆盖且本次未报错、部分覆盖和工具失败不得合并为 clean。
3. **语义正确与 SIMT 前进性。** LLVM 明确要求 masked-off 元素不发生内存访问。[L05] 检查器应相应避免无效元素地址查询引入额外 fault，并区分向量元素 mask 与硬件活跃 lane。合法执行应保持目标语言／ISA 可观察语义；不得通过意外全局串行化掩盖竞争。锁、日志满和单 lane 出错路径均须验证无新增死锁；错误后的跳过或终止按实际硬件能力约定，不能直接让其他线程永久等在 barrier。
4. **可用性与性能可度量。** 提供可复现的编译／运行入口、checker 选择、过滤、符号化及 CI 退出状态。分别测量编译、首次初始化、kernel、端到端时间，以及 shadow、描述符、日志的实际内存、寄存器与 spill 开销。性能门槛须在首轮真机基线后按代表性工作负载确定，不套用 CPU 或论文数字；受控串行模式应单独披露。
5. **可部署、可演进。** Compiler/设备检查库、KMD、UMD、module manifest和报告格式必须做版本及兼容检查。新ISA、allocator、JIT或外部模块加入时，明确更新支持集合并用实体GPU回归，不静默沿用旧保证。

**P0／P1／P2 优先级。** 下表用于安排投入，不新增工期或人力承诺；P0／P1／P2 是优先级，公共底座仍记作 P，B～F 是可部署候选技术路线，第20章的 M0～M6 是里程碑。

| 优先级 | 建议范围与退出条件 | 与原计划的关系 |
|---|---|---|
| P0：基础可用 | 公共底座、选定空间的M1/M2/M6、基础A1与明确窗口M5；实体GPU可诊断，覆盖和资源失败可见 | 主要支撑里程碑M0～M2；先完成真实硬件M1窄原型 |
| P1：语义与场景扩展 | 扩展空间和指令；按需求建设 D1 的 M3/M4、受控 D2 的 M5，以及 I1/S1/shared R1；每项分别验收身份、同步和性能 | 对应里程碑 M2～M4，可在公共协议稳定后并行 |
| P2：生态与效率 | 按实际缺口补二进制／JIT／外部库及更广的 R1；仅在测得瓶颈和收益后研究硬件辅助 | 对应里程碑 M4 扩展、M5/M6，另审依赖与预算 |

**非目标与完成标准。** 首版不承诺任意第三方 ABI 的完整时间安全、全值级 MSan、通用跨 CPU/GPU TSan、所有输入与调度的安全证明，也不承担恶意 kernel 隔离、芯片全面正确性验证或生产环境近零开销承诺。每项限制应出现在支持矩阵和用户报告中。

按第19章验收：声明支持且确实触发的确定性错误集应全部出现预期诊断；合法反例集无已知误报；unsupported、超时及工具失败单列；概率检测另设统计口径。每类场景应具备“错误实例—合法对照—触发条件—预期证据—适用路线—漏检边界”，并由确定性测试预期与真机原始报告逐步核对。研发阶段的交付物包括版本化协议、实现与测试、命令和输入 hash、原始性能数据、安装／CI 指南及已知限制；完成一个最小演示不能替代这些证据。

### 2.3 本报告采用的项目假设

用户尚未提供目标GPU实际实现仓库、ISA、GPUVA布局、编译器分支和KMD/UMD接口清单。为使方案与预算可讨论，采用如下可替换假设：

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

## 3. 对参考材料的关键修正

用户给出的方向大体有效，但以下细节会影响最终技术选型：

| 原始表述或容易产生的理解 | 核验后应采用的表述 | 影响 |
|---|---|---|
| 两个 NVIDIA 地址以 svg 结尾 | 使用参考文献中的规范产品页和 index.html 手册入口 | 方便后续稳定查阅 |
| NVIDIA 四种 checker 都支持编译期插桩 | 当前手册明确仅 memcheck；其余仍需运行时二进制插桩 | 四种工具不能共用同一覆盖结论 [N02] |
| initcheck 只查 global | 当前版本还支持 shared／all；global 是默认设置 | 友商能力矩阵需按新版更新 [N05] |
| hybrid 可任意混合已插桩和未插桩函数 | 不同编译单元可混合；同一 module 内未插桩函数不会自动接受二进制补插桩 | 自研需要显式模块协议 [N05] |
| ROCm 的 xnack 要求代表所有 GPU ASan 都需要 xnack | 这是其具体平台实现约束；自研可采用显式管理的 shadow | 避免把一条移植路线误当必要条件 [A01] |
| hipFree／hipMemcpy 限制是所有 AMD 版本的永久属性 | 应引用具体 ROCm／LLNL 环境和当期 Known issues | 区分产品版本和现场经验 [A01][A02] |
| MUSA 的一个实现说明国内主流或行业必然路线 | 只能说明该公开版本的实现和能力；没有样本支撑行业泛化 | 不用地域概括替代架构判断 [M01] |
| cuCatch 保持 ABI 就不需要处理指针跨函数／内存传播 | 公开接口布局不变不代表不需要额外的内部元数据协议 | D 路线的主要成本在传播和边界 [P01] |
| HiRace 的 FSM 能直接处理任意 GPU 同步 | 其关键压缩依赖层次式、bulk-synchronous 同步模型 | acquire/release、锁和特殊 mask 需额外建模 [P03] |
| GPUArmor 的 2.3% 是完整端到端开销 | 2026 文稿将其与使用 hardened-NOP 基线的分解实验关联；vanilla 基线下为 8% | 两个数字不可互换 [P04] |
| 在 allocation table 加 generation 就解决所有 UAF | 地址重用后还必须知道指针的旧身份 | 独立列 D1 与 D2 的能力和预算 |
| 软件方案必须修改KMD，或完全不依赖KMD | 真机需要KMD提供契约；是否新增代码取决于已有能力 [K01][K02] | 逐方案列必需支持和条件增量 |

**官方来源之间也有更新不同步。** NVIDIA 产品总览仍保留 initcheck 仅 global 的概括，而当前用户手册与 release notes 已列出 shared 支持；本报告按后两者的版本信息处理这一冲突。[N01][N02][N05]

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

**版本化事实入口：** AMD `amd-rocm-gpu-asan` Profile 与官方 ROCm 7.2.4 文档 [A01]。成熟 AMD-B2 由 LLVM/Clang 在 GPU Device Load/Store/Atomic 前插入设备 Shadow/Poison 检查，Allocator Redzone 与运行时元数据保持对象有效性；并不是仅靠 KMD MMU 页映射判断逻辑对象边界。[L02][L03]

**不要混淆官方支持与自研可行性。** 官方 GPU ASan 有 `xnack+`、Linux HMM 与匹配的 ASan 版 HIP/HSA Runtime 前提；自研 GPU 即使没有 XNACK，也可显式建立/初始化设备 Shadow，但需要在实际设备访存前确保 Shadow 地址确已映射。[A01] XNACK 重试、HMM 的不同角色、三种 Shadow 物理映射取舍与 Sparse GPUVA 全部以 [AMD 硬件适配专题](amd-gpu-asan-hardware-fallback.md) 为准。Compiler/Device/UMD/KMD 的通用伪代码只维护在 [组件指南](real-gpu-components-implementation.md)。

**已知范围：** 有限 Redzone 可能被跨过；引用版本内某些 Private/Stack 不插桩，Shared/LDS 可能重映射为 Global。不能把 LLNL 单独的 rocmcc 7.0.0 现场 Known Issues 或 7.13.0 预览缺陷直接升级为 ROCm 7.2.4 所有部署均有的已知问题。[A01][A02]


### 4.3 Intel oneAPI：设备侧 Compiler Sanitizers 与 Unified Runtime 层的责任边界

**完整版本化能力、SYCL/OpenMP 构建、UR 接口与限制，唯一参照 [Intel Device Sanitizers 专题](intel-oneapi-device-sanitizers.md)**；证据 `intel-oneapi-device-sanitizers` Profile 保留 DPC++/C++ 2026.0 与独立 UR 规范的不同维护状态。[I01][I02][I03]

成熟 Intel 体系的关键是 Compiler 设备插桩、SYCL/OpenMP Runtime、Unified Runtime/Level Zero 的层次协作。ASan 负责内存可访问性和资源状态；MSan 负责初始化定义性；TSan 负责支持范围内的竞争，三者不能合并为单个 Shadow 检查函数。官方 GPU 条件是 Linux 与列举的 Ponte Vecchio 单卡；ASan 会使 Kernel 串行化，Workgroup 规模过大时某些 Local/Private 检查可能跳过。因此报告必须注明支持范围及实际检查覆盖，不能把“运行正常”认作并发检测通过。[I01][I02]

UR 规范中某些 Checker 的 `planned` 表述，与 Intel 2026.0 编译器产品示例之间可能存在版本和 Adapter 支持差异，不能拿一个覆盖另一份产品支持说明，更不能推广到所有 GPU 厂商。[I03]


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

### 4.6 原始研究给产品路线提供哪部分机制：不要混淆“论文提出”和“厂商当前支持”

| 公开研究 | 能明确借鉴的机制 | 本项目路线 | 不能直接复制的假设 |
|---|---|---|---|
| cuCatch，PLDI2023 [P01] | 编译期找到pointer root，维护对象bounds与tag相关元数据 | D1/D2的来源思路；B+D融合 | NVIDIA论文原型数据结构与自研GPUVA/ABI不必一致；产品版非论文完整移植 |
| BARRACUDA，PLDI2017 [P02] | 二进制/中间形式插桩取事件、Host侧访问历史与HB分析 | C的访问采集；R1的事件/分析分层 | 其fat binary→PTX→JIT不是任意最终机器码rewriter |
| HiRace，SC2024 [P03] | 受限层次同步下的FSM、scoped clocks和设备端CAS更新 | R1 shared checker | 不支持的锁/scope/epoch wrap不能当作已证明 |
| GPUArmor，TACO2026 [P04][P05] | 软硬件协同对象元数据、Metadata Lookaside Buffer与开销分析 | F的对象metadata cache/ISA研究；D1 | 作者模拟参数、vanilla与NOP baseline不能当自研真机指标 |
| SuperCollider，PLDI2026 [P08] | 对某些访问重复读取/随机延迟、地址碰撞的race观测 | R1概率辅助模式 | 同值/ABA/采样窗口可漏检，不能当作全HB保证 |

**判断方法：** 标“参考NVIDIA”不意味着我们拥有NVIDIA内部源码；标“参考cuCatch/GPUArmor”意味着借鉴公开论文机制，不意味着该研究已经成为本项目能直接使用的实现。

### 4.7 以一条 S01 Global Store 对齐不同厂商/LLVM 的真实机制

~~~text
原指令：if (active && predicate) GLOBAL_STORE32 [addr] = value

公共输入：addr、size=4、GLOBAL、context、lane、predicate、site_id
  ├─ 运行时patch类[N02][N03]：观察最终访存点 → 做选定的地址检查
  ├─ LLVM/ROCm类[L01][A01]：IR/MIR插入check → shadow/有效区判断
  ├─ NVIDIA编译期[N02][N04]：同时携带pointer原对象base/bounds
  ├─ Intel UR类[I01][I03]：编译检查器 + UMD/runtime分配/队列协同
  ├─ MUSA产品参照[M01]：公开错误类型/报告要求；内部算法未知
~~~

此流程是**原创对照与接口设计图**，不是从任一厂商内部仓库复制的调度流程。判断 S01 合法性时，应检查受控逻辑对象的完整实际访问区间；只有 page/BO map 时，4B 小幅越界可能仍合法映射。若只观察最终地址而目标落入另一个live对象，则需D1来源信息。

### 4.8 三核心参考配置对应的B/C/D工程方式与其他扩展来源

<a id="route-provenance-matrix"></a>

| 路线／S01变体 | 公开机制源头（官方/论文） | 单一借鉴、融合还是本项目新增 | 自研部分必须真正实现 | 不能冒称 |
|---|---|---|---|---|
| B1（原I）：LLVM访存插桩＋当前live对象区间（非第四种基础配置） | LLVM ASan插桩结构[L01][L03]＋NVIDIA地址有效性原则[N04] | **公开通用机制融合＋本项目具体区间服务**，非AMD产品同款算法 | Sorted interval/sparse registry快照、UMD发布协议、失败和未知地址策略 | “B1等于ASan 1:8 shadow”或“检出原对象跨入B” |
| B2：AMD-B2 LLVM Shadow／Redzone | Google ASan [L02]＋ROCm GPU ASan [A01]＋LLVM目标适配[L03] | **明确借鉴的GPU ASan路线＋自研GPUVA/UMD/KMD适配** | GPUVA→shadow布局、设备helper、poison顺序、SIMT-safe报告 | “直接重用CPU runtime无需修改” |
| C：NV-R运行时二进制插桩（工程方式） | NVIDIA Patching API [N03]；BARRACUDA [P02] 作为研究对照 | **公开patch入口模式＋自研ISA重写器**；实际判定必须再选设备Checker，具体rewriter属自主实现 | Decode/CFG/encode/relocation/资源重算/原PC映射/patch cache | “复制到了NVIDIA未公开的二进制引擎” |
| D1：NV-C原对象base/bounds | NVIDIA编译期memcheck [N02][N04]＋cuCatch [P01] | **公开编译期fat-pointer理念融合＋自研SSA/ABI传播** | GEP/phi/select/调用参数root、sidecar与unknown降级 | “仅按当前地址查表就是provenance” |
| D2：指针时间身份 | cuCatch tag/meta思路[P01]＋LLVM HWASan tag[L04] | **公开时间安全思路融合＋针对本项目的强身份协议** | epoch随指针load/store/copy/atomic一致传播，复用与回收协议 | “分配表多一个generation就能检出旧裸指针” |
| E：编译＋binary混合（集成策略，非独立检查算法） | NVIDIA hybrid instrumentation [N02][N05] | **厂商公开产品策略＋B/D/C多机制融合＋自研ABI/manifest** | 模块能力选择、拒绝不兼容、不同保证级别与跨模块来源降级 | “不同模块混用自动等于所有函数被检测” |
| F1：Tag Compare | LLVM HWASan [L04] | **公开tag算法＋目标ISA/tag存储适配** | tag表示/碰撞/short granule、空转与失效/异常 | “任何tag匹配均证明原对象bounds正确” |
| F2：硬件Bounds／MLB | GPUArmor [P04]＋cuCatch/NVIDIA对象bounds [P01][N04] | **论文研究结构启发＋独立自研ISA/RTL/driver契约** | metadata cache、一致性、fault时序、RTL/DV | “软件原型性能就是芯片最终开销” |
| I1 Initcheck | NVIDIA initcheck [N02]＋Intel device MSan [I01] | **分离未写过bitmap与值级定义性传播的融合设计** | byte definedness/copy/async时序；完整值级MSan另立 | “bitmap checker就是MSan” |
| R1 Racecheck | NVIDIA racecheck [N02]＋BARRACUDA/HiRace/SuperCollider [P02][P03][P08] | **算法按同步子集选择的多源融合** | HB/FSM/state、lane/byte mask、scope、资源上限 | “一次执行无报告就证明race-free” |
| S1 Synccheck | NVIDIA synccheck [N02]＋目标ISA/LangRef [N09][L05] | **检测目标借鉴＋目标barrier状态机原创适配** | 参与集合、phase、token、合法部分mask与超时分类 | “所有超时都是证明的死锁” |

**“原创”的精确含义：** 本表说的“自研/原创”是针对本项目需独立设计和写代码的工程实现、接口及适配，不是对世界首次提出该算法的学术新颖性宣称。公开文献已有的基本shadow、对象bounds、tag、HB和设备trace不能改名归为新算法。实际在实现时，许可证允许复用代码才复用；否则独立实现并保留公开引用。


### 4.9 对每种产品机制最有价值的源码／官方证据入口

**全部一手来源、版本化条件和厂商支持声明的唯一登记**：[厂商版本化证据索引](research-notes/vendors.md) 和 [证据来源 JSON](research-notes/vendors_sources.json) 的 `vendor_profiles` / `sources[].id`。每个 Profile 统一提供产品版本、硬件与 Runtime 条件、检测能力、已知限制、原始来源、详细专题和硬件验收状态，主研究不复制第二张逐项 URL 表。

NVIDIA NV-R/NV-C 机制与命令参见 [S01 厂商指南](S01-vendor-implementation-guide.md)，AMD XNACK/HMM 与官方构建命令参见 [AMD 硬件适配专题](amd-gpu-asan-hardware-fallback.md)，Intel ASan/MSan/TSan 的 SYCL/OpenMP 配置参见 [Intel 专题](intel-oneapi-device-sanitizers.md)。自研 Compiler/UMD/KMD 的具体实现契约则只维护在 [组件实施指南](real-gpu-components-implementation.md)。

### 4.10 厂商版本、运行命令与可复制的实验入口

版本表由 `vendor_profiles[].version_baseline` 和 `sources[].version` 唯一维护。实际运行必须使用所选厂商**确实支持的 GPU、编译工具、驱动与配套 Sanitizer Runtime**；不可混用不同版本开关并据此推断通用结果。完整命令归各厂商专题，不在主研究再维护可漂移的第二份命令块。

厂商工具检测成功只说明该厂商工具在所选条件下报告了错误；它不等于本仓库自研 GPU Checker 已实施。设备命中、仅有 Host API/CPU ASan、未知覆盖、工具失败及 `GPU_NOT_RUN` 都应保留独立状态。

## 5. 核心论文、原理与效果证据

### 5.1 论文选择原则

将正式发表的 PLDI、SC、TACO 工作作为主要机制依据；将 2026 年新预印本作为补充问题与基准线索。新近发表不自动等于影响力已被长期验证，本文不以搜索结果中的被引数字建立排名。

| 文献 | 主要价值 | 本报告采用方式 |
|---|---|---|
| cuCatch，PLDI 2023 | 编译器／驱动协作，对象 bounds 和时间 tag | D 路线机制参照 [P01] |
| BARRACUDA，PLDI 2017 | GPU 二进制级 happens-before 竞争检测 | C 和 Racecheck 的历史基础 [P02] |
| HiRace，SC 2024 | 层次同步下用状态机压缩访问历史 | 高覆盖共享／全局访问检查的候选算法 [P03] |
| GPUArmor，TACO 2026 文稿 | 对象元数据与硬件缓存协同 | F 路线及性能拆分方法 [P04] |
| SuperCollider，PLDI 2026 | 基于重复访问／碰撞的另一种 race 检测路线 | 与 HB 方法比较精度、范围和开销 [P08] |
| GMSBench，2026 预印本 | 跨空间、对象和时间错误的细分测试 | 扩展验收分类，不照搬总检测率 [P06] |
| SCuBA，2026 预印本 | 输入依赖、逻辑分区错误及分析方法 | 提醒动态检查的输入覆盖边界 [P07] |

### 5.2 cuCatch

**【论文结果】** cuCatch 将编译分析、Shadow Tagged Base & Bounds 与运行时对象信息结合；其设计使用 BST 对象表、指针中可用位和影子元数据，并在后端追踪访问所依赖的 base。它说明“公开内存布局不变”与“内部维护 fat-pointer 类信息”可以并存。[P01]

对本项目的主要启发是**保留原对象信息直到最终访存**。不能把论文中的指针位分配和 PTX 实现原封不动搬到自研 ISA；第 12 章给出的实施方案必须根据本机地址格式、函数调用与原子能力重做。

论文原型排除了特定设备 malloc、动态并行和预编译库场景。产品后来采用相关编译分析方向，不代表论文所有实验数字、限制和当前产品实现逐项相同。[P01][N04]

### 5.3 BARRACUDA

**【论文结果】** BARRACUDA 从 fat binary 提取 PTX，在 JIT 前插桩，由 GPU 记录事件、CPU 侧处理检测状态。它结合访问历史、线程／同步时钟判断竞争；这一历史实现不能直接等同于任意纯机器码插桩器。[P02]

**【工程建议】** 可借鉴其算法分层和实验问题，不直接复制元数据结构。自研 ISA 的 memory scope、warp 调度和原子语义不同，尤其不应把 2017 年目标平台的同步假设当作今天的通用 GPU 内存模型。

### 5.4 HiRace

**【论文结果】** HiRace 通过源码包装和 Clang AST 重写插桩，采用 25 状态、1200 转移的 FSM，并以原子 CAS 更新 shadow。FSM 本身占 5 bit；连同线程身份和 scoped clocks，默认每个受监控位置合计 8 字节元数据。任意锁协议、非层次同步和 clock 溢出不能直接归入该默认保证。[P03]

**【工程建议】** 这是第一代共享内存／规则 barrier 竞争检查的有力候选。实现时先给出正式支持的同步子集，再验证 clock 溢出、任意参与 mask 和跨 kernel 事件，不应宣传为“所有 GPU 程序都只需固定 8 字节元数据”。

### 5.5 GPUArmor

**【论文结果】** GPUArmor 将编译分析与对象元数据结构、Metadata Lookaside Buffer（MLB）结合。2026 文稿的关键贡献是减少元数据访问和存储成本，并区分检查本身与编译器优化损失。[P04]

2025 年 arXiv 版本保留作版本对照；采用其数字时必须明确该版本的基线，不能与 2026 正式论文信息混用。[P05]

**【工程建议】** 如果自研硬件可改，应先测对象访问工作集、metadata cache 命中率和额外寄存器压力，再确定缓存容量和 ISA 设计。没有这些数据，仅按论文平均开销选择硬件结构，很容易对自己的 workload 失效。

### 5.6 SuperCollider

**【论文结果】** SuperCollider 对 weak 访存加入随机延迟及 strong、system-scope 重复读，比较值并补充 warp 内写地址碰撞检查。它不把所有 strong／atomic 操作按同一规则检查；同值、ABA 和窗口未命中仍可能漏检。[P08]

**【工程建议】** 可作为低元数据成本、拓宽并发场景的候选模式，与精确建模的 HB checker 分开报告。不能把“本次未观察到变化”当作不存在竞争的证明。其复现材料包含专有 runtime 二进制，开源实验包不等于可直接移植完整工具。[P09]

### 5.7 2026 年的补充证据

GMSBench 提供 149 个 CUDA 测试，按空间、对象和时间语义分类；其数据可帮助发现测试盲区。但它的 Compute Sanitizer 结果只反映论文中的命令、适用测试和模式，不能当作新版编译插桩产品的总体检测率。[P06]

SCuBA 关注输入依赖的越界与大分配中的逻辑分区，提示 allocator bounds 之外还可能需要 tensor view／子对象语义。本文把它作为验证与注解设计参考，不声称其预印本结果已成为业界公认结论。[P07]

### 5.8 量化效果：哪些数字可以比较

| 工作／版本 | 报告的数字 | 平台、基线和统计口径 | 本项目可以如何使用 |
|---|---|---|---|
| cuCatch，PLDI 2023 | 几何平均 1.19×，即 19% 开销 | 176 个 workload，RTX 2080 Ti；部分计时采用 GPU-only，Host 代码未插桩，未计分配拦截和元数据管理成本 | 证明软件对象检查有优化空间，不能当应用端到端承诺 [P01] |
| GPUArmor，2026 文稿 | 几何平均 GPUArmor 1.08×；同平台 cuCatch 1.36× | 28 kernels，GA100 trace／NVAS 模拟；Fig.14 的统一比较，8-entry MLB；不计内存管理 wrapper 时间 | 这两个数字可在该 kernel 实验内部比较 [P04] |
| GPUArmor，开销分解 | 2.3% | hardened binary 中检查指令按 NOP 处理的基线；§10.1／Fig.12 | 衡量检查和 metadata 成本，不能替换 vanilla 基线下的 8% kernel 开销 [P04] |
| SuperCollider，PLDI 2026 | fast 配置几何平均 4.86× | 130 个未触发 race 报告的 HeCBench；CUDA13，读／写延迟1ns，RTX Ada 6000 48 GB | 未报告不等于已证明无race；不能和memory checker横排 [P08] |
| SuperCollider 的共同子集 | 几何平均5.88×；iGUARD 159×；Racecheck 178× | 59项共同完成；其余Racecheck有67项超时，iGUARD有39项超时，两者重叠；HiRace未参加此比较 | 仅对共同子集成立，不能把67与39相加成不同应用总数 [P08] |
| NVIDIA 编译插桩博客 | 部分场景显著改善，未提供通用平均保证 | 运行期开销说明，不含重编译成本 | 作为方向证据，不填成统一百分比 [N04] |

**本报告没有运行自研硬件、复现全部论文或完成友商同平台横测。** 因而不提供伪造的“自研方案预计快 3.7×”或统一厂商排行榜。第 19 章给出真正可复现的比较方法。

对 HiRace、BARRACUDA 的性能，正文优先使用已核验的机制与适用范围；未把不具备共同基线的摘要速度比硬拼入上表。[P02][P03]

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

| 事件 | 生产方 | 必须携带 | 消费方 |
|---|---|---|---|
| suballocation register／release | 内存池／框架接口 | parent ID、子范围、独立生存期 | bounds、init、报告 |
| free request／free begin／retire complete | runtime／UMD | 对象身份、命令位置、生效边界、完成证据 | 时间身份检查、元数据回收 |
| map／unmap／import／alias | UMD逻辑登记＋KMD资源映射契约 | 规范对象、BO/映射身份、权限、device/context | 地址解析与peer检查 |
| job submit／complete／fault | UMD命令关联＋KMD/设备完成源 | job/queue ID、依赖、完成状态、原始fault信息 | 生命周期、错误归因、资源回收 |
| abort／reset／context destroy | KMD及设备/固件；UMD消费 | 终止范围、完成/失败状态、受影响任务 | 真实GPU报告写入、设备完成与受控终止 |
| copy／memset submit／complete | runtime／copy engine 接口 | 源目标范围、顺序、定义性 | init、生命周期 |
| barrier／fence／atomic | 插桩 | scope、ordering、参与集合、phase | race／sync 分析 |
| module load／unload／JIT | loader | build ID、ABI、覆盖位图、code map | 混合模式与符号化 |

API 回调与底层实际命令必须去重。一个高级 API 触发若干内部 driver 调用时，不应把同一分配记录多次。

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

公开 kernel 参数 ABI 可以保持裸指针，但 UMD 可为已插桩模块提供参数 sidecar，或在私有 launch descriptor 中传入 sanitizer context。设备函数可使用隐藏参数或克隆版本。

仅扫描 kernel 的顶层指针参数不够：结构体中的指针、指针数组、间接加载、虚函数目标和嵌套容器也会传递对象身份。D2 必须明确闭合的支持集合。

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

以下分工、接口字段与验收项是【工程建议】；官方资料用于校准职责及语义，不代表用户现有驱动已经具备这些能力。

#### 顶层组件与内部边界

本节关注**为什么**要区分语义的生产方与可验证设备结果：Compiler/Device Runtime 在真实 GPU 产生 Checker 访问；UMD 管理逻辑对象、Module/Queue/Graph 与 Report；KMD 提供 BO/GPUVA、提交、完成和异常；Firmware/RTL 只在真实芯片或所选失败策略需要时修改。具体可执行伪代码与组件矩阵的唯一来源是[组件实施指南 §1～11](real-gpu-components-implementation.md)。

KMD 看不到未上报的 Tensor 子分配，Host API 成功不代表 GPU 指令已检查；Binary Patch 也不会凭空恢复 D1 原对象来源。既有 KMD 契约充分时新增代码可以为零，**仍必须验证其可见性、失败和终止范围**。

**官方依据及适用边界。** DRM GPUVM文档把GPUVA映射与其backing GEM对象联系起来；GEM本身不理解缓冲内容，设备特定操作仍需驱动接口。[K01] DRM的reset约定由KMD判断和执行复位、上送fence／context错误，UMD检查提交结果并向应用报告。[K02] 据此，本报告把底层映射和故障契约归KMD，把应用对象语义及诊断归用户态；这是对自研架构的设计建议，不要求采用DRM类名或相同ioctl。

#### “必须支持”与“必须新增代码”分开记录

**必须支持**表示路线成立所需的能力或契约；**必须新增代码**表示与现有平台相比尚未满足该契约的差额。B、C、D、E、F在真机或运行guest驱动的完整执行路径上均依赖真实／guest KMD支持，但若现有映射、提交、完成和所选错误终止契约已经充分，KMD新增代码可以为0。接口存在但没有证明其时序、错误回传或终止范围充分，不能直接登记为“已满足”。


#### 事件的语义生产方、底层真值及消费关系

一个逻辑动作可能同时经过API、UMD、KMD及设备执行。应关联这些观察点，而非将每层回调都登记成新动作。下面的“真值来源”按事件类型划分；不存在能替代其他来源的全局“万能分配表”。

| 事件／信息 | 语义生产方 | 可确认结果的来源与边界 | 主要消费方 |
|---|---|---|---|
| 应用allocation、池内suballocation、view创建 | 对应Host allocator／框架接口；设备allocator由device检查库接入 | 该allocator的成功结果和对象生命周期；KMD仅补充其backing BO／映射关系，通常看不到未上报的池内切分 | UMD对象服务、编译器device检查库 |
| BO创建及GPUVA map／unmap／权限变更 | UMD发起请求；KMD实施相应特权操作 | KMD／设备定义的生效与失败结果；异步接口须保留关联完成或依赖证据，不能把请求字段当已生效状态 | UMD映射表、shadow布局和真实设备fault归因 |
| free请求、应用对象失效、底层资源退休 | allocator定义应用生存期；UMD把相关动作接入命令顺序 | 失效点按API／执行侧协议确定；KMD完成证据可证明相关作业／映射操作退休，却不能自动给出池内对象free的精确开始时刻 | poison／unpoison、D2身份退休与元数据回收 |
| launch／graph节点／提交与完成 | UMD提供应用launch和每次graph执行实例；KMD处理底层提交 | 接受／拒绝取自KMD，硬件执行完成由其回传；应用launch与底层作业可为一对多，必须保留关联 | 生命周期和HB分析、device context、报告 |
| 每条实际GPU访存及mask、宽度、线程 | B/D的设备插桩、C的patch stub、F实际硬件事件；B/D/C的真实设备检查入口 | 真机访存证据必须来自实际设备执行。KMD通常仅观测触发fault等有限异常，不生产常规全访存流 | 地址／bounds／identity检查器、init／race引擎 |
| barrier／atomic／async copy及可见性 | GPU实际执行事件；UMD补充queue／event／copy命令 | 需要对应scope、order、phase与完成关系；KMD提供相关提交／完成契约，CPU时间戳不能替代HB | 真实GPU Race／Sync／Init检查器 |
| fault、abort、reset及执行终态 | 设备／固件触发，KMD接收、控制并回传；UMD也可请求所支持的终止动作 | 保留实际终止范围、context状态、完成错误与报告可读性；只有硬件或驱动提供时才声明精确fault PC／地址 | UMD错误动作和报告、CI终态、资源回收 |
| 模块覆盖、原始PC与patch后PC | 编译器生成初始manifest／debug map；UMD loader／patch器补充运行版本 | 以实际加载并执行的代码hash、ISA和ABI版本关联；不能仅靠源文件名 | device记录解析、覆盖报告 |

dma-fence用于表达异步硬件操作的完成；dma-buf为跨驱动／设备共享提供对象与同步框架。[K03] 因而报告中仍应分别保存“调用成功”“提交接受”“完成成功／错误”。`DMA_BUF_IOCTL_SYNC`的缓存一致性保证不阻止其他设备或进程同时访问，也不能单独作为CPU/GPU无竞争的证据。[K03]

**建议的去重键。** 使用一个逻辑`operation_id`关联高层请求、子调用、提交和完成；分配实例使用不可因VA或句柄复用而重复的`allocation_id/epoch`。`backing_bo_id`、`mapping_id`、`suballocation_id`、`launch_id`和`execution_id`各有独立含义。一次API成功产生一个逻辑分配实例；其底层BO创建回调补充backing关系，不再创建第二个同义分配。一个BO上确实存在多个独立子分配时，应有多个子对象实例，并通过parent关系连接到同一backing。

#### KMD支持缺口与验收证据

Linux GPU调度器的`cancel_job`回调针对尚未经`run_job`执行的作业；正在执行作业的超时恢复另有驱动／固件相关流程。[K01] 因此，“存在cancel回调”“能杀掉Host进程”或“有看门狗”均不足以直接验收sanitizer所需的运行中终止能力。

| 待确认契约 | 何时构成缺口 | 需要留下的验收证据 | 影响路线 |
|---|---|---|---|
| GPUVA／BO布局与权限 | shadow／descriptor／日志不能按所选方案安全映射；映射失败或异步生效不可查询／排序 | 正常映射、容量不足、冲突及失败路径；访问前初始化完成；unmap／重映射与仍在执行作业的依赖结果 | B～F |
| BO、映射与应用对象关联 | 导入、别名或池内子分配被重复登记／错误合并；句柄复用使不同实例混淆 | 一个BO多映射、重复import、同VA重用、失败分配及独立子分配的对象账本；核对父子和别名关系 | B～F |
| 提交与完成状态 | 提交失败记成成功，错误fence被当正常完成，reset后的context仍被当有效 | 接受、拒绝、正常完成和错误完成分别可观测；launch到作业映射可追踪；reset状态沿KMD→UMD→应用一致上送 | 所有实际驱动路径 |
| 释放／元数据退休顺序 | free按Host返回立即生效，或完成后仍有检查器／报告读取被提前回收 | 受控同queue前置访问、跨queue等待、缺依赖反例、同VA复用；同时检查应用对象失效与元数据可回收边界 | B、D、E及F生命周期 |
| 运行中停止／恢复 | 检查失败后继续会破坏同步或状态，却没有经过验证的终止策略 | 在访存检查失败、部分lane已到barrier、日志／metadata不足等情形下，验证所宣称dispatch／context／全GPU终止范围、终态、报告保留及资源可回收性；说明无辜context是否受影响 | 依赖该失败策略的B～F |
| fault归因与覆盖 | 只见device lost却宣称精确内存错误；缺失PC／地址时仍补造字段 | 只读出硬件／KMD实际提供的故障信息，关联代码版本和context；无法归因标记APPLICATION_FAILED或未知，检查器资源失败单列 | B～F故障报告；F尤其关键 |

若现有KMD契约全部满足选定路线，记录“**复用现有KMD接口，新增代码为0；所列契约已联测**”。若尚未联测，则记录“**拟复用，待验**”。若功能缺失，列出具体API／固件／硬件依赖和所阻塞的运行模式；对应新增代码和测试按第16.8节条件包估算，不能视为基础预算已经包含。

#### guest／host地址域及无重复身份

VFIO文档将设备访问、I/O地址映射及中断等接口分开，并在示例中分别填写Host进程`vaddr`与设备看到的`iova`；IOMMUFD也是管理I/O页表的接口。[K04] 这些接口不提供GPU语言对象边界或指针历史身份。以下是据此形成的跨域接口建议，而非宣称所有GPU使用同一条固定地址转换链。

| 域／身份 | 建议携带的区分信息 | 易混淆的关系 |
|---|---|---|
| guest CPU虚拟地址 | VM实例、guest进程／地址空间、GVA | 不等于guest GPUVA；相同整数在不同进程也不能直接合并 |
| guest GPU虚拟地址 | VM实例、虚拟GPU、GPU context／VMID／PASID（按实际能力）、address space、映射世代 | 由guest GPU地址空间解释；不能拿Host进程指针表直接查询 |
| guest物理地址／设备IOVA | 地址域类型、IOMMU上下文、translation generation、范围 | 不同部署中可能存在等值或多阶段转换，必须以实际映射记录证明，不默认相等 |
| Host CPU虚拟地址及Host物理／VRAM位置 | Host进程、backend资源、实际存储域及映射记录 | backing存储可以迁移或重新映射；物理地址变化不应自动产生新的应用分配身份 |
| 逻辑分配实例 | 规范`allocation_id/epoch`、allocator／owner、parent、原始范围 | 与BO、FD、handle、GPUVA及mapping身份分开；上述数字重用不使旧实例复活 |
| 导入／alias映射 | 规范backing或分配关联、mapping owner、权限、映射范围和偏移 | import通常增加一个使用／映射关系；不能凭导入回调建立第二个独立拥有权并据此double free或漏报 |

**地址归一化应保留证据。** 只有存在经过验证的映射关系时，才将不同地址域的访问归一到同一规范对象与偏移。若外部导入接口不能提供足够的来源关联，应标记关联未知并降低跨域检测保证。FD、GEM handle、裸GPUVA或当前物理页号均不宜单独充当全局不可复用的allocation identity。


#### 逐路线表的阅读口径

以下按B/C/D/E/F五个**工程交付标签**展开职责，它们不是五种独立检测谓词；三种核心成熟厂商参考配置另见[核心比较](core-design-comparison.md)。D拆分D1/D2；“新增”只指当前生产组件缺失的能力差额，公共底座及共享设备库计一次；不能把模拟的映射或Host异常当作真实KMD与GPU设备检查。


### 8.8 各生产组件的真实GPU可执行修改代码

Compiler/LLVM、Device Runtime、UMD Allocator/Loader/Binary Rewriter、KMD、Firmware、实体 GPU RTL 与 CLI 的**唯一完整伪代码、通用 ABI 和模块职责矩阵**统一见[逐组件实施指南](real-gpu-components-implementation.md)。本章 §8.7 保留事件真值、跨 Guest/Host 地址域、KMD 能力缺口等设计判断，不再重复第二张组件表。

**纯 C Runtime Binary 路线**可不修改生产 Compiler；UMD 自行 Decode/Patch/Relocate。**KMD** 在既有映射、完成及安全终止契约充分时亦可零新增。均不表示免于源码级契约审计或实体设备验证。

<a id="hardware-deployment-gate"></a>

## 9. 真实GPU部署准入

### 9.1 必须由实体GPU作出访问检查判定

B、C、D、E 的设备检查代码必须存在于真实GPU执行的ISA路径并在实际访问之前生效；**F若要作为独立的对象检查硬件候选，必须有实体GPU真实Tag/Bounds能力（F1/F2）**。Host侧仅收到API报错、编译器生成了IR call或设备映射成功，均不代表已实现GPU访问Sanitizer。

### 9.2 五个工程交付标签及边界（不等于五种独立算法）

| 路线 | 真机生效位置 | 必要条件 | 不可宣称 |
|---|---|---|---|
| B | LLVM最终ISA插入的B2 Shadow/Redzone或B1当前Live区间查询 + GPU设备检查库 | Metadata可被GPU读取、预映射/驻留与UMD异步发布正确；**XNACK不是自研B算法的必要硬件功能** | B1/B2已自动提供D1原对象来源或D2同VA复用旧指针检测 |
| C | UMD重写后在GPU上执行的Binary和检查stub | 目标ISA解码/重写透明、代码正式加载 | 所有未知opcode或闭源模块都得到覆盖 |
| D | GPU设备代码的原对象Bounds和可选完整指针身份传播 | D1来源保留、D2身份跨ABI/存取一致 | 仅从当前GPUVA反查可判复用后旧指针 |
| E（集成策略） | 按模块执行B/D/C已插桩/已改写GPU检查代码 | B/D/C已有真正GPU checker；统一ABI、检查等级和覆盖缺口可见 | 混用模块自动具有最强保证，或E本身新增了访存检测算法 |
| F | **实体GPU现有或另行交付的Tag/Bounds对象检查硬件能力** | 必须有实际F1/F2硬件ISA/元数据/失效与故障契约 | 把没有真实芯片能力的F1/F2列为已具备 |

**分类补充**：核心成熟参考配置为NV-R、AMD-B2、NV-C，具体能力对照见[唯一分类与能力矩阵](core-design-comparison.md)。I并非新增路线，而是B1区间表数据结构；Tag软件比较不等于硬件F1，F2缺芯片应列未来硬件研发，E混合是集成包装。关于无XNACK时官方与自研的不同可行性及实现代码，详见[S01 §2.4与§3](S01-vendor-implementation-guide.md)。

### 9.3 研发与验收顺序

先确定Compiler、UMD和KMD的生产资源契约及元数据ABI；随后交付真实GPU可执行的B Global load/store Shadow检查、报告与可靠失败路径，在实体GPU上用S01错误及合法对照验收。D1再补原对象Bounds，D2按必要的指针身份闭合度独立验收；对现成二进制/JIT有明确缺口时才引入C/E；F以实际芯片支持和收益数据决定是否立项。参见[逐组件实现与验收表](real-gpu-components-implementation.md#11-bf-的选择与组件总结)。
## 10. 路线 B：编译期Addressability实现（B2 Shadow / B1 当前Live区间）

### 10.1 原理

将 LLVM 访存检查与设备 shadow 查询结合；allocator 根据对象的创建和释放顺序维护可访问状态，设备端记录违规访问。基础目标是 M1/M2/M6 和在 poison 窗口内的 M5，随后扩展 shared/private 及初始化检查。

B2与经典ASan Shadow/Redzone算法同源；**B1（原方案I）则使用GPU设备端有序Live Allocation区间表，属于同一编译期地址检查路线的无Shadow实现选项，不是AMD官方产品同款算法，也不是D1原对象来源检查**。B1和B2仅利用当前地址可访问性，可能放过A→live B；D1必须传original base/size，D2还需旧指针epoch。自研目标必须自己确定GPUVA映射、设备metadata/Runtime、错误终止及地址空间支持。[L01][L02][L03]

### 10.2 B2设备Shadow的必需映射与B1地址表替代

B2需要目标GPU可读、在dispatch前已映射和完成初始化的Shadow元数据；Device Runtime按实际访问字节检查Poison。元数据不可达、未初始化或发布顺序错误必须报告TOOL_FAILURE，不可把异常当作成功检查。B1是GPU端当前Live Allocation查询，实现范围不同，不能自动获得D1指针来源保障。没有XNACK不妨碍自研GPU使用预映射Shadow，但实际设备资源与同步仍须验证。

### 10.3 编译器改动

1. 明确 sanitizer driver option、offload 编译行为及 device runtime link 规则。
2. 在 Pass 中识别 load/store、atomics、内存 intrinsic、masked／vector 操作。
3. 根据 target address-space 判断 global/shared/private，处理 generic pointer 的运行时分类。
4. 为目标 intrinsic、async copy 和 backend 新增访存设计补充插桩或缺口标记。
5. 管理 runtime helper 的不再插桩属性、错误路径、函数属性和优化交互。
6. shared/private 若采用 redzone，调整 layout、frame 和资源用量 metadata；不能仅把空间标成可插桩。

第一版可选择“独立 GPU pass＋复用 LLVM 通用工具”来减少对上游大型 ASan pass 的侵入；若目标与现有上游支持高度相似，也可以扩展原有实现。两者应以改动可维护性和地址空间语义为准。

### 10.4 编译器、KMD、UMD支持与修改

B2 由 Compiler/Device Runtime 在声明支持的最终 GPU ISA 访存前查询 Shadow，UMD 管理逻辑对象、Redzone、Poison 与元数据 Fence，KMD 仅补实际 GPUVA、权限、完成、Fault/Abort 缺口。B1 查询当前 Live 对象区间，不会因此自动具有 D1 原对象保证。

**通用实现单源：** [Compiler/Checker/UMD/KMD §2～§7](real-gpu-components-implementation.md)。XNACK/HMM、物理 Shadow 三种取舍和 Sparse GPUVA 仅在[AMD 硬件专题](amd-gpu-asan-hardware-fallback.md)维护，主稿不再复制组件实现矩阵。

### 10.5 能力边界与退出条件

基础B2地址检查无法保证发现从A来源越界进入另一个存活对象B；需要D1原对象来源Bounds。地址复用旧指针还需要D2身份传播。

只有在实际 shadow 布局、典型内存占用和真机 slowdown 均可接受时，才把 B 扩展成默认 CI 模式。

预算：在已有KMD契约可复用时，P＋B生产核心约25–48 kLOC、测试约21–39 kLOC。这里是具备多空间和异常路径处理的内测版本，不是插入几个 load/store helper 的演示程序。

<a id="route-b-platforms"></a>

### 10.6 参考配置② AMD-B2与B1区间实现的来源

**出处：** B2主要借鉴Google/LLVM ASan 1:8 Shadow/Redzone[L01][L02]及AMD GPU ASan的compiler/device runtime联动[A01]；异构Host/Device资源分工可参照Intel Unified Runtime [I01][I03]。**原创工程适配：** 目标GPUVA→shadow映射、UMD顺序poison、SIMT报错/停止、KMD映射、目标ISA最终访存覆盖。

**组件：** Compiler新增pass＋GPU设备库；KMD仅补缺失GPUVA/完成/所选终止契约；UMD新增DebugAllocator、ShadowManager、ReportDecoder。

**跨 Shadow Granule 检查、未映射元数据故障与安全 Report 的唯一设备伪代码在**[组件指南 §3.1](real-gpu-components-implementation.md)。


B1的sorted live-object interval查找是另一个**本项目简化原型**，借鉴LLVM插桩组织但**不声称是AMD的Shadow实现**，只能保证当前地址有效性。

## 11. 路线 C：运行时二进制插桩（借鉴NV-R的插桩方式，非独立检查谓词）

### 11.1 原理和前提

UMD 在模块加载或 JIT 产物生成后取得目标代码，解析指令与控制流，为被选择的访存、atomic 和同步指令插入检查，再以新的资源用量和 PC map 启动 kernel。

它能覆盖无法重编译的模块，但“API 拦截”本身不能看到 kernel 内每次访存。也需要区分中间表示重写与最终机器码重写：例如 BARRACUDA 的 binary-level 路径依赖从 fat binary 提取 PTX，不能据此认为其能够处理所有纯机器码。[P02]

### 11.2 编译器、KMD、UMD支持与修改

NV-R 风格 Runtime Binary 检查**不要求修改生产 Compiler 或重新编译用户应用**；UMD 内 Runtime Binary Rewriter 对目标 Opcode、访问宽度、Predicate、Register/Scratch、Branch 与原 PC 映射负责，KMD 提供正规可执行资源及提交/错误契约。未支持指令必须拒绝或明确标 `PARTIAL_COVERAGE`，不能只记录 Host API 就宣称 GPU 已插桩。

实现单源见[组件指南 §5～§7](real-gpu-components-implementation.md)，NVIDIA 官方编译/执行条件见[S01 厂商专题](S01-vendor-implementation-guide.md)。

### 11.3 必须单独预算的工程风险

- 修改代码长度后，branch target、literal pool、重定位和调用约定都要更新。
- 额外寄存器和 scratch 会改变 occupancy；原 launch 资源声明未更新可能直接导致错误。
- patch 必须保留 predicate、scoreboard、barrier、依赖等待和特殊访存修饰符。
- 模块被多个 context／线程并发加载时，patch cache 需要绑定完整 code hash、ISA 和工具版本。
- 不能用未知指令的“近似解码”继续检查；应清楚拒绝或标成部分覆盖。
- 对不可识别对象 provenance 的代码，基础 C 只能保证地址／区间层面的检查。

### 11.4 什么时候不优先做 C

如果全部核心应用都能重编译，而项目尚无成熟的机器码重编码／调试 patch 设施，C 的基础建设通常会挤占最急需的对象与生命周期工作。可以先把模块未覆盖作为可见缺口，等实际闭源覆盖需求出现再补。

预算：在**已有成熟解码/编码/patch及可复用KMD契约**时，P＋C生产核心约31–60 kLOC、测试约27–51 kLOC。若这些前提不存在，还需追加第 16 章列出的基础设施投入。

<a id="route-c-platforms"></a>

### 11.5 参考配置① NV-R的Runtime插桩来源与自研差异

**出处：** C借鉴NVIDIA公开Patching API的指令级callback模式[N03]及BARRACUDA的历史事件采集设计[P02]；**自研具体rewriter**（decode/encode、重定位、谓词、资源重算、PC-map/cache）不是NVIDIA未公开实现的复制品。C本身只决定如何采集检查点，实际判定还需B1/B2/D元数据。

**组件：** **生产Compiler源码可完全不修改。** ISA解码、编码、重定位和资源重算均由**UMD内的独立Runtime Binary Rewriter**负责；复用既有MC库仅是可选优化，而不是先修改Compiler的前置依赖。KMD复用正规的代码映射/资源审计，UMD在module load/JIT patch并计算新资源；改写前后合法结果、谓词、barrier和原PC的对应关系必须在真实GPU上验收。

**GPU Binary Patch 的唯一 Decoder/Relocation/Resource 样例在**[组件指南 §5](real-gpu-components-implementation.md)。


## 12. 路线 D：D1原对象Bounds（借鉴NV-C）与D2时间身份扩展

### 12.1 必须拆成 D1 与 D2

| 子路线 | 检查保证 | 关键工程问题 |
|---|---|---|
| D1：对象 bounds 分析 | 在来源仍可追踪的范围内检查原对象边界 | IR／MIR 中对象身份如何保留、root 丢失如何报告 |
| D2：时间身份传播 | 在闭合的身份传播链中检测旧对象指针 | pointer load/store、memcpy、atomic、ABI 和回收 |

D1 可以先带来明显的覆盖提升。D2 会改变更多组件契约，不应隐藏在“给分配表增加一个字段”的小任务里。

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

### 12.7 不直接照抄 cuCatch 的理由

cuCatch 中 BST 是 **Base and Size Table**，GPUArmor 的 BST 则可以指 **Binary Search Tree**，两者名称容易混淆。[P01][P04] 自研设计要分别讨论“表中存什么”和“怎样索引表”。

此外，能找到一个 reaching base pointer，不意味着它一定是最初 allocation base；跨函数或内存加载可能已经成为新的分析 root。本文 D2 的闭合身份传播要求比“局部后端分析恢复 bounds”更强，成本也更高。

预算：在已有KMD契约可复用时，P＋D生产核心约46–89 kLOC、测试约39–74 kLOC；只完成 D1 的受控原型应按任务单单独裁剪，不能承诺同时具有完整 D2 能力。

<a id="route-d-platforms"></a>

### 12.8 参考配置③ NV-C的原对象来源与D2时间身份扩展

**出处：** D1借鉴NVIDIA编译期Memcheck的base/bounds[N02][N04]及cuCatch[P01]；D2借鉴cuCatch的时间元数据和LLVM HWASan tag/L04的研究机制。**融合与自研部分：** SSA对象来源、Kernel参数sidecar、跨call/phi/select/存储的companion metadata以及atomic pointer一致发布，属于目标ABI自主设计，不宣称学术首创。

**组件：** Compiler传播原对象及指针身份；KMD保留映射/队列契约但不会替UMD恢复pointer来源；UMD登记base/size/object_id/epoch；D1/D2的原对象身份与指针传播必须由实际设备代码执行检查。

**原对象 Bounds、Unknown Provenance 的 GPU 设备代码单源为**[组件指南 §2.1～§3](real-gpu-components-implementation.md)。


D2只有“当前对象表含generation”仍不足以分辨同GPUVA旧裸指针，必须传播指针预期身份。

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

### 13.5 E的Hybrid集成来源（不新增独立空间谓词）

**出处：** E采用NVIDIA hybrid instrumentation的产品策略[N02][N05]，融合B2＋D1＋受控C fallback。**本项目原创工程**是Sanitizer ABI版本、module manifest、patch/compiled选择、按模块/地址空间的覆盖等级和unknown来源降级协议；不能假设不同模块混用等于同模块未插桩函数自动补全。

**组件：** Compiler产出检查能力manifest；KMD复用现有代码映射/完成契约；UMD选择编译插桩或patch并共享Shadow/对象表；不同检查模式最终均在实体GPU执行并明确报告实际覆盖等级。

**Hybrid 模块 ABI 选择与 Runtime Patch Fallback 伪代码只维护在**[组件指南 §5～§6](real-gpu-components-implementation.md)。


E按共享对象表和设备检查库去重预算，不把B/C/D公共模块重复计费。

## 14. 路线 F：真实Tag/Bounds芯片条件性研究

### 14.1 可选硬件方向

| 方向 | 硬件／ISA 支持 | 软件支持 | 能力边界 |
|---|---|---|---|
| Tag compare | pointer tag、memory tag、同步 fault／屏蔽语义 | tag 分配、传播、清除／重用 | 通常有碰撞与粒度边界 |
| Bounds check 指令 | metadata handle／base-size 检查、准确错误 PC | 对象分析、descriptor 管理 | 无正确来源仍无法证明原对象 bounds |
| Metadata cache／MLB | per-SM 缓存、失效和一致性协议 | profile、lookup、free 生命周期 | cache 大小和工作集有关 |
| 同步事件 trace | barrier／atomic／queue 事件输出 | race/sync 分析、流量控制 | 全访存 trace 的带宽可能不可接受 |

### 14.2 编译器、KMD、UMD及硬件支持

F1/F2 **仅在实体 GPU 真正具备 Tag/Bounds ISA、RTL、元数据失效和 Fault/终止语义**时成立。Compiler 选择实际指令，UMD 发布对象与生命周期，KMD/Firmware 接通硬件资源和 Context 状态。软件使用普通 GPU 指令检查 Bounds 不能冒充硬件。

通用 RTL/设备故障伪代码及验收在[组件指南 §8～§9](real-gpu-components-implementation.md)；缺失的芯片能力应明确 `UNSUPPORTED/FUTURE_HARDWARE`，不得将软件 kLOC 当作 RTL/DV 成本。

### 14.3 选择 F 的证据门槛

建议满足以下条件后再进入正式硬件投入：

1. 软件原型已有稳定错误分类和合理覆盖。
2. 至少一组代表性业务证明 metadata／检查指令是主要瓶颈，而非日志或 CPU wrapper。
3. 采集对象工作集、热点和缓存命中曲线。
4. 验证硬件方案不会让 free／alias／peer 的一致性成本抵消热路径收益。
5. 获得下一代芯片和软件发布节奏的一致资源安排。

### 14.4 预算解释

第16章P＋F生产基础软件核心约23–46 kLOC、测试约22–42 kLOC；前提是用于对象检查的**F1 Tag或F2 Bounds硬件已经存在或另有交付，相关KMD基础契约也可复用**。KMD新增使能按真实驱动契约缺口另计。缺少这些能力时，不能用这张软件表报价完整 F。ISA／RTL／DV／固件的新增工作必须另行立项。

<a id="route-f-platforms"></a>

### 14.5 方案出处、融合与本项目新增工作

F1的Tag表示与传播可参考LLVM HWASan原理，但只有目标实体GPU真正支持相应ISA、Tag存储和失效规则才能列为硬件方案；F2的硬件Bounds/Metadata机制还需要RTL、固件、KMD和UMD完整配合。全部硬件机制在缺乏芯片证据时必须标为未支持。代码接口详见[硬件部分](real-gpu-components-implementation.md#9-rtl实体-gpu-mmutagbounds仅真实硬件已有或另行实现-f)。
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

### 16.1 估算口径、三组件归属与适用边界

**本章全部数字均为【设计估算】，不是仓库实测、厂商内部数据、已完成实现的统计或工期承诺。** 尚未审计目标 GPU 软件仓库；区间用于拆分责任、比较投入和安排技术摸底。统一单位为 kLOC，1 kLOC 约为 1,000 行新写或实质修改的手写代码，同一行重复修改不反复计入。核心代码与手写测试分别列示；空行、注释、机械生成代码、未修改的第三方依赖均不计入。

生产软件按 **Compiler、KMD、UMD 三个顶层责任包**组织。这里是预算归属，不是执行位置或代码仓库布局：

- **Compiler＝Pass／后端／编译链接支持＋device 检查库。** 前一子项在编译工具链工作，后一子项链接进设备程序并在 GPU 上执行；不能把设备检查开销解释为编译阶段开销。
- **KMD＝内核态驱动中实际新增或实质修改的契约实现。** 只有已有接口无法满足检查器所需的映射、完成、错误和隔离契约时才追加；复用已有能力不计为新增实现。固件不是 KMD，缺失的固件配合单独列出。
- **UMD＝SDK/API 接入、Host runtime、用户态 driver/loader 和 CLI/符号化等用户态工具的责任包。** SDK 暴露注册和配置入口；Host runtime 维护 API、队列与 graph 语义；用户态 driver/loader 管理对象、模块和提交；CLI/报告工具负责启动、符号化和结果消费。上述模块可分库、分进程，不要求把 SDK 或 CLI 嵌入底层 UMD driver。


**C纯runtime交付与历史统计口径必须分开：** §16.3原Compiler预算含GPU设备Patch代码，并非必须修改生产编译器；在纯C方案下Compiler新增源码=0、Runtime Binary Rewriter属于UMD，详情见§16.5a。下列历史分组保留便于复核，但不能按组件修改清单直接套用。

本章以**已有KMD契约足够**为基础假设，仍需目标LLVM GPU后端、UMD和实体GPU。C额外需要成熟decode/encode与合法patch装载；F只在硬件能力真正存在或另立项交付后成立。基础表包含失败路径与工程化，但不覆盖任意语言ABI与全部历史硬件。

**当前正式预算只对应B～F的实体GPU生产能力**，完整Init/Race/Sync另见§16.7。F所需RTL/DV/硅验证不包含在软件估算中。

区间端点逐项相加仅形成规划范围，不是统计置信区间；下限对应基础设施和接口较完备，上限对应更多受控边界与兼容工作，架构前提失效仍可能超出上限。

### 16.2 公共底座 P：按三个生产组件归属，只计一次

| 责任包／范围 | 核心 kLOC | 手写测试 kLOC | 包含内容 |
| --- | --- | --- | --- |
| Compiler | 2–4 | 2–4 | Pass/后端 1–2、测试 1–2；device 检查库 1–2、测试 1–2。option、manifest、context、报告缓冲、基础 ABI。 |
| KMD | 0 | 0 | 现有映射、队列完成、错误回传和隔离契约已足够；没有新增 KMD 实现。 |
| UMD | 6–11 | 5–8 | SDK/Host runtime/用户态 driver 联合项 4–7、测试 3–5；CLI/符号化工具 2–4、测试 2–3。 |
| **三生产组件小计** | 8–15 | 7–12 | 只表示生产侧软件责任包，不含固件和新硬件。 |

KMD 的“0”表示不新增该组件代码，不表示接口免于验证。调用既有接口的分配、失败完成、取消、资源回收等集成用例计入UMD与实卡测试；若这些用例证明契约不足，再转入 §16.8 的缺失能力包。

### 16.3 B／C 在 P 之上的新增量

以下每行均未含 P；KMD 列为既有契约满足时的基线。设备运行库归入Compiler交付包但在真实GPU执行。

| 路线 | 责任包 | 新增核心 kLOC | 新增手写测试 kLOC | 核心工作范围 | 测试工作范围 |
| --- | --- | --- | --- | --- | --- |

| B | Compiler | 12–22 | 10–18 | 访存/atomic/intrinsic/mask 插桩、多地址空间 lowering、shared/private 布局；设备 shadow 查询和报告 helper。 | 优化交互、任意访问长度、masked/EVL、原子语义、helper 不重入插桩及 SIMT 前进性。 |
| B | KMD | 0 | 0 | 复用现有 GPUVA/映射、完成、错误回传及所选终止策略的契约；不足时按实际缺口选 K-M/K-E/K-A。 | 新增 KMD 测试为 0；对既有契约的端到端用例包含在 UMD/设备运行测试。 |
| B | UMD | 5–11 | 4–9 | shadow 资源、poison/unpoison 排序、context/JIT 链接、池分配及 CLI 覆盖/资源不足报告。 | 异步 alloc/free、shadow 可见性、映射失败、卸载回收、日志满和合法别名。 |
| C | **历史Compiler交付包（非编译器源码必改）** | 6–13 | 5–11 | 该分组中主要是GPU设备Patch stub和可选MC适配，**不表示生产Compiler必须修改**；纯runtime时可全部由UMD/运行时工具交付。 | Patch透明性、寄存器/谓词恢复、地址宽度和原子语义；不重计已有解码器。 |
| C | KMD | 0 | 0 | 复用已有正规代码加载/执行、映射、完成及所选终止契约；调试接口仅在实现依赖时要求，缺口按需选 K-M/K-E/K-A/K-X。 | 新增 KMD 测试为 0；代码生命周期和故障归因的集成测试计在 UMD/设备集成测试。 |
| C | UMD | 17–32 | 15–28 | 运行时重写、CFG/branch relocation、资源重算、patch cache、JIT/unload；CLI 恢复原 PC。 | 分支/调用/常量区重定位、并发加载、缓存版本、拒绝未知编码、资源声明及异常路径。 |

**C（NVIDIA-R）务必区分“交付包归属”与“生产Compiler源码修改”。** 设备Patch是GPU执行的代码，可用未经修改的现有目标编译器构建工具CUBIN/Device Library；机器码decode/re-encode/CFG/resource修正由UMD Runtime Binary Rewriter实现。**纯runtime C实际修改生产Compiler源码可以为0**。原表把GPU设备Patch库及可选MC适配统计在历史Compiler包，不能将6–13 kLOC解读为“必须修改LLVM后端”。按照实际负责人重分类时，应把此份工作迁入UMD或独立运行时工具责任包，不能在两边重复收费。单一访存事件采集不等于完整race/sync分析；I1/S1/R1 按 §16.7 选定的工作包补充。

### 16.4 D／E／F 在 P 之上的新增量

以下每行均未含 P；KMD 列为既有契约满足时的基线。设备运行库归入Compiler交付包但在真实GPU执行。

| 路线 | 责任包 | 新增核心 kLOC | 新增手写测试 kLOC | 核心工作范围 | 测试工作范围 |
| --- | --- | --- | --- | --- | --- |
| D | Compiler | 28–54 | 23–45 | Pass/后端传播 bounds/identity、指针 load/store/copy/atomic 和受控 ABI；设备 descriptor/pointer-shadow 协议。 | phi/select、spill/reload、部分 copy、值-身份原子配对、ABA、unknown 来源及受控跨函数边界。 |
| D | KMD | 0 | 0 | 复用已有 metadata 映射、context 隔离、完成及所选终止契约；不足时按需选 K-M/K-E/K-A。 | 新增 KMD 测试为 0；metadata 生存期与地址复用验证计入 UMD/设备集成测试。 |
| D | UMD | 10–20 | 9–17 | 对象 ID/epoch、参数 sidecar、外部对象注册、能力协商、metadata 退休与 CLI 保证等级。 | 参数布局、epoch 复用、并发退休、未插桩 writer 边界和跨模块协商。 |
| E | Compiler | 40–76 | 33–63 | B 全量＋D 独有部分＋C stub 子集和模式边界；共享检查 ABI、manifest 和 descriptor。 | 模式间值/身份流转、重复插桩防护、版本协商、provenance 降级及元数据一致性。 |
| E | KMD | 0 | 0 | 共享 B/D/C 所需契约并集；不足时选 K-M/K-E/K-A/K-X，每包只计一次。 | 新增 KMD 测试为 0；跨模式集成验证已在 E 的 UMD/设备运行测试去重。 |
| E | UMD | 21–41 | 19–38 | 按模块选择编译插桩或受控 binary fallback、缓存和版本校验、共同对象表及分级报告。 | 混合模块、JIT/graph 重放、共享对象身份、未知模块和拒绝重复 patch。 |
| F | Compiler | 9–18 | 9–18 | 已有硬件能力的 intrinsic/指令选择与 ABI；设备 tag/bounds/metadata helper 及报告。 | 目标指令 lowering、硬件能力协商、错误 PC、metadata 版本和不支持设备回退。 |
| F | KMD | 0 | 0 | 基线假设所需硬件资源/故障/上下文接口也可复用；缺口选 K-M/K-E/K-A/K-F。 | 新增 KMD 测试为 0；缺失契约的驱动验证另在条件包，不用此数覆盖固件或 DV。 |
| F | UMD | 6–13 | 6–12 | 资源初始化、tag/descriptor 生命周期、上下文协商、失效请求及诊断工具。 | 释放后失效、context switch、已有支持范围内的 alias/peer、故障归因和资源不足。 |

#### 责任包内的可追溯子项

下表保留原五项预算中 Compiler 与 Device runtime 的原始区间。P 是公共底座，其余均为路线增量；将两子项相加即得上表的 Compiler 列。

| 范围 | Pass／后端核心 | Pass／后端测试 | 设备检查库核心 | 设备检查库测试 |
| --- | --- | --- | --- | --- |
| P | 1–2 | 1–2 | 1–2 | 1–2 |

| B | 7–13 | 6–11 | 5–9 | 4–7 |
| C | 0–2 | 0–2 | 6–11 | 5–9 |
| D | 18–34 | 15–29 | 10–20 | 8–16 |
| E | 23–44 | 19–36 | 17–32 | 14–27 |
| F | 5–10 | 5–10 | 4–8 | 4–8 |

UMD 责任包同样保留两个可追溯子项。原预算没有进一步将 SDK、Host runtime 和用户态 driver 三者量化分开，因此联合区间不能冒充三个模块分别测得的 SLOC；代码审计后再按接口和文件归属细分。

| 范围 | SDK／Host runtime／driver 核心 | SDK／Host runtime／driver 测试 | CLI／符号化工具核心 | CLI／符号化工具测试 |
| --- | --- | --- | --- | --- |
| P | 4–7 | 3–5 | 2–4 | 2–3 |

| B | 4–8 | 3–6 | 1–3 | 1–3 |
| C | 14–26 | 12–23 | 3–6 | 3–5 |
| D | 7–14 | 6–12 | 3–6 | 3–5 |
| E | 16–31 | 14–28 | 5–10 | 5–10 |
| F | 4–8 | 4–8 | 2–5 | 2–4 |

上述子项单位均为 kLOC；它们已经包含在路线表内，不能再次加入总量。D 包含受控接口集合内的 D2，不能据此承诺任意 C++ ABI、任意第三方库或任意未插桩 writer 都保留身份。

### 16.5 从 sanitizer 零起点建设的生产基础总量

“零起点”仅指Sanitizer自身，仍要求已有的LLVM GPU后端、GPU执行栈及UMD/KMD基础能力。单位kLOC，核心与手写测试分开；以下是**设计估算，不是测量结果**。

| 方案 | 生产核心 kLOC | 生产手写测试 kLOC | 生产核心＋测试 kLOC |
|---|---:|---:|---:|
| P＋B | 25–48 | 21–39 | 46–87 |
| P＋C | 31–60 | 27–51 | 58–111 |
| P＋D | 46–89 | 39–74 | 85–163 |
| P＋E | 69–132 | 59–113 | 128–245 |
| P＋F | 23–46 | 22–42 | 45–88 |

不得将P重复叠加到多条路线；KMD实际缺失的契约按§16.8另计。F的生产软件预算**只有在实体GPU所需硬件能力已存在或另行交付时**才有效；不存在硬件机制不能将软件或固件开销直接当作“硬件辅助”已完成。
#### 16.5a 纯运行时 C 的责任归属：Compiler 源码可零修改

| 组件 | 生产核心 kLOC | 手写测试 kLOC | 责任 |
|---|---:|---:|---|
| 生产 Compiler 新增源码 | 0 | 0 | 不需要重新编译用户应用；可使用既有LLVM/MC构建工具代码 |
| UMD＋Runtime Binary Rewriter＋GPU Patch库 | 31–60 | 27–51 | 包括解码/编码/重定位、设备stub、装载与诊断；历史Compiler包的设备库成本全部转列，不重复计费 |
| KMD（已有正规加载/映射/完成/异常契约） | 0 | 0 | 契约缺口另见§16.8 |
| **生产软件参考总计** | **31–60** | **27–51** | 不含额外硬件/固件，不是已审计SLOC |

### 16.6 E 混合方案的生产代码去重计算

E 只复用一套公共协议 P、Shadow/对象表、设备报告和 KMD 资源契约。C fallback 是受控子集，不是重新实现整个 C。

| 组成（生产 Compiler/UMD/KMD） | 核心 kLOC | 手写测试 kLOC |
|---|---:|---:|
| 公共底座 P | 8–15 | 7–12 |
| B 完整设备检查增量 | 17–33 | 14–27 |
| D 在 B 上独有的身份/ABI代码 | 27–51 | 22–42 |
| C 的受控二进制 fallback 子集 | 9–18 | 8–17 |
| 跨模式接口与模块覆盖边界 | 8–15 | 8–15 |
| **P＋E 生产软件合计** | **69–132** | **59–113** |

另一种按责任主体核对：Compiler（含设备库）P＋E为42–80/35–67，UMD为27–52/24–46，复用足够契约时KMD新增0；两组求和仍为69–132/59–113。硬件扩展与缺失的KMD/Firmware契约另列，不重复收费。
### 16.7 Initcheck、Racecheck、Synccheck 的独立新增预算

以下为已存在公共设备事件及报告ABI后的**生产软件规划区间**，只统计Compiler（含实体GPU设备检查库）与UMD；既有KMD契约能复用时KMD新增0。手写测试是设计分配而非既有源码计数。

| 额外真实GPU能力 | Compiler 核心/测试 kLOC | UMD 核心/测试 kLOC | 生产核心合计 kLOC | 生产手写测试合计 kLOC |
|---|---|---|---:|---:|
| I1 Initcheck逐字节定义性 | 5–10 / 4–8 | 3–6 / 3–6 | 8–16 | 7–14 |
| S1 Synccheck barrier/mask/phase | 5–10 / 5–10 | 2–4 / 2–4 | 7–14 | 7–14 |
| R1 shared race HB/FSM | 11–22 / 11–22 | 3–7 / 3–7 | 14–29 | 14–29 |
| R1扩展至global和跨kernel | 8–16 / 8–17 | 5–11 / 5–12 | 13–27 | 13–29 |
| 重复访问/碰撞模式 | 6–12 / 5–10 | 2–4 / 2–5 | 8–16 | 7–15 |

各checker可以复用设备端事件及报告，但必须按真实GPU scope、barrier、async与原子语义独立验收。不得把初始化位图当值级MSan，也不得把一次调度下无race当成任何调度都安全。
### 16.8 KMD 条件增强与其他前提缺口

#### 16.8.1 KMD 只为缺失契约追加

下列是对**既有可用 KMD**做有限扩展的设计区间。先逐接口确认现有能力；足够时该包为 0，只在确有缺口时计费。各包核心/测试均为净新增，功能和文件边界必须在任务分解时再次去重。

| 条件包 | 新增核心 kLOC | 新增手写测试 kLOC | 实现范围 | 测试范围 | 前提与去重边界 |
| --- | --- | --- | --- | --- | --- |
| K-M 内存/metadata 映射 | 3–7 | 3–8 | GPUVA 保留/映射权限/稀疏预映射或受控映射服务及资源回收。 | 映射冲突、权限、生命周期、失败注入和并发回收。 | 仅扩展已有 GPUVM/页表设施；不新建 GPU 内存管理器，不重复 UMD 的 shadow 分配策略。 |
| K-E 故障/完成证据 | 2–4 | 2–5 | dispatch/context 标识、故障记录、失败完成与用户态回传。 | fault 与完成竞态、错误归因、reset 后旧完成、缓冲溢出。 | 已有队列和中断设施；不含 K-A 的取消状态机，不重复 UMD 的报告格式/符号化。 |
| K-A 安全 abort 的 KMD 部分 | 2–5 | 2–5 | 调用已有硬件/固件取消能力、隔离受影响 dispatch/context、确认停止后清理。 | 取消与完成竞态、残留 DMA、队列阻塞、隔离及资源安全回收。 | 硬件支持已存在；这里仅计 KMD。固件尚需接线时另见正文。 |
| K-X 正规可执行代码接口 | 1–3 | 1–3 | 既有加载/调试设施中补受控代码版本、可执行映射或资源校验接口。 | 权限、版本、并发加载卸载、错误拒绝与兼容。 | 不重计 UMD 的 decoder、CFG、patch cache 或 branch relocation；不以此预算承诺绕过既有安全策略。 |
| K-F F 专属状态契约 | 3–7 | 3–8 | 已有 tag/bounds 等硬件资源的 context 保存恢复、失效及能力接口。 | context switch、失效顺序、旧身份残留、资源隔离。 | 排除 K-M 的映射、K-E 的故障输送、K-A 的 abort；不含 RTL/DV/新硬件。 |

#### 16.8.2 每个方案的基线与条件附加

下表右侧仅表示“列出的相关能力恰好全部缺失，但仍符合包内前提”时的组合上/下界，**不是每条路线的默认附加费**。实际增量等于所缺子集之和；B/C/D/E 共享包，E 尤其只能取契约并集。

| 路线 | 已有契约时核心 | 已有契约时测试 | 可触发的缺失能力包 | 所列全缺时核心 | 所列全缺时测试 |
| --- | --- | --- | --- | --- | --- |

| B | 0 | 0 | K-M＋K-E＋K-A | 7–16 | 7–18 |
| C | 0 | 0 | K-M＋K-E＋K-A＋K-X | 8–19 | 8–21 |
| D | 0 | 0 | K-M＋K-E＋K-A | 7–16 | 7–18 |
| E | 0 | 0 | K-M＋K-E＋K-A＋K-X | 8–19 | 8–21 |
| F | 0 | 0 | K-M＋K-E＋K-A＋K-F | 10–23 | 10–26 |

K-A 的 KMD 核心 2–5、测试 2–5，是将原“安全 dispatch/context abort 软件接线核心 4–10、测试 4–10”规划为内核驱动与外部固件两个责任包。若固件也尚未接通，再列**固件接线核心 2–5、测试 2–5**，两者合计仍为 4–10/4–10。这个二分是本次设计估算，不是原预算已有实测拆分。若固件已完成，只计缺失的 KMD 部分；用户态错误消费已在 UMD 基础范围时不得再加。若硬件取消能力本身不存在，上述区间不适用，先验证停止/隔离的可行性。

K-F只为未具备的真实驱动状态契约计费。F基础预算已包含UMD元数据初始化和GPU设备检查支撑，同一份源码不得搬入K-F重复收费。

#### 16.8.3 保留的其他条件项

| 缺失前提 | 核心 kLOC | 手写测试 kLOC | 处理边界 |
| --- | --- | --- | --- |
| 成熟机器码重写基础不存在 | 15–35 | 15–40 | 仅补 decoder/encoder、语义与基础重定位/资源接口；剔除 C 已计的 CFG、patch 管理和缓存。 |
| GPUVA/metadata 映射契约可通过既有设施有限扩展 | 采用 K-M | 采用 K-M | 若没有可行地址空间/页表或权限机制，先做可行性原型；不能默认 K-M 能新建整套 GPUVM。 |
| 多 GPU/IPC/外部导入为首版硬需求 | 单独分解 | 单独分解 | 规范对象身份、别名、一致性和跨设备时序；现有 API 规模与保证未确定，不给统一追加数。 |
| F 需要新硬件或大幅新增固件 | 不纳入软件表 | 不纳入软件表 | ISA/RTL/DV/性能模型/硅片和超出接线范围的固件另立计划。 |
| 完整D2时间身份、值级MSan或通用跨CPU/GPU TSan | 单独设计 | 单独设计 | 必须设计真正能在GPU执行的检查代码，不得以Host事件记录代替；具体语义另外评审。 |

### 16.9 只统计真实GPU产品组件与条件KMD缺口

正式预算只计算Compiler（含GPU设备运行库）、UMD、必要时的KMD；同一公共底座P只计算一次。以下是在目标软件栈已有、KMD原有映射/完成接口足够、软件硬件能力前提成立时的**未实测设计范围**，不包含实体芯片RTL/DV/新增固件及全新GPU栈。C在纯runtime方案下可把设备patch库和重写器全部归入UMD，使生产Compiler新增源码为0，不改变核心总额。

| 方案 | 核心 kLOC | 手写测试 kLOC | 核心＋测试 kLOC | 关键前提 |
|---|---:|---:|---:|---|
| P＋B | 25–48 | 21–39 | 46–87 | LLVM设备检查器、Shadow、UMD映射与生命周期 |
| P＋C | 31–60 | 27–51 | 58–111 | 成熟目标ISA decoder/encoder和合法patch装载 |
| P＋D | 46–89 | 39–74 | 85–163 | D1/D2保留各自来源和ABI前提 |
| P＋E | 69–132 | 59–113 | 128–245 | B/D/C受控子集去重，模式兼容 |
| P＋F | 23–46 | 22–42 | 45–88 | **实体F1/F2 Tag/Bounds对象检查硬件已存在**；芯片新增另估算 |

组合公式为“所选P＋路线生产软件区间＋**经源码审计证明确实缺失**的KMD契约包＋可选额外checker区间＋另行立项的Firmware/RTL/DV”。不得直接叠加多个P；同一映射、报告、对象表、终止代码只计一次。详见[生产预算JSON](estimates-v1.8.json)与[逐组件源码入口](real-gpu-components-implementation.md)。
### 16.10 将区间转为项目预算与算术校验

建议以两周左右技术摸底收集 opcode/访存类型、kernel 与 allocator/API 数量、已支持的 patch 模式、module ABI 数量、现有 KMD 契约、实际设备与编译产物差异，以及**目标实体GPU实际ISA、元数据访问、完成和fault接口的代码级契约**。先做可运行的最小链路，再按“接口、算法、失败路径、兼容、验证”建立工作包和责任人；人月需要结合团队经验与审查/验证成本估计，不用统一的每天写多少行换算。

本章数值的复核规则为：

- 编译器责任包包含真实GPU设备库的交付，UMD责任包包含SDK/Host Runtime/Loader/CLI；KMD具备所需既有契约时新增0。
- 三生产组件小计＝Compiler＋KMD＋UMD；从Sanitizer零起点总量＝公共P＋所选路线增量。纯C Runtime可将原设备库工作转列至UMD。
- E＝P＋B＋D在B上的独有部分＋C受控子集＋跨模式集成；共享对象表与KMD代码不重复计算。
- 区间相加采用 `[a,b]＋[c,d]＝[a＋c,b＋d]`；核心与手写测试独立相加，再检查核心＋测试总量。

[当前B～F生产软件预算](estimates-v1.8.json)只保留生产三组件与实际KMD/硬件依赖，所有数字均为设计估算，不代表目标GPU已实现。

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

诊断 NOP 基线用于解释编译扰动，不能替代未插桩基线；GPUArmor 的不同数字正说明了这一点。[P04]

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

**关键判定：** 真实访问是否超出有效范围；需要的核心信息是地址、宽度、边界（M1/M2/M3）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S01 错误与合法对照、检测方法、组件职责及验收](../samples/S01/README.md) · [CUDA 完整源码](../samples/S01/cuda.cu) · [AMD HIP 完整源码](../samples/S01/amd.hip.cpp) · [三种厂商参考技术路径](S01-vendor-implementation-guide.md)。

<a id="case-s02"></a>

### 18.2 S02：访问首地址合法，宽访问的末端越界

**关键判定：** 首字节合法但末端非法；需要的核心信息是完整访问足迹（M2/M3）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S02 错误与合法对照、检测方法、组件职责及验收](../samples/S02/README.md) · [CUDA 完整源码](../samples/S02/cuda.cu) · [AMD HIP 完整源码](../samples/S02/amd.hip.cpp)。

<a id="case-s03"></a>

### 18.3 S03：范围正确但违背指令对齐约束

**关键判定：** 是否满足该指令的对齐要求；需要的核心信息是opcode 与对齐契约（M2）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S03 错误与合法对照、检测方法、组件职责及验收](../samples/S03/README.md) · [CUDA 完整源码](../samples/S03/cuda.cu) · [AMD HIP 完整源码](../samples/S03/amd.hip.cpp)。

<a id="case-s04"></a>

### 18.4 S04：从对象 A 越界，恰好落入仍存活的对象 B

**关键判定：** 地址有效但原对象越界；需要的核心信息是原对象身份与 bounds（M3）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S04 错误与合法对照、检测方法、组件职责及验收](../samples/S04/README.md) · [CUDA 完整源码](../samples/S04/cuda.cu) · [AMD HIP 完整源码](../samples/S04/amd.hip.cpp)。

<a id="case-s05"></a>

### 18.5 S05：内存池子分配、granule 孔洞与 tensor view 边界

**关键判定：** 外层 allocation 合法，逻辑子边界非法；需要的核心信息是子对象、维度、stride（M4）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S05 错误与合法对照、检测方法、组件职责及验收](../samples/S05/README.md) · [CUDA 完整源码](../samples/S05/cuda.cu) · [AMD HIP 完整源码](../samples/S05/amd.hip.cpp)。

<a id="case-s06"></a>

### 18.6 S06：静态／动态 shared 越界与 block 身份

**关键判定：** block 的共享对象边界；需要的核心信息是launch/block 与共享布局（M2/M3）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S06 错误与合法对照、检测方法、组件职责及验收](../samples/S06/README.md) · [CUDA 完整源码](../samples/S06/cuda.cu) · [AMD HIP 完整源码](../samples/S06/amd.hip.cpp)。

<a id="case-s07"></a>

### 18.7 S07：Private／stack 越界与返回后使用指针

**关键判定：** 栈对象边界与 frame 生命周期；需要的核心信息是线程、frame、对象身份（M3/M5）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S07 错误与合法对照、检测方法、组件职责及验收](../samples/S07/README.md) · [CUDA 完整源码](../samples/S07/cuda.cu) · [AMD HIP 完整源码](../samples/S07/amd.hip.cpp)。

<a id="case-s08"></a>

### 18.8 S08：Free 已生效、地址尚未复用的 UAF

**关键判定：** free 生效后仍访问旧对象；需要的核心信息是生命周期与 poison 状态（M5）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S08 错误与合法对照、检测方法、组件职责及验收](../samples/S08/README.md) · [CUDA 完整源码](../samples/S08/cuda.cu) · [AMD HIP 完整源码](../samples/S08/amd.hip.cpp)。

<a id="case-s09"></a>

### 18.9 S09：同一 GPUVA 被新对象复用后的旧指针

**关键判定：** 新旧裸地址相同但身份不同；需要的核心信息是指针预期 epoch 与当前 epoch（M5）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S09 错误与合法对照、检测方法、组件职责及验收](../samples/S09/README.md) · [CUDA 完整源码](../samples/S09/cuda.cu) · [AMD HIP 完整源码](../samples/S09/amd.hip.cpp)。

<a id="case-s10"></a>

### 18.10 S10：Invalid free——内部指针与错误所有权

**关键判定：** 传给释放接口的身份／所有权合法；需要的核心信息是allocator、原始基址、owner（M6/A1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S10 错误与合法对照、检测方法、组件职责及验收](../samples/S10/README.md) · [CUDA 完整源码](../samples/S10/cuda.cu) · [AMD HIP 完整源码](../samples/S10/amd.hip.cpp)。

<a id="case-s11"></a>

### 18.11 S11：同一分配实例被释放两次

**关键判定：** 同一生命周期被重复释放；需要的核心信息是对象状态和释放记录（M6）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S11 错误与合法对照、检测方法、组件职责及验收](../samples/S11/README.md) · [CUDA 完整源码](../samples/S11/cuda.cu) · [AMD HIP 完整源码](../samples/S11/amd.hip.cpp)。

<a id="case-s12"></a>

### 18.12 S12：在约定检查点仍未释放的显存对象

**关键判定：** 约定检查点仍有应释放对象；需要的核心信息是活跃账本与所有权策略（M6）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S12 错误与合法对照、检测方法、组件职责及验收](../samples/S12/README.md) · [CUDA 完整源码](../samples/S12/cuda.cu) · [AMD HIP 完整源码](../samples/S12/amd.hip.cpp)。

<a id="case-s13"></a>

### 18.13 S13：未初始化读取、部分字节写入与copy传播

**关键判定：** 被读的每个 byte 已有定义性来源；需要的核心信息是初始化位图与复制传播（I1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S13 错误与合法对照、检测方法、组件职责及验收](../samples/S13/README.md) · [CUDA 完整源码](../samples/S13/cuda.cu) · [AMD HIP 完整源码](../samples/S13/amd.hip.cpp)。

<a id="case-s14"></a>

### 18.14 S14：shared RAW、WAR与WAW竞争

**关键判定：** 冲突访问之间存在合法顺序；需要的核心信息是访问身份、HB、barrier（R1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S14 错误与合法对照、检测方法、组件职责及验收](../samples/S14/README.md) · [CUDA 完整源码](../samples/S14/cuda.cu) · [AMD HIP 完整源码](../samples/S14/amd.hip.cpp)。

<a id="case-s15"></a>

### 18.15 S15：global同block、跨block竞争与合法atomic

**关键判定：** 跨线程／block 的读写与原子语义；需要的核心信息是规范地址、order/scope（R1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S15 错误与合法对照、检测方法、组件职责及验收](../samples/S15/README.md) · [CUDA 完整源码](../samples/S15/cuda.cu) · [AMD HIP 完整源码](../samples/S15/amd.hip.cpp)。

<a id="case-s16"></a>

### 18.16 S16：跨kernel／queue竞争与event正确反例

**关键判定：** launch 间依赖是否覆盖冲突；需要的核心信息是queue/event 依赖图（R1/A1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S16 错误与合法对照、检测方法、组件职责及验收](../samples/S16/README.md) · [CUDA 完整源码](../samples/S16/cuda.cu) · [AMD HIP 完整源码](../samples/S16/amd.hip.cpp)。

<a id="case-s17"></a>

### 18.17 S17：block barrier缺失live参与者与phase错配

**关键判定：** 参与集合、到达代次符合约定；需要的核心信息是barrier ID、phase、线程集合（S1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S17 错误与合法对照、检测方法、组件职责及验收](../samples/S17/README.md) · [CUDA 完整源码](../samples/S17/cuda.cu) · [AMD HIP 完整源码](../samples/S17/amd.hip.cpp)。

<a id="case-s18"></a>

### 18.18 S18：warp／subgroup无效mask

**关键判定：** 调用者与命名参与者满足同步规则；需要的核心信息是lane ID、mask、目标语义（S1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S18 错误与合法对照、检测方法、组件职责及验收](../samples/S18/README.md) · [CUDA 完整源码](../samples/S18/cuda.cu) · [AMD HIP 完整源码](../samples/S18/amd.hip.cpp)。

<a id="case-s19"></a>

### 18.19 S19：async copy缺wait、等待了错误批次或phase

**关键判定：** 消费前完成对应复制及同步；需要的核心信息是copy group、完成、phase（S1/I1/R1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S19 错误与合法对照、检测方法、组件职责及验收](../samples/S19/README.md) · [CUDA 完整源码](../samples/S19/cuda.cu) · [AMD HIP 完整源码](../samples/S19/amd.hip.cpp)。

<a id="case-s20"></a>

### 18.20 S20：async free跨queue缺依赖与释放生效边界

**关键判定：** 所有使用都先于释放生效；需要的核心信息是提交、free 开始与回收证据（M5/A1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S20 错误与合法对照、检测方法、组件职责及验收](../samples/S20/README.md) · [CUDA 完整源码](../samples/S20/cuda.cu) · [AMD HIP 完整源码](../samples/S20/amd.hip.cpp)。

<a id="case-s21"></a>

### 18.21 S21：原子指令访问越界——原子性不保证地址有效

**关键判定：** 原子性不能替代空间合法性；需要的核心信息是原子实际宽度和范围（M2/M3）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S21 错误与合法对照、检测方法、组件职责及验收](../samples/S21/README.md) · [CUDA 完整源码](../samples/S21/cuda.cu) · [AMD HIP 完整源码](../samples/S21/amd.hip.cpp)。

<a id="case-s22"></a>

### 18.22 S22：尾部 mask／EVL——被屏蔽的非法地址不得产生误报

**关键判定：** 关闭的元素不产生虚构访存；需要的核心信息是lane、predicate、元素 mask、EVL（合法反例；M/R/I 插桩正确性）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S22 错误与合法对照、检测方法、组件职责及验收](../samples/S22/README.md) · [CUDA 完整源码](../samples/S22/cuda.cu) · [AMD HIP 完整源码](../samples/S22/amd.hip.cpp)。

<a id="case-s23"></a>

### 18.23 S23：只读对象与 image／surface——检查权限、坐标及寻址模式

**关键判定：** 只读属性、维度及寻址模式；需要的核心信息是资源描述符和访问模式（M1/M2/M3/A1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S23 错误与合法对照、检测方法、组件职责及验收](../samples/S23/README.md) · [CUDA 完整源码](../samples/S23/cuda.cu) · [AMD HIP 完整源码](../samples/S23/amd.hip.cpp)。

<a id="case-s24"></a>

### 18.24 S24：CPU/GPU共享、peer／IPC别名的身份与同步

**关键判定：** 可访问性、一致性和跨处理器顺序；需要的核心信息是系统 scope、规范对象、映射（R1/A1/M5）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S24 错误与合法对照、检测方法、组件职责及验收](../samples/S24/README.md) · [CUDA 完整源码](../samples/S24/cuda.cu) · [AMD HIP 完整源码](../samples/S24/amd.hip.cpp)。

<a id="case-s25"></a>

### 18.25 S25：atomic pointer 的值与身份不一致——合法程序也可能被错误插桩误报

**关键判定：** 指针值与身份来自同次发布；需要的核心信息是原子配对、发布序号、epoch（合法反例；D2 正确性）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S25 错误与合法对照、检测方法、组件职责及验收](../samples/S25/README.md) · [CUDA 完整源码](../samples/S25/cuda.cu) · [AMD HIP 完整源码](../samples/S25/amd.hip.cpp)。

<a id="case-s26"></a>

### 18.26 S26：未插桩库与 JIT——没有观测到访问不能当作检查通过

**关键判定：** 真实访问是否进入检查范围；需要的核心信息是模块 manifest、opcode 与 ABI（覆盖完整性）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S26 错误与合法对照、检测方法、组件职责及验收](../samples/S26/README.md) · [CUDA 完整源码](../samples/S26/cuda.cu) · [AMD HIP 完整源码](../samples/S26/amd.hip.cpp)。

<a id="case-s27"></a>

### 18.27 S27：graph replay捕获已释放对象与执行ID／allocation epoch

**关键判定：** 重放不能复活已失效的指针；需要的核心信息是捕获对象身份与执行实例（M5/A1）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

**唯一详细案例及运行入口：** [S27 错误与合法对照、检测方法、组件职责及验收](../samples/S27/README.md) · [CUDA 完整源码](../samples/S27/cuda.cu) · [AMD HIP 完整源码](../samples/S27/amd.hip.cpp)。

<a id="case-s28"></a>

### 18.28 S28：日志满、metadata 不足与错误终止——检查器必须说明检查是否完成

**关键判定：** 错误、覆盖不足与工具失败可区分；需要的核心信息是丢失计数、资源状态、abort 协议（所有 checker 的可靠性）。本节仅保留索引摘要，不重复维护示例代码与验收细节。

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

<a id="validation"></a>

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

### 19.6a Docker编译与GPU检测的证据分级

| 阶段 | 完成判定 | 本轮状态 |
|---|---|---|
| SOURCE_ADDED | 已提供完整CUDA/HIP源码、中文注释、README和manifest | **已完成28×2厂家对照源码** |
| STATIC_STRUCTURE_PASS | Python/Bash语法和目录清单、文档链接与分类一致 | 脚本/CI已编写；GitHub Actions须以实际运行结论为准 |
| GPU_COMPILED | 在指定容器版本/目标架构上，nvcc和hipcc真实成功，未静默忽略GPU ASan | **尚未验证** |
| GPU_DETECTED | 执行`--bad`后得到具备源PC/地址/设备身份的GPU检查报告 | **尚未验证** |
| GOOD_NO_FALSE_POSITIVE | 同样架构上执行`--good`，无GPU sanitizer误报 | **尚未验证** |
| UNSUPPORTED | 厂商checker不覆盖或ISA/Driver不支持，明确返回不支持 | 显式写在manifest，仍需针对平台核查 |

**不得混淆容器镜像与目标GPU。** Docker仅准备NVCC、Compute Sanitizer或HIP+ASan runtime，不能在普通CPU宿主上完成GPU Kernel检测。CUDA编译无需`-fsanitize=address`（这会混入CPU ASan）；AMD HIP使用`-fsanitize=address`时也需验证设备目标和instrumented runtime，不能把CPU报告冒充GPU报告。每次实验保留GPU架构、驱动、镜像、编译参数、Sanitizer版本、log和受支持指令集合。

### 19.6b GitHub Actions GPU硬件验收（可选启用，非CPU模拟）

**本项目新增真实GPU持续验证工作流：** [`.github/workflows/gpu-hardware.yml`](../.github/workflows/gpu-hardware.yml)搭配[GPU Runner与Actions配置说明](gpu-actions-ci.md)。NVIDIA目标采用Compute Sanitizer的memcheck/initcheck/racecheck，AMD目标采用`-fsanitize=address -shared-libsan --offload-arch=gfx...:xnack+`与真正instrumented HIP runtime。两边都不允许用Host CPU ASan、单纯API异常或`ERROR SUMMARY`非零替代设备侧具体错误证据。[N02][A01]

| 执行层 | 运行位置与操作 | 只能证明什么 |
|---|---|---|
| CPU静态GitHub CI | 标准`ubuntu-latest`，验证56份主源代码文件存在、脚本Python/Bash语法及合成日志解析 | 结构与解析器单元测试，**不证明GPU编译或检测** |
| NVIDIA GPU CI | 专用`gpu-nvidia` Runner，Docker GPU可见，S01/S13/S14可选择`smoke`，也可`compile-all`或`safe-all` | 只有获得正确具体GPU错误日志且合法对照没有误报时，才称本轮支持子场景成功 |
| AMD GPU CI | 专用`gpu-amd-xnack` Runner，`ROCM_ARCH`必须为真实支持的`gfx...:xnack+`，镜像插桩runtime必需可验证 | S01设备ASan必须报告`on amdgpu device`且错误类别吻合；普通CPU ASan不能代替 |
| 无可用GPU Runner / 未启用 | `GPU_CI_ENABLED`默认未设置，所有GPU硬件Job显示Skipped | **GPU_NOT_RUN，不是检出、通过或可用GPU平台的证明** |

**触发策略：** 仅可信master的push、schedule和workflow_dispatch；GPU job显式`GPU_CI_ENABLED=true`才调度。自托管Runner上不能让不可信`pull_request`或`pull_request_target`执行代码，需隔离/短生命周期Runner；GitHub托管T4大型GPU Runner仅在有对应组织/企业资质并完成配置后使用。危险S17/S18/S20/S27不在常规GPU CI中运行。所有GPU结果以原始日志与`results/<backend>/summary-*.json` Artifact存档，不可以凭CPU静态CI绿色状态发布“GPU Sanitizer全28例通过”。[C01][A04]

**状态（2026-10-21）：** 工作流/脚本/README已经提交，CPU静态CI有成功记录，GPU硬件Job尚未获得真实GPU运行结果。GitHub Actions GPU能提高验收自动化，但只有真实GPU Runner已配置且正确报告对应设备错误时，才能升级对应测试状态。

### 19.7 实体GPU逐层验收与不完整状态

| 层级 | 实际检查与证据 | 状态规则 |
|---|---|---|
| 编译/静态 | IR/MIR/ISA插桩覆盖、设备ABI和manifest | 只证明静态结构，不升级GPU_DETECTED |
| 实卡最小例 | 真实GPU执行S01～S28的合法与非法子例、保留源PC/宽度/lane/对象 | 真机报告匹配时可记GPU_DETECTED |
| KMD/UMD端到端 | 真实BO/GPUVA映射、queue依赖、GPU完成/fault、context abort/reset与释放 | 仅Host故障注入不能计设备访存检出 |
| 性能/长期运行 | 实体GPU应用基线、Sanitizer开销、显存/occupancy、长时间负载 | 需原始统计和相同条件对照 |

**KMD专项必须覆盖**：metadata/report映射成功与失败；跨queue正确/错误依赖；真正运行中遇错后的abort或可靠拒绝；alias/import与context销毁的引用和报告；以及报告缓冲满、metadata OOM情况下不虚报clean。只完成源码/脚本设计时均不能宣称实卡通过。
<a id="roadmap"></a>

## 20. 分阶段研发计划和团队安排

### 20.1 推荐交付阶段

下表时间是有经验团队、已有硬件／基础栈条件下的规划区间，表示主动开发日历跨度，不包含等待硬件、长周期平台故障或所有阶段的简单串行总和。

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

由于本任务已有明确的 GitHub 持续保存要求，建议在仓库以正文、证据笔记和进度记录共同维护；发生中断时从已提交的内容接续，不依赖聊天中某段未保存输出。

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

以下清单用于后续把方案落实到用户实际 GPU；它不是本轮交付的阻塞问题。

1. LLVM／Clang 分支版本，是否使用自定义中间表示或二次后端。
2. GPUVA有效位数、generic pointer、不同空间aperture及可用tag位。
3. warp/wave大小、独立线程调度、谓词、whole-wave执行语义。
4. 全部scalar/vector/atomic/async/tensor访存指令及修饰符。
5. 原子宽度、memory order/scope和支持的同步原语。
6. Host分配、设备分配、pool、VMM、managed、IPC、peer、图和JIT API清单。
7. KMD的OS/驱动架构、BO/GPUVA/导入接口、可执行代码加载、依赖与完成fence、fault精度，以及queued cancel、运行中abort、context/device reset的真实能力。
9. 真正要支持的语言／框架与闭源模块比例。
10. 第一版是开发者工具、CI工具，还是更强的运行时防护需求。
11. 必须支持的地址空间和错误类型，特别是对象／子对象与复用UAF。
11. 可投入团队、真实GPU硬件、可用显存与性能预算；明确三个软件责任包及设备库、Firmware/RTL等生产角色。

回答这些参数后，应优先修订第16章的估算和第20章排期，再进入具体实现设计。本轮结论已经足以决定首个原型的方向：公共P＋真机B＋独立的实体GPU S01合法/非法访存验收，以及明确预留D的对象身份协议。

<a id="references"></a>

## 23. 参考文献、阅读路径与核验记录

### 23.1 官方文档与上游源码

首版资料主要在 **2026-10-20** 核验；**第4章新增的机制与官方页面于2026-10-21补核**。未提供可靠单篇发布日期的动态文档，以版本与访问日为准；不把抓取日当成发布日期。

**[N01] NVIDIA. NVIDIA Compute Sanitizer.** 官方产品总览，动态页面。

- [规范入口](https://developer.nvidia.com/compute-sanitizer)
- 已读：工具分工及 API 总览。
- 用途：产品体系入门；initcheck 空间描述与新版手册存在更新差异，能力判断以 [N02][N05] 为准。

**[N02] NVIDIA. Compute Sanitizer User Manual.** 当前在线用户手册。

- [官方手册](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)
- 已读重点：各工具章节、Compile-time patching、Base-and-bounds analysis、Hybrid instrumentation、Memory Footprint、Error Actions。
- 用途：操作要求、编译／运行模式和能力范围；第4章的命令入口依据。

**[N03] NVIDIA. Compute Sanitizer API Reference Manual.** 当前在线 API 指南。

- [API 指南](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html)
- 已读：callback、patching、memory，以及特殊 launch 路径说明。
- 用途：资源／命令回调、patch 参数和元数据操作的职责分离；不能假设 device-side launch 都有 Host 回调。

**[N04] Mark Stephenson, Sana Damani, Anis Ladram. Better Bug Detection: How Compile-Time Instrumentation for Compute Sanitizer Enhances Memory Safety.** NVIDIA Technical Blog，2025-12-10。

- [官方博客](https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/)
- 已读：编译分析、fat pointer 思路、相邻 allocation 越界、实际使用与限制。
- 用途：理解 CUDA13.1 新方向；博客的部分场景性能表述不能转成通用平均开销。

**[N05] NVIDIA. Compute Sanitizer Release Notes.** 读取时最新章节为 Updates in 2026.3。

- [版本说明](https://docs.nvidia.com/compute-sanitizer/ReleaseNotes/index.html)
- 已读重点：2025.4、2026.3、Known Limitations、Known Issues。
- 用途：shared initcheck、function-level hybrid 限制、HMM／cluster／JIT 等当期条件。动态页面将继续变化，实际部署应记录安装版本。

**[N06] NVIDIA. CUDA-MEMCHECK User Manual. CUDA Toolkit 11.8.0.** 归档文档，页面最后更新 2022-10-03。

- [历史手册](https://docs.nvidia.com/cuda/archive/11.8.0/cuda-memcheck/index.html)
- 已读：旧版工具分工、race hazard 与 synccheck 报告章节。
- 用途：RAW/WAR/WAW、参与线程／mask 等错误分类的历史参照；不能用其硬件支持和限制描述2026产品。其已被弃用的状态也在归档文档中说明。

**[N07] NVIDIA. NVIDIA CUDA Compiler Driver NVCC Documentation.** 读取时在线版本13.4。

- [NVCC 手册](https://docs.nvidia.com/cuda/cuda-compiler-driver-nvcc/)
- 已读：device sanitize、PTXAS sanitize 选项与运行约束。
- 用途：编译插桩产物需要匹配检查器与工具链的契约。

**[N08] NVIDIA. CUDA Programming Guide.** 在线版，页面标注更新日期2026-09-10；访问及相关章节核验日期2026-10-20，不从更新日推定Toolkit版本。

- [官方入口](https://docs.nvidia.com/cuda/cuda-programming-guide/index.html)
- 重点章节：[流序分配／释放](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/stream-ordered-memory-allocation.html)、[CUDA C++内存模型](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/cuda-cpp-memory-model.html)、[语言扩展与同步](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/cpp-language-extensions.html)、[异步barrier](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/async-barriers.html)、[设备primitive](https://docs.nvidia.com/cuda/cuda-programming-guide/05-appendices/device-callable-apis.html)。
- 场景补充：[异步执行与event](https://docs.nvidia.com/cuda/cuda-programming-guide/02-basics/asynchronous-execution.html)、[系统内存](https://docs.nvidia.com/cuda/cuda-programming-guide/02-basics/understanding-memory.html)、[IPC](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/inter-process-communication.html)、[虚拟内存映射](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/virtual-memory-management.html)、[CUDA Graphs](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/cuda-graphs.html)。
- 用途：S15～S20、S24、S27的作用域、同步参与、phase、copy批次、释放生效、外部映射和重放语义参照；CUDA规则不能未经核验直接当作自研ISA规则。

**[N09] NVIDIA. Parallel Thread Execution ISA, Version 9.4.** 当前在线PTX ISA手册，访问日期2026-10-20。

- [官方ISA手册](https://docs.nvidia.com/cuda/parallel-thread-execution/index.html)
- 已读重点：访问宽度与自然对齐、bar.warp.sync、surface load/store及边界处理模式。
- 用途：S21原子访问足迹、S23资源坐标单位与TRAP／CLAMP／ZERO等模式的目标语义参照；自研目标以自身ISA规范为准，不据此假设任何硬件必须采用PTX规则。

**[A01] AMD. Using the AddressSanitizer on a GPU.** ROCm docs-7.2.4，页面属于 llvm-project 文档。

- [版本化官方文档](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)
- 已读：Compiling for ASan、instrumented ROCm libraries、运行准备、Runtime overhead／reporting、Known issues。
- 用途：GPU ASan 的实际编译／runtime 支持和已知边界；不混入7.13.0 technology preview的独有问题。

**[A02] Lawrence Livermore National Laboratory. Using El Capitan Systems: Debugging Tools.** 当前官方集群指南。

- [LLNL 指南](https://hpc.llnl.gov/documentation/user-guides/using-el-capitan-systems/using-el-capitan-systems-debugging-tools)
- 已读：GPU Address Sanitizer、编译／运行和Known Issues；示例注明rocmcc/7.0.0。
- 用途：实际环境设置、magic compiler 和API回归用例；它不是所有ROCm版本的支持矩阵。

**[I01] Intel. Device-Side Compiler Sanitizers.** oneAPI DPC++/C++ Developer Guide 2026.0，页面日期2026-04-28。

- [设备 sanitizer 官方文档](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/device-side-compiler-sanitizers.html)
- 已读：ASan、MSan、TSan的设备支持、配置和运行层。
- 用途：异构运行时与多checker产品形态；平台限制需合读[I02]。

**[I03] oneAPI Unified Runtime. Introduction — Sanitizers（官方运行时架构规范）。**

- [官方页面](https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers)
- 核验日期：2026-10-21；已核对UR loader层对device-side ASan及其他checker的接口定位。
- 用途：第4.3节比较编译器/异构UMD runtime分工；该规范中个别checker标为planned，应与[I01] 2026编译器指南的实际产品支持分别解释，不直接推断目标GPU支持。

**[I02] Intel. Compiler Sanitizers.** oneAPI DPC++/C++ Developer Guide 2026.0。

- [系统要求与限制](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/compiler-sanitizers.html)
- 已读：System Requirements、Limitations。
- 用途：Linux／Ponte Vecchio单卡、kernel串行化、规模导致检查跳过等条件。

**[M01] 摩尔线程. MUSA Compute Sanitizer 用户指南.** 当前在线指南。

- [官方用户指南](https://docs.mthreads.com/mooreperf/mooreperf-doc-online/musa_compute_sanitizer/user_guide/)
- 已读：主要功能、输出控制、功能范围与限制。
- 用途：global OOB、API错误、显存泄漏与公开限制；未据此断言内部二进制插桩实现。

**[M02] 摩尔线程. MUSA Compute Sanitizer v1.1.1 版本说明.**

- [官方版本说明](https://docs.mthreads.com/mooreperf/mooreperf-doc-online/musa_compute_sanitizer/releasenote/v1.1.1_release_note/)
- 已读：环境要求、API error过滤、修复项。
- 用途：约束具体版本和部署环境；未查到可靠发布日期，故不补写猜测日期。

**[L01] LLVM Project. AddressSanitizer.** Clang官方文档。

- [ASan 使用与结构](https://clang.llvm.org/docs/AddressSanitizer.html)
- 已读：工具结构、支持的错误类型、使用与限制。
- 用途：通用框架理解；CPU性能描述不直接转作GPU预算。

**[L02] Sanitizers Project. AddressSanitizerAlgorithm.** 原项目算法说明。

- [算法原始说明](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm)
- 已读：shadow、redzone、分配／释放与检查流程。
- 用途：经典1:8映射；具体GPU布局由目标实现决定。

**[L03] LLVM Project. AddressSanitizer.cpp.** 在线源码快照，读取时页首24.0.0git。

- [上游源码](https://llvm.org/docs/doxygen/AddressSanitizer_8cpp_source.html)
- 已核：shadow mapping、AMDGPU地址空间过滤、stack poison的目标条件。
- 用途：确认已有复用基础与剩余目标工作；不是用户仓库固定commit的替代。

**[L04] LLVM Project. Hardware-assisted AddressSanitizer Design Documentation.**

- [HWASan设计文档](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html)
- 已读：tag算法、granule／short granule、插桩与比较。
- 用途：tagging方案的机制和精度边界。

**[L05] LLVM Project. LLVM Language Reference Manual.**

- [LLVM IR语义](https://llvm.org/docs/LangRef.html)
- 已核相关部分：pointer/address space、atomic、masked/VP memory operations、poison／未定义行为等。
- 用途：自研插桩必须遵守的IR语义；本文的目标实现细节属于工程建议。

**[L06] LLVM Project. User Guide for AMDGPU Backend.**

- [AMDGPU后端文档](https://llvm.org/docs/AMDGPUUsage.html)
- 已核：address spaces、memory model和目标执行约定相关内容。
- 用途：成熟GPU后端的目标语义参照；不能直接复制其编号与硬件假设。

下列四条均为Linux内核官方在线文档，访问日期为2026-10-20；已阅读所列章节。未检查用户实际KMD、固件、ioctl实现或硬件，因此本文没有把通用Linux能力登记成用户平台既有能力。在线文档可能持续更新，落地实现应固定到所采用内核／驱动版本。

**[K01] The Linux Kernel Documentation. DRM Memory Management.** [官方文档](https://docs.kernel.org/gpu/drm-mm.html)。已读GEM职责、DRM GPUVM Overview、GPU Scheduler backend callbacks；支持BO／GPUVA关系、驱动特定接口、`run_job`／`cancel_job`边界。由此提出的sanitizer职责划分属于工程推导。

**[K02] The Linux Kernel Documentation. Userland interfaces — Device reset.** [官方文档](https://docs.kernel.org/gpu/drm-uapi.html#device-reset)。已读Kernel Mode Driver、User Mode Driver与Reporting causes of resets；支持reset责任、fence错误和context状态向用户态传播的约定，不证明某平台具有细粒度abort。

**[K03] The Linux Kernel Documentation. Buffer Sharing and Synchronization (dma-buf).** [官方文档](https://docs.kernel.org/driver-api/dma-buf.html)。已读三种共享／同步原语、exporter／importer与CPU同步说明；支持dma-fence完成语义、共享关系及缓存一致性不等于访问互斥的边界。

**[K04] The Linux Kernel Documentation. VFIO — “Virtual Function I/O”.** [官方文档](https://docs.kernel.org/driver-api/vfio.html)。介绍IOMMU/设备隔离、DMA map及IOMMUFD，不能单独证明目标GPU具有对象级内存检查。







**[N10] NVIDIA. CUDA Driver API — Virtual Memory Management.**
- [GPUVA保留/映射/权限官方API](https://docs.nvidia.com/cuda/archive/13.4.2/cuda-driver-api/cuda_driver_api/group__CUDA__VA.html)；2026-10-21核验。
- 依据：`cuMemAddressReserve / cuMemCreate / cuMemMap / cuMemSetAccess / cuMemUnmap`，支持GPU页映射但不等于字节级逻辑对象Sanitizer。

**[A03] AMD. HIP Runtime API — Virtual memory management.**
- [官方GPU VMM文档](https://rocm.docs.amd.com/projects/HIP/en/latest/how-to/hip_runtime_api/memory_management/virtual_memory.html)；2026-10-21核验。
- 依据：VMM支持属性、硬件granularity及`hipMemAddressReserve / hipMemCreate / hipMemMap / hipMemSetAccess`；属于GPUVA管理，不是CPU mprotect。

**[C01] NVIDIA. CUDA Container Images — Supported tags.**
- [官方镜像标签列表](https://gitlab.com/nvidia/container-images/cuda/blob/master/doc/supported-tags.md)；2026-10-21核验。
- 样例默认使用`nvidia/cuda:13.1.2-devel-ubuntu24.04`，具体tag和宿主驱动匹配应现场核对；需要实际NVIDIA GPU。

**[A04] AMD. Running ROCm Docker Containers.**
- [ROCm官方Docker运行说明](https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.3/how-to/docker.html)；2026-10-21核验。
- 使用官方dev-complete镜像、宿主`/dev/kfd`和`/dev/dri`；这只是容器条件，不保证目标GPU ASan支持。

### 23.2 原始论文与可复现材料

**[P01] Mohamed Tarek Ibn Ziad, Sana Damani, Aamer Jaleel, Stephen W. Keckler, Mark Stephenson. cuCatch: A Debugging Tool for Efficiently Catching Memory Safety Violations in CUDA Applications.**

*Proceedings of the ACM on Programming Languages*, 7, PLDI, Article111，2023，24页。DOI：[10.1145/3591225](https://doi.org/10.1145/3591225)。

- [作者／NVIDIA页面](https://research.nvidia.com/publication/2023-06_cucatch-debugging-tool-efficiently-catching-memory-safety-violations-cuda)
- [作者完整论文](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)
- 已读全文相关章节：§3数据结构、§4编译分析、§5覆盖、§6性能、§7边界。
- 证据界限：论文原型与实验集；未在本项目硬件复现。

**[P02] Ariel Eizenberg, Yuanfeng Peng, Toma Pigli, William Mansky, Joseph Devietti. BARRACUDA: Binary-level Analysis of Runtime RAces in CUDA programs.**

PLDI2017，126–140页。DOI：[10.1145/3062341.3062342](https://doi.org/10.1145/3062341.3062342)。

- [作者完整论文](https://mansky-cs.red.uic.edu/wp-content/uploads/sites/862/2021/08/4.-barracuda.pdf)
- [会议页面](https://pldi17.sigplan.org/details/pldi-2017-papers/32/BARRACUDA-Binary-level-Analysis-of-Runtime-RAces-in-CUDA-programs)
- 已读：PTX提取／JIT、事件日志、CPU分析、epoch／vector clock、实验和同步推断边界。
- 证据界限：历史平台与模型，不能当现行所有GPU同步的完整证明。

**[P03] John Jacobson, Martin Burtscher, Ganesh Gopalakrishnan. HiRace: Accurate and Fast Data Race Checking for GPU Programs.**

SC2024。DOI：[10.1109/SC41406.2024.00042](https://doi.org/10.1109/SC41406.2024.00042)。

- [SC24作者完整论文](https://userweb.cs.txstate.edu/~mb92/papers/sc24.pdf)
- [会议页面](https://sc24.supercomputing.org/proceedings/tech_paper/tech_paper_pages/pap649.html)
- [作者实验材料](https://github.com/JohnJacobsonIII/HiRace-Artifact-SC24)
- 已读：FSM、CAS、元数据、scoped clocks、限制和实验表；以正式稿为主，未把早期arXiv标题／数字混入。
- 证据界限：其原实现使用源码重写／wrapper；本文建议在LLVM后端实现同类思想，属于新的工程移植。

**[P04] Mohamed Tarek Ibn Ziad, Sana Damani, Mark Stephenson, Stephen W. Keckler, Aamer Jaleel. GPUArmor: A Hardware-Software Co-Design for Efficient and Scalable Memory Safety on GPUs.**

*ACM Transactions on Architecture and Code Optimization*, 23(3)，2026，25页。正式online日期2026-07-23。DOI：[10.1145/3815783](https://doi.org/10.1145/3815783)。

- [NVIDIA作者页面](https://research.nvidia.com/index.php/publication/2026-05_gpuarmor-hardware-software-co-design-efficient-and-scalable-memory-safety-gpus)
- [2026作者文稿](https://d1qx31qr3h6wln.cloudfront.net/publications/TACO2026_GPUArmor_preprint.pdf)
- 已读重点：平台与基线、§10.1、Fig.12开销分解、Fig.14比较；作者页面与期刊online日期分别保留。
- 证据界限：GA100 traces＋NVAS模拟；文稿页眉残留模板信息不作为最终卷期日期。

**[P05] 同[P04]作者. GPUArmor: A Hardware-Software Co-design for Efficient and Scalable Memory Safety on GPUs.**

arXiv:2502.17780v1，2025-02-25。

- [2025预印本](https://arxiv.org/abs/2502.17780)
- [版本化全文](https://arxiv.org/html/2502.17780v1)
- 用途：版本与基线对照；不是一项可独立重复计数的新实验或另一个产品。

**[P06] Saurabh Singh, Jaewon Lee, Seonjin Na, Hyesoon Kim. Towards Standardized Evaluation of GPU Memory Safety with GMSBench.**

arXiv:2609.08871v1，2026-09-08。

- [论文记录](https://arxiv.org/abs/2609.08871)
- [版本化全文](https://arxiv.org/html/2609.08871v1)
- 已读：测试分类、setup／manifestation、Table I／II及平台。
- 证据界限：新预印本和研究基准，未称行业标准；本文没有运行其149项测试。

**[P07] Anubhab Ghosh, Ajay Nayak, Dhananjay Rao Thallikar Shyam, Arkaprava Basu. Chasing Elusive Memory Bugs in GPU Programs.**

arXiv:2601.21552v1，2026-01-29；工具名SCuBA。

- [论文记录](https://arxiv.org/abs/2601.21552)
- [版本化全文](https://arxiv.org/html/2601.21552v1)
- 已读：输入依赖、逻辑分区、host/kernel关系、分析与evaluation。
- 证据界限：预印本；未核实后续正式发表或本地复现，不把人工注入错误说成全部新发现野外bug。

**[P08] Mark Stephenson, Sana Damani, Mohamed Tarek Ibn Ziad, Anis Ladram, Michael Garland. SuperCollider: Scalable and Effective Data Race Detection for CUDA.**

*Proceedings of the ACM on Programming Languages*, 10, PLDI, Article261，2026-06，25页。DOI：[10.1145/3808339](https://doi.org/10.1145/3808339)。

- [作者页面](https://mtarekibnziad.onrender.com/publication/supercollider)
- [作者完整论文](https://mtarekibnziad.onrender.com/files/preprint_PLDI26_SuperCollider.pdf)
- 已读全文并提取核对：重复访问机制、限制、§5.3.1与§5.3.2性能；130项与59项子集分别记录。
- 证据界限：结果依赖配置、观测窗口和数据集，不能作为race freedom证明。

**[P09] Mark Stephenson, Sana Damani, Mohamed Tarek Ibn Ziad, Anis Ladram, Michael Garland. Reproduction Package for PLDI 2026 Article “SuperCollider: Scalable and Effective Data Race Detection for CUDA”.**

Zenodo v1，2026-03-17。DOI：[10.5281/zenodo.19058944](https://doi.org/10.5281/zenodo.19058944)。

- [复现材料与许可说明](https://zenodo.org/records/19058944)
- 已核：artifact记录和许可范围，包含专有评估runtime二进制。
- 未执行实验包；其可下载性不能解释为全部实现均开源。

### 23.3 建议阅读次序

| 读者／任务 | 优先资料 | 阅读后应产出 |
|---|---|---|
| 技术负责人做选型 | N02/N05、A01、P01、P04 | 覆盖目标与路线条件 |
| LLVM后端工程师 | L05/L06、L03、P01、P08 | 插桩点、目标指令清单、身份传播边界 |
| UMD/runtime工程师 | N03/N08、A01/A02、I01/I02、K01～K04、本文第8/18章 | 逻辑分配与内核资源对应、命令生效和报告协议 |
| KMD工程师 | K01～K04、本文第8/16/19章 | 映射、完成、fault/abort/reset支持清单和新增包 |
| 测试/性能工程师 | P03、P06、P08、本文第17/18/19章 | 数据集分母、三基线、失败与跳过记录 |
| 产品化与部署 | N05/N07、I02、M01/M02 | 版本矩阵、已知限制、错误处理策略 |

### 23.4 检索方法和来源选择

实际使用互联网检索服务的 system1/system2 引擎、原始页面打开、官方站内链接追踪、论文全文检索及必要的PDF文本提取。权威产品条件优先使用官方来源；关键论文机制和数字回到作者全文；未使用个人博客作为设计事实依据。

以下为本轮实际执行的代表性查询原文；它们是检索日志节选，不是穷尽全部搜索或可用于PRISMA的统计：

| 日期 | 引擎 | 实际查询 | 目的 |
|---|---|---|---|
| 2026-10-20 | system1 | site.docs.nvidia.com/compute-sanitizer "Compile-time patching" | 定位编译插桩手册；首个site限制语法不规范，后续以规范URL直接核验 |
| 2026-10-20 | system1 | site:developer.nvidia.com/blog "Compile-Time" "Sanitizer" | 找到2025-12官方说明 |
| 2026-10-20 | system1 | site:arxiv.org GPUArmor memory safety GPU 2026 | 发现版本、近期基准与相关论文 |
| 2026-10-20 | system1 | site:clang.llvm.org AddressSanitizer HardwareAssistedAddressSanitizerDesign | 定位上游机制文档 |

搜索引擎没有为本调研提供可验证的全库命中总数，因此没有虚构“筛选了N篇论文”的统计。已选来源按同一论文的预印本／正式版本去重理解；GPUArmor P04/P05保留两个记录仅用于版本比较。

1.1版新增实例采用官方页面直接打开、站内链接与页面定位核验，重点复核[N02]的synccheck/async copy/stream-ordered检查、[N03]的device graph launch、[N08]的十个语义章节、[N09]的访存及资源指令，以及[L05]的masked/VP操作。此轮补充没有新增关键词检索统计。新增代码是原创语义示例，未执行CUDA编译、GPU运行或用户平台测试。


1.3版核验了NVIDIA/AMD/LLVM与Host Runtime的公开实现边界；具体厂商产品与本项目工程适配不可混为一谈。

### 23.5 核验中保留的分歧与限制

- NVIDIA产品总览与新版initcheck章节存在更新不同步，正文采用新版手册和release notes。
- HiRace正式稿的一处百分比表述与Table I使用的分母不一致，本文没有引用“93%”作为positive recall。
- cuCatch和GPUArmor的“BST”含义不同，正文已明确；两篇的性能成本也不等于包含所有Host wrapper的应用端到端时间。
- GPUArmor作者页面、期刊online日期和文稿模板页眉不同，参考文献采用期刊元数据，性能机制采用已读作者全文。
- 对MUSA内部插桩机制，当前公开证据不足以确认用户参考描述的具体架构；正文保留可证实的功能，不补写内部实现。
- 第16章按三个生产组件统计，仅KMD确实缺失的接口另计；预算未基于目标GPU源码审计或实测。
- Linux DRM/VFIO是公开接口机制资料，不证明目标GPU已有同等fault/abort硬件能力。

### 23.6 文档完成标准与后续入口

本版覆盖真实GPU可执行的B～F候选、Compiler/Device Runtime/UMD/KMD/Firmware/RTL/CLI逐组件伪代码与预算。28个错误场景均保留合法对照和真实硬件验收责任，尚未在目标自研GPU完成部署或验证。

下一步若进入实现，应以第22章的实际平台参数收敛M0/M1设计；把选定路线转成具体目录、接口、opcode和测试工作包。本版是一份可用于该决策的调研文档，尚不是已实现的GPU sanitizer。
