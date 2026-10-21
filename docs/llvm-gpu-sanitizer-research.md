# 自研 LLVM GPU 后端 Sanitizer 技术调研与实施方案

**版本：1.3 · 资料核验日期：2026-10-21 · 文档语言：中文**

**用途：技术立项、编译器／驱动／架构联合评审、研发任务拆分与预算讨论。**

**1.3 版修订：** 用户明确确认 **QEMU 全系统平台现有可执行目标GPU ISA的执行模型（Q2）**；本版将其作为既有前提而非待建设能力，并按实际缺失接线重新解释预算。[友商及LLVM机制深挖](#vendor-deep-dive)逐一说明原理/代码入口/证据限制；[全部路线出处及融合矩阵](#route-provenance-matrix)明确借鉴、融合与本项目需自行实现的部分，保留28个详细实例。

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
9. 路线 A：cmodel 检测
10. 路线 B：LLVM ASan 式影子内存
11. 路线 C：运行时二进制插桩
12. 路线 D：对象边界与时间身份传播
13. 路线 E：编译插桩与运行时补充的混合方案
14. 路线 F：硬件辅助
15. Initcheck、Racecheck 与 Synccheck 的独立设计
16. [组件改动与代码量估算](#component-estimates)
17. 性能、显存和可观测性设计
18. [检测场景与实例分析](#detection-scenarios)
19. [验证基准与验收标准](#validation)
20. [分阶段研发计划和团队安排](#roadmap)
21. 主要风险与决策条件
22. [架构评审时应确认的项目参数](#platform-parameters)
23. [参考文献、阅读路径与核验记录](#references)

---

## 1. 决策结论与推荐路线

### 1.1 推荐结论

**【工程建议】优先建立统一的分配／生命周期登记和诊断协议，以 LLVM 插桩在真机上提供基础内存检查，以 cmodel 做独立交叉验证；随后增加对象边界与时间身份传播。**

推荐的建设顺序是：

1. 建设公共底座 P：统一对象登记、异步生命周期、模块能力声明、错误报告和符号化。
2. 同时验证 A 的窄范围 cmodel 检测和 B 的真机全局内存检查。前者帮助确认语义，后者给出实际性能和可用性。
3. 扩展 B 的地址空间和指令覆盖；增加 D1 的编译器对象 bounds 分析，弥补“越界后落入另一合法分配”的漏检。
4. 对明确需要地址复用后 UAF 检测的场景，再做 D2 的完整指针时间身份传播；与仅查询分配表的方案分别命名、分别验收。
5. 当闭源库、已有二进制、JIT 模块构成实际覆盖缺口时，增加 E 的二进制补充。C 的工程成本与已有解码、重编码和 patch 基础设施高度相关。
6. Racecheck、Initcheck、Synccheck 作为独立检查器迭代；如果软件方案的真实瓶颈足够集中，再评估 F 的硬件辅助。

这一路线要求编译器与UMD可扩展，KMD提供所需映射、完成、故障和终止契约；已有契约充分时可复用，不要求每条路线都新增KMD代码。**用户确认既有QEMU全系统平台可执行目标GPU ISA，本项目现状按Q2记录**：它可用于Guest UMD/KMD到真实模型访存的系统验证，未实现的只是具体checker/metadata/report接线；独立功能cmodel是否与QEMU复用同一模型仍待源码确认，不能重复计为独立oracle。第一版不必同时完成四类检查器、全部CUDA类功能或完整二进制重写器。

### 1.2 三组件交付与两类验证平台

一个LLVM Pass可以插入检查，但不能独自提供资源生存期、特权映射、队列完成或可靠终止。本文按以下三项组织生产软件支持与预算，并将功能级cmodel、基于QEMU的全系统虚拟平台（QEMU-based full-system virtual platform）作为独立执行／验证环境逐方案讨论。

| 顶层组件 | 定义与职责 | 本报告的子模块归属 |
|---|---|---|
| 编译器 | 前端选项、中端分析、后端访存覆盖、对象来源与模块契约 | Pass/后端/MC；随工具链交付的GPU设备检查库分别列项 |
| KMD（Kernel-Mode Driver，内核态驱动） | 特权资源、BO/GPUVA映射、提交/完成、fault、context及可用终止/reset | 只承担目标驱动确有的内核接口与状态；硬件/固件前提另列 |
| UMD（User-Mode Driver，用户态驱动） | 逻辑资源与命令、loader/JIT/patch、元数据调度、API诊断和报告 | SDK/Host runtime、用户态driver、CLI/符号化分层实现并归入同一预算组 |

**归属不等于运行位置。** 设备检查库在GPU执行，不能因其预算归入编译器包就称为编译期检查；CLI和Host SDK也不必嵌入底层UMD库。这样分组用于满足三组件比较口径，不要求搬动用户现有源码目录；团队调整归属时保持总量去重。

**模型与平台分别回答两件事：能否执行目标检查器，以及能否提供独立oracle。** 已有完整ISA模型可能直接执行B/C/D/E的插桩产物；额外增加独立模型检查仍需要模型hook和分析逻辑。通用QEMU的TCG回调不自动观察GPU内部每条访存；**本项目QEMU已具有目标GPU ISA执行模型，属于Q2**，因此可在模型真实访存入口直接加检查。Q0/Q1/Q3保留作其他部署形态的比较，而非声称本项目仍缺GPU执行器；详见第7.7节。[Q01][Q02]

### 1.3 三项必须坚持的能力区分

**第一，地址有效性不等于对象边界正确。** 某个地址属于当前存活分配，并不能证明从对象 A 推导出来的指针有权访问对象 B。ASan 式 redzone 和分配区间查询都可能漏掉这种情况。[L02][N04]

**第二，分配表 generation 不等于完整时间安全。** 地址重用后，新旧裸指针可以具有完全相同的位值；只有当前对象表而没有指针自身的历史身份，就无法区分两者。D2 必须解决指针身份的生成、传播、存取和跨模块边界。

**第三，插桩能够观察到一次执行，不等于证明所有输入和调度都安全。** 输入生成、静态分析、符号执行和 cmodel 调度扰动需要作为验证补充；新近研究也直接讨论了输入依赖和逻辑子分区导致的漏检。[P07]

### 1.4 立项时最实用的选择表

| 项目现实条件 | 优先选择 | 主要原因 |
|---|---|---|
| ISA／UMD 尚在频繁变化，已有可运行 cmodel | P＋A 窄原型，同时预留 B 接口 | 先稳定错误语义与协议，避免反复重写 |
| 有真机和稳定 LLVM 后端，用户可重编译 | P＋B，逐步引入 D1 | 能较快形成实际可用工具并测量真机开销 |
| 大量对象越界落入另一合法 buffer | D1，必要时 D2 | 需要保留原对象身份，单纯地址查询不足 |
| 高频异步内存池、地址复用和指针缓存 | D2＋完整生命周期事件 | redzone／quarantine 只能覆盖部分时间错误 |
| 闭源库覆盖是近期交付硬要求，且已有成熟 patch 框架 | C 或 E | 以最终二进制为入口补充覆盖 |
| 下一代芯片可修改，软件原型已找到明确瓶颈 | F | 有证据地设计 metadata cache／检查指令 |

## 2. 项目背景、建设目标与研究范围

<a id="background"></a>

### 2.1 项目背景

**建设动因与问题边界。** 自研 GPU 的调试需要回答：某次访问是否违反对象边界、生命周期或同步约定；违规发生在哪个 kernel、指令和线程；相关资源由谁创建、释放和传入。仅验证 kernel 能编译、执行并给出预期数值，无法同时回答这些问题。**【工程建议】** 应把 sanitizer 建设为贯通编译器及设备检查库、KMD、UMD及诊断工具的动态检查能力，使应用与基础软件团队可以围绕同一份证据定位问题。

**【官方事实】** NVIDIA 将内存访问、初始化、竞争和同步检查分为不同工具，且区分能够归因到指令及线程的精确访问错误与归因能力有限的硬件异常。[N02] **【工程建议】** 本项目应沿用这种能力拆分，避免用一个“支持 sanitizer”的标签掩盖不同保证。下述风险属于立项需要验证的场景，不表示用户平台已经存在相应缺陷；实际 ISA、执行模型、故障精度和工作负载仍待确认。

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
| 页保护／guard page | 拦截触及禁止访问页的操作 | 同页子对象越界、落入其他可访问页的越界；依赖 GPUVA 与权限能力 |
| 功能级 cmodel | 按已实现ISA语义执行检查产物，或增加独立模型检查 | 不自动执行真实KMD，不覆盖所有调度和真机时序 |
| QEMU全系统虚拟平台 | 接入目标GPU模型后运行Guest应用/UMD/KMD与设备协议 | 只有设备桩时不执行目标kernel；全系统不等于全GPU语义或周期精确模型 |

**CPU ASan 不能只改目标名后直接移植。** **【官方事实】** ASan 本身由编译插桩与 runtime 配合组成；ROCm GPU ASan 还要求相应运行库配合，其文档明确列出设备地址空间覆盖及 shadow 维护的限制。[L01][A01] **【工程建议】** 可复用 LLVM 分析与 shadow／redzone 思路，但必须重新确定设备可访问的元数据布局、shared/private 身份、最终 ISA 覆盖、异步生命周期、设备报告通道和错误终止协议。SIMT 下的锁、分支和退出路径还须单独验证前进性，不能假设 CPU 线程的处理方式自然成立。

**面向的使用者与工作流。** 下表为拟议交付用途。

| 使用者 | 典型工作流与所需结果 |
|---|---|
| 应用／算子开发者 | 选择 checker 和保留行信息的构建方式，运行输入，获得访问位置、对象边界及分配／释放证据，缩小复现并回归修复 |
| 编译器工程师 | 将最小源码依次对照 IR、MIR／反汇编与覆盖清单，区分源程序违规、错误 lowering 和漏插桩 |
| UMD／runtime 工程师 | 对照逻辑分配、copy、launch、free，核查元数据与设备命令生效顺序 |
| KMD 工程师 | 核查BO/GPUVA映射、提交完成、fault、abort/reset和上下文清理，区分设备要求与现有接口 |
| QEMU 平台工程师 | 集成目标GPU模型、Guest驱动与DMA/IRQ，构造系统故障注入，声明实际模型覆盖 |
| 架构／cmodel 工程师 | 用同一语义用例核对地址空间、mask、原子和同步行为，再用真机验证模型边界与终止能力 |
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
| R1 | 在声明的内存与同步模型内识别冲突访问 | 无序冲突被报告，合法同步对照通过；概率方案另报重复试验结果 |
| S1 | 检查参与 mask、到达集合、phase 与作用域 | 非法同步被诊断；合法子组与目标允许的退出方式通过 |
| A1 | 检查 API、资源身份与异步依赖关系 | 同队列和跨队列生命周期正反例通过；Host 提交顺序不替代设备依赖 |

**共同质量目标。** 以下均为【工程建议】，与功能检出同时验收。

1. **可定位、可观察。** 报告关联 checker、错误类别、build ID、kernel／PC、访问范围、空间及线程身份；可获得时补充源行、对象边界、分配／释放栈和 queue／event。缺少符号时保留原始 PC 与模块信息；报告截断、丢失数量和检查是否继续必须可见。复杂问题提供相关事件时间线，不凭结果猜测责任组件。
2. **覆盖可核验。** 维护“能力×地址空间×指令族×模块／执行路径”的支持矩阵，关联插桩清单和实际运行记录。未知 opcode、未插桩模块、来源丢失及工具资源不足必须产生明确状态。已覆盖且本次未报错、部分覆盖和工具失败不得合并为 clean。
3. **语义正确与 SIMT 前进性。** LLVM 明确要求 masked-off 元素不发生内存访问。[L05] 检查器应相应避免无效元素地址查询引入额外 fault，并区分向量元素 mask 与硬件活跃 lane。合法执行应保持目标语言／ISA 可观察语义；不得通过意外全局串行化掩盖竞争。锁、日志满和单 lane 出错路径均须验证无新增死锁；错误后的跳过或终止按实际硬件能力约定，不能直接让其他线程永久等在 barrier。
4. **可用性与性能可度量。** 提供可复现的编译／运行入口、checker 选择、过滤、符号化及 CI 退出状态。分别测量编译、首次初始化、kernel、端到端时间，以及 shadow、描述符、日志的实际内存、寄存器与 spill 开销。性能门槛须在首轮真机基线后按代表性工作负载确定，不套用 CPU 或论文数字；受控串行模式应单独披露。
5. **可部署、可演进。** 编译器及设备检查库、KMD、UMD、模块manifest和报告格式需有版本及兼容检查；模型和QEMU接入也记录版本及能力集合。新 ISA、allocator、JIT 或外部模块加入时，补充支持清单与回归，不静默沿用旧保证；公共协议稳定后再按需求增加独立 checker。

**P0／P1／P2 优先级。** 下表用于安排投入，不新增工期或人力承诺；P0／P1／P2 是优先级，公共底座仍记作 P，A～F 是技术路线，第20章的 M0～M6 是里程碑。

| 优先级 | 建议范围与退出条件 | 与原计划的关系 |
|---|---|---|
| P0：基础可用 | 公共底座、选定空间的 M1/M2/M6、基础 A1 与明确窗口的 M5；真机可诊断，cmodel 小用例独立核对，覆盖和资源失败可见 | 主要支撑里程碑 M0～M2；先完成 M1 窄原型 |
| P1：语义与场景扩展 | 扩展空间和指令；按需求建设 D1 的 M3/M4、受控 D2 的 M5，以及 I1/S1/shared R1；每项分别验收身份、同步和性能 | 对应里程碑 M2～M4，可在公共协议稳定后并行 |
| P2：生态与效率 | 按实际缺口补二进制／JIT／外部库及更广的 R1；仅在测得瓶颈和收益后研究硬件辅助 | 对应里程碑 M4 扩展、M5/M6，另审依赖与预算 |

**非目标与完成标准。** 首版不承诺任意第三方 ABI 的完整时间安全、全值级 MSan、通用跨 CPU/GPU TSan、所有输入与调度的安全证明，也不承担恶意 kernel 隔离、芯片全面正确性验证或生产环境近零开销承诺。每项限制应出现在支持矩阵和用户报告中。

按第19章验收：声明支持且确实触发的确定性错误集应全部出现预期诊断；合法反例集无已知误报；unsupported、超时及工具失败单列；概率检测另设统计口径。每类场景应具备“错误实例—合法对照—触发条件—预期证据—适用路线—漏检边界”，并由独立 oracle 与真机证据逐步确认。研发阶段的交付物包括版本化协议、实现与测试、命令和输入 hash、原始性能数据、安装／CI 指南及已知限制；完成一个最小演示不能替代这些证据。

### 2.3 本报告采用的项目假设

用户尚未提供GPU实现仓库、ISA、GPUVA布局、编译器分支、KMD/UMD接口清单，以及功能级cmodel和QEMU目标设备模型的详细设计。为使方案和预算可讨论，本文采用以下可替换假设：

- 已有能编译并执行计算 kernel 的 LLVM GPU 后端，已有基本调试行信息。
- 已有可执行kernel的Host runtime/UMD与KMD基本栈，以及能运行目标ISA的功能级cmodel；KMD是否满足sanitizer所需映射/错误处理契约须逐项核查。
- 不假设已经有完整QEMU GPU设备模型。Q0/Q1/Q2/Q3按第7.7节分别讨论；Q2的设备桥接和Guest集成工作在第16章单独估算。
- 至少支持全局内存、共享／局部数据存储和线程私有／栈地址语义；具体 address-space 编号以目标定义为准。
- GPU 支持基本原子操作和可由驱动管理的设备缓冲；不假设具有 CPU 式虚拟内存缺页能力、Top-Byte Ignore、128-bit 原子或可靠线程级恢复。
- 第一阶段面向开发和 CI 调试，不将 sanitizer 当作恶意 kernel 的隔离边界。
- 代码量预算针对已有基础设施上的 sanitizer 建设，不包含从零开发LLVM后端、完整UMD/KMD、完整GPU ISA模型或整个GPU；QEMU平台基础缺口另列，不能藏进几行插桩预算。

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
| S1 | barrier／mask／异步同步误用 | 参与集合、到达次数、phase 和作用域 | 功能模型无法自动覆盖全部调度 |
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
| cmodel 能给出真机 sanitizer 的性能结论 | 功能cmodel用于语义验证；真机性能需实测，硬件设计需校准时序模型 | 不把模拟器运行时间当产品overhead |
| QEMU全系统模式天然能检测GPU内部访存 | 需要实际执行目标ISA的GPU模型或真机检查器；TCG vCPU插件不是通用GPU插桩器 [Q01][Q02] | 区分Q0/Q1/Q2/Q3并记录检测位置 |
| 软件方案必须修改KMD，或完全不依赖KMD | 真机需要KMD提供契约；是否新增代码取决于已有能力 [K01][K02] | 逐方案列必需支持和条件增量 |

**官方来源之间也有更新不同步。** NVIDIA 产品总览仍保留 initcheck 仅 global 的概括，而当前用户手册与 release notes 已列出 shared 支持；本报告按后两者的版本信息处理这一冲突。[N01][N02][N05]

## 4. 友商产品与上游实现：机制、实现接线及可复用边界

<a id="vendor-deep-dive"></a>

**本章采用“证据分级”：官方明确公开的算法／接口＝【官方事实】；从接口推导的内部数据结构＝【工程推断】，不是厂商源码事实；本项目推荐的实现＝【自研设计】。** 不能把同样具有“越界检测”能力的产品推断为用了相同的 shadow、BST 或二进制重写器，也不能把论文实验原型说成产品的开源实现。每段提供官方链接与限长原文节选；真正可抄的只是接口/开源代码在许可允许范围内的部分，GPU ISA、地址空间、KMD及失败处理均需重做。[N01][N02][N03][A01][I01][M01][L01]

### 4.1 NVIDIA Compute Sanitizer：运行时插桩、编译期 bounds 与多 checker 不是同一种算法

#### 4.1.1 Memcheck 的两条入口与可观测数据

**官方事实。** [Compute Sanitizer User Manual — Compile-time patching](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compile-time-patching) 写明：

> For now, only memcheck supports compile-time instrumentation.

即当前仅 memcheck 支持编译期插桩，racecheck/initcheck/synccheck 依赖运行时二进制插桩。Memcheck 的默认路径与可选编译期路径须分开比较。[N02][N05]

~~~text
运行时检查：CUDA模块 → Sanitizer执行期识别/补入访存检查 → 设备端访问观察
             + Host资源/API事件 → 对象状态/错误判定 → 设备PC、线程及Host栈报告

编译期检查：CUDA源/PTX → NVCC/PTXAS插桩 + base-and-bounds分析
             → 已插桩模块 → Compute Sanitizer memcheck配套runtime
             → 地址/原对象约束 + 资源事件 → 报告
~~~

**运行时入口的公开事实**是工具能检查 global/local/shared 越界、对齐、GPU硬件异常、API错误和泄漏，并将精确设备违规与较粗的硬件故障分开报告；官方未公开可据以复现所有内部数据结构的完整源代码。因此本文不能断言其默认路径一定使用某个 1:8 shadow 排列、红黑树或某种 GPUVA 页表结构。[N01][N02]

**编译期入口的增益**来自原对象关联。例如 A=[0x1000,0x1040)、B=[0x1040,0x1080)，A 派生指针计算出 0x1040：只按当前地址定位可能认为 B 合法；保留 A 的 base/size 后则报告 A 越界。官方明确写出 *compiler tracks which allocation a pointer refers to*，并描述分析失败后的普通 allocation bounds fallback。这里的 fallback 降低保证，不能继续宣称已追踪原对象。[N02][N04]

**工程移植点：** Compiler 必须明确是在 IR/MIR 保留 pointer origin 还是仅 final addr；UMD 必须提供 allocation identity/size 与异步生命周期；设备库执行真实访存前的检查，PC 与符号化由工具层关联。只有从旧裸地址查询“当前 live 对象”不构成同等级的编译期 bounds 分析。

#### 4.1.2 API 的 callback／patch／memory 三通路

官方 [Compute Sanitizer API Reference](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html) 将开发接口分为 callback、patching、memory，且公开 [Patching API](https://docs.nvidia.com/compute-sanitizer/api/group__SANITIZER__PATCHING__API.html) 的指令枚举与参数语义。[N03]

| 公开接口 | 实际观察／控制的层次 | 对本项目的模块设计启示 |
|---|---|---|
| Callback API | Runtime/Driver调用、resource、launch、memcpy/memset、event/graph等事件 | UMD Event Adapter 去重、记录逻辑 allocation 与 Host 调用栈；API entry/exit 不直接等于 GPU 命令生效 |
| Patching API | 在已知指令种类插入 callback，例如 global/shared/local memory access、barrier、async copy 等 | C 路线的 InstructionSemantics 表和 CodePatch 边界必须保留 access size、predicate、PC、sync type |
| Memory API | 工具元数据所需设备内存管理与访问能力 | 设备报告缓冲、元数据映射与初始化；不要让 GPU user load 误写检测元数据 |

公开结构 **SanitizerCallbackMemoryAccess** 提供 PC、地址、宽度、flags 等参数，官方列出1/2/4/8/16字节访问宽度；shared/local 指针在该回调中可表达为相应内存窗口的偏移。这证明指令宽度／空间分类是运行时工具的核心输入，但并不证明你能够原样移植 NVIDIA 的机器码 patch 实现。[N03]

**自研落地对照：** UMD 在 module load/JIT 阶段选择 patch、记录原PC↔新PC及资源用量；Compiler/MC 提供自研 ISA decode/encode；KMD 复用正规可执行映射与调度接口，不应绕过代码安全策略；cmodel和已具有目标GPU ISA执行能力的QEMU全系统模型对 patch 前后的合法输入结果与错误PC作差分。

#### 4.1.3 Racecheck、Initcheck、Synccheck 的内部语义差异

| Checker | 公布的检测对象与原理输入 | 自研实现时必需的附加分析 | 不能从该产品推出 |
|---|---|---|---|
| memcheck | 实际访存地址、access size/space、资源范围、对齐/异常 | Validity、allocation或bounds描述符、API状态机、报告 | 仅依赖GPU MMU就能识别小对象越界 |
| racecheck | shared 访存 hazard；RAW、WAR、WAW、block/warp和若干barrier/async-copy情形 | 冲突访问历史、线程身份、barrier/phase、适用同步范围 | 自动等同跨所有GPU/global/device的完备TSan |
| initcheck | 未初始化 device 内存读取；当前版本支持 global/shared，默认 global | 有效字节的定义性bitmap、写入／copy/memset及完成时序 | 它与值级 MSan 的全部寄存器 poison 传播相同 |
| synccheck | 不合法 barrier/syncwarp 等用法及部分扩展同步 | 参与mask、有效参与者、phase/token、作用域及错误分类 | 运行了synccheck就自动检查所有内存越界 |

当前手册允许 initcheck 以 `--initcheck-address-space shared` 或 `all` 检查 shared；这一点应优先采用当前手册而非更新滞后的产品总览。[N02][N05]

NVIDIA release notes 同时公开混合插桩、HMM、共享变量间越界、特殊指令及资源变化的限制。特别是 **不同 compilation unit 混合可行，不代表同一 module 中未插桩函数会被自动补齐**。我们的 module manifest 必须以支持的检查点为单位输出覆盖，不允许“无报错＝完全通过”。[N05]

### 4.2 AMD ROCm GPU AddressSanitizer：LLVM IR 检查＋Shadow／Redzone＋异构 Runtime

**官方实现形态**与B路线最接近。 [AMD Using the AddressSanitizer on a GPU](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html) 描述编译侧插桩、compiler-rt/ASan库改造和运行时元数据；其原文有：

> ASan detection works by checking each address at runtime

[ A01 ] 描述了每个实际 load/store/atomic 前进行检查：增加对“shadow”记录的 poisoned 状态读取及判定逻辑；allocator/global/stack 的 Redzone 与 shadow poison 配合。[A01]

~~~text
HIP/C++ → Clang/LLVM ASan instrumentation → AMDGPU target device code
                     ↓
          GPU-side shadow lookup + poisoned-byte validation
Host allocation/free → GPU可见shadow/redzone更新
                     ↓
           设备报告 / 主机ASan runtime与符号化
~~~

**具体实现分工：**
1. Clang／LLVM：识别地址空间和实际访存，为普通/原子访问插入设备侧 checker；global、LDS/shared、private/stack不能无条件按CPU的单地址空间处理。可参考 [LLVM AddressSanitizer.cpp](https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html) 中的 AMDGPU 分支 [L03]。
2. Runtime/UMD：建立 shadow 内存布局、poison/unpoison、对象Redzone、device allocator及报告关联。Host API 调用是输入，shadow 更新必须按 GPU 队列可见性生效。
3. KMD：保证额外 metadata/报告 BO 的 GPUVA映射与执行生命周期；不要求由 KMD理解每个源码数组。
4. cmodel/QEMU：可直接执行新增GPU检查指令的前提是目标 ISA 覆盖；另加独立 oracle 才能构成双实现验证。

**AMD特有约束不通用。** 官方文档使用 `-fsanitize=address -shared-libsan -g`，强调 `xnack+` 与相关运行环境，并列出 limited redzones、runtime未正确更新shadow时可能漏报/误报、目标空间/ABI限制。它表明“一种AMD GPU ASan移植”需要特定平台契约，不证明自研 GPU 必须有XNACK，或默认具有GPU缺页机制。[A01][A02]

**代码级观察点：** 采用 (S=shadow_root+(addr >> 3)) 只是候选，必须先验证GPUVA映射和granule表示；单shadow字节只能表示一组应用字节中的全有效、全毒或有效前缀，不能编码同一granule中任意不连续有效岛。热路径还要报告寄存器/occupancy、编译新增访存的覆盖以及工具OOM状态。[L02][A01]

### 4.3 Intel oneAPI：设备侧 Compiler Sanitizers 与 Unified Runtime 层的责任边界

[2026 oneAPI Device-Side Compiler Sanitizers](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/device-side-compiler-sanitizers.html) 明确给出 SYCL／OpenMP 的 device ASan/MSan/TSan 编译开关、支持空间、选项和运行时错误类别，ASan可报告 device-global、buffer、USM、local/private及use-after-free等在其适用平台下的错误。[I01]

[oneAPI Unified Runtime 规范](https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers) 的原文：

> Unified Runtime loader implements the runtime part of device-side sanitizers

这是跨层分工的直接证据：编译后的设备检查与 Runtime 对实际 device allocation、context、事件及后端（Level Zero/OpenCL）的资源服务协作。[I03]

~~~text
SYCL/OpenMP ─ device compiler -fsanitize=address/memory/thread
                  ↓
           instrumented device binary
                  ↓
        Unified Runtime sanitizer layer
                  ↔ USM/buffer allocation, queue/event, backend adapter
                  ↓
        device reports + host reporting / symbolization
~~~

**不能混淆三个问题：**
- ASan 的“地址有效性”需要 metadata 与内存分配接口；MSan 检查定义性（有时涉及传播）；TSan 检查访问冲突及同步，三者不是同一个shadow查询函数。
- Unified Runtime 是对运行时职责的公开参照，**不是**可直接移植到自研 GPU 的 KMD。设备映射和驱动提交仍需本项目KMD/UMD实现。
- UR 规范页面对 MSan/TSan 仍可能标为 planned，而 Intel 2026 编译器指南明确给出设备侧MSan/TSan产品命令；版本和适用路径应分别注明，不应写成两者无条件、全平台同等可用。[I01][I03]

**运行模式影响证据。** Intel限制文档披露某些设备/单卡要求，以及设备ASan下kernel串行执行、部分workgroup规模可能跳过私有/局部检查的问题。我们不能通过强制串行化换取低误报后声称并发Racecheck已经通过；应在结果里显式记录并发模式、checked/skipped数及失败原因。[I02]

### 4.4 MUSA Compute Sanitizer：可证实的公开行为与不可推断的内部实现

[摩尔线程官方用户指南](https://docs.mthreads.com/mooreperf/mooreperf-doc-online/musa_compute_sanitizer/user_guide/) 明确公开了 `mt-compute-sanitizer` 的运行期检查形态：默认启用 global 越界；同时检查 MUSA Runtime/Driver API错误；显存泄漏须显式开启 `--leak-check=full`，并提供错误调用栈/定位输出。[M01][M02]

**官方原文片段：** “检测 kernel 访问不属于自己的 global memory 地址”。[M01]

本报告**不能由上述公开功能推断**它是否在某个版本采用：LLVM ASan shadow、完全机器码 patch、硬件事件、对象区间表、三者融合或私有固件算法。除非获得官方设计说明或可核实源码，均标记“实现机制未披露/未核实”，不能以其作为C路线实际采用某种具体ISA重写算法的证据。

本项目可以借鉴的公开产品契约包括：默认 checker 开关、单独的 leak 模式、UMD/Driver API 返回码与GPU访存异常分别归类、报错包含源/Host stack及过滤参数；这些属于产品与接口设计，不意味着复制其内部核心算法。

### 4.5 LLVM 上游 ASan／HWASan：能够直接借鉴的算法与仍缺的 GPU 适配

**经典ASan**：[Clang AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html)、[AddressSanitizerAlgorithm](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm)、[LLVM AddressSanitizer.cpp](https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html) 给出了“编译器访问点插桩＋运行时分配/poison＋shadow/redzone/quarantine”的公共结构。[L01][L02][L03]

~~~c
// 算法示意：只有Shadow映射已受控且状态有效才能读取
idx = gpuva >> 3;                         // 常见1:8 granule
s = safe_shadow_lookup(context, idx);     // 查不到≠有效
check_all_active_bytes(s, gpuva, width);  // 全部字节，而非只查首字节
~~~

ASM／特殊设备指令可能在IR pass后出现，必须用目标后端的指令清单复核插桩。CPU compiler-rt 的线程局部、系统调用、mmap、异常机制及符号化工具，不能不加处理地假设 GPU 设备端存在。

**HWASan**：[LLVM Hardware-Assisted AddressSanitizer Design](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html) 定义 pointer tag／memory tag 比较和short granule路径。[L04] 它不是“任何越界都会检出”的确定性对象bounds算法：需要可用tag位或独立携带方式、granule语义、tag重选和碰撞模型，还要处理tag与地址运算。已有CPU专用Top Byte Ignore不能推导自研GPU自动具备同样硬件功能。

**上游与自研分工：** Compiler 借鉴LLVM访存识别、检查插桩、DebugLoc、ignorelist/优化交互；UMD提供对象/分配信息、Shadow资源、设备报告；KMD只补缺失的映射/异常/终止；cmodel与QEMU现有GPU ISA执行器用于原指令和插桩后指令的语义验证。这些GPU地址空间、异步依赖和执行/终止策略属于本项目必须完成的实现。

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
  └─ 本项目QEMU+GPU ISA模型：可执行插桩码，也可前置独立访存oracle
~~~

此流程是**原创对照与接口设计图**，不是从任一厂商内部仓库复制的调度流程。判断 S01 合法性时，应检查受控逻辑对象的完整实际访问区间；只有 page/BO map 时，4B 小幅越界可能仍合法映射。若只观察最终地址而目标落入另一个live对象，则需D1来源信息。

### 4.8 A～F每条方案的出处、融合及本项目新增设计

<a id="route-provenance-matrix"></a>

| 路线／S01变体 | 公开机制源头（官方/论文） | 单一借鉴、融合还是本项目新增 | 自研部分必须真正实现 | 不能冒称 |
|---|---|---|---|---|
| A：功能模型语义检查 | LLVM LangRef [L05]、AMDGPU目标语义参照[L06]、QEMU平台[Q01][Q03] | **已有GPU模型＋原创检查器接入**；逻辑区间检查属通用算法 | GPU ISA执行入口前的lane/byte谓词、对象范围oracle、错误事件和差分接口 | “QEMU自带ASan/所有GPU空间自动被检查” |
| B1：LLVM访存插桩＋当前live对象区间 | LLVM ASan插桩结构[L01][L03]＋NVIDIA API/资源检查设计[N03] | **公开通用机制融合＋本项目具体区间服务**，非AMD产品同款算法 | Sorted interval/sparse registry快照、UMD发布协议、失败和未知地址策略 | “B1等于ASan 1:8 shadow”或“检出原对象跨入B” |
| B2：LLVM Shadow／Redzone | Google ASan [L02]＋ROCm GPU ASan [A01]＋LLVM目标适配[L03] | **明确借鉴的GPU ASan路线＋自研GPUVA/UMD/KMD适配** | GPUVA→shadow布局、设备helper、poison顺序、SIMT-safe报告 | “直接重用CPU runtime无需修改” |
| C：运行时二进制插桩 | NVIDIA Patching API [N03]；BARRACUDA [P02] 作为研究对照 | **公开patch入口模式＋自研ISA重写器**；具体rewriter属自主实现 | Decode/CFG/encode/relocation/资源重算/原PC映射/patch cache | “复制到了NVIDIA未公开的二进制引擎” |
| D1：原对象base/bounds | NVIDIA编译期memcheck [N02][N04]＋cuCatch [P01] | **公开编译期fat-pointer理念融合＋自研SSA/ABI传播** | GEP/phi/select/调用参数root、sidecar与unknown降级 | “仅按当前地址查表就是provenance” |
| D2：指针时间身份 | cuCatch tag/meta思路[P01]＋LLVM HWASan tag[L04] | **公开时间安全思路融合＋针对本项目的强身份协议** | epoch随指针load/store/copy/atomic一致传播，复用与回收协议 | “分配表多一个generation就能检出旧裸指针” |
| E：编译＋binary混合 | NVIDIA hybrid instrumentation [N02][N05] | **厂商公开产品策略＋B/D/C多机制融合＋自研ABI/manifest** | 模块能力选择、拒绝不兼容、不同保证级别与跨模块来源降级 | “不同模块混用自动等于所有函数被检测” |
| F0：Guard Page | Linux GPUVM/页表映射 [K01]及目标MMU原理 | **通用映射保护的工程应用** | 页对齐placement、guard map与fault attribution | “页级保护检测任意4B对象内越界” |
| F1：Tag Compare | LLVM HWASan [L04] | **公开tag算法＋目标ISA/tag存储适配** | tag表示/碰撞/short granule、空转与失效/异常 | “任何tag匹配均证明原对象bounds正确” |
| F2：硬件Bounds／MLB | GPUArmor [P04]＋cuCatch/NVIDIA对象bounds [P01][N04] | **论文研究结构启发＋独立自研ISA/RTL/driver契约** | metadata cache、一致性、fault时序、RTL/DV | “软件原型性能就是芯片最终开销” |
| I1 Initcheck | NVIDIA initcheck [N02]＋Intel device MSan [I01] | **分离未写过bitmap与值级定义性传播的融合设计** | byte definedness/copy/async时序；完整值级MSan另立 | “bitmap checker就是MSan” |
| R1 Racecheck | NVIDIA racecheck [N02]＋BARRACUDA/HiRace/SuperCollider [P02][P03][P08] | **算法按同步子集选择的多源融合** | HB/FSM/state、lane/byte mask、scope、资源上限 | “一次执行无报告就证明race-free” |
| S1 Synccheck | NVIDIA synccheck [N02]＋目标ISA/LangRef [N09][L05] | **检测目标借鉴＋目标barrier状态机原创适配** | 参与集合、phase、token、合法部分mask与超时分类 | “所有超时都是证明的死锁” |

**“原创”的精确含义：** 本表说的“自研/原创”是针对本项目需独立设计和写代码的工程实现、接口及适配，不是对世界首次提出该算法的学术新颖性宣称。公开文献已有的基本shadow、对象bounds、tag、HB和设备trace不能改名归为新算法。实际在实现时，许可证允许复用代码才复用；否则独立实现并保留公开引用。

### 4.9 对每种产品机制最有价值的源码／官方证据入口

| 需要核对的问题 | 官方定位链接 | 可直接对应到的本项目模块 |
|---|---|---|
| NVIDIA编译期与runtime区别、base/bounds与hybrid | [Compute Sanitizer compile-time](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compile-time-patching) | Compiler instrumentation mode / manifest |
| NVIDIA每条指令可携带的地址/宽度/PC及patch入口 | [Sanitizer Patching API](https://docs.nvidia.com/compute-sanitizer/api/group__SANITIZER__PATCHING__API.html) | C: OpcodeSemantics/PatchedPCMap |
| NVIDIA资源、stream、graph、memcpy回调 | [Sanitizer Callback API](https://docs.nvidia.com/compute-sanitizer/api/group__SANITIZER__CALLBACK__API.html) | UMD EventAdapter/AllocationRegistry |
| AMD GPU ASan的shadow/redzone/device runtime约束 | [ROCm GPU ASan 7.2.4](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html) | B: ShadowManager/DeviceChecker |
| Intel异构runtime sanitizer layer | [Unified Runtime sanitizers](https://oneapi-src.github.io/unified-runtime/core/INTRO.html#sanitizers) | UMD context/queue/backend adapters |
| MUSA公开错误类型、默认开关及leak模式 | [MUSA用户指南](https://docs.mthreads.com/mooreperf/mooreperf-doc-online/musa_compute_sanitizer/user_guide/) | CLI / ReportDecoder / API error checks |
| LLVM设备目标已有ASan插桩实现的代码位置 | [AddressSanitizer.cpp](https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html) | Compiler pass/target address-space |
| HWASan tag/short granule详细算法 | [HWASan Design](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html) | F1、D2机制比较 |

### 4.10 厂商版本、运行命令与可复制的实验入口

| 来源 | 本次核验版本／时间 | 应实际检查的开关和限制 |
|---|---|---|
| NVIDIA | Release Notes 顶部 2026.3；NVCC 在线手册 13.4 | memcheck 编译插桩与工具版本配套；其余 checker 单独运行 [N05][N07] |
| ROCm | 版本化文档 docs-7.2.4 | xnack+、HMM、匹配的 ASan runtime；xnack- 可能只告警而不插桩设备码 [A01] |
| LLNL | 当前页面；示例 rocmcc/7.0.0 | HSA_XNACK、ASAN_OPTIONS 和 magic compiler 包装器 [A02] |
| Intel | DPC++/C++ 2026.0，设备章节日期 2026-04-28 | Linux／Ponte Vecchio 单卡限制；ASan 串行化与检查跳过 [I01][I02] |
| MUSA | 用户指南＋v1.1.1 版本说明 | global OOB／API error／leak；其他空间与特殊指令不能默认已覆盖 [M01][M02] |
| LLVM 上游源码 | 读取时页首 24.0.0git | 开发快照，不代表用户当前 LLVM 分支；应以用户实际 checkout 复核 [L03] |

以下只是**已存在厂商工具的命令示例**，用于复现实验入口，不是自研后端已经支持的命令：

~~~bash
# NVIDIA：编译器与 Compute Sanitizer 使用同一 Toolkit 版本
nvcc -lineinfo -fdevice-sanitize=memcheck sample.cu -o sample
compute-sanitizer --tool memcheck ./sample

# ROCm 文档示例路线：具体 arch、runtime 路径随环境调整
hipcc -g -fsanitize=address -shared-libsan --offload-arch=gfx90a:xnack+ sample.cpp -o sample
HSA_XNACK=1 ./sample
~~~

独立 PTXAS 的 sanitize 选项、JIT 选项和运行时启动要求见 [N02][N05][N07]。LLNL 的关闭 leak detection 环境设置属于其示例配置，不应默认强加到所有 GPU ASan 环境。[A02]

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

这里的括号不可省略。1:8 是影子映射比例，不表示整个工具只有 12.5% 显存开销；redzone、quarantine、元数据、报告缓冲和页表还要另外计算。

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

quarantine 延迟复用可以提高一些 UAF 的检出率，但没有永久解决地址重用后身份丢失的问题。

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

优先考虑无锁／有限重试、warp 聚合和按 launch 分片的结构；必须用目标执行模型验证前进性。即便使用 CAS，热点竞争仍会显著改变调度，不能把“无显式锁”解释成“无性能或进展风险”。

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

### 7.6 cmodel、真机与安全模型

功能 cmodel 可以精确执行所实现的语义，但其调度集合未必包含真机的全部可见执行。顺序执行通过不能证明 race-free，模拟中的 bug 也需要排除模型自身与目标语义不一致。

同时，普通调试 sanitizer 的元数据通常与应用同处设备地址空间。除非另有硬件权限隔离与全面覆盖，不能声称它能防御恶意 kernel 任意破坏 shadow／对象表。

<a id="model-platforms"></a>

### 7.7 功能级cmodel与QEMU全系统平台的支持边界

**【已知项目事实（用户于2026-10-21确认）】基于QEMU的全系统虚拟平台已具有目标GPU ISA执行模型，因此本项目当前应归类为Q2，而不是Q0或只实现MMIO/队列桩的Q1。** 是否已覆盖全部访存opcode、lane谓词、GPUVA/MMU、shared/private、async和同步语义，以及内部是否复用独立功能cmodel，未见源码与测试证据，必须逐项检查。QEMU具备执行器不意味着已经实现Sanitizer检测器、错误协议或每一项ISA语义。

#### 7.7.1 先区分“执行检查代码”与“独立检查”

本文把独立运行的功能级 cmodel 理解为：能够执行已实现的目标 GPU ISA，具有测试入口或 Host 调用适配，但默认不包含完整 Guest OS、KMD 和目标总线设备前端。已有模型若包含这些部分，应按实际能力调整分类。

必须区分以下两件事：

1. **执行插桩产物。** B、C、D、E 的检查逻辑可以表现为普通目标 ISA 指令、设备 runtime 与元数据访存。只要现有 cmodel 已正确实现产物使用的 ISA、地址空间、原子及同步语义，它就可以执行这些检查代码，**不必为了执行软件 sanitizer 而必然给 GPU 执行核心再写一套 checker**。仍须接通 shadow／对象表、启动参数、设备 runtime、Host 生命周期和报告通道；原模型缺少这些服务时，适配层需要修改。
2. **充当独立 oracle。** 若要求在原始访存生效前核对真正地址、对象身份、有效位、同步关系，或比较插桩报告是否正确，就要在 cmodel 中另加 A 路线的观察与判断。A0、A1、A2 的工作量及保证分别验收；重复执行同一套软件查询函数不构成独立 oracle。

F 的新指令、tag、fault 或 metadata cache 行为若不属于现有 ISA／模型，则必须扩展模型语义。仅给 Host 侧模型进程加 ASan，也不能代替这项工作。

#### 7.7.2 QEMU 平台配置定义

**【官方事实】** QEMU system emulation 提供 CPU、内存和设备的机器模型以运行 Guest OS；TCG 是 CPU 仿真的一种执行方式，KVM 等则是另一类 accelerator。设备由具体设备模型或直通机制提供。[Q01]

**【工程分类】** 为避免把“安装了 QEMU”与“拥有可执行 GPU 模型”混为一谈，本节采用下列配置名。Q0～Q3 仅标识平台组成，与原报告 V0～V3 验证层级无关，也不是检测能力从低到高的统一等级。

| 配置 | 组成与执行位置 | 能否执行目标 GPU kernel | 适用范围与明确边界 |
|---|---|---|---|
| Q0：普通 QEMU，无目标 GPU | Guest CPU／OS 和已有通用设备；未接入目标 GPU | 不能 | 可测试通用 Host 软件和安装流程；现有显示设备不因此成为自研计算 GPU |
| Q1：目标 GPU 协议桩 | 实现目标寄存器、队列、DMA／中断等约定的设备前端；不执行目标 kernel | 不能 | 可测试 KMD 协议、提交、映射、完成和故障处理；合成 completion 或 fault 不算一次 GPU 检测 |
| **Q2：本项目既有全系统GPU ISA模型** | 用户已确认平台能执行目标GPU ISA；它可以直接包含GPU模拟器，或通过桥接复用功能cmodel，具体架构待代码核验 | **能，现有能力**（以实现的opcode/语义为限） | 增加Sanitizer观察入口、shadow/对象身份/报告及Guest生命周期接线；GPU执行器本身不再作为待开发项目计费 |
| Q3：VFIO 直通真实 GPU | Guest 驱动控制分配给 VM 的真实设备，kernel 在真实 GPU 上执行 | 能，取决于硬件及 Guest 驱动支持 | 可运行已经实现的真机 sanitizer；不自动获得 Q2 的内部模型观测与确定性执行能力 |

KVM 的 Guest CPU 加速方式与 Q1／Q2／Q3 的 GPU 接入方式应分开记录。例如 Q2 可以使用 KVM 运行 Guest CPU，并在软件 cmodel 中运行 GPU；这不表示 KVM 已提供目标 GPU ISA 模型。

#### 7.7.3 官方接口能观察什么

**【官方事实】** TCG 插件提供翻译块、指令执行及访存回调。访存回调注册在被插桩指令上，并在成功的 load／store 之后调用；faulting access 不进入这一成功访存回调。指令执行前回调也不能保证该指令最终完成。[Q02]

**【工程推断】** 对常见的“Guest CPU 经 TCG 执行，GPU 是独立设备模型”配置，TCG 插件观察的是 Guest CPU 的相关指令，不会自动进入 GPU 执行器。因此：

- CPU 向 GPU doorbell 的 MMIO 写入不等于 GPU 内部所有 lane 的访存记录。
- 设备模型发出的 DMA、模型内部 VRAM／shared／private 操作，不能假定都会经过上述 TCG 指令回调。
- 不能把“成功访存后的观察”宣传为“错误访存生效前阻止”。需要阻止时，应在 GPU 模型的实际语义操作前或软件检查路径上建立接口。
- 若 Guest CPU 改用 KVM 执行，也不能继续把 TCG 插件视为该 CPU 执行路径的检查入口；GPU 模型的专用观察接口可独立存在。

**【官方事实】** QEMU Memory API 用 MemoryRegion 描述 RAM、MMIO 等区域，用 AddressSpace 表示 CPU 或设备看到的地址空间；MMIO 可以由回调实现，RAM 可由 Host 内存支持，IOMMU region 可以翻译并转发地址。其接口包含地址空间读写与 RAM 指针访问。[Q03]

**【工程推断】** 这为目标设备 DMA 和地址映射提供基础，但不足以还原 GPU 对象语义。桥接层需要区分 GPUVA、设备 IOVA、Guest physical address 与 Host pointer；在 global、shared、private 等空间中分别保留 context／block／thread／frame 身份。只检查最终 DMA 范围，既不保证看见模型内部的全部访问，也不能知道一个可访问地址原本由哪个对象指针推导而来。若模型直接访问 Host backing memory，应把检查入口放在模型的内存语义入口，而不能只依赖 MMIO 回调。

**【官方事实】** VFIO 提供设备 I/O region、DMA 映射和中断等直接访问基础，并以 IOMMU 隔离框架支持设备分配。[K04] **【工程推断】** Q3 的真实 GPU 内部访存不因此转化为 QEMU 可逐条解释的 GPU 指令。IOMMU 映射成功也不证明 allocation 内的对象边界、suballocation、shared/private 访问或指针时间身份正确。

#### 7.7.4 六路线检测能力总表

下表的“可运行”表示接通该路线所需组件后，能够执行相应动态检测；不表示原始平台已经自带该 sanitizer。所有能力仍受已执行路径、ISA 覆盖与元数据闭合范围限制。

| 路线 | 独立功能级 cmodel | Q0 | Q1 | Q2 | Q3 |
|---|---|---|---|---|---|
| A：模型侧检查 | 需加入 A0 地址观察、A1 对象 map 或 A2 来源身份的相应检查 | 无目标 GPU 执行 | 仅协议／DMA检查，不能算 GPU kernel A 检测 | 可复用并集成 A 检查器；另接系统事件 | 直通不提供 A 的内部观测；不能自动运行模型侧检查 |
| B：LLVM ASan 式 shadow | 可执行插桩代码；接通 shadow 与生命周期；独立 oracle 另加 | 不能检测目标 kernel | 可验元数据准备和报告协议，不能验真实访存判断 | 可执行 B，并联通 Guest 驱动；模型 oracle 可选 | 真机 B 已实现且 Guest 环境支持时可运行 |
| C：运行时二进制插桩 | 可执行 patch 后合法 ISA；可对原始／patch 代码做差分 | 同上 | 可验 loader／patch／资源描述流程，不能确认 kernel 检查效果 | 可验 patch 加载到实际模型执行的全链路 | 真机 C 与代码加载接口支持时可运行 |
| D：D1 bounds／D2 时间身份 | 可执行相应检查代码；独立来源／时间身份 oracle 另加 | 同上 | 可验对象表和 ABI 的传输，不能证明指针传播闭合 | 可验 D1／D2 的实际访问及跨组件身份协议 | 真机实现和身份传播边界满足时可运行 |
| E：编译＋binary 混合 | 可执行组合产物并检查模式交接；覆盖等级仍分别记录 | 同上 | 可验模式选择／拒绝与 manifest；不能验 kernel 行为 | 可联测编译、fallback、加载和检测；缺失身份不能凭全系统补齐 | 真机 E 已实现时可运行；虚拟化不提高 fallback 保证 |
| F：硬件辅助 | 需增加所选硬件／ISA 语义，验证功能规则 | 不能 | 可注入硬件 fault 验处理流程，不能声称硬件已检出 | 在模型中实现后可验功能及驱动协议；不证明 RTL 与性能 | 仅当真实 GPU 具备所需功能时能验真实硬件路径 |

**A 的分层不能跳过。** A0 的地址合法性／初始化位图不自动包含 A1 对象布局；A1 的地址到对象 map 不自动建立原始指针来源；A2 来源跟踪若未包含时间身份传播，也不能宣称解决地址复用后的 UAF。race／sync 检查还须独立加入参与集合、phase、scope、memory order 和 HB 规则，不能由“能观察所有地址”推出。

#### 7.7.5 Host ASan、Guest KASAN 与 GPU sanitizer 的检查对象

**【工程边界】** 给 QEMU／cmodel 的 Host 进程开启 ASan，检查的是该进程自身的 Host 分配及访问是否合法，可帮助发现模型实现中的 C/C++ 内存错误。若整个 GPU 内存由一个 Host 数组支持，GPU 对象 A 越入同一数组中的 live B，Host 访问仍可能完全合法；要识别 GPU 对象越界，仍需 GPU 对象元数据与相应检查器。

类似地，在 Guest OS 内核启用 KASAN，可以用于 KMD 的内核内存错误测试；它不自动为 GPU kernel 的 lane、对象边界与时间身份插桩。三类工具可以协同使用，报告应注明发现的是 Host 模型错误、Guest KMD 错误，还是目标 GPU 程序错误。

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

功能级cmodel可以接受独立夹具输入，也可以作为Q2的GPU执行核心。前一种路径通常不经过真实KMD；后一种路径实际执行Guest KMD并验证设备协议，结构见第8.8节。两条路径共享对象/命令协议时，独立oracle仍应与被测检查逻辑分开。

### 8.2 对象描述符：示意协议

下面的字段是**设计草案**，不是要求立即冻结的二进制 ABI：

~~~c
struct ObjectDesc {
    uint64_t object_id;
    uint64_t epoch;
    uint64_t base;
    uint64_t size;
    uint64_t owner_context;
    uint64_t parent_object_id;  // 可选：子分配或view
    uint32_t address_space;
    uint32_t permissions;
    uint32_t state;            // pending/live/free-queued/dead/retired
    uint32_t allocation_site;
};
~~~

描述符还可能需要 peer alias、外部资源、共享／线程局部身份。不要在热路径每次读取全部字段：将检查必需字段与诊断扩展信息分开，并允许按 kernel／对象缓存。

这里展示的字段合计 64 字节。第 17 章会单独比较 32 字节紧凑热描述符与 64 字节完整描述符，不能在预算中混用。

### 8.3 统一事件集

| 事件 | 生产方 | 必须携带 | 消费方 |
|---|---|---|---|
| allocation create／publish | Host runtime／UMD／device allocator | 对象 ID、范围、allocator、queue／event | 对象表、shadow、cmodel |
| suballocation register／release | 内存池／框架接口 | parent ID、子范围、独立生存期 | bounds、init、报告 |
| free request／free begin／retire complete | runtime／UMD | 对象身份、命令位置、生效边界、完成证据 | quarantine、时间检查、元数据回收 |
| map／unmap／import／alias | UMD逻辑登记＋KMD资源映射契约 | 规范对象、BO/映射身份、权限、device/context | 地址解析与peer检查 |
| job submit／complete／fault | UMD命令关联＋KMD/设备完成源 | job/queue ID、依赖、完成状态、原始fault信息 | 生命周期、错误归因、资源回收 |
| abort／reset／context destroy | KMD及设备/固件；UMD消费 | 终止范围、完成/失败状态、受影响任务 | 错误协议、模型与真机对照 |
| launch／graph node | runtime／UMD | launch ID、module、queue、grid、参数元数据 | runtime、cmodel、报告 |
| copy／memset submit／complete | runtime／copy engine 接口 | 源目标范围、顺序、定义性 | init、生命周期 |
| barrier／fence／atomic | 插桩或 cmodel | scope、ordering、参与集合、phase | race／sync 分析 |
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


本版统一使用三个软件组件归属：**编译器、KMD（Kernel-Mode Driver，内核态驱动）、UMD（User-Mode Driver，用户态驱动）**。这是交付和成本归属约定，不是把原有运行层合并。设备检查库仍在GPU上运行；Host runtime、用户态驱动、loader与CLI之间仍通过明确接口协作。功能cmodel、QEMU、固件及硬件是另列的执行／验证环境或配套组件，不强行放进这三个软件运行层。

| 顶层组件 | 必须区分的子项 | 主要责任与输出 | 不应推导出的能力 |
|---|---|---|---|
| 编译器 | 前端／编译驱动 | sanitizer选项、源级对象／作用域信息、offload编译和链接规则、必要的语义标记 | 有源码和调试信息，不代表已经知道全部动态分配和异步释放时刻 |
| 编译器 | LLVM中端 | 选择检查点，保留实际mask、访问宽度和原子语义；按路线生成shadow检查、bounds或identity传播；输出覆盖声明 | 一个通用load/store Pass不自动覆盖目标intrinsic、后端新生访存及未插桩模块 |
| 编译器 | 后端／MC／链接 | 目标地址空间和调用约定、实际frame及资源布局、检查器helper链接、原始PC映射；C/E按需复用解码与重编码能力 | 后端能解码指令，不表示运行时patch器已经处理控制流、资源重算与代码生命周期 |
| 编译器 | 随工具链交付的device检查库 | GPU侧shadow／descriptor查询、pointer metadata协议、allocator钩子、SIMT可前进的错误记录、受支持错误动作 | 设备检查库不归并成Host runtime；常规检查不能假设每次访存都调用KMD |
| KMD | 特权内存与映射接口 | 接受并验证用户态请求，管理／接通BO、GPUVA映射与权限、必要的驻留／失效契约，返回实际结果 | BO映射范围不能替代框架子分配、tensor view、shared对象或线程私有frame边界 |
| KMD | 提交、完成与故障接口 | 作业提交结果、依赖和完成证据；按平台能力与固件／硬件协作处理fault、abort、reset及context状态，并回传错误 | 完成fence不等于每条GPU访存轨迹；存在reset不等于存在细粒度dispatch abort或可恢复lane异常 |
| UMD | Host runtime／API拦截／allocator适配 | 记录应用分配和子分配的语义、生存期、调用点、graph及queue依赖；关联成功／失败结果并去重 | 用户API返回、提交成功和设备完成是不同事件；底层驱动不知道的池内分配须由allocator提供 |
| UMD | 用户态驱动／loader／JIT／patch管理 | 准备shadow与descriptor等资源，排入正确依赖；管理模块manifest、参数sidecar、代码patch、资源声明与缓存；调用KMD契约 | API拦截本身不能观察kernel内部每次访存；当前GPUVA反查不能恢复历史指针身份 |
| UMD | 报告、符号化与CLI | 汇合编译器的PC／源码map、device检查记录、对象／队列语义及KMD故障；显示覆盖、资源失败和执行终态 | 空日志、未发生GPU fault、模型测试通过均不能单独等同于检查通过 |

**官方依据及适用边界。** DRM GPUVM文档把GPUVA映射与其backing GEM对象联系起来；GEM本身不理解缓冲内容，设备特定操作仍需驱动接口。[K01] DRM的reset约定由KMD判断和执行复位、上送fence／context错误，UMD检查提交结果并向应用报告。[K02] 据此，本报告把底层映射和故障契约归KMD，把应用对象语义及诊断归用户态；这是对自研架构的设计建议，不要求采用DRM类名或相同ioctl。

#### “必须支持”与“必须新增代码”分开记录

**必须支持**表示路线成立所需的能力或契约；**必须新增代码**表示与现有平台相比尚未满足该契约的差额。B、C、D、E、F在真机或运行guest驱动的完整执行路径上均依赖真实／guest KMD支持，但若现有映射、提交、完成和所选错误终止契约已经充分，KMD新增代码可以为0。接口存在但没有证明其时序、错误回传或终止范围充分，不能直接登记为“已满足”。

路线A的独立功能cmodel夹具可自行供给对象和命令事件，绕过真实KMD。该夹具验证的是模型检查器及模拟契约；若目标包含驱动联调，须另外运行真实／guest KMD路径。模拟一次map成功、fence完成或reset通知，不是对真实KMD实现的验收。

#### 事件的语义生产方、底层真值及消费关系


一个逻辑动作可能同时经过API、UMD、KMD及设备执行。应关联这些观察点，而非将每层回调都登记成新动作。下面的“真值来源”按事件类型划分；不存在能替代其他来源的全局“万能分配表”。

| 事件／信息 | 语义生产方 | 可确认结果的来源与边界 | 主要消费方 |
|---|---|---|---|
| 应用allocation、池内suballocation、view创建 | 对应Host allocator／框架接口；设备allocator由device检查库接入 | 该allocator的成功结果和对象生命周期；KMD仅补充其backing BO／映射关系，通常看不到未上报的池内切分 | UMD对象服务、编译器device检查库、cmodel独立oracle |
| BO创建及GPUVA map／unmap／权限变更 | UMD发起请求；KMD实施相应特权操作 | KMD／设备定义的生效与失败结果；异步接口须保留关联完成或依赖证据，不能把请求字段当已生效状态 | UMD映射表、shadow布局、fault归因、模型输入适配 |
| free请求、应用对象失效、底层资源退休 | allocator定义应用生存期；UMD把相关动作接入命令顺序 | 失效点按API／执行侧协议确定；KMD完成证据可证明相关作业／映射操作退休，却不能自动给出池内对象free的精确开始时刻 | poison／unpoison、D2身份退休、quarantine与元数据回收 |
| launch／graph节点／提交与完成 | UMD提供应用launch和每次graph执行实例；KMD处理底层提交 | 接受／拒绝取自KMD，硬件执行完成由其回传；应用launch与底层作业可为一对多，必须保留关联 | 生命周期和HB分析、device context、报告 |
| 每条实际GPU访存及mask、宽度、线程 | B/D的设备插桩、C的patch stub、F实际硬件事件；A的模型语义入口 | 真机事件来自实际执行；模型事件只对其建模执行有效。KMD通常仅观测触发fault等有限异常，不生产常规全访存流 | 地址／bounds／identity检查器、init／race引擎 |
| barrier／atomic／async copy及可见性 | GPU实际执行事件；UMD补充queue／event／copy命令 | 需要对应scope、order、phase与完成关系；KMD提供相关提交／完成契约，CPU时间戳不能替代HB | race／sync／init引擎与独立模型 |
| fault、abort、reset及执行终态 | 设备／固件触发，KMD接收、控制并回传；UMD也可请求所支持的终止动作 | 保留实际终止范围、context状态、完成错误与报告可读性；只有硬件或驱动提供时才声明精确fault PC／地址 | UMD错误动作和报告、CI终态、资源回收 |
| 模块覆盖、原始PC与patch后PC | 编译器生成初始manifest／debug map；UMD loader／patch器补充运行版本 | 以实际加载并执行的代码hash、ISA和ABI版本关联；不能仅靠源文件名 | device记录解析、覆盖报告、cmodel差分 |

dma-fence用于表达异步硬件操作的完成；dma-buf为跨驱动／设备共享提供对象与同步框架。[K03] 因而报告中仍应分别保存“调用成功”“提交接受”“完成成功／错误”。`DMA_BUF_IOCTL_SYNC`的缓存一致性保证不阻止其他设备或进程同时访问，也不能单独作为CPU/GPU无竞争的证据。[K03]

**建议的去重键。** 使用一个逻辑`operation_id`关联高层请求、子调用、提交和完成；分配实例使用不可因VA或句柄复用而重复的`allocation_id/epoch`。`backing_bo_id`、`mapping_id`、`suballocation_id`、`launch_id`和`execution_id`各有独立含义。一次API成功产生一个逻辑分配实例；其底层BO创建回调补充backing关系，不再创建第二个同义分配。一个BO上确实存在多个独立子分配时，应有多个子对象实例，并通过parent关系连接到同一backing。

#### KMD支持缺口与验收证据


Linux GPU调度器的`cancel_job`回调针对尚未经`run_job`执行的作业；正在执行作业的超时恢复另有驱动／固件相关流程。[K01] 因此，“存在cancel回调”“能杀掉Host进程”或“有看门狗”均不足以直接验收sanitizer所需的运行中终止能力。

| 待确认契约 | 何时构成缺口 | 需要留下的验收证据 | 影响路线 |
|---|---|---|---|
| GPUVA／BO布局与权限 | shadow／descriptor／日志不能按所选方案安全映射；映射失败或异步生效不可查询／排序 | 正常映射、容量不足、冲突及失败路径；访问前初始化完成；unmap／重映射与仍在执行作业的依赖结果 | B～F；A驱动集成 |
| BO、映射与应用对象关联 | 导入、别名或池内子分配被重复登记／错误合并；句柄复用使不同实例混淆 | 一个BO多映射、重复import、同VA重用、失败分配及独立子分配的对象账本；核对父子和别名关系 | B～F；A输入协议 |
| 提交与完成状态 | 提交失败记成成功，错误fence被当正常完成，reset后的context仍被当有效 | 接受、拒绝、正常完成和错误完成分别可观测；launch到作业映射可追踪；reset状态沿KMD→UMD→应用一致上送 | 所有实际驱动路径 |
| 释放／元数据退休顺序 | free按Host返回立即生效，或完成后仍有检查器／报告读取被提前回收 | 受控同queue前置访问、跨queue等待、缺依赖反例、同VA复用；同时检查应用对象失效与元数据可回收边界 | B、D、E及F生命周期 |
| 运行中停止／恢复 | 检查失败后继续会破坏同步或状态，却没有经过验证的终止策略 | 在访存检查失败、部分lane已到barrier、日志／metadata不足等情形下，验证所宣称dispatch／context／全GPU终止范围、终态、报告保留及资源可回收性；说明无辜context是否受影响 | 依赖该失败策略的B～F |
| fault归因与覆盖 | 只见device lost却宣称精确内存错误；缺失PC／地址时仍补造字段 | 只读出硬件／KMD实际提供的故障信息，关联代码版本和context；无法归因标记APPLICATION_FAILED或未知，检查器资源失败单列 | B～F故障报告；F尤其关键 |
| guest／host映射及虚拟设备后端 | guest GPUVA被当host CPUVA／IOVA；仅验证QEMU通知而未验证所部署驱动路径 | 记录地址域及映射世代；从guest提交到后端实际执行的关联；明确guest KMD、host原生KMD或VFIO各自是否参与 | 采用QEMU／虚拟化部署的路线 |

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

**虚拟化路径按实际部署填写。** 设备透传时，guest厂商KMD与Host VFIO／IOMMU内核路径的职责不同；不能默认Host还运行同一块设备的原生厂商KMD。虚拟GPU后端若调用Host原生GPU，则guest KMD、后端协议及Host KMD均属于实际路径。纯用户态CPU仿真没有guest内核，不能据此声称验过guest KMD。每种路径仍需分别说明哪一个执行引擎真正执行GPU ISA、在哪一层产生访存检查事件。

#### 逐路线表的阅读口径

以下A～F表按原六路线展开职责，D分别列出D1和D2。“新增”指现有平台缺失对应能力时的实现差额；公共底座P和共享device检查库只计一次，第16章保留原始子项与基础区间，并单列本次识别的KMD和QEMU条件追加。功能cmodel／QEMU检测条件由各路线环境表补充，不能把模拟契约通过算作真实KMD验收。

<a id="qemu-integration"></a>

### 8.8 QEMU全系统接入与地址域

#### 8.8.1 Q1／Q2 需要补齐的系统接口

**【本项目Q2已知前提】** 用户明确确认QEMU全系统虚拟平台已经能够执行目标GPU ISA。所以下表从已有GPU执行模型出发，检查Sanitizer所需的设备/模型/Guest驱动接口，而非要求重新开发GPU执行器。**下表是目标设备与 sanitizer 的接入清单；可优先扩展设备模型、cmodel 桥接和测试服务，是否必须修改 QEMU 通用核心取决于所选版本的接口缺口。

| 组件 | Q1 最低支持 | Q2 增加的支持／需要核对的契约 |
|---|---|---|
| 设备前端 | 目标总线、寄存器、doorbell、队列、完成、中断、reset；若为 PCIe 则包括所需 BAR／MSI 等语义 | 把真实提交描述符交给 GPU 执行器；完成／fault 必须来自相应执行结果 |
| GPU 执行核心 | Q1对照配置不执行目标kernel；**本项目已具备目标GPU ISA模型** | 接入访存前hook、检查器helper及错误报告；逐项检查现有dispatch/ISA/mask/atomic/barrier/address-space覆盖，只有缺项才修改执行核心 |
| 内存／GPU MMU 桥接 | 已支持的 DMA 与 Guest 映射、权限、错误返回 | GPUVA 转换、VRAM／system memory、shared/private 身份、目标要求的映射失效语义；不能把 GPU MMU 与平台 IOMMU 合并为一张假定正确的表 |
| Guest UMD／Host runtime | 对象与队列管理、模块加载、提交／完成流程 | 将真实 alloc／publish／free-effective／reuse、事件依赖、模块能力、启动 sidecar 送入检查路径；device allocator 同步进入协议 |
| Device runtime／检查元数据 | 可构造元数据和模拟报告 | B 的 shadow，D 的 bounds／pointer metadata，C/E stub 与 ABI，以及执行中的真实报告 |
| KMD／固件 | 设备探测、映射、提交、中断、错误与资源回收 | 与执行器保持 context／dispatch／fault 身份一致；是否运行真实固件需单列，固件桩不能当作固件代码覆盖 |
| 诊断和错误控制 | 合成错误注入、报告读取、溢出／超时处理 | GPU PC 与源映射、出错地址／宽度／lane／对象、错误发生前后状态、停止范围及失败原因 |
| 快照与重放 | 保存协议桩的状态 | 若要重放，须包括 GPU 执行状态、队列、映射、检查元数据、相关定时器及外部模型状态 |

若设备前端忠实实现生产硬件 ABI，Guest KMD 可争取使用原有路径；若用专门测试 ioctl 或替代固件，应将其记为测试适配，并把未覆盖的生产路径列出。不能把“做 QEMU 平台”一律理解为“生产 KMD 必须重写”，也不能假定现有 KMD 无适配就能工作。

建议 GPU 语义事件至少携带 dispatch／context、GPU PC、地址空间、实际地址、宽度、读写／atomic、lane／byte mask；shared/private 另含其作用域身份。对象 map、原对象 ID、时间身份及同步关系根据 A1／A2／D1／D2 和 race checker 的声明追加。只记录最终地址并不能事后补出全部字段。

#### 8.8.2 KMD 端到端能验什么

“端到端”应列出实际到达的终点：Q1 可以到达协议桩完成／注入异常，Q2 可以到达模型执行并产生报告，Q3 可以到达真实 GPU 执行。三者均有价值，但证据不同。

| 验证对象 | Q1 可提供的证据 | Q2／Q3 能进一步提供的证据 | 不能据此证明 |
|---|---|---|---|
| 探测与提交 | probe、ioctl、handle、队列描述符、doorbell、中断的协议正反例 | GPU 实际执行结果与 completion 对应 | Q1 返回 success 不证明 kernel 已执行 |
| 地址与生命周期 | 已建模的 DMA map/unmap、权限、失败清理与提交顺序 | 实际 GPU 访问发生时的映射、poison／身份状态 | 页映射有效不等于对象／view 访问正确；Host free 调用时刻不等于设备 free 生效 |
| 异常与终止 | 注入 fault 后的上下文定位、错误码、唤醒、reset／cleanup | Q2 中真实检查触发异常；Q3 中硬件或软件 checker 实际触发 | 注入异常只验证接收处理，不能验证检测器判定；模型终止不证明硅实现精准终止 |
| 工具资源失败 | shadow 分配失败、报告缓冲满、ABI 不兼容、未支持模块／指令的可见性 | 实际检查流量下的溢出与恢复 | 没有报告不等于没有错误；unsupported 和丢失事件不能计作通过 |
| 多 context／队列 | context 归属、隔离、事件依赖、释放与回收的协议测试 | GPU 执行与驱动状态交互的特定合法调度 | 一次调度通过不证明所有 race、cache／TLB 一致性或弱内存行为均正确 |

功能模型还不能给出真机 sanitizer slowdown、occupancy、带宽或硬件面积结论。Q3 可以产生真实设备上的测量，但应报告 VM、驱动、直通和宿主配置，并与所需的原生执行基线分开。

#### 8.8.3 icount 与 record/replay 的实际作用

**【官方事实】** icount 以执行指令数量关联虚拟时间，不是 cycle-accurate 模拟；QEMU 文档明确其与 multi-threaded TCG 不兼容。[Q04] record/replay 依赖 icount，以日志和同步机制控制非确定性事件；设备中影响 Guest 状态的行为须满足确定性要求，相关异步回调须接入重放机制，保存／恢复的设备状态须完整。[Q05]

**【工程推断与验收要求】** Q2 中的 GPU 外部进程、内部工作线程、随机调度、宿主时钟及异步完成，均须纳入确定性设计或记录协议；给启动命令加一个 icount 选项不能自动完成这些工作。可先采用受控 GPU 调度、固定种子和明确的跨队列事件序列，再验证重复执行所得状态、事件和报告一致。

重放适合复现“这一次”驱动与设备交互、回溯报告来源；它不会自动生成 GPU HB，也不会枚举所有合法执行。不要把 Host vCPU 指令计数当作 GPU cycle，不要把模型内一次确定顺序当作 GPU race-free 证明，也不要默认 Q3 的真实设备内部状态可被通用 QEMU 日志完整重放。

#### 8.8.4 实用用例与各自提供的证据

以下用例是针对平台与驱动差异的工程验收设计。每例均设置错误实例与合法对照，并记录 Qx、ISA／工具版本和实际参与的 checker。

| 用例 | 最低平台与错误／合法对照 | 验收证据及边界 |
|---|---|---|
| Q-U1：KMD 提交与异常回收 | Q1：非法 DMA 描述符／映射失败／注入 fault，对照合法提交 | 验 probe、队列推进、context 归属、唤醒与资源清理；必须标记未执行目标 GPU kernel |
| Q-U2：异步释放与 poison 顺序 | Q2：受控顺序中 free-effective 后 kernel 访问旧地址，对照最后一次使用完成后再释放 | B 在其 poison 窗口报告，A0 可独立核对；记录 enqueue 与 effective，不把 Host 调用顺序当设备顺序 |
| Q-U3：从 A 越入 live B | cmodel／Q2：A、B 同时存活，来源为 A 的访问落入 B；对照携带 B 身份访问同地址 | D1／A2 检原对象来源；地址合法性检查及 Host ASan 无报告均不能作为该例通过证据 |
| Q-U4：同 GPUVA 复用 UAF | cmodel／Q2：保存 A 的指针身份，释放并由 B 复用地址，再 reload；对照更新为 B 的身份 | D2 检预期旧身份与当前身份不符；覆盖 spill／reload、拷贝及受支持 atomic 发布；unknown 路径单列 |
| Q-U5：C/E patch 语义与资源 | cmodel 差分＋Q2 Guest loader：谓词、lane mask、宽访存、分支与 barrier；设置合法与错误版本 | 合法输出及同步一致，错误定位回原 PC；核对实际代码 hash 与资源描述；Q1 只能验加载协议 |
| Q-U6：race 重现与 HB | cmodel／Q2：冲突访存，对照正确 scope／order／barrier 的同步；用受控调度或已接入 replay 复现 | 分别验执行可重现和 GPU HB 判断；Host TCG trace、icount 或单次串行顺序不能替代 GPU HB |
| Q-U7：F 触发与 fault 处理 | Q1 注入 fault，Q2 由新增 tag／bounds 语义实际触发，Q3 在有相应硬件后重复；对照合法 tag／bounds | Q1 验接收处理，Q2 验模型规则触发，Q3 验真实设备路径；三种证据分别记录，不能互换 |

## 9. 路线 A：cmodel 检测

### 9.1 原理和适用目标

**【工程建议】** 在 cmodel 真正解释访存、资源和同步语义的位置增加观察入口。每次即将生效的读写携带实际地址、宽度、byte enable、lane、作用域等信息，查询对象／有效性状态，随后决定报告或按检查策略阻止操作。

可分成三个层级：

- A0：实际地址、范围、对齐、分配 API、初始化位图。
- A1：结合编译器提供的 shared／stack 对象 map，检查对象边界。
- A2：为指针运算及寄存器／内存增加身份跟踪，并实现多个合法调度或 HB 分析。

**A0 不能自动获得 A1/A2 的保证。** 实际地址同样无法区分从 A 越入 B，也无法独立识别地址复用后的旧裸指针。

### 9.2 编译器、KMD、UMD支持与修改

**cmodel。** 

| 路线 | 组件 | 必须支持的契约／交付物 | 是否必须新改代码及判断条件 |
|---|---|---|---|
| A：cmodel | 编译器 | 生成模型能执行的目标代码；A1需要与代码一致的对象／frame map，A2按采用的身份模型提供必要语义；设备allocator事件可由device库钩子供给 | A0可复用现有产物而不改编译器；A1/A2缺少对应语义或map时新增。不能因无编译改动就声称具备A2 |
| A：cmodel | KMD | 独立模型夹具不要求经过KMD；若联调真实／guest驱动，则须提供模型接入所使用的BO、map、submit、completion及错误契约 | 独立夹具可为0且不验KMD；完整驱动路径按实际缺口改，不能把“模型可运行”作为KMD支持证据 |
| A：cmodel | UMD | 为模型供给allocation、子分配、copy、launch、queue／event与free语义；报告和CLI关联模型结果 | 纯夹具可自行构造输入；要重放真实应用则新增／复用UMD事件适配、去重及结果接口。输入层不可把CPU返回时间当设备生效点 |

### 9.3 代价与局限

优势是易于观察语义、调试检查器、构造确定输入；不需要先解决真机上全部设备 runtime 性能问题。缺点是无法单凭功能 cmodel 推断真机 slowdown，也很难按真机规模运行数十 GiB 数据和全部调度。

不要在 cmodel 中强行重放它不能表达的 ISA 或内存序。出现与真机分歧时，应先确认模型、编译器和驱动的实际事件一致，再判断谁有错。

### 9.4 建议第一批验收

验证 scalar/vector 越界、mask、alignment、同地址不同 block、alloc/free 顺序和日志截断；为每种误报设计合法反例。A1/A2 另做身份传播及调度测试，不与 A0 的完成度合并。

预算见第16章：含一次cmodel的P＋A基础研发参考包，核心实现约 18–38 kLOC，手写测试约 16–33 kLOC；窄范围 A0 探索原型可远小于此完整内测范围。

<a id="route-a-platforms"></a>

### 9.5 功能级cmodel与QEMU的检测能力及改动

| 问题 | 功能级 cmodel | QEMU 全系统平台 |
|---|---|---|
| 能检测什么 | A0：已实现空间的实际范围、对齐、有效性／初始化；A1：追加对象布局；A2：在传播闭合范围追加来源判断；具体 checker 分别启用 | Q2 可获得同等模型检查能力，并关联 Guest UMD／KMD 的实际执行；Q0/Q1 无 kernel 检测，Q3 不自动提供模型观测 |
| 必须修改什么 | 修改模型的实际访存、寄存器／内存身份及所需同步入口；A1/A2 不能只加最终地址日志 | 增加目标设备前端、cmodel 桥接、Guest 生命周期与诊断协议；是否改 QEMU 核心按接口缺口判断 |
| 其他组件支持 | A0 可不要求应用重编译；A1 需 compiler map；A2 需明确来源输入和传播；runtime／UMD 提供对象与异步事件 | KMD 提供映射、完成、fault/context 信息；若现有硬件 ABI 足够可复用驱动，缺失信息通过明确接口补齐 |

## 10. 路线 B：LLVM ASan 式影子内存

### 10.1 原理

将 LLVM 访存检查与设备 shadow 查询结合；allocator 根据对象的创建和释放顺序维护可访问状态，设备端记录违规访问。基础目标是 M1/M2/M6 和在 poison 窗口内的 M5，随后扩展 shared/private 及初始化检查。

这条路线和 ASan 算法同源，但自研目标必须自己确定 GPUVA 映射、页表管理、设备 runtime、错误终止及地址空间支持。[L01][L02][L03]

### 10.2 Shadow 的三种实现选择

| 选择 | 前提 | 优点 | 主要成本 |
|---|---|---|---|
| 固定或动态 offset 的线性 shadow | 可保留足够 GPUVA，访问映射受控 | 热路径简单，可做向量化查询 | 巨大虚拟保留区、稀疏驻留、碰撞处理 |
| 多级稀疏 shadow | 驱动可按需建页表，runtime 可安全查表 | 不必连续保留全部 shadow | 额外访存、缺页／缺表策略、并发创建 |
| 按 allocation 的 shadow | 已有可靠对象定位或区域索引 | 仅为实际对象分配状态 | 每次定位对象的成本，wild pointer 处理 |

没有设备缺页能力并不排除 B：可以由 UMD 在公开 allocation 时提前分配／初始化对应 shadow，再把依赖排入 queue。不得让检查器首次访问一个尚未映射的 shadow 页时自己 fault。

### 10.3 编译器改动

1. 明确 sanitizer driver option、offload 编译行为及 device runtime link 规则。
2. 在 Pass 中识别 load/store、atomics、内存 intrinsic、masked／vector 操作。
3. 根据 target address-space 判断 global/shared/private，处理 generic pointer 的运行时分类。
4. 为目标 intrinsic、async copy 和 backend 新增访存设计补充插桩或缺口标记。
5. 管理 runtime helper 的不再插桩属性、错误路径、函数属性和优化交互。
6. shared/private 若采用 redzone，调整 layout、frame 和资源用量 metadata；不能仅把空间标成可插桩。

第一版可选择“独立 GPU pass＋复用 LLVM 通用工具”来减少对上游大型 ASan pass 的侵入；若目标与现有上游支持高度相似，也可以扩展原有实现。两者应以改动可维护性和地址空间语义为准。

### 10.4 编译器、KMD、UMD支持与修改

**shadow。** 

| 路线 | 组件 | 必须支持的契约／交付物 | 是否必须新改代码及判断条件 |
|---|---|---|---|
| B：shadow | 编译器 | 前／中／后端建立声明范围内的检查覆盖；device库执行shadow查询、mask和完整范围检查、错误记录与所选失败策略 | sanitizer从零建设时需要新增插桩与device库；已有实现可移植。后端新增访存和目标intrinsic须各自覆盖或标缺口 |
| B：shadow | KMD | 按所选布局提供shadow／日志BO和GPUVA映射、权限及使用寿命；保证相关提交／完成；支持所选错误策略要求的fault／abort／reset路径 | 既有契约充分可0新增。缺少所需VA布局、映射生效依赖、错误完成或可靠终止时按需改；不以GPU可缺页作为所有B实现的硬前提 |
| B：shadow | UMD | 维护真实分配／子分配事件，分配和初始化shadow，将poison／unpoison排入正确依赖；传入context，链接模块并报告覆盖 | 必须有这条集成路径；可复用P，缺失的shadow资源管理及异步生命周期适配需新增。KMD不会替UMD自动poison每个池内对象 |

### 10.5 能力边界与退出条件

完成 redzone 检查后，如果业务错误主要是对象 A 跳入 live B，应转向 D1，而不是只无限增加 padding。若地址复用 UAF 构成重要需求，应做 D2，而不是宣称更大的 quarantine 已解决全部问题。

只有在实际 shadow 布局、典型内存占用和真机 slowdown 均可接受时，才把 B 扩展成默认 CI 模式。

预算：在已有KMD契约可复用、含一次cmodel且不含QEMU追加时，P＋B核心约 27–53 kLOC，测试约 23–44 kLOC。这里是具备多空间和异常路径处理的内测版本，不是插入几个 load/store helper 的演示程序。

<a id="route-b-platforms"></a>

### 10.6 功能级cmodel与QEMU的检测能力及改动

| 问题 | 功能级 cmodel | QEMU 全系统平台 |
|---|---|---|
| 能检测什么 | 运行 B 插桩产物与 device runtime，可在 B 声明的地址空间和 poison 窗口内报告错误 | Q2 可运行相同检查码并联通 Guest 驱动；Q3 依赖已实现真机 B；Q1 只能测试准备／报告协议 |
| GPU 执行核心是否要改 | 若已支持产物全部 ISA／空间／同步，执行核心可不改；缺失语义才补实现。要做独立区间／有效性 oracle 时另改模型 | Q2 复用该核心，但需接通 Guest 内的 shadow 分配、映射、poison 命令、生效顺序及启动 ABI；TCG 插件不替代这些工作 |
| 其他组件支持 | compiler pass、device helper、UMD 生命周期及 shadow 服务、报告通道 | 复用KMD现有映射、完成和所选错误策略所需的终止／回传契约，仅在相应契约不足时补接口；精确故障信息按硬件与驱动实际能力声明；区分应用与检查器自身访存 |

## 11. 路线 C：运行时二进制插桩

### 11.1 原理和前提

UMD 在模块加载或 JIT 产物生成后取得目标代码，解析指令与控制流，为被选择的访存、atomic 和同步指令插入检查，再以新的资源用量和 PC map 启动 kernel。

它能覆盖无法重编译的模块，但“API 拦截”本身不能看到 kernel 内每次访存。也需要区分中间表示重写与最终机器码重写：例如 BARRACUDA 的 binary-level 路径依赖从 fat binary 提取 PTX，不能据此认为其能够处理所有纯机器码。[P02]

### 11.2 编译器、KMD、UMD支持与修改

**binary。** 

| 路线 | 组件 | 必须支持的契约／交付物 | 是否必须新改代码及判断条件 |
|---|---|---|---|
| C：binary | 编译器 | 不要求应用重编译；可复用MC解码／编码和ISA描述；按本章归属提供device检查库／patch helper及其调用约定 | 前中端不必新增；MC已有能力可0新增，缺失部分按实际补齐。patch引擎在UMD列，避免MC或stub重复计费 |
| C：binary | KMD | 接受经过许可的可执行代码资源、更新后的资源声明与正常提交；提供代码／数据映射寿命、完成及所选失败路径 | 现有正式加载／执行接口充分可0新增；仅在代码加载／权限／调试或终止接口缺失时新增正规契约，不默认要求KMD实现二进制重写 |
| C：binary | UMD | loader／JIT取得目标代码，完成控制流重定位、资源重算、patch cache和卸载生命周期；保留原PC、覆盖与失败原因 | 需要二进制重写与管理能力；可复用已有框架。API wrapper本身不构成C；每个实际opcode及资源副作用必须处于支持集合 |

### 11.3 必须单独预算的工程风险

- 修改代码长度后，branch target、literal pool、重定位和调用约定都要更新。
- 额外寄存器和 scratch 会改变 occupancy；原 launch 资源声明未更新可能直接导致错误。
- patch 必须保留 predicate、scoreboard、barrier、依赖等待和特殊访存修饰符。
- 模块被多个 context／线程并发加载时，patch cache 需要绑定完整 code hash、ISA 和工具版本。
- 不能用未知指令的“近似解码”继续检查；应清楚拒绝或标成部分覆盖。
- 对不可识别对象 provenance 的代码，基础 C 只能保证地址／区间层面的检查。

### 11.4 什么时候不优先做 C

如果全部核心应用都能重编译，而项目尚无成熟的机器码重编码／调试 patch 设施，C 的基础建设通常会挤占最急需的对象与生命周期工作。可以先把模块未覆盖作为可见缺口，等实际闭源覆盖需求出现再补。

预算：在**已有成熟解码/编码/patch及可复用KMD契约**时，含一次cmodel、未计QEMU追加的P＋C核心约 34–67 kLOC，测试约 30–58 kLOC。若这些前提不存在，还需追加第 16 章列出的基础设施投入。

<a id="route-c-platforms"></a>

### 11.5 功能级cmodel与QEMU的检测能力及改动

| 问题 | 功能级 cmodel | QEMU 全系统平台 |
|---|---|---|
| 能检测什么 | 执行已正确重写的代码并报告所选检查错误；可对合法程序做原始／patch 后语义差分 | Q2 可覆盖 Guest 模块加载、patch、资源声明到实际执行；Q3 运行真机 C；Q1 不验证 GPU 指令重写后的真实语义 |
| GPU 执行核心是否要改 | 产物只用现有完整支持的 ISA 时可不改；独立核对访存、mask、barrier 或异常时加观察接口 | 增加 loader／设备模型集成，保证提交的是实际 patch 代码及匹配的资源描述；QEMU 的 CPU TCG 不是目标 GPU binary rewriter |
| 其他组件支持 | 目标解码／编码、CFG 与 relocation、stub ABI、寄存器保存、资源重算、原／新 PC map | UMD／JIT／patch cache／模块生命周期必须走实际路径；KMD／固件提供所需的正式代码加载、执行权限及错误处理契约，调试或专用patch接口仅在实现实际依赖时要求；未知编码明确拒绝或降级 |

## 12. 路线 D：对象边界与时间身份传播

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

**对象bounds与时间身份。** D1和D2分行列出，不将身份传播合并成普通bounds支持。

| 路线 | 组件 | 必须支持的契约／交付物 | 是否必须新改代码及判断条件 |
|---|---|---|---|
| D1：bounds | 编译器 | 保留可恢复的原对象bounds，传播到检查点；前端／IR／后端与device描述符查询配套，来源丢失显式降级 | 通常需新增对象分析与传播；只查询当前地址所属分配不能算D1闭合。子对象／view边界需额外输入 |
| D1：bounds | KMD | 为描述符及日志提供普通BO／GPUVA资源和一致的提交、完成、回收、错误接口；返回backing／映射结果供关联 | 既有契约充分可0新增；KMD无需替代编译器传播源级对象。只有实际布局或错误接口不足才改 |
| D1：bounds | UMD | 登记应用对象与合法子范围，建立kernel参数sidecar和外部资源关联，按依赖发布／回收描述符 | 需具备bounds对象服务；P已有事件可复用。未知provenance不能在launch时只按当前VA反查后冒充原对象 |
| D2：identity | 编译器 | 在声明支持集合内闭合指针身份传播，覆盖call／return、spill／reload、pointer load/store、copy、atomic和ABI；device库保证值与身份匹配 | 必须有完整身份协议；不能以对象表增设generation字段代替。已有D1只复用共有部分，D2独有传播／原子发布仍需实现 |
| D2：identity | KMD | 提供元数据存储、映射、完成与安全退休等底层契约；跨context／导入所需的backing关系须可关联 | 现有契约充分可0新增；KMD不负责自动恢复旧裸指针身份。只有所需共享／隔离／退休接口确有缺口时新增 |
| D2：identity | UMD | allocator创建不可混淆的分配实例；参数、graph捕获和受控Host边界保留原身份；按设备依赖退休旧描述符和pointer metadata | 需要身份与生命周期集成；设备内部每次指针传播仍归编译器／device库。不能在同VA复用后把旧指针重标成新对象 |

### 12.6 子对象和 tensor view

大块 GPU allocation 可能被框架划分成多个 tensor、workspace 和临时区。只检查最外层 allocation 无法判断逻辑 view 越界。推荐提供可选的 suballocation／view 注册接口，字段包括 parent、offset、length、stride、element size 和 lifetime。

第一版支持连续子范围即可；非连续 strided view 需要选择逐维 bounds 检查或明确只提供外包范围检查。后者不能声称完整 tensor 索引安全。

### 12.7 不直接照抄 cuCatch 的理由

cuCatch 中 BST 是 **Base and Size Table**，GPUArmor 的 BST 则可以指 **Binary Search Tree**，两者名称容易混淆。[P01][P04] 自研设计要分别讨论“表中存什么”和“怎样索引表”。

此外，能找到一个 reaching base pointer，不意味着它一定是最初 allocation base；跨函数或内存加载可能已经成为新的分析 root。本文 D2 的闭合身份传播要求比“局部后端分析恢复 bounds”更强，成本也更高。

预算：在已有KMD契约可复用、含一次cmodel且不含QEMU追加时，P＋D核心约 50–98 kLOC，测试约 43–83 kLOC；只完成 D1 的受控原型应按任务单单独裁剪，不能承诺同时具有完整 D2 能力。

<a id="route-d-platforms"></a>

### 12.8 功能级cmodel与QEMU的检测能力及改动

| 问题 | 功能级 cmodel | QEMU 全系统平台 |
|---|---|---|
| 能检测什么 | D1 在来源可恢复范围检查原对象 bounds；D2 在闭合的时间身份传播范围区分同地址新旧对象指针 | Q2 可联测 GPU 检查与对象发布／回收；Q3 依赖同等真机实现；Q1 只能验证描述符传递，不能验证 GPU 指针流 |
| GPU 执行核心是否要改 | 执行既有 ISA 的 metadata/check 代码可不改核心；独立 D1/D2 oracle 则要增加寄存器、内存槽、spill／reload、memcpy、atomic 等身份跟踪 | 需要把 Guest 中的对象 ID／epoch、参数 sidecar、设备分配及实际生效时序送到 Q2；QEMU 页表或分配表不能代替指针自身身份 |
| 其他组件支持 | compiler provenance 分析／传播、device metadata runtime、UMD 描述符、跨函数／模块 ABI | KMD 管理的页映射不能单独推导 UMD 子对象或语言级 provenance；外部裸指针和不受控 writer 必须标记 unknown／覆盖缺口 |

## 13. 路线 E：编译插桩与运行时补充的混合方案

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

**hybrid。** 

| 路线 | 组件 | 必须支持的契约／交付物 | 是否必须新改代码及判断条件 |
|---|---|---|---|
| E：hybrid | 编译器 | B／D支持的编译覆盖、稳定manifest、边界摘要及兼容device检查库；按每条边界声明bounds／identity保证 | 在已有B／D上补齐混合协议；共享检查器复用。binary fallback不能补回已经丢失的D2来源 |
| E：hybrid | KMD | 提供所选B／D及C子集所需契约的并集；不同模式共用明确的context、映射、提交和错误路径 | 不因“混合”自动增加一套KMD；现有契约充分可0新增，新增项由具体缺口决定 |
| E：hybrid | UMD | 按模块选择编译插桩、binary补充或拒绝／降级；共享对象表和报告ABI，防止重复插桩；管理跨模式参数和代码生命周期 | 需要loader策略、版本校验及跨模式适配。B/C/D共有的分配与报告底座复用；每段代码分别展示实际保证 |

第16章的P＋E基础研发参考包为核心76–146 kLOC、测试66–127 kLOC，已计一次cmodel；KMD缺口与QEMU平台按独立包增加。E按B全量、D独有部分、C fallback子集和集成去重，不直接叠加B/C/D总量。

<a id="route-e-platforms"></a>

### 13.5 功能级cmodel与QEMU的检测能力及改动

| 问题 | 功能级 cmodel | QEMU 全系统平台 |
|---|---|---|
| 能检测什么 | 执行编译插桩与 binary fallback 的组合，按模块保留 B／D1／D2／裸地址的保证等级 | Q2 可验模式选择、加载与真实执行的交接；Q3 可运行既有真机 E；Q1 只能验 manifest／模式选择协议 |
| GPU 执行核心是否要改 | 合法产物全部落在现有 ISA 能力内时可不改；跨模式独立 oracle 或缺失语义另改 | 接通统一对象／queue 服务、manifest、runtime ABI、代码版本与覆盖报告；不能因接入全系统就为 fallback 恢复已丢失的来源 |
| 其他组件支持 | compiler manifest、UMD patch 选择和避免重复插桩、共享 device runtime、边界摘要 | Guest 驱动及诊断服务保留 module／dispatch／checker 标识；资源失败和拒绝加载必须可见 |

## 14. 路线 F：硬件辅助

### 14.1 可选硬件方向

| 方向 | 硬件／ISA 支持 | 软件支持 | 能力边界 |
|---|---|---|---|
| Tag compare | pointer tag、memory tag、同步 fault／屏蔽语义 | tag 分配、传播、清除／重用 | 通常有碰撞与粒度边界 |
| Bounds check 指令 | metadata handle／base-size 检查、准确错误 PC | 对象分析、descriptor 管理 | 无正确来源仍无法证明原对象 bounds |
| Metadata cache／MLB | per-SM 缓存、失效和一致性协议 | profile、lookup、free 生命周期 | cache 大小和工作集有关 |
| Watchpoint／保护页 | 地址范围、权限及 fault 归因 | GPUVA 管理、guard page | 粒度较粗，无法替代对象级检查 |
| 同步事件 trace | barrier／atomic／queue 事件输出 | race/sync 分析、流量控制 | 全访存 trace 的带宽可能不可接受 |

### 14.2 编译器、KMD、UMD及硬件支持

**hardware。** 

| 路线 | 组件 | 必须支持的契约／交付物 | 是否必须新改代码及判断条件 |
|---|---|---|---|
| F：hardware | 编译器 | 根据选定机制生成tag／bounds／metadata指令或标记，传播所需来源；device库承担尚未硬化的检查和报告 | 所用ISA／ABI已有支持时可复用；新指令、新指针表示或上下文参数需要修改。保护页等机制不必强制新增编译指令 |
| F：hardware | KMD | 接通所选硬件的特权配置、metadata资源、context状态、失效／一致性、fault归因及终止／恢复；与固件职责明确 | 只有所需机制和契约均已存在才可能0新增；新寄存器、fault类型、context保存恢复或失效路径通常需配套改动。不能由软件预算推定硬件已经存在 |
| F：hardware | UMD | 分配并发布tag／bounds／metadata，配置所支持模式，处理alias／peer／迁移和回收，解释硬件错误并给出覆盖边界 | 需与选定硬件语义集成；已有库可复用。硬件仅提供加速查询时，软件仍承担对象、身份和生命周期正确性 |

新指令之外还需明确metadata权限、cache一致性、free失效、context切换、peer/迁移和fault重放。硬件、固件与RTL/DV是外部前提，功能模型实现相同语义不代表硅片已经具备能力。GPUArmor提供软硬协同研究参照，基线仍按第5章区分。[P04]

### 14.3 选择 F 的证据门槛

建议满足以下条件后再进入正式硬件投入：

1. 软件原型已有稳定错误分类和合理覆盖。
2. 至少一组代表性业务证明 metadata／检查指令是主要瓶颈，而非日志或 CPU wrapper。
3. 采集对象工作集、热点和缓存命中曲线。
4. 验证硬件方案不会让 free／alias／peer 的一致性成本抵消热路径收益。
5. 获得下一代芯片和软件发布节奏的一致资源安排。

### 14.4 预算解释

第16章P＋F基础研发参考包的软件核心30–60 kLOC、测试29–56 kLOC，含一次cmodel；前提是所需硬件能力**已经存在或另有交付，相关KMD基础契约也可复用**。KMD新增使能及QEMU平台另按条件包计。缺少这些能力时，不能用这张软件表报价完整 F。ISA／RTL／DV／固件的新增工作必须另行立项。

<a id="route-f-platforms"></a>

### 14.5 功能级cmodel与QEMU的检测能力及改动

| 问题 | 功能级 cmodel | QEMU 全系统平台 |
|---|---|---|
| 能检测什么 | 新增模型后验证 tag compare、bounds 指令、保护或 fault 的功能规则 | Q2 验证功能规则与 Guest 驱动交互；Q1 注入 fault 只验证处理；Q3 需要真实硬件已经提供相应能力 |
| 必须修改什么 | 对缺失的新 ISA、metadata 状态、失效规则、异常／屏蔽行为修改模型；只是普通软件 helper 时应按 B/D 说明 | 修改设备模型与桥接，连接固件／KMD 的初始化、context、fault、reset；若模拟 cache，清楚声明其功能及一致性范围 |
| 还需什么证据 | 功能模型可作为规则原型，不能替代校准后的时序模型与 RTL/DV | Q2 中 fault 正确不证明真实流水线、并发一致性、精准异常、面积或 slowdown；这些须由后续相应验证提供 |

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

### 15.6 三组件与两类平台的checker职责

以下是【工程建议】的职责分解。Device runtime是运行在GPU上的检查库，本报告在编译器交付包内单列；Host runtime、SDK拦截和报告工具在UMD交付包内分层实现。功能级cmodel与QEMU平台是不同的执行／验证环境，不与前三项合并为同一种生产软件组件。

| 检查器 | 编译器及设备检查库 | KMD | UMD及用户态工具 | 功能级cmodel | QEMU全系统平台 |
|---|---|---|---|---|---|
| Initcheck | 实际load/store/copy事件；byte定义性bitmap；设备端并发更新 | 提供映射、DMA/copy完成与失败契约；不推断寄存器值是否初始化 | 登记allocator、copy/memset及Host来源策略；按设备依赖更新状态 | 执行检查库或额外实现独立定义性oracle；必须看到实际byte enable | Q2接通Guest UMD/KMD、copy engine模型及GPU内访存；Q1仅能验证copy命令协议，不能覆盖kernel消费 |
| Racecheck | 访存、atomic、scope/order和barrier采集；HB/FSM等独立引擎 | 提供提交、完成、共享对象与外部同步信息；job fence不自动等于任意GPU内存操作间HB | 建立queue/event/graph依赖，关联Host与设备事件 | 独立历史或HB检查；调度扰动仅扩大已探索执行，不能证明全部调度安全 | Q2保留GPU模型的并发语义并接Guest命令图；CPU/GPU联合检测还需CPU访问及同步观测，TCG插件不自动补齐GPU事件 |
| Synccheck | 同步ID、参与mask、phase/token来源；设备状态与报告 | 提供故障通知和可用的abort/reset边界；取消未提交job不能替代中止正在执行的dispatch | launch参数、支持集合、等待失败和用户可见诊断 | 维护参与者、到达集合和等待依赖；区分确定违规、循环等待证据与普通超时 | Q2验证GPU同步状态到IRQ/完成/Guest错误回传全链；Q1只能注入错误码验证后半段，不能证明前半段已检测 |

KMD能力充分时可以复用现有接口，无需为三个checker分别增加重复ioctl。每个checker应独立开关、单独统计性能与覆盖；共用事件格式也不能把I1、R1、S1归并成一种shadow检查。硬件错误回传与Linux驱动的取消／重置语义应按实际实现核对。[K01][K02]

<a id="component-estimates"></a>

## 16. 组件改动与代码量估算

### 16.1 估算口径、三组件归属与适用边界

**本章全部数字均为【设计估算】，不是仓库实测、厂商内部数据、已完成实现的统计或工期承诺。** 尚未审计目标 GPU 软件仓库；区间用于拆分责任、比较投入和安排技术摸底。统一单位为 kLOC，1 kLOC 约为 1,000 行新写或实质修改的手写代码，同一行重复修改不反复计入。核心代码与手写测试分别列示；空行、注释、机械生成代码、未修改的第三方依赖均不计入。

生产软件按 **Compiler、KMD、UMD 三个顶层责任包**组织。这里是预算归属，不是执行位置或代码仓库布局：

- **Compiler＝Pass／后端／编译链接支持＋device 检查库。** 前一子项在编译工具链工作，后一子项链接进设备程序并在 GPU 上执行；不能把设备检查开销解释为编译阶段开销。
- **KMD＝内核态驱动中实际新增或实质修改的契约实现。** 只有已有接口无法满足检查器所需的映射、完成、错误和隔离契约时才追加；复用已有能力不计为新增实现。固件不是 KMD，缺失的固件配合单独列出。
- **UMD＝SDK/API 接入、Host runtime、用户态 driver/loader 和 CLI/符号化等用户态工具的责任包。** SDK 暴露注册和配置入口；Host runtime 维护 API、队列与 graph 语义；用户态 driver/loader 管理对象、模块和提交；CLI/报告工具负责启动、符号化和结果消费。上述模块可分库、分进程，不要求把 SDK 或 CLI 嵌入底层 UMD driver。

**功能级 cmodel 独立计费。** A 中它承载检测主体；B–F 中它主要承担参考语义、差分执行或新增指令语义验证。随后另列的 QEMU 全系统平台只计算系统设备接线与 guest 集成，不再次购买同一个 cmodel 检测引擎。

本章先给出“**已有 KMD 契约足够**”的基础估算，以保持既有五项预算的数值可追溯；再给条件性 KMD 增强和 QEMU 平台投入。基础前提为已有可用 LLVM GPU 后端、UMD、功能级 GPU cmodel 和基础调试能力；C 另外要求已有成熟 decode/encode 和基本 patch 框架。基础表包含内部可发布版本的失败路径和工程化，不覆盖全部历史硬件、任意语言 ABI 或商业工具的全部优化。

A 的主预算为 A0 地址检查、受控 A1 shared/stack 对象 map 和同步事件观察/接线；不是通用 A2 指针 provenance、全指令值传播、所有合法调度穷举或完整 HB 引擎。B–F 的主预算针对内存检查与公共设施；完整 init/race/sync 引擎见 §16.7。F 仅为已有硬件能力的软件使能，RTL、DV、硅片及完整硬件设计不属于本章软件 SLOC。

区间端点逐项相加仅形成规划范围，不是统计置信区间；下限对应基础设施和接口较完备，上限对应更多受控边界与兼容工作，架构前提失效仍可能超出上限。

### 16.2 公共底座 P：按三个生产组件归属，只计一次

| 责任包／范围 | 核心 kLOC | 手写测试 kLOC | 包含内容 |
| --- | --- | --- | --- |
| Compiler | 2–4 | 2–4 | Pass/后端 1–2、测试 1–2；device 检查库 1–2、测试 1–2。option、manifest、context、报告缓冲、基础 ABI。 |
| KMD | 0 | 0 | 现有映射、队列完成、错误回传和隔离契约已足够；没有新增 KMD 实现。 |
| UMD | 6–11 | 5–8 | SDK/Host runtime/用户态 driver 联合项 4–7、测试 3–5；CLI/符号化工具 2–4、测试 2–3。 |
| **三生产组件小计** | 8–15 | 7–12 | 只表示生产侧软件责任包，不含下列功能模型。 |
| 功能级 cmodel | 1–2 | 1–2 | 事件适配、统一输入输出及模型侧协议验证。 |
| **含 cmodel 的公共研发参考包 P** | 9–17 | 8–14 | 对应原五项公共底座的合计；三组件与模型均未重复计数。 |

KMD 的“0”表示不新增该组件代码，不表示接口免于验证。调用既有接口的分配、失败完成、取消、资源回收等集成用例计入 UMD 或模型测试；若这些用例证明契约不足，再转入 §16.8 的缺失能力包。

### 16.3 A／B／C 在 P 之上的新增量

以下每行均未含 P；KMD 列为既有契约满足时的基线。模型行与三个生产组件分别归属。

| 路线 | 责任包 | 新增核心 kLOC | 新增手写测试 kLOC | 核心工作范围 | 测试工作范围 |
| --- | --- | --- | --- | --- | --- |
| A | Compiler | 1–3 | 1–3 | Pass/后端输出 PC、frame、shared/stack 对象 map；设备库按需提供 device allocator 事件。 | sidecar 与最终代码匹配、地址空间和 frame 边界、设备事件对齐。 |
| A | KMD | 0 | 0 | 独立模型夹具可不参与 KMD；接入现有真实事件源时复用已有契约。 | KMD 无新增代码测试；事件重放和合法/非法次序由 UMD/模型测试承担。 |
| A | UMD | 2–6 | 2–6 | SDK/runtime/driver 导出 alloc/free/copy/launch/queue 依赖；CLI 合并模型与 Host 诊断。 | API 与底层命令去重、异步生效边界、图重放 ID、日志截断和覆盖状态。 |
| A | 功能级 cmodel（单列） | 6–12 | 5–10 | 真实访存 tap、宽度/byte enable/权限/对齐、受控对象 map、同步事件记录及停止策略。 | masked/vector、同址不同 block、分配释放顺序、合法反例和模型确定性；不含完整 A2。 |
| B | Compiler | 12–22 | 10–18 | 访存/atomic/intrinsic/mask 插桩、多地址空间 lowering、shared/private 布局；设备 shadow 查询和报告 helper。 | 优化交互、任意访问长度、masked/EVL、原子语义、helper 不重入插桩及 SIMT 前进性。 |
| B | KMD | 0 | 0 | 复用现有 GPUVA/映射、完成、错误回传及所选终止策略的契约；不足时按实际缺口选 K-M/K-E/K-A。 | 新增 KMD 测试为 0；对既有契约的端到端用例包含在 UMD/模型测试。 |
| B | UMD | 5–11 | 4–9 | shadow 资源、poison/unpoison 排序、context/JIT 链接、池分配及 CLI 覆盖/资源不足报告。 | 异步 alloc/free、shadow 可见性、映射失败、卸载回收、日志满和合法别名。 |
| B | 功能级 cmodel（单列） | 1–3 | 1–3 | 独立区间 oracle，核对 shadow 状态与应用命令的生效次序。 | 与插桩结果差分、边界精度、检测器自身访存分类和失败注入。 |
| C | Compiler | 6–13 | 5–11 | 复用 MC/指令语义接口；设备 patch stub、寄存器保存恢复、metadata 查询与报告。 | stub 透明性、寄存器/谓词恢复、访问宽度和原子保持；不重计已有解码器。 |
| C | KMD | 0 | 0 | 复用已有正规代码加载/执行、映射、完成及所选终止契约；调试接口仅在实现依赖时要求，缺口按需选 K-M/K-E/K-A/K-X。 | 新增 KMD 测试为 0；代码生命周期和故障归因的集成测试计在 UMD/模型。 |
| C | UMD | 17–32 | 15–28 | 运行时重写、CFG/branch relocation、资源重算、patch cache、JIT/unload；CLI 恢复原 PC。 | 分支/调用/常量区重定位、并发加载、缓存版本、拒绝未知编码、资源声明及异常路径。 |
| C | 功能级 cmodel（单列） | 2–5 | 2–5 | 原始代码与重写代码的独立差分执行；识别原指令和 stub 的语义差异。 | 编码、谓词、依赖等待、分支、特殊访存、异常路径的等价和错误反例。 |

C 的运行时重写/patch 管理延续原预算归入 UMD。若团队将其抽成独立库，调整责任归属即可，不能在 Compiler 与 UMD 各计一份。A 中同步事件 tap 不等于已经交付完整 race/sync 分析；I1/S1/R1 按 §16.7 选定的工作包补充。

### 16.4 D／E／F 在 P 之上的新增量

以下每行均未含 P；KMD 列为既有契约满足时的基线。模型行与三个生产组件分别归属。

| 路线 | 责任包 | 新增核心 kLOC | 新增手写测试 kLOC | 核心工作范围 | 测试工作范围 |
| --- | --- | --- | --- | --- | --- |
| D | Compiler | 28–54 | 23–45 | Pass/后端传播 bounds/identity、指针 load/store/copy/atomic 和受控 ABI；设备 descriptor/pointer-shadow 协议。 | phi/select、spill/reload、部分 copy、值-身份原子配对、ABA、unknown 来源及受控跨函数边界。 |
| D | KMD | 0 | 0 | 复用已有 metadata 映射、context 隔离、完成及所选终止契约；不足时按需选 K-M/K-E/K-A。 | 新增 KMD 测试为 0；metadata 生存期与地址复用验证计入 UMD/模型。 |
| D | UMD | 10–20 | 9–17 | 对象 ID/epoch、参数 sidecar、外部对象注册、能力协商、metadata 退休与 CLI 保证等级。 | 参数布局、epoch 复用、并发退休、未插桩 writer 边界和跨模块协商。 |
| D | 功能级 cmodel（单列） | 3–7 | 3–7 | 受控寄存器/frame/内存槽身份 oracle 与原子发布参考语义；不是通用 A2 模型。 | 旧指针/新地址实例、部分写破坏身份、原子配对、已定义 ABI 集合的最小反例。 |
| E | Compiler | 40–76 | 33–63 | B 全量＋D 独有部分＋C stub 子集和模式边界；共享检查 ABI、manifest 和 descriptor。 | 模式间值/身份流转、重复插桩防护、版本协商、provenance 降级及元数据一致性。 |
| E | KMD | 0 | 0 | 共享 B/D/C 所需契约并集；不足时选 K-M/K-E/K-A/K-X，每包只计一次。 | 新增 KMD 测试为 0；跨模式集成验证已在 E 的 UMD/模型测试去重。 |
| E | UMD | 21–41 | 19–38 | 按模块选择编译插桩或受控 binary fallback、缓存和版本校验、共同对象表及分级报告。 | 混合模块、JIT/graph 重放、共享对象身份、未知模块和拒绝重复 patch。 |
| E | 功能级 cmodel（单列） | 6–12 | 6–12 | 共享 B 的区间 oracle、D 的受控身份参考和 C 子集差分，另加跨模式检查。 | 模式组合、同一对象跨边界、覆盖降级与失败路径；模型代码只计一份。 |
| F | Compiler | 9–18 | 9–18 | 已有硬件能力的 intrinsic/指令选择与 ABI；设备 tag/bounds/metadata helper 及报告。 | 目标指令 lowering、硬件能力协商、错误 PC、metadata 版本和不支持设备回退。 |
| F | KMD | 0 | 0 | 基线假设所需硬件资源/故障/上下文接口也可复用；缺口选 K-M/K-E/K-A/K-F。 | 新增 KMD 测试为 0；缺失契约的驱动验证另在条件包，不用此数覆盖固件或 DV。 |
| F | UMD | 6–13 | 6–12 | 资源初始化、tag/descriptor 生命周期、上下文协商、失效请求及诊断工具。 | 释放后失效、context switch、已有支持范围内的 alias/peer、故障归因和资源不足。 |
| F | 功能级 cmodel（单列） | 6–12 | 6–12 | 在已有 GPU 模型上补受限硬件检查语义及 reference fault；不是新建完整 GPU 模拟器。 | 新检查语义、缓存/身份失效可见性、故障/恢复协议的功能反例；不替代 RTL/DV。 |

#### 责任包内的可追溯子项

下表保留原五项预算中 Compiler 与 Device runtime 的原始区间。P 是公共底座，其余均为路线增量；将两子项相加即得上表的 Compiler 列。

| 范围 | Pass／后端核心 | Pass／后端测试 | 设备检查库核心 | 设备检查库测试 |
| --- | --- | --- | --- | --- |
| P | 1–2 | 1–2 | 1–2 | 1–2 |
| A | 1–2 | 1–2 | 0–1 | 0–1 |
| B | 7–13 | 6–11 | 5–9 | 4–7 |
| C | 0–2 | 0–2 | 6–11 | 5–9 |
| D | 18–34 | 15–29 | 10–20 | 8–16 |
| E | 23–44 | 19–36 | 17–32 | 14–27 |
| F | 5–10 | 5–10 | 4–8 | 4–8 |

UMD 责任包同样保留两个可追溯子项。原预算没有进一步将 SDK、Host runtime 和用户态 driver 三者量化分开，因此联合区间不能冒充三个模块分别测得的 SLOC；代码审计后再按接口和文件归属细分。

| 范围 | SDK／Host runtime／driver 核心 | SDK／Host runtime／driver 测试 | CLI／符号化工具核心 | CLI／符号化工具测试 |
| --- | --- | --- | --- | --- |
| P | 4–7 | 3–5 | 2–4 | 2–3 |
| A | 1–3 | 1–3 | 1–3 | 1–3 |
| B | 4–8 | 3–6 | 1–3 | 1–3 |
| C | 14–26 | 12–23 | 3–6 | 3–5 |
| D | 7–14 | 6–12 | 3–6 | 3–5 |
| E | 16–31 | 14–28 | 5–10 | 5–10 |
| F | 4–8 | 4–8 | 2–5 | 2–4 |

上述子项单位均为 kLOC；它们已经包含在路线表内，不能再次加入总量。D 包含受控接口集合内的 D2，不能据此承诺任意 C++ ABI、任意第三方库或任意未插桩 writer 都保留身份。

### 16.5 从 sanitizer 零起点建设的基础总量

“零起点”只指 sanitizer 功能本身，仍要求 §16.1 所述现有工具链、驱动和 GPU 模型基础。每个方案的两行是不同统计边界，**不能相加**。既有第16章的 18–38、27–53 等核心区间对应“含功能 cmodel 的研发参考包”；不是三生产组件的软件小计。

| 方案 | 统计边界 | 核心 kLOC | 手写测试 kLOC | 核心＋测试 kLOC |
| --- | --- | --- | --- | --- |
| P＋A | 三生产组件小计 | 11–24 | 10–21 | 21–45 |
| P＋A | 含功能 cmodel 的研发参考包 | 18–38 | 16–33 | 34–71 |
| P＋B | 三生产组件小计 | 25–48 | 21–39 | 46–87 |
| P＋B | 含功能 cmodel 的研发参考包 | 27–53 | 23–44 | 50–97 |
| P＋C | 三生产组件小计 | 31–60 | 27–51 | 58–111 |
| P＋C | 含功能 cmodel 的研发参考包 | 34–67 | 30–58 | 64–125 |
| P＋D | 三生产组件小计 | 46–89 | 39–74 | 85–163 |
| P＋D | 含功能 cmodel 的研发参考包 | 50–98 | 43–83 | 93–181 |
| P＋E | 三生产组件小计 | 69–132 | 59–113 | 128–245 |
| P＋E | 含功能 cmodel 的研发参考包 | 76–146 | 66–127 | 142–273 |
| P＋F | 三生产组件小计 | 23–46 | 22–42 | 45–88 |
| P＋F | 含功能 cmodel 的研发参考包 | 30–60 | 29–56 | 59–116 |

**A 的检测器主体就在功能 cmodel 中，三生产组件小计不足以交付完整 A。** B–F 的模型工作则用于研发参考、语义差分或功能验证；若项目另有等价验证资产，只能在逐工作包确认复用后扣除相应量，不能默认整包删除。

所有基础总量均假设新增 KMD 为 0；缺失能力另加 §16.8，选择 QEMU 全系统验证再加 §16.9。也不能把 P＋B、P＋D、P＋C 的“从零总量”相加；公共 P、设备报告、对象表和其他共享实现会被重复计费。

### 16.6 E 的去重计算

| 构成 | 核心 kLOC | 手写测试 kLOC | 去重说明 |
| --- | --- | --- | --- |
| 公共 P | 9–17 | 8–14 | 共用协议只计一次 |
| B 完整增量 | 18–36 | 15–30 | 作为 E 的基础插桩路径 |
| D 在 B 上的独有新增 | 29–55 | 24–46 | 去除与 B 复用的检查通路、对象管理和报告 |
| C 受控 fallback 子集 | 10–20 | 9–19 | 不包含 C 的全部复杂 ISA 和全部 patch 情况 |
| 跨模式集成与边界协议 | 10–18 | 10–18 | 只计各单一路线不需要的模式选择与交接 |
| **总计 P＋E** | **76–146** | **66–127** | 与 §16.5 含模型参考包一致 |

按新责任包校验，E 在 P 之上的增量为 Compiler 核心 40–76、测试 33–63；UMD 核心 21–41、测试 19–38；功能 cmodel 核心 6–12、测试 6–12；基线 KMD 为 0。四项分别相加得到核心 67–129、测试 58–113，再加 P 得到上述参考包。

该估算不同时承诺完整 C、通用 A2 或任意语言 ABI 的 D2。若需要这些能力，先列出相对 E 的独有工作包；已有对象表、设备 runtime、报告和 KMD 契约均不得再次计费。

### 16.7 Initcheck、Racecheck、Synccheck 的独立新增预算

以下以 P 和已稳定的访存事件/报告基础为前提。KMD 现有契约足够时均为新增核心 0、测试 0；如果需要扩充驱动契约，仍从 §16.8 按缺口选取，不能每增加一种 checker 就再收一套 KMD 底座。

核心区间由原五项预算直接重组。**原预算的手写测试只有合计，没有组件分项；下表测试分摊是本次为排期提出的设计分配，保留原合计，不是补造历史测量值。**

| 新增能力 | 责任包 | 新增核心 kLOC | 新增手写测试 kLOC |
| --- | --- | --- | --- |
| I1 内存初始化位图 | Compiler | 5–10 | 4–8 |
| I1 内存初始化位图 | UMD | 3–6 | 3–6 |
| I1 内存初始化位图 | 功能级 cmodel（单列） | 1–2 | 1–2 |
| S1 基础 barrier/mask | Compiler | 5–10 | 5–10 |
| S1 基础 barrier/mask | UMD | 2–4 | 2–4 |
| S1 基础 barrier/mask | 功能级 cmodel（单列） | 2–4 | 2–4 |
| R1 shared HB/FSM | Compiler | 11–22 | 11–22 |
| R1 shared HB/FSM | UMD | 3–7 | 3–7 |
| R1 shared HB/FSM | 功能级 cmodel（单列） | 3–6 | 4–7 |
| R1 扩至 global/跨 kernel | Compiler | 8–16 | 8–17 |
| R1 扩至 global/跨 kernel | UMD | 5–11 | 5–12 |
| R1 扩至 global/跨 kernel | 功能级 cmodel（单列） | 2–4 | 3–5 |
| 重复访问/碰撞模式 | Compiler | 6–12 | 5–10 |
| 重复访问/碰撞模式 | UMD | 2–4 | 2–5 |
| 重复访问/碰撞模式 | 功能级 cmodel（单列） | 1–3 | 1–3 |

| 新增能力 | 核心合计 kLOC | 手写测试合计 kLOC | 边界 |
| --- | --- | --- | --- |
| I1 内存初始化位图 | 9–18 | 8–16 | 读写/copy 的字节定义性、并发位合并、copy/memset 生效和 Host 边界；不含值级寄存器 MSan。 |
| S1 基础 barrier/mask | 9–18 | 9–18 | 参与集合、barrier 状态和 phase；复杂 async/cluster 另按范围扩展。 |
| R1 shared HB/FSM | 17–35 | 18–36 | shared 访存历史、层次同步与 scope/ordering、clock 容量和前进性。 |
| R1 扩至 global/跨 kernel | 15–31 | 16–34 | 在前一 R1 上扩 global、跨 block/kernel 和 queue/event/graph 边；不含全部跨设备内存模型。 |
| 重复访问/碰撞模式 | 9–19 | 8–18 | 受控重复访问、延迟/碰撞观测及内存语义反例；报告概率与窗口限制。 |

多个 checker 可复用事件、符号化和基础状态表，因此不能要求上表全选后机械相加。A 的基础事件入口或已有位图原语可以成为 I1/S1/R1 的复用资产，只有对应完整工作包已经满足验收范围时才能扣除；不能把“观察到了 barrier”当作完整 synccheck，也不能把功能模型按顺序执行一次当作完整 racecheck。

值级 MSan、通用跨 CPU/GPU TSan、完整 A2 provenance 与多调度探索须另作语义设计和工作分解；本章不提供伪精确总量。

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
| A | 0 | 0 | 独立模型夹具无需 KMD | 0 | 0 |
| B | 0 | 0 | K-M＋K-E＋K-A | 7–16 | 7–18 |
| C | 0 | 0 | K-M＋K-E＋K-A＋K-X | 8–19 | 8–21 |
| D | 0 | 0 | K-M＋K-E＋K-A | 7–16 | 7–18 |
| E | 0 | 0 | K-M＋K-E＋K-A＋K-X | 8–19 | 8–21 |
| F | 0 | 0 | K-M＋K-E＋K-A＋K-F | 10–23 | 10–26 |

K-A 的 KMD 核心 2–5、测试 2–5，是将原“安全 dispatch/context abort 软件接线核心 4–10、测试 4–10”规划为内核驱动与外部固件两个责任包。若固件也尚未接通，再列**固件接线核心 2–5、测试 2–5**，两者合计仍为 4–10/4–10。这个二分是本次设计估算，不是原预算已有实测拆分。若固件已完成，只计缺失的 KMD 部分；用户态错误消费已在 UMD 基础范围时不得再加。若硬件取消能力本身不存在，上述区间不适用，先验证停止/隔离的可行性。

K-F 同样只为未具备的驱动状态契约计费。F 的基础预算已经包含 UMD 资源初始化与功能 cmodel 新检查语义，不能再将这些实现搬入 K-F 后重复收费。

#### 16.8.3 保留的其他条件项

| 缺失前提 | 核心 kLOC | 手写测试 kLOC | 处理边界 |
| --- | --- | --- | --- |
| 成熟机器码重写基础不存在 | 15–35 | 15–40 | 仅补 decoder/encoder、语义与基础重定位/资源接口；剔除 C 已计的 CFG、patch 管理和缓存。 |
| GPUVA/metadata 映射契约可通过既有设施有限扩展 | 采用 K-M | 采用 K-M | 若没有可行地址空间/页表或权限机制，先做可行性原型；不能默认 K-M 能新建整套 GPUVM。 |
| 多 GPU/IPC/外部导入为首版硬需求 | 单独分解 | 单独分解 | 规范对象身份、别名、一致性和跨设备时序；现有 API 规模与保证未确定，不给统一追加数。 |
| F 需要新硬件或大幅新增固件 | 不纳入软件表 | 不纳入软件表 | ISA/RTL/DV/性能模型/硅片和超出接线范围的固件另立计划。 |
| 完整 A2、值级 MSan 或通用跨 CPU/GPU TSan | 单独设计 | 单独设计 | 语义和调度集合尚未冻结，不以 A 或 I1/R1 的基础区间替代。 |

### 16.9 功能 cmodel 与 QEMU 全系统平台分别计费

#### 16.9.1 四种环境不是同一种检测能力

本节Q0–Q3是用于区别架构的通用配置名。**本项目现有QEMU全系统平台经用户确认已具备目标GPU ISA执行模型，采用Q2；Q0/Q1不再是当前待完成GPU执行的阶段。** 未核对源码的Guest接口、地址域和检查器能力仍待逐项确认，不能按全部缺失计费。

| 配置 | 定义与能验证的范围 | 平台新增核心 kLOC | 平台新增手写测试 kLOC | 不能据此承诺的能力 |
| --- | --- | --- | --- | --- |
| Q0：无目标 GPU 设备 | 已有 QEMU 运行 CPU/guest 的环境；可执行 CPU 侧单元测试。 | 0 | 0 | 没有目标 GPU kernel 执行，也没有 kernel 内访存/sync 检测。 |
| Q1：不解释 GPU ISA 的设备桩 | 枚举、有限 MMIO/队列、命令格式、模拟完成或故障的协议夹具。 | 2–5 | 2–5 | 假完成或 DMA copy 不能证明 GPU load/store、atomic、mask、barrier 或 sanitizer 检查逻辑正确。 |
| **Q2：本项目已具备GPU ISA执行模型** | 现有全系统平台能执行目标kernel；下列14–28/14–30为**仅当全部列出的Guest/设备接线缺失时**的条件参考包，不能当作当前固定新增成本。 | **0～14–28（按实际缺口取子包）** | **0～14–30（按实际缺口取子包）** | GPU ISA模型已存在；已实现的设备前端、队列、Guest驱动接入和执行桥不得重复开发或重复计费。 |
| Q3：已有可用 VFIO/设备直通路径 | guest 驱动向真实 GPU 提交，另做平台验证与诊断接线。 | 3–7 | 4–9 | kernel 检测发生在真实 GPU 及其 sanitizer 上，不是由 QEMU CPU 模拟器完成。 |

Q0 的 0 表示不建设 GPU 平台，不是“零成本拥有 GPU 检测”。Q1 是可选协议原型，不能自动再叠加到 Q2 全额预算；升级 Q2 时，实际复用的设备前端代码须从 Q2 相应包扣除。Q3 的范围是一套既定 guest OS/设备组合与已选 B–F 真机路径的验证接线；它不提供 A 所需的 GPU 模型，不能以设备直通替代 A 的模型执行。Q3 的前提包括已有适用的直通路径、IOMMU/设备隔离、reset 能力和可工作的 guest GPU 驱动；若这些前提尚未成立，3–7/4–9 只覆盖接线与验证，不能报价完整直通使能。

#### 16.9.2 Q2 的一次性平台基础

**已确认条件：GPU ISA执行模型存在于本项目QEMU全系统平台。** 以下平台基础数字是v1.2列出的**全部系统接线子项尚需新建**的规划包，并非v1.3的实测缺口；具体已有哪些设备前端/guest KMD/地址空间/队列桥不明，应逐项标注“已存在”“需适配”“缺失”。下表既有cmodel执行桥一项只有当现有QEMU与独立cmodel确实分离且缺少桥接时才成立；**QEMU内部已实现独立GPU ISA解释器时，该项可以为0，不能再给它重复建设一个模型。**

| 平台包 | 核心 kLOC | 手写测试 kLOC | 内容与边界 |
| --- | --- | --- | --- |
| 设备前端：MMIO/IRQ/reset | 3–6 | 3–6 | 设备枚举与寄存器、门铃、中断、复位和错误路径；不实现 GPU 指令。 |
| 队列/DMA/地址空间桥接 | 5–9 | 5–10 | guest 提交队列、DMA 到 guest 内存、现有 GPUVA 翻译/权限接口的连接及完成次序；不重写 GPUVM。 |
| 现有 GPU cmodel 执行桥 | 3–6 | 3–7 | 命令送入模型、访存回调、执行/停止和完成回调；不复制 A–F 检测器或模型 ISA 实现。 |
| 已有 guest KMD 集成 | 3–7 | 3–7 | 使既有驱动绑定模型设备、连接已定义契约、启动回归、故障注入及退出清理；不从零开发 KMD。 |
| **Q2 平台基础合计：一次** | 14–28 | 14–30 | 同一设备/OS/驱动支持范围内共享；多平台移植根据差异另计。 |

guest KMD 的平台适配行与 §16.8 的“新增驱动契约”按修改内容划界：连接一个已存在的接口计在 Q2；实现此前不存在的 KMD 能力计在 K-M/K-E/K-A/K-X/K-F。一个工作包只能归入一处。

因此，基础表的KMD为0不代表选择Q2后所有Guest驱动适配也是0。上表已包含该集成责任包；项目按文件和责任人记账时，可以把其中确属Guest KMD的修改移列到KMD，但总额不变，不能再加同一份代码。

#### 16.9.3 Q2 的路线特有新增与去重

下列都是平台接线/端到端验证增量，检测算法、device 检查库和功能 cmodel 检查实现仍在 §16.3–§16.7 内，只计一次。


| 路线 | 路线特有核心 kLOC | 路线特有手写测试 kLOC | 范围 |
| --- | --- | --- | --- |
| A | 2–4 | 2–5 | 模型事件与 guest 队列/对象 ID 的关联、停止与报告传回；复用已有 A0/A1 模型检查器。 |
| B | 2–4 | 3–6 | guest shadow 映射/权限、poison 命令和实际完成、失败归因与多层地址一致性。 |
| C | 3–6 | 4–8 | guest 模块/重写代码生命周期、执行可见性、patch cache 与原 PC 诊断的全链路接线。 |
| D | 3–6 | 4–8 | 参数 sidecar、descriptor/epoch、生存期与地址复用跨 guest/UMD/模型边界。 |
| E | 5–10 | 6–12 | B 接线全量＋D 独有接线＋C 受控子集＋模式切换；不相加 B/C/D 三个全额。 |
| F | 4–8 | 5–10 | 已有/另计的模型硬件语义与设备资源、metadata 状态、IRQ/fault 和 guest 错误协议连接。 |

| 路线 | Q2 基础＋路线接线：核心 kLOC | Q2 基础＋路线接线：测试 kLOC |
| --- | --- | --- |
| A | 16–32 | 16–35 |
| B | 16–32 | 17–36 |
| C | 17–34 | 18–38 |
| D | 17–34 | 18–38 |
| E | 19–38 | 20–42 |
| F | 18–36 | 19–40 |

E 的平台特有增量按 B 接线核心 2–4/测试 3–6，加 D 独有 1–2/1–2、C 子集 1–2/1–2、跨模式交接 1–2/1–2，合为核心 5–10、测试 6–12。Q2 平台基础只加一次。

**组合公式：** 选定路线的含功能 cmodel 研发参考包＋实际缺失的 KMD 能力净增量＋所选平台的净增量＋所选额外 checker 的去重增量。外部固件、硬件和其他已声明的前置项目单列。

例如 B：基础参考包核心27–53、测试23–44；**只有Q2全部旧条件包确实尚未实现时**，再加平台基础与B接线核心16–32、测试17–36，才有核心**43–85**、测试**40–80**这一情景预算。对已拥有Q2 GPU ISA执行平台的本项目，这不是默认追加额；应按已存在的设备前端和缺失的Sanitizer接线逐包裁剪，可能只需B特有的shadow/报告接入。若 K-M/K-E/K-A 又确实全缺，且符合各包前提，再加核心 7–16、测试 7–18，得核心 **50–101**、测试 **47–98**；固件缺口另列。不能先加含模型的 B，再把同一个模型随 Q2 重计一次。

Q2 的通过说明已实现模型语义与指定 guest 栈的集成结果，不自动证明全部真机调度、真实 cache/一致性时序或性能。Q3 则使用真实 GPU，仍需按真实软件栈与工具覆盖范围解释结果。

### 16.10 将区间转为项目预算与算术校验

建议以两周左右技术摸底收集 opcode/访存类型、kernel 与 allocator/API 数量、已支持的 patch 模式、module ABI 数量、现有 KMD 契约、模型与真机差异，以及**既有Q2 GPU ISA模型的访存hook和Guest系统接线现状**（不再摸底Q2是否存在）；Q3另按实际情况判断。先做可运行的最小链路，再按“接口、算法、失败路径、兼容、验证”建立工作包和责任人；人月需要结合团队经验与审查/验证成本估计，不用统一的每天写多少行换算。

本章数值的复核规则为：

- Compiler 新区间＝原 Compiler＋原 Device runtime；UMD 新区间＝原 Host runtime＋UMD＋原 Tooling；功能 cmodel 保留原项；KMD 基线为 0。
- 三生产组件小计＝Compiler＋KMD＋UMD；含模型参考包＝三生产组件小计＋功能 cmodel；从 sanitizer 零起点总量＝P＋路线增量。
- E＝P＋B＋D 在 B 上的独有新增＋C 受控子集＋跨模式集成；共享 KMD 能力、Q2 基础和模型检查器均只计一次。
- 区间相加采用 `[a,b]＋[c,d]＝[a＋c,b＋d]`；核心与手写测试独立相加，再检查核心＋测试总量。

[配套算术数据](estimates-v1.2.json)保留了原五项矩阵、重组后的三组件/模型矩阵、逐路线总量、E 分项、KMD 条件包、额外 checker 与 QEMU 组合。程序化复核只说明重组与加总一致，不是实现功能、性能或工程量预测已经得到验证。

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
执行环境：native / functional-cmodel / QEMU-Q2 / QEMU-Q3
模型环境另记：QEMU / GPU-model / Guest-kernel / Guest-KMD / CPU-accelerator
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

本章将前面的能力、算法和组件职责落实到具体错误。每个场景回答六个问题：**什么输入和执行条件会触发；哪一行或哪一对事件有问题；违反了什么约束；有哪些检测方案；哪些组件必须提供信息；修复后与合法反例应得到什么结果。** S01～S28 是场景编号；M1～M6、I1、R1、S1、A1 是第2章的检测能力编号，与第20章里程碑 M0～M6 不同。

**例子的地位与使用方式。** 除明确标注的规范事实外，本章是【工程建议】的原创教学和验收设计，尚未在用户 GPU 上编译或运行。代码采用 C/CUDA 风格伪代码及少量 ISA 风格操作，`alloc`、`kernel`、`consume`、`thread_id`、受控 allocator 和 metadata API 都表示相应语义，不是声称用户 SDK 已有同名接口。源级例子中的非法访问会涉及未定义行为；真实验收需要确认目标访问在优化后确实存在，必要时用受控 IR/MIR/ISA 测试补充，不能要求运行时报告一个已被编译器消除的操作。

未另外说明时，分配成功、对象已发布且具有例中声明的访问权限；用于隔离单一错误的初始化和队列同步必须执行。一个场景里的错误版、修复版和其他变体应独立运行，不能让前一次非法操作污染后一次判断。访问范围统一使用半开区间 `[addr, addr+width)`；实际实现使用防整数溢出的等价比较，不直接信任可能回绕的 `addr+width`。

**本版组件口径。** 各例中的deviceRT是编译器交付包内的设备检查库；Host runtime/loader/tooling在UMD交付包内分层。每例新增的KMD及功能级cmodel/QEMU支持关系统一列于[第18.29节](#scenario-platforms)，需要与例中检测前提一同阅读。

**路线和检查器是两个维度。** A 是 cmodel，B 是 LLVM shadow，C 是二进制插桩，D1/D2 是对象 bounds／时间身份，E 是混合方案，F 是硬件辅助。它们决定事件从哪里采集、元数据怎样提供；init、race、sync 仍需各自的状态和分析引擎。例如“用 LLVM 插入 shared 访存事件”可以作为 race detector 的入口，但现成的地址 shadow 不会因此自动获得 happens-before 检测能力。以下只列与该例有关的可行路线，不把六条路线都写成无条件支持。

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

对跨页、负偏移、整数回绕、稀疏／非连续访问、peer／IPC 和多种特殊 opcode，应在对应场景下展开参数化变体。这 28 个实例覆盖本报告已经声明的主要场景类别，不构成任意 ISA 和全部语言错误的穷举。项目新增能力时，先增加场景及合法对照，再扩充支持矩阵。

S01～S12 除另行声明外，读入数据已初始化、输出对象独立有效，单个 kernel 只由指定线程执行目标访问。路线 A 的 A0／A1／A2 沿用第9章的地址观察、对象 map 与身份跟踪三个层次；模型必须具备相应输入，不能只凭能执行 kernel 就宣称对象或时间安全。


<a id="case-s01"></a>

### 18.1 S01：Global 对象头部与尾部越界

**前提与错误代码。** 受控夹具给 `a` 分配 64 B，即 16 个 `uint32_t`，首地址为 `0x1000`；前后各 16 B 是保留的不可访问区，不包含其他 live 对象。启动一个 32 线程 block，分别传入 `tail=false` 和 `tail=true`：

~~~cpp
__global__ void s01(uint32_t *a, bool tail) {
    if (threadIdx.x == 0) {
        int i = tail ? 16 : -1;
        a[i] = 7;  // 错误：分别写 [0x1040,0x1044) 或 [0x0ffc,0x1000)
    }
}
// 独立运行：s01<<<1,32>>>(a, false); s01<<<1,32>>>(a, true);
~~~

**哪里不对、怎样检。** 合法字节区间只有 `[0x1000,0x1040)`；两次 4 B 写分别从尾后第一字节和头前 4 B 开始。正确条件是整个实际访问包含于该对象，并按第 6 章的减法条件避免地址加法溢出。B 可将两侧 redzone poison，检查被写入的全部字节；A0 可用 UMD 的有效区间和实际 store 范围检出本例。二者依赖这里确实存在的无效邻区；更远的访问若落入 live B，需转入 S04 的来源检查。D1 保留 `a` 的原对象 bounds 后也可检出，无须依赖邻区布局。[L02][L05]

**组件观测。** compiler 保留 store 的谓词、4 B 宽度和源位置；deviceRT 在执行写之前检查，不能让其他 31 个未执行线程产生报告；UMD 提供逻辑长度 64 B，而非把 allocator 的对齐补齐长度当对象长度；cmodel 对照实际地址与 byte enable，独立判断头、尾两侧。

**合法反例、修复及报告。** 改为 `a[0]`、`a[15]` 应通过；循环边界应使用 `0 <= i && i < 16`。预期为 global 越界写，包含 M1／M3 对应保证、宽度 4、对象范围及偏移 `-4`／`+64`。若只有页级映射检查，两个地址仍位于已映射页时可能漏检；这不是对象检查已经通过。

<a id="case-s02"></a>

### 18.2 S02：访问首地址合法，宽访问的末端越界

**前提与错误代码。** `src` 是首地址按 16 B 对齐、逻辑长度恰为 30 B 的 global 对象，尾部已 poison；`dst` 是独立的 16 B 对齐、16 B 有效对象。下面的示意 intrinsic 明确对应一个要求 16 B 对齐、实际读取 16 B 的目标操作：

~~~cpp
__global__ void s02(const uint8_t *src, uint8_t *dst) {
    if (threadIdx.x == 0) {
        u8x16 v = load16_req_align16(src + 16); // 错误：读偏移 [16,32)
        store16_req_align16(dst, v);
    }
}
// s02<<<1,32>>>(src, dst);  // src 的偏移 30、31 两字节无效
~~~

**哪里不对、怎样检。** 起点 `src+16` 合法且对齐正确，但 16 B 读取中只有前 14 B 属于对象。应检查完整访问范围，不能用“起始地址在对象内”代替范围条件。B 必须检查访问涉及的各 shadow granule，并识别最后一个 granule 的有效前缀；A0 或 C 则必须从执行语义／解码结果取得真实的 16 B 宽度，验证所有有效字节。若只有 4 B 标量模板、只查首字节或错误忽略尾部，均会漏检。[L02][L05]

**组件观测。** compiler 记录向量访存及后端拆分情况；deviceRT 的长度检查覆盖跨 granule 尾部；UMD 登记 30 B 逻辑长度，不能擅自扩大到 32 B；cmodel 观察指令的有效访问字节，不把缓存行预取或合并事务覆盖的所有字节都当作程序读取。若最终被拆成多个标量指令，验收按实际指令集合核对，不要求日志仍声称执行了一条 16 B 指令。

**合法反例、修复及报告。** `load16_req_align16(src)` 合法；尾部可用明确只读取 14 B 的操作，或在程序契约允许时将对象实际分配并初始化到 32 B。预期报告 global 越界读、原操作宽度 16、对象长度 30、首次非法偏移 30，以及执行线程。未覆盖的宽 opcode 必须标记部分覆盖，不能降成“未发现错误”。

<a id="case-s03"></a>

### 18.3 S03：范围正确但违背指令对齐约束

**前提与错误代码。** `bytes` 是按 16 B 对齐的 64 B global 对象。测试 ISA 明确规定 `load_u32_req_align4` 必须按 4 B 对齐；其读出的 4 B 均在分配内，因而本例单独考察 M2：

~~~cpp
__global__ void s03(const uint8_t *bytes, uint32_t *out) {
    if (threadIdx.x == 0)
        out[0] = load_u32_req_align4(bytes + 1);
        // 错误：地址低两位为 01，访问 [base+1,base+5) 本身没有越界
}
// s03<<<1,32>>>(bytes, out);
~~~

**哪里不对、怎样检。** 应验证的是该目标操作要求的对齐，即 `address % 4 == 0`，并另行验证范围。A0 在解释实际 opcode 时检查对齐；C 可对已明确解码的同类指令插入模运算／位检查。两者均须使用指令契约，不能因为源码类型为 `uint32_t` 就断言任何机器访问都必须对齐。B 的普通可访问性 shadow 和 D1 的 bounds 查询会接受这四字节，只有额外插入对齐条件才具有本例能力。[L05][N02]

**组件观测。** compiler 必须区分目标硬约束、IR 的 `align` 承诺和实际降级生成的操作；若向 IR 提供了错误的强对齐承诺，优化器可能先利用未定义行为，不能靠事后插桩恢复被删去的访问。deviceRT 输出要求值与实际余数；UMD 提供分配基址；cmodel 在指令生效前比对 ISA 规则，不能只看宿主机模拟读写是否成功。

**合法反例、修复及报告。** 改用 `bytes+4`，或使用目标明确允许非对齐的字节读取／拷贝路径，应通过。预期报告 `MISALIGNED_ADDRESS`、global、宽度 4、要求对齐 4、实际地址与 PC，而不是对象越界。若该 ISA 本来允许此操作非对齐，原错误前提就不成立，应换用确有对齐约束的 opcode。

<a id="case-s04"></a>

### 18.4 S04：从对象 A 越界，恰好落入仍存活的对象 B

**前提与错误代码。** 受控 allocator 建立两个独立、相邻且已初始化的 live 对象：A 为 `[0x1000,0x1040)`，B 为 `[0x1040,0x1080)`。记录并核对实际布局，不能假定两次普通分配必然相邻。`a` 的来源身份是 A：

~~~cpp
__global__ void s04(uint32_t *a, uint32_t *out) {
    if (threadIdx.x == 0)
        out[0] = a[18]; // 错误：A+72 = 0x1048，落在 live B 的偏移 8
}
// s04<<<1,32>>>(a, out);  // 读取宽度 4 B
~~~

**哪里不对、怎样检。** 目标四字节可访问，不代表从 A 推导的访问合法；应使用指针携带的 A 边界判断偏移 72。D1 可沿 `a` 的偏移计算传播 A 的 base／size，在访存处发现超出 64 B；A2 可独立跟踪参数、寄存器和指针运算的来源身份，再验证相同约束。两条路线都要求来源链没有被外部裸指针、整数往返或未受控内存槽破坏。只有分配表与实际地址的 plain A／C、以及只反映地址有效性的 B，都不能保证检出本例。[P01][N04]

**组件观测。** compiler 为 `a` 保留 A 的身份，不能在最终地址上反查到 B 后覆盖它；deviceRT 查询预期对象 A；UMD 发布 A／B 两份不同描述符和参数 sidecar；cmodel 同时记录“来源 A”和“地址当前落入 B”，用独立身份传播核对编译插桩结果。若 E 的 fallback 仅剩裸地址，应降级保证并计数。

**合法反例、修复及报告。** 若确实要读 B，应通过正确携带 B 身份的 `b[2]`；若要读 A，则索引应小于 16。预期报告 A 的越界读、偏移 72／宽度 4、A 的 64 B 边界，可补充实际落入 B。不能把“未发生硬件 fault”或“B 也存活”解释为通过；来源 unknown 时也不能伪造 A 级保证。

<a id="case-s05"></a>

### 18.5 S05：内存池子分配、granule 孔洞与 tensor view 边界

**前提与错误代码。** 一个 128 B pool 全部可寻址；同一 8 B granule 中，子对象 A 占偏移 `[0,3)`、B 占 `[5,8)`，中间是未分配孔洞。另在 `pool+32` 注册 float view：2 行、3 列、行步长 4 元素。下列两个分支分别运行，`view_store` 的示意实现暂未检查索引：

~~~cpp
uint8_t *a = register_suballocation(pool, 0, 3);
uint8_t *b = register_suballocation(pool, 5, 3);
View v = register_view(pool + 32, /*rows=*/2, /*cols=*/3,
                       /*row_stride=*/4, /*element_bytes=*/4);
__global__ void s05(uint8_t *a, View v, int which) {
    if (threadIdx.x != 0) return;
    if (which == 0) a[4] = 1;           // 错误：进入逻辑孔洞，仍在 pool 内
    else view_store(v, 0, 3, 1.0f);     // 错误：列 3；写到行间 padding
}
// 两次独立 launch：<<<1,32>>>，which 分别为 0、1。
~~~

**哪里不对、怎样检。** M4 需要子对象／view 契约。前一写超出 A 的 3 B；后一写位于 view 外包区间内，却不满足 `row<2 && col<3`。D1 扩展可为 A 收窄 bounds，为 view 保存形状、步长和索引检查；A2 可结合注册事件和访问的 view 身份作独立坐标判断。若降成裸指针且索引语义丢失，仅有外包范围不能还原任意 view 的逻辑约束。[P01][L05]

**组件观测。** compiler 保留子对象身份与行列操作；deviceRT 查询对应逻辑描述符；UMD／框架登记 parent、子范围及生存期；cmodel 以合法元素集合对照访问。B 可用更细位图表达特定孔洞，但经典 1:8 的单字节编码不能表示本例“有效三字节、无效两字节、有效三字节”。全局 poison 也不能区分同地址上的多个重叠 view 权限。[L02]

**合法反例、修复及报告。** `a[2]` 与 `view_store(v,0,2,...)` 应通过；合法的 pool 管理操作可持有 parent 范围，不能因逻辑孔洞被永久误禁。预期报告 parent／子对象 ID、错误偏移，或 `(row=0,col=3)` 与形状、步长。未登记子分配时只能报告 pool 级覆盖，不能将“地址在 128 B 内”当作 tensor 索引安全。

<a id="case-s06"></a>

### 18.6 S06：静态／动态 shared 越界与 block 身份

**前提与错误代码。** 静态数组 `s` 只有 8 个 32 位元素；动态 shared 的应用请求长度是 24 B。两个 kernel 分别启动两个 block、每个 32 个线程，并只由各 block 的 thread 0 写，排除本例中的数据竞争干扰：

~~~cpp
__global__ void s06_static() {
    __shared__ uint32_t s[8];
    if (threadIdx.x == 0) s[8] = 1; // 错误：静态对象偏移 32，写 4 B
}
__global__ void s06_dynamic() {
    extern __shared__ uint32_t d[];
    if (threadIdx.x == 0) d[6] = 2; // 错误：动态请求仅 24 B，偏移 24
}
s06_static<<<2,32>>>();
s06_dynamic<<<2,32,24>>>();
~~~

**哪里不对、怎样检。** 检查必须分别知道静态对象大小和本次 launch 的动态长度。A1 可使用 compiler 与访问关联的静态 shared 对象 map、UMD 的动态请求，按 `(launch,block,object)` 判范围；B 可为 static layout 加 redzone，并为动态区尾部 poison，但须同时更新实际资源占用和设备端布局。只检查整个 shared backing，可能漏掉静态对象跨入邻变量；此时需原对象关联／D1 来源，只有地址布局表仍不够，也不能拿额外预留空间扩充应用合法范围。[L05][L06]

**组件观测。** compiler 输出最终布局而非优化前偏移；deviceRT 为不同 block 选择独立 shadow／对象上下文；UMD 传递请求的 24 B 及 sanitizer 额外占用；cmodel 让两个 block 中相同数值偏移分别指向不同对象实例。不能让 block 0 的状态误用到 block 1，也不能把不同 block 的 `s[0]` 当作共享同一对象。

**合法反例、修复及报告。** 分别改为 `s[7]`、`d[5]`，两个 block 均应通过。错误报告须区分 static／dynamic shared、block ID、4 B 宽度与偏移 32／24；可以合并重复报告，但保留观察到的 block／错误计数。若错误策略在首个 block 后终止 launch，未执行 block 不计漏检。仅完成 global 插桩的 B／C 不具有本例覆盖。

<a id="case-s07"></a>

### 18.7 S07：Private／stack 越界与返回后使用指针

**前提与越界代码。** 第一段传入运行时索引 4；第二段的 `make_bad` 确实使用独立栈 frame 并返回。只对最终存在相应 private／stack 访存的产物验收；源码数组若被标量替换、函数被消除或悬垂路径改变，须另建目标夹具。

~~~cpp
__device__ __noinline__ uint32_t read_local(int i) {
    uint32_t a[4] = {10, 20, 30, 40};
    return a[i]; // 错误：调用时 i=4，读取对象偏移 16 的 4 B
}
__global__ void s07_oob(uint32_t *out, int i) {
    if (threadIdx.x == 0) out[0] = read_local(i);
}
// s07_oob<<<1,32>>>(out, 4);
~~~

**返回后指针的独立代码。** 返回地址本身不能延长局部对象生存期；错误访问发生在 caller 解引用时：

~~~cpp
__device__ __noinline__ uint32_t *make_bad() {
    uint32_t a[4] = {10, 20, 30, 40};
    return &a[0]; // 产生即将悬垂的指针；不保证真实编译器保留此表示
}
__global__ void s07_uar(uint32_t *out) {
    if (threadIdx.x == 0) {
        uint32_t *p = make_bad();
        out[0] = p[0]; // 错误：被指向 frame 已返回，读取 4 B
    }
}
// s07_uar<<<1,32>>>(out);
~~~

**哪里不对、怎样检。** 第一段属于 M3，D1 检查 `a` 的 16 B bounds；第二段属于 M5，D2 需保留指针预期的 thread／frame／epoch 并发现 frame 已结束。另一可行路线是 A1 的 frame 对象 map 加 A2 的 call／return 身份跟踪。B 的 stack redzone 可检查第一段；返回后检查还须实现延迟复用的 frame／fake-stack 等机制，普通栈地址重新变为有效后，单纯 poison 不再保证检出。[L01][L05]

**组件观测。** compiler 区分源级局部对象、spill 槽及真实 frame 生存期；deviceRT 按 logical thread 和 frame 管理状态；UMD 提供 launch／线程上下文，heap 分配表不能代替栈对象表；cmodel 独立观察 frame 进入、退出和指针逃逸。相同数值栈偏移属于不同线程时，必须保留不同身份。

**合法反例、修复及报告。** 索引改为 3；返回值改为标量，或写入 caller 持有且仍存活的输出数组。分别报告 private 越界读与 stack-use-after-return，列出对象范围、thread、frame 和返回位置。scratch 物理地址仍在某块合法 global backing 内，不是两例合法的证据。

<a id="case-s08"></a>

### 18.8 S08：Free 已生效、地址尚未复用的 UAF

**前提与错误代码。** 受控 allocator 分配 64 B，退休后保留底层映射并禁止复用到本例结束；`out` 事先分配。`wait_host` 确认该 queue 上的 free 已生效，故本例没有“free 尚在排队”的歧义：

~~~cpp
__global__ void read_first(uint32_t *old, uint32_t *out) {
    if (threadIdx.x == 0) out[0] = old[0]; // 错误：已退休对象上的 4 B 读取
}
uint32_t *p = test_alloc(64);
uint32_t *old = p;
fill_async(p, 64, stream);
free_async(p, stream);
Event retired = record_event(stream);
wait_host(retired);
require(test_is_dead_and_not_reused(p));
read_first<<<1,32,0,stream>>>(old, out);
~~~

**哪里不对、怎样检。** 地址可能仍映射，但对象生存期已结束。B 在退休命令真正生效后 poison，对旧范围读做检查；A0 可保留 dead 对象／tombstone，并根据完整命令顺序确认读取发生在退休之后。C 配合相同生命周期事件也可检查此窗口。这里尚无新对象占用该地址，因此“当前地址已 dead”足以判错；不能由此推导已具备 S09 的复用后时间安全。[L02]

**组件观测。** compiler 保留旧指针的实际 load；deviceRT 保证 poison 与后续访问具有正确可见顺序；UMD 提供 free 请求、完成事件及禁止复用条件；cmodel 依照 queue／event 顺序推进状态，不能把 Host API 的返回时间当成所有队列均已退休。测试启动前必须核对 dead 状态，不能靠一次 sleep 猜测。

**合法反例、修复及报告。** 同 queue 中先提交读取、再提交 free，读取仍应合法，即使 Host 很快看见 free API 返回。修复是把释放排在所有合法使用之后，并补齐跨 queue 的依赖；“再等待一次已完成的 free”不会使旧指针恢复有效。预期报告 UAF、对象 ID、4 B 读取、allocation／free／访问位置与次序。若释放时删掉全部元数据而映射仍在，只有映射检查的实现可能漏检。

<a id="case-s09"></a>

### 18.9 S09：同一 GPUVA 被新对象复用后的旧指针

**前提与错误代码。** `slot` 和 `out` 预先分配且不参与复用。先把 A 的指针存入设备 pointer slot，再退休 A，由受控 allocator 尝试让 B 复用同一个 64 B 槽位；必须核对实际 GPUVA 相等和 A dead／B live 两个生命周期事件。未观察到复用应标记夹具条件未满足，不得计为通过：

~~~cpp
__global__ void save_ptr(uint32_t **slot, uint32_t *p) {
    if (threadIdx.x == 0) *slot = p; // D2 还须保存 A 的旁路时间身份
}
__global__ void use_saved(uint32_t **slot, uint32_t *out) {
    if (threadIdx.x == 0) out[0] = (*slot)[0]; // 错误：解引用旧 A 指针
}
uint32_t *a = test_alloc(64);
GPUVA old_va = gpuva(a);
AllocationID old_id = test_identity_at_allocation(a); // 分配时保存，不能事后反查
save_ptr<<<1,32>>>(slot, a);
wait_all();
test_free_and_wait(a);
uint32_t *b = test_alloc_reusing(old_va, 64);
AllocationID new_id = test_identity_at_allocation(b);
require(gpuva(b) == old_va && new_id != old_id);
require(test_is_dead(old_id) && test_is_live(new_id));
fill_and_wait(b, 64);  // B 全部已初始化；仍保留 slot 中的旧 A 指针
use_saved<<<1,32>>>(slot, out);
~~~

**哪里不对、怎样检。** 访问范围仍是新 B 的合法前 4 B，但指针预期 A 的生存期已经结束。D2 要让 pointer store／reload 传递 `(A_ID,A_epoch)`，与当前对象身份比较；A2 可对寄存器、内存槽和 allocator 事件独立跟踪同样的时间来源。普通分配表查询只得到 B 当前 generation，缺少指针预期值；B 的 unpoison、D1 的同尺寸 bounds 和 plain A／C 因而均不保证识别旧指针。[P01][L02]

**组件观测。** compiler 保留 slot 的 pointer metadata 配对；deviceRT 避免载入时把旧指针重新标记为 B；UMD 发布新身份，并保持检查所需的旧描述符；cmodel 核对真实复用及保存／加载链。普通 `malloc` 是否复用不可假定；扩大 quarantine 只能推迟这一条件。

**合法反例、修复及报告。** 独立运行通过正确携带 B 身份的 `b[0]` 应通过；修复是清除旧引用并分发新指针及其身份。报告应同时给出相同 GPUVA、预期 A 和当前 B 的 ID／epoch、两次分配与 A 的释放位置。未知 writer、身份丢失、tag 碰撞或 epoch 回绕必须作为保证边界，不能静默退回地址查询后仍声称完整 M5。

<a id="case-s10"></a>

### 18.10 S10：Invalid free——内部指针与错误所有权

**前提与错误代码。** allocator 契约要求使用拥有者和原始基址释放。A 上下文拥有一个 64 B 对象；B 只导入其非拥有映射，导入句柄必须通过 `close_import` 关闭。以下两种误用分别在独立夹具中运行：

~~~cpp
uint8_t *p = owner_alloc(context_A, 64);
owner_free(context_A, p + 4); // 错误一：内部指针，不是 allocation base

// 独立运行第二例：重新创建 p，并建立非拥有导入映射 q。
uint8_t *q = import_nonowning(context_B, export_handle(p));
owner_free(context_B, q);     // 错误二：B 无权调用拥有者释放接口
~~~

**哪里不对、怎样检。** M6 需要验证原始基址、规范对象、allocator 类型、所有权和当前状态；“地址属于某个 live 对象”远远不够。A 可对 allocator／import 事件建立独立状态机，在模型重放中拒绝非法 free；B 或 E 的 runtime 拦截层可用同一类登记信息，在真正释放前报告并拒绝操作。后一能力来自 allocator 协议，单独给 load／store 加 shadow 并不能发现没有后续访存的错误。[L01][N02]

**组件观测。** compiler／loader 保留调用点；deviceRT 若支持设备堆，须把设备侧 free 接入相同事件；UMD 将导入别名归一到同一对象，但保留映射拥有权与资源拥有权的差别；cmodel 核对请求的 allocator、context、base 与合法关闭动作。API 和底层 driver 回调必须去重，不能把一次拒绝误记成一次成功释放。

**合法反例、修复及报告。** 内部指针可用于范围内访问，但释放时应传回 `p`；B 应 `close_import(q)`，最后由 A 在使用完成后释放 `p`。预期分别报告内部偏移 `+4` 和所有权／释放接口不匹配，附原 allocation site 与错误 API site。若 API 自身返回错误也应记录误用，不能要求一定发生设备 fault。未登记的自定义 allocator 或导入句柄只能标记覆盖缺口。

<a id="case-s11"></a>

### 18.11 S11：同一分配实例被释放两次

**前提与错误代码。** 分配 64 B，保存两个别名；第一次 free 成功且已经退休。到第二次调用前不允许新的分配或 GPUVA 复用，排除 S09 的时间身份歧义：

~~~cpp
uint8_t *p = test_alloc(64);
uint8_t *alias = p;
test_free_and_wait(p);       // 第一次合法释放，状态变为 dead
test_free_and_wait(alias);   // 错误：再次释放同一 allocation instance
~~~

**哪里不对、怎样检。** 第二次调用试图再次终止已经结束的同一对象生存期。A 的 allocation 事件账本可发现 `dead -> free` 非法转移；B／E 的 allocator 拦截层可保留释放记录或 tombstone，在调用底层释放前检查。两条路线都依赖成功／失败结果与实例身份；只记录“调用过 free”会把失败的首次请求也错误计入，只有 shadow poison 而未观察第二次 API 则不会产生必要诊断。[L01][N02]

**组件观测。** compiler／deviceRT 为设备侧释放保留调用点并可靠发布状态，Host 释放则由 runtime 拦截；UMD 区分请求 ID、成功提交及退休完成，对嵌套回调去重；cmodel 独立检查状态序列。并发 free 还需在对象状态上执行一致的仲裁，不能让两个请求都基于旧 live 状态通过后才写 dead。

**合法反例、修复及报告。** 使用单一明确拥有者，清除所有需要失效的别名；只把 `p` 赋空并不能修复仍存活的 `alias`。第一次释放请求失败且对象仍 live 时，之后一次合法成功释放不应被报 double free。预期报告对象 ID、64 B 大小、allocation site、首次成功释放与第二次调用位置。若同 GPUVA 已创建新对象，应允许新指针正常释放；要阻止旧指针误释放新对象则需要 S09 的 D2 身份，不能仅凭地址历史断言 double free。

<a id="case-s12"></a>

### 18.12 S12：在约定检查点仍未释放的显存对象

**前提与错误代码。** 一个独立检查 scope 进行三次 256 B 拥有型分配；每次 kernel 只在 thread 0 写该对象前 4 B，且等待完成。scope 约定结束时无应保留业务对象，只有 `k==1` 的分配没有释放，也没有把指针交给其他拥有者：

~~~cpp
begin_checked_scope(context);
for (int k = 0; k < 3; ++k) {
    uint8_t *p = owner_alloc(context, 256);
    touch_first_u32<<<1,32>>>(p);  // 合法写 [p,p+4)
    wait_all();
    if (k != 1) owner_free_and_wait(context, p);
    // 错误：k==1 时丢失最后一个业务引用，256 B 仍为 live。
}
report_leaks_before_context_teardown(context);
~~~

**哪里不对、怎样检。** 错误是拥有型对象在约定结束点仍未配对释放，不要求出现非法设备访存。A 可从完整 alloc／成功 free 事件重放中计算存活差集；B／E 的 allocator 账本可在 runtime 检查点扫描同样的未关闭实例。这两种方案不依赖 D2 的每次解引用，但必须明确统计策略：账本证明“仍登记为未释放”，一般不能仅由此证明任意业务中的对象已不可达。本例还通过 scope 契约与引用丢失给出泄漏前提。[N02][L01]

**组件观测。** compiler／loader 提供分配调用栈与模块定位；deviceRT 补充设备堆分配、失败结果及释放事件；UMD 等待相关命令完成，在 context 自动回收前取快照；cmodel 以独立实例 ID 对账，不因两次分配恰好同地址而合并。工具自己的 shadow、quarantine 和报告缓冲须与应用账本分开。

**合法反例、修复及报告。** 无条件释放每次 `p` 即可；若确实要跨 scope 保留，则把拥有权显式移交并按约定报告。合法 allocator 缓存、仍由调用者持有的对象及非拥有导入映射，不能一律报成应用泄漏。预期为 1 个、256 B 未释放拥有型对象，指出 `k==1` 对应分配位置与检查点。异常退出前未形成快照、漏接设备堆事件或只观察驱动大 pool，都会使该结论不完整。

<a id="case-s13"></a>

### 18.13 S13：未初始化读取、部分字节写入与copy传播

**前提与错误。** `src/dst`来自真实分配，初始定义性位均为0；两者8字节均可寻址且满足4字节读取对齐。所有命令在同一stream，排除顺序错误。`fixed=false/true`两版独立运行，不依赖错误后继续执行。`read32`实际消费偏移0的4字节并写入有效输出；它不能被优化掉。按byte复制含未定义字节的对象表示，本身不一概构成程序错误；copy应传播定义性，是否同时报告copy源未定义属于工具策略。错误断言落在随后消费未定义byte 3的读取。

~~~c
cudaMalloc(&src, 8);                      // I(src[0..7]) = 0
cudaMalloc(&dst, 8);                      // I(dst[0..7]) = 0
cudaMemsetAsync(src, 0, 3, s);            // 只定义src[0..2]
if (fixed) cudaMemsetAsync(src + 3, 0, 5, s); // 独立修复版补齐真实写入
cudaMemcpyAsync(dst, src, 8, D2D, s);     // 传播源定义性，不能把dst全标已定义
read32<<<1, 1, 0, s>>>(dst, out);         // BAD：fixed=false时消费未定义byte 3
cudaStreamSynchronize(s);
~~~

**实现路线与观测点。** A在allocation时置未定义位，按实际byte enable更新store/memset，copy按源位图传播；B额外建设独立初始化bitmap，由Compiler插入load/store检查，Device runtime原子合并共享metadata word。E接入runtime/UMD及copy engine的提交、完成、依赖事件，不能在Host提交copy时提前定义目的端。H2D若只采用“Host来源已定义”假设，须明确其覆盖边界；这属于第15章I1设计，不是普通地址ASan的附赠能力。[N03][L05]

**修复、报告与边界。** 独立运行`fixed=true`版本，完整有序memset后的读取应无报；另测仅补写byte 3后读取4字节合法、读取8字节仍非法。报告至少给出`dst+3`、读取宽度、未定义byte mask、源allocation和copy链。不得把“向内存store一个来源未定义的寄存器”当作值级定义性已正确实现；完整MSan传播另立能力。若copy engine不可见、只按word标记初始化、未跟踪Host来源或实际指令被消除，均需单列漏检边界。

<a id="case-s14"></a>

### 18.14 S14：shared RAW、WAR与WAW竞争

**前提与错误。** 一个block至少2个线程；`mode`为全block一致的测试选择。先由线程0写入常量，再由完整block barrier发布，保证后续读取有已定义的旧值。三组分别覆盖写→读、读→写、写→写冲突；箭头表示待观测的访问顺序，不构成跨线程HB，运行时RAW/WAR标签可随观测次序变化。

~~~c
__shared__ volatile int x;               // volatile只用于保留访问，不提供同步
if (tid == 0) x = 7;
__syncthreads();                         // 初始化已完成
if (mode == RAW) {
    if (tid == 0) x = 1;                 // BAD①：T0 W(x)
    if (tid == 1) out[1] = x;            // BAD①：T1 R(x)，无中间同步
} else if (mode == WAR) {
    if (tid == 0) out[0] = x;            // BAD②：T0 R(x)
    if (tid == 1) x = 2;                 // BAD②：T1 W(x)
} else { if (tid == 0) x = 1; if (tid == 1) x = 2; } // BAD③：W/W
__syncthreads();
if (tid == 0) out[2] = x;                // 末尾barrier不能倒推消除先前冲突
~~~

**实现路线与观测点。** A按`launch/block/shared对象/byte`记录两端PC、线程及barrier epoch，检查重叠字节、至少一写、原子属性和HB。E由Compiler或C补齐实际shared访存与barrier事件，Device runtime使用HB历史或适用同步子集内的层次FSM；状态更新原子不能为应用制造HB。[P02][P03] 以线程32替换线程1、block设64，可扩展为跨warp测试；不同block相同shared偏移必须分离身份。

**修复、报告与边界。** 将完整block同步放在两段访问之间；WAW可明确先后或令各线程写独立位置。同warp也须按目标规则建立有效同步。报告两个访问位置、R/W类型、字节交集及缺失的HB，不能报成未初始化读取。NVIDIA racecheck的shared范围可作对照，但不推出global覆盖。[N02] 重复读、随机延迟或碰撞方案可能漏同值写、ABA或未命中窗口，应与HB/FSM的适用模型保证分栏。[P08]

验收同时加入同一word内不同byte的独立并发写，确认检测依据是实际字节交集，而非仅因共用metadata单元就误报冲突。

<a id="case-s15"></a>

### 18.15 S15：global同block、跨block竞争与合法atomic

**前提与错误。** `x`是已置0并完成发布的有效global `int`；两次错误测试分别重置并等待，互不叠加。普通`x++`包含读和写，多个线程的访问没有HB，即使最终计数偶尔正确也不合法。跨block原子反例限定同一device和适用的memory synchronization domain。[N08]

~~~c
__global__ void bad_increment(int *x) {
    int old = *x;                        // BAD：另一线程可能并发写
    *x = old + 1;                        // BAD：R/W及W/W冲突
}
reset_to_zero_and_wait(x);
bad_increment<<<1, 2>>>(x);              // 独立用例①：同block
wait_reset_to_zero_and_wait(x);
bad_increment<<<2, 1>>>(x);              // 独立用例②：跨block
__global__ void good_increment(int *x) {
    cuda::atomic_ref<int, cuda::thread_scope_device> a(*x);
    a.fetch_add(1, cuda::memory_order_relaxed); // OK：只要求计数更新原子性
}
wait_reset_to_zero_and_wait(x);
good_increment<<<2, 1>>>(x);              // 独立合法反例：完成后计数应为2
~~~

**实现路线与观测点。** A使用global规范对象和全grid线程身份维护冲突历史，消费真实同步scope、ordering和barrier事件；block barrier只向同block参与者提供同步。C可在可解码指令处提取load/store/atomic宽度及scope，再经runtime汇总HB；E结合编译器语义、缺失模块补丁和UMD launch事件。两者都必须另建设Racecheck，B的可寻址位和D1/D2对象检查只能确认访问对象，不能判定竞争。[P02][P03]

**修复、报告与边界。** `good_increment`用相同适用scope的原子操作访问同一计数器，不应报race；用block-scope原子跨block通信仍不足。relaxed仅保护该计数器：若用flag发布普通payload，仍须建立合适scope的release/acquire关系，且acquire读到对应发布值。[N08] 报告两端PC/block/thread、对象内字节、atomic属性和未成立的HB。未建模锁、跨domain同步、历史溢出或未插桩模块应降级；概率重复访问未报错不能作无竞争证明。[P08]

另保留普通读与原子写并发的混合访问错误，避免检查器只见到一端为atomic，就把同址的全部访问放行。

<a id="case-s16"></a>

### 18.16 S16：跨kernel／queue竞争与event正确反例

**前提与错误。** `q0/q1`为不同的nonblocking stream，`x`已初始化并完成发布；`write_x`写`x[0]`，`read_x`读取相同4字节。排除legacy默认stream等隐式依赖。Host先提交writer再提交reader不建立跨queue顺序；最后一次全设备等待只等两者结束，不在二者之间补出HB。[N08]

~~~c
write_x<<<1, 1, 0, q0>>>(x, 7);
read_x<<<1, 1, 0, q1>>>(x, out);         // BAD：两次kernel访问无HB
cudaDeviceSynchronize();                 // 不追溯修复已发生的冲突
// 下方是独立合法反例，e已创建
write_x<<<1, 1, 0, q0>>>(x, 9);
cudaEventRecord(e, q0);                  // event覆盖writer完成
cudaStreamWaitEvent(q1, e, 0);            // wait必须位于reader之前
read_x<<<1, 1, 0, q1>>>(x, out);         // OK：writer -> event -> reader
cudaDeviceSynchronize();
~~~

**实现路线与观测点。** A同时模拟命令DAG与访存：将kernel内部线程时钟接到所属launch/queue节点，允许合法并行调度。E由Host runtime/UMD记录event版本、record/wait、launch与完成事件，再将B所承载的额外race插桩或C采集的地址历史接入HB引擎；不能在每个kernel结束时无条件清空global历史。只有Host API回调而没有设备访问事件或可信访问范围声明，无法确定两个kernel是否碰同一字节。[N03][P02]

**修复、报告与边界。** 上述event链及同一queue有序执行都是合法反例。设备内flag协议还须核对atomic scope与发布／获取，普通fence不能代替对端协议。报告应包含两个kernel、launch ID、queue、对象偏移及缺少的依赖边。驱动偶然串行、工具强制串行、默认stream配置改变或未执行目标路径，会影响实验覆盖；不能据此声称检查通过。基于随机延迟和值差异的发现仍是概率证据，须保留HB检测的独立口径。[P08]

event被重复record时必须按记录版本建边，不能把前一轮已完成的同名event错误套用到新一轮读写。

<a id="case-s17"></a>

### 18.17 S17：block barrier缺失live参与者与phase错配

**前提与错误。** 用例①启动64线程，`go`为已置0的block-scope原子，且只有等待barrier之后的线程0能置1；后半block持续存活并等待`go`，不会退出，因而缺失参与明确成立。用例②独立启动一个线程，`b`为正确分配的shared barrier；使用可保存整数token的低层primitive，避免混入C++移动后token的另一种误用。[N08]

~~~c
// 用例①：64线程；go在launch前已初始化为0
if (tid < 32) {
    __syncthreads();                     // BAD①：另一live warp永不参与
    if (tid == 0) go.store(1, relaxed);
} else { while (go.load(relaxed) == 0) {} }
// 用例②：独立的1线程block
__mbarrier_init(&b, 1);
auto t0 = __mbarrier_arrive(&b);          // 完成phase 0
while (!__mbarrier_test_wait(&b, t0)) {}
auto t1 = __mbarrier_arrive(&b);          // 完成phase 1，当前为phase 2
while (!__mbarrier_test_wait(&b, t1)) {}
__mbarrier_test_wait(&b, t0);             // BAD②：token早于立即前一phase
~~~

**实现路线与观测点。** A在同步指令执行处维护barrier身份、期望计数、live参与者、到达集合和完整phase；同时建等待依赖，区分循环等待证据与单纯超时。E由Compiler/C记录初始化、arrive/wait/drop、mask和退出事件，Device runtime维护同步状态，UMD提供block维度；F若能导出硬件barrier状态，可补充缺失参与证据，但仍需软件归因。

**修复、报告与边界。** 用例①把完整block barrier移到全员必经位置，或采用语义匹配的子组同步；用例②只使用当前或立即前一phase的有效token。报告barrier PC/ID、期望／实际参与集合、等待链或token/current phase。[N08] 不将普通barrier之前的任意return一概判错，退出线程必须按目标规范处理。NVIDIA当前synccheck的错误类、架构适用范围和barrier跟踪容量只是产品对照；自研“完整参与集合及phase验证”是目标能力。仅超时或缺失退出观测时，结论必须保留为疑似阻塞／覆盖不足。[N02]

若目标token仅编码phase parity，phase 0与phase 2可能具有相同值，纯token值或当前硬件状态不能保证识别这种ABA。A须额外保留token来源phase；E要保证旧token识别，须传播barrier身份和完整phase代数等sidecar。缺少该来源／epoch信息时，只能声明部分错配检测，不能宣称完整phase覆盖。

<a id="case-s18"></a>

### 18.18 S18：warp／subgroup无效mask

**前提与错误。** 以下限定32-lane CUDA warp、完整32线程启动、`mode`全warp一致，`go`为已置0的合法原子。mode 0中lane 16调用同步却未把自身写进mask；mode 1中低16个lane声明全warp，但其余lane保持live，等待只有同步后才会发生的写入。退出lane与仍live但缺席lane必须区分。[N08]

~~~c
unsigned lane = tid % 32;
if (mode == 0) {
    if (lane <= 16)
        __syncwarp(0x0000ffff);          // BAD①：lane 16的self bit为0
} else {
    if (lane < 16) {
        __syncwarp(0xffffffff);          // BAD②：named live lanes 16..31未到达
        if (lane == 0) go.store(1, relaxed);
    } else { while (go.load(relaxed) == 0) {} }
}
~~~

**实现路线与观测点。** A按动态warp、同步PC/实例记录每lane的mask、live/exit和到达状态：先检查self bit，再检查同组被点名且未退出的lane是否执行匹配调用。E由Compiler或C保存每lane原始mask与实际执行mask，Device runtime核对相交mask的一致性和等待关系；F只能在确有mask/到达状态观测接口时补充。不能把访存谓词mask直接当同步参与mask。

**修复、报告与边界。** 独立合法反例是仅lane 0–15调用`__syncwarp(0x0000ffff)`，其他lane不被点名；相互不重叠且满足目标规范的子组mask也不能误报。报告调用lane、self bit、声明mask、实际到达／已退出／仍待到达集合。CUDA要求被点名且尚未退出的参与线程以相同mask完成相应调用；具体收敛约束、wave宽度及退出语义须按目标ISA核验，不能照搬到其他GPU。[N08][L06] NVIDIA手册明确：synccheck可报self-mask错误，但不检测所有被点名线程未到达的情况；后者是自研目标，不可标成现产品保证。[N02]

合法mask应在参与者关系明确的程序点构造，不能把分支内瞬时执行mask直接当作算法预定的完整协作集合。

<a id="case-s19"></a>

### 18.19 S19：async copy缺wait、等待了错误批次或phase

**前提与错误。** 单线程block、支持所示pipeline primitive的目标，global源32字节已经完整初始化，源／shared目的满足16字节对齐。先把shared写为0，排除未初始化读取。mode 0在传输完成前读目的；mode 1等待的只保证较早批次，不能保护随后读取的最新批次。[N08]

~~~c
__shared__ alignas(16) unsigned char sm[32];
for (int i = 0; i < 32; ++i) sm[i] = 0;
__pipeline_memcpy_async(sm, src, 16);
__pipeline_commit();                     // batch 0
if (mode == 0) consume(sm[0]);            // BAD①：没有wait
else {
    __pipeline_memcpy_async(sm + 16, src + 16, 16);
    __pipeline_commit();                 // batch 1
    __pipeline_wait_prior(1);            // 仅保证batch 0完成
    consume(sm[16]);                     // BAD②：batch 1仍可能在途
}
__pipeline_wait_prior(0);                // 收尾等待不追溯修复过早读
~~~

**实现路线与观测点。** A把异步传输作为独立访问主体，追踪源读／目的写、commit组号、完成及消费者可见性；指令发出不等于目的已经可读。C拦截实际async opcode与wait，或E结合Compiler高层pipeline信息、runtime completion和Device checker；必须关联到同一个传输组／barrier phase，不能把任意wait当作全局完成。[N02][N03]

**修复、报告与边界。** 读取前`__pipeline_wait_prior(0)`保护本线程已提交批次；其他线程读取时，还需规定的参与同步与可见性。barrier版本要等绑定当前copy的phase：上一phase的token可能仍是合法参数，但成功等待它不证明当前传输已完成；比立即前一phase更旧的token则另属S17。报告copy发起PC、范围、组号／phase、等待实际覆盖范围及过早访问PC。预置0仍可发生传输竞争，不能只依赖I1位图。未观测copy engine、未支持bulk/tensor异步指令、仅观察一次幸运完成顺序均可能漏检；NVIDIA现有shared async-copy检查不能外推为全部引擎覆盖。[N02]

测试最终仍等待所有在途操作完成，以把过早读取与退出时资源生存期错误分开验证。

<a id="case-s20"></a>

### 18.20 S20：async free跨queue缺依赖与释放生效边界

**前提与错误。** 两个独立queue；`read_only`只读取有效、已初始化的分配，从而隔离生命周期竞争。allocation发布已通过`ready`正确排序，唯一缺失的是q1最后一次使用到q0释放的依赖。`fixed`是Host端测试选择，两种版本分开运行。

~~~c
cudaMallocAsync(&p, N, q0);
cudaMemsetAsync(p, 0, N, q0);
cudaEventRecord(ready, q0);
cudaStreamWaitEvent(q1, ready, 0);        // allocation和初始化均已发布
read_only<<<1, 32, 0, q1>>>(p, out);
if (fixed) {
    cudaEventRecord(done, q1);
    cudaStreamWaitEvent(q0, done, 0);     // 修复：最后使用先于free
}
cudaFreeAsync(p, q0);                    // BAD：fixed=false时无使用->free依赖
cudaDeviceSynchronize();
~~~

**实现路线与观测点。** A同时记录对象epoch、命令DAG、free请求、生效与完成；访问和free无序时报生命周期竞争，free生效后执行的访问报UAF。E由runtime/UMD将free命令及依赖下发，B按设备顺序poison；D2再传播旧指针身份，以覆盖随后同VA复用。free提交不能立即全局poison，同queue排在free之前的合法访问应放行。[N02][N03]

**修复、报告与边界。** `fixed=true`建立合法链；同queue中“使用→free”也是合法反例。CUDA规定free操作开始后不能继续使用；`retire complete`是确认退休、回收描述符及后续复用的证据，不应把非法访问边界拖到完成，更不能用Host API返回代替设备时刻。[N08] 报告对象/epoch、最后使用queue与launch、free提交点及缺失依赖；有完成证据时补报实际退休节点。仅监控API调用顺序会误报或漏报；B遇同VA新分配重新unpoison可能漏旧指针，D2仍需完整身份传播；隐藏free、外部queue或不可见依赖必须输出unknown。

内存池若插入真实设备依赖，须纳入命令图；工具为诊断插入的同步不得伪造应用原本有序。

<a id="case-s21"></a>

### 18.21 S21：原子指令访问越界——原子性不保证地址有效

**场景与例子。** 对应能力 M2/M3。`bins` 是已清零的 64 个 `uint32_t`，共 256 B；只有线程 0 执行下列原子操作，因此没有其他线程竞争，也没有未初始化读取干扰。为确定验收 B/C/A0，夹具保证 `[base+256,base+260)` 位于已 poison 的尾部 redzone 或不可访问范围；落入另一存活对象的变体另用于验收 D1／具有对象来源信息的检查。

~~~cpp
// Host：bins = alloc(64 * sizeof(uint32_t)); memset_zero(bins, 256);
kernel void bad_bin(uint32_t* bins) {
    if (thread_id() == 0) {
        atomic_add_u32(&bins[64], 1);  // 错误：有效下标只有 0..63
    }
}
~~~

**哪里不对、应检测什么。** 原子操作触及 `[base+256, base+260)`，不在分配范围内；“多个线程更新不会丢失”与“地址合法”是不同条件。检查必须覆盖 atomic RMW、CAS 及目标特殊原子，而不能只匹配普通 load/store。CAS 即使比较失败，也执行了读取；其范围仍须检查，是否产生写事件则需按实际结果处理。访问宽度、对齐和作用域依据目标指令定义。[L05][N09]

**实现方案与组件。** B 在 LLVM atomic 指令或目标 intrinsic 前执行完整范围检查，device runtime 查询 shadow；UMD 提供真实 allocation 范围。C/E 在解码后的原子指令前补检查，必须正确保存返回值、谓词和内存顺序。A 在模型的原子语义入口检查同一范围。D1 可进一步用原对象 bounds 识别原子地址落入另一存活对象的情况；硬件 fault/F 可提供补充，但页内越界不一定触发映射故障。所有路线均应保持应用原子的 scope/order，不得通过拆成非原子读写来插桩。

**修复、合法反例与预期报告。** 将下标限制在 `[0,64)`；多个线程合法执行 `atomic_add_u32(&bins[0],1)`，在该原子作用域覆盖这些线程且无混合非原子访问的前提下，不应报本例的空间错误或把同类原子冲突直接报成数据竞争。错误报告应为 `atomic out-of-bounds`，给出 4 B、偏移 256、对象大小 256 和 PC；仅报告“发生竞争”没有指出本例根因。

<a id="case-s22"></a>

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

<a id="case-s23"></a>

### 18.23 S23：只读对象与 image／surface——检查权限、坐标及寻址模式

**场景与例子。** 对应 M1/M2/M3/A1。两个子例分别针对只读权限和资源坐标；资源均已创建且仍存活，排除无效 handle 的干扰。

~~~cpp
// 子例 A：模块登记 coef 为设备只读对象。
addr = address_of_module_constant("coef");
store_u32(addr, 7);  // 错误：示意 ISA 写入只读对象，不是合法的 Host 初始化

// 子例 B：64×32、每像素4 B的可写 surface，边界模式为 TRAP。
surface_store_u32(surface, x_byte=64*4, y=0, value=7, mode=TRAP);
// 错误：最后一个有效像素的 x_byte 为 63*4；这里已超出行宽。
~~~

**哪里不对、应检测什么。** A 的地址可能已映射且在 allocation 内，但写权限不允许。B 不能只检查底层 pitch allocation：行尾 padding 即使在物理分配内，也未必是合法像素。检查应理解维度、格式、坐标单位、mip/layer 和边界模式。PTX 对 surface 的边界处理提供明确模式；目标 ISA 的合法 clamp/zero 等行为必须按其语义处理，不能把所有超出原始坐标范围的操作都报为非法访存。[N09]

**实现方案与组件。** A 按资源描述符和权限在模型入口检查。Compiler＋UMD＋device runtime 可提供资源类插桩：加载时登记只读符号，创建资源时登记格式、维度与权限，在 texture/surface intrinsic 前检查参数。C/E 若覆盖资源 opcode，可查询同一描述符；只实现线性地址 shadow 的 B 不能自动获得这项能力。D 可以为显式资源 view 保留逻辑边界；F 的权限或坐标 fault 可作为硬件信号，但报告工具仍需关联资源 ID 和源码。

**修复、合法反例与预期报告。** A 应写可写对象或使用允许的 Host 更新接口；B 改为合法像素坐标。另设采用规定 clamp 模式的越界采样作为合法反例：它可以返回边界像素，不应报告通用 OOB；如果应用要求更严格的逻辑坐标，需作为独立约束声明。预期报告分别为 `write-to-read-only` 与 `surface-coordinate-out-of-range`，包含资源格式、坐标、单位和模式。资源 opcode 不受支持时必须显示覆盖缺口。

<a id="case-s24"></a>

### 18.24 S24：CPU/GPU共享、peer／IPC别名的身份与同步

**前提与错误。** CPU/GPU分支只在已确认允许该内存类型被两端并发访问、且目标定义相应一致性的系统运行；不能因调用`cudaMallocManaged`就默认满足。Host-mapped内存须另核平台规则。peer/IPC分支使用另一块已合法导入、授予读写权限的device分配，不能把managed分配直接当作传统CUDA IPC可导出对象。[N08]

~~~c
cudaMallocManaged(&p, sizeof(int));
*p = 0;                                 // launch前真实初始化
write_x<<<1, 1, 0, s>>>(p, 1);
*p = 2;                                 // BAD①：CPU写与GPU写无HB
cudaStreamSynchronize(s);                // 将此等待移到CPU写之前可修复
// 独立用例②：a为可导出的device对象；导入/映射/授权过程均成功
handle = export_device_allocation(a);    // 伪API：导出共享对象
b = import_and_map_rw(handle, peer);     // a、b可有不同VA，规范对象相同
write_on_device(owner, a, q0, 3);
write_on_device(peer,  b, q1, 4);         // BAD②：同对象写写，无跨device依赖
~~~

**实现路线与观测点。** A须连同Host访问、peer设备、映射别名及命令同步建模；只模拟GPU访存不能检出CPU对端。E将CPU checker的访问/HB事件、GPU Compiler/C访存事件与UMD的map/import/alias/权限事件归并，以规范对象ID＋偏移匹配；导入产生映射身份，不凭空创建独立所有权。D2能保存身份但仍须另加HB引擎。[N03]

**修复、报告与边界。** CPU写前等待GPU完成；peer/IPC使用目标支持的跨设备／进程事件或完成协议，并遵守别名一致性规则。system-scope release/acquire只在该内存、地址宽度、对齐及互连支持相互原子性时成为合法反例，device-scope不能同步CPU；驻留、prefetch或映射成功都不建立HB。[N08] 报告CPU/两GPU端PC、device/context、两VA到同对象的关系及缺失同步。若peer未授权，应报权限/不可访问错误，不能以此代称数据race。不可见CPU访问、IPC另一进程、别名漏登记或概率观测窗口未命中均限制覆盖。

<a id="case-s25"></a>

### 18.25 S25：atomic pointer 的值与身份不一致——合法程序也可能被错误插桩误报

**场景与例子。** 这是 D2 的正确性验收场景，关联 M5。A、B 的初始化在并发执行前对双方可见，此后对象持续存活且数据不再修改；slot 的原子作用域覆盖发布者和读取者。发布者只更换一个原子指针，读取者读取被选对象的不可变数据。

~~~cpp
atomic_pointer slot = &A;
// 发布者：
slot.store(&B, release);
// 读取者：
p = slot.load(acquire);
consume(p->immutable_value);  // A、B都live，读到任一合法值都不应被误报

// 错误插桩示意：两次独立取值可能跨过一次发布。
v = atomic_load(slot);       // 取到旧值 &A
id = atomic_load_pointer_shadow(slot); // 却取到新身份 B；writer也独立原子写此字段
check_temporal(v, id);       // 工具错误：把不属于同一次发布的二者组合
~~~

**哪里不对、应检测什么。** 上半段不是应用缺陷；下半段是检查器的元数据一致性缺陷。同类不一致在地址复用场景中，还可能把旧裸指针附上新 allocation 身份，从而漏掉 UAF。分别原子读取 value 和 metadata 不保证它们来自同一次写入，单纯“再读一次指针值”也不能排除 ABA。这里要求的是检查器维护发布身份的一致协议，不能靠一次地址表查找重建历史。[L05] 该结论由原子接口语义与身份模型推导，属于工程设计要求。

**实现方案与组件。** 可选一：在目标支持合适宽度且 ABI 受控时原子发布 `{value, identity}`。可选二：所有 writer/reader 都遵守经过证明的版本化 side metadata 协议，携带发布序号、正确的内存顺序和失败重试策略。Compiler 负责完整识别 store/exchange/CAS 与跨函数传播，device runtime 实现协议；UMD 只提供 allocation epoch，不能代替指针发布身份。增加受控调度枚举的 A 扩展可对有界交错做协议反例验证，但普通地址模型本身不提供 D2 或调度穷举。C/E 只有在全部相关 writer 可纳入协议时才能给同等保证；SIMT 内自旋锁和无界重试还需要单独证明前进性。

**修复、合法反例与预期报告。** 修复检查器，原合法程序应零误报。另设“保存旧指针→释放A→同地址新对象”的受控变体：旧身份应报 M5，新发布身份应通过。未知 writer 或无法证明成对一致时应报告 `provenance-unavailable`／覆盖不足，不能伪造当前 epoch 后宣布时间安全。

<a id="case-s26"></a>

### 18.26 S26：未插桩库与 JIT——没有观测到访问不能当作检查通过

**场景与例子。** 对应 M1/M3 的覆盖边界和模块契约。应用能够重编译，第三方 kernel 暂时只有预编译代码；`p` 分配 64 个 `int32_t`，末尾有已登记的 poison 检查区域。

~~~cpp
// 应用模块启用编译插桩。
p = alloc(64 * sizeof(int32_t));
launch(prebuilt_library_kernel, p, 64);

// 库内真实执行的逻辑；该模块没有编译期插桩。
kernel void prebuilt_library_kernel(int32_t* p, int n) {
    if (thread_id() == 0) p[n] = 1;  // 错误：p[64]越界
}
~~~

**哪里不对、应检测什么。** 应用确实有一次非法访问，但只在调用方插桩，无法观察库内每次访存。没有报告只能说明检查器没有看到错误，不能说明库安全。模块 manifest 至少应声明检查器、地址空间、指令覆盖及 ABI 版本；JIT 产物同样必须登记。NVIDIA 的 hybrid 也有编译单元／module 内函数粒度限制，不能据其名字假定任意未插桩函数都会自动补齐。[N05]

**实现方案与组件。** 最直接是重编译库，使用与调用方兼容的 B/D 插桩。无法重编译时，C/E 在模块加载或 JIT 完成后进行受支持 opcode 的二进制插桩，共享 UMD 对象表及报告协议。A 可在模型支持全部库指令时独立观察实际访存。UMD/loader 必须检查覆盖声明并区分“成功加载”和“获得检查覆盖”；tooling 显示未覆盖模块。binary fallback 能检查地址不意味着能恢复已丢失的原对象／历史指针身份，D2 降级需单列。

**修复、合法反例与预期报告。** 库中改用 `p[n-1]` 是合法反例。对可插桩的错误版，应报告库内 PC、模块名和 4 B 越界；对暂不支持的二进制，应明确 `unsupported-module` 或拒绝该检查模式，不能输出无条件 clean。验收同时运行“已插桩→未插桩→已插桩”的指针返回链，确认 unknown 身份不会被包装成强保证。

<a id="case-s27"></a>

### 18.27 S27：graph replay捕获已释放对象与执行ID／allocation epoch

**前提与错误。** `write_x`只写`p[0]`，避免引入初始化问题；p是graph外分配，capture只保存访问它的kernel节点，不含allocation节点。第一次replay完成后释放p，再重放原graph：节点仍持有旧指针。地址复用变体由测试allocator确保q取得同VA；普通malloc恰巧复用不能作为确定触发前提。

~~~c
cudaMalloc(&p, N);
cudaStreamBeginCapture(s, cudaStreamCaptureModeGlobal);
write_x<<<1, 1, 0, s>>>(p, 1);            // capture保存p的参数与原身份
cudaStreamEndCapture(s, &graph);
cudaGraphInstantiate(&exec, graph, 0);
cudaGraphLaunch(exec, s);                 // replay 1：p仍live，OK
cudaStreamSynchronize(s);
cudaFree(p);
cudaMalloc(&q, N);                       // 可选变体：专用夹具确保q与旧p同VA
cudaGraphLaunch(exec, s);                 // BAD：replay 2仍访问旧p
cudaStreamSynchronize(s);
~~~

**实现路线与观测点。** A记录capture节点、每次执行实例、对象创建/释放和节点实际访存；模型若只有VA表也会漏同VA复用，因此必须保留被捕获参数的原身份。E＋D2由runtime为节点参数建立identity sidecar，replay生成新的execution/launch ID，Compiler传播对象ID/epoch至使用点；不能在每次launch按当前VA表重新“认领”新对象。C可补充设备访问，但裸地址补丁无法自行恢复丢失来源。[N03]

**修复、报告与边界。** 让p存活至所有replay完成，或在受支持更新入口同时替换节点指针和身份。连续合法replay应具有不同执行ID、相同仍live对象epoch；graph内alloc/free节点每次建立新的分配生存期时，才生成相应新allocation epoch，不能机械地按replay全量换epoch。[N08] 报告graph/exec/replay/node ID、旧对象与free位置、若复用则给当前对象身份。B在地址仍poison时可报UAF，复用unpoison后可能漏；未跟踪节点参数更新、间接指针或device-side graph launch时须降级，不能假定每个设备发起replay都有Host回调。[N03]

<a id="case-s28"></a>

### 18.28 S28：日志满、metadata 不足与错误终止——检查器必须说明检查是否完成

**场景与例子。** 这是工具可靠性场景，而非新增的应用错误类别；它决定所有能力的结果是否可信。p 指向恰好 n 个 `int32_t` 的有效分配，n≥1，尾部已 poison；block 至少两个线程，全部属于 barrier 的参与集合。下例只有线程 0 触发越界，其余线程会执行 barrier；本例选择固定到达计数且不会自动退订退出线程的目标同步原语。若目标会按规定调整参与集合，必须另按其规则验收，不能把所有提前 return 一概视为非法。

~~~cpp
kernel void error_then_barrier(int32_t* p, int n) {
    if (thread_id() == 0) p[n] = 1;  // 原始应用错误
    counted_barrier(expected_arrivals=block_size());
    // 如果检查器只让线程0 return，其余线程可能永远等不到正确参与集合。
}
// 日志故障注入：容量=1且预填未消费记录，再触发本例；或直接注入“已满”。
// 另用至少两个独立诊断事件核对丢失计数。
// shadow分配失败、clock耗尽分别独立运行，不要求同时触发。
~~~

**哪里不对、应检测什么。** sanitizer 发现错误后的动作也是工具语义的一部分。只退出一个参与线程、静默丢掉唯一错误或把 metadata 分配失败当作“所有地址可访问”，都会使结果失真；单纯等待超时也无法说明是应用 barrier 误用还是工具引入死锁。

**实现方案与组件。** Compiler/device runtime 设置统一错误状态，采用目标可保证前进性的报告协议；UMD 与已有 KMD/固件取消能力配合执行受控 dispatch/context abort，并保留完成状态。没有安全恢复但有可靠 abort 时，终止受影响执行；连可靠终止能力也缺失时，应将依赖该能力的模式标为不支持，并在启动前拒绝相应配置，不能承诺任意 lane 局部恢复。A 可注入队列、缓冲和生命周期故障校验状态机；C/E 的补充路径也必须遵守同一错误处理 ABI。报告可聚合重复事件，但应保留“已经检测到错误”的持久标志及记录丢失计数；shadow/clock 不足则标记检查不完整，按配置拒绝启动或安全终止。具体错误动作需要结合目标能力设计。[N02]

**修复、合法反例与预期报告。** 修复应用下标后，在工具资源充足时应正常完成；保持应用合法但注入 metadata 失败时，应报告 `tool-resource-exhausted`，不能伪装成应用 OOB。错误程序即使日志满，也应保留失败状态并报告记录丢失；触发 abort 后确认其他任务的影响范围符合声明。验收分别检查资源耗尽、日志丢失和 barrier 终止，不能只用“进程退出了”作为通过证据。



<a id="scenario-platforms"></a>

### 18.29 场景与三组件、双平台的对应关系

本表是对S01～S28的【工程建议】补充。各例原有compiler/deviceRT职责归入编译器交付包，Host runtime/UMD/tooling职责归入UMD交付包；KMD职责单独列出。表中“模型能检”均以相应检查器和输入已经实现为前提，不表示裸cmodel自带这些功能。Q2使用第7.7节定义的可执行目标ISA的GPU模型；Q0/Q1不能替代它。Q3是否检出取决于直通真机上的实际检查器，不增加模型级可见性。

| 场景 | 功能级cmodel需要什么 | Q2新增系统验证／修改 | KMD应提供什么，不能替代什么 |
|---|---|---|---|
| [S01](#case-s01) 头／尾越界 | 实际访问范围、逻辑分配长度和无效区；或执行B检查 | Guest分配与GPUVA/shadow映射接通，错误回传 | 有效映射与权限；页／BO边界不能代替逻辑对象长度 |
| [S02](#case-s02) 宽访问 | 全部有效字节和跨granule判断 | DMA／地址翻译不可截短目标访问，保留GPU原始宽度 | 映射与fault访问范围；不只依赖一次页命中 |
| [S03](#case-s03) 对齐 | opcode自身要求和真实地址；IR承诺另查 | GPU模型按ISA处理，不能以Host可读写为正确标准 | 转交已发生的对齐fault及上下文；若硬件不报则依赖检查器 |
| [S04](#case-s04) A进入live B | D1产物或独立A2来源跟踪 | 传递原对象sidecar，不能按Guest/GPU地址查当前BO代替来源 | BO/GPUVA映射；通常不知道该指针原本来自A |
| [S05](#case-s05) 子分配／view | 子对象和stride语义，或执行相应D检查 | Guest框架/UMD注册子分配，区分父BO和子生命周期 | 支撑父映射；同一BO中的tensor边界由上层登记 |
| [S06](#case-s06) Shared | launch/block/对象布局与实际访存 | 接通launch参数和GPU内部shared模型 | 调度资源配置；无法从外部DMA自动观察每个shared访问 |
| [S07](#case-s07) Private/stack | thread/frame/对象身份及返回事件 | 执行插桩frame布局和scratch配置，核验上下文隔离 | backing映射及资源管理；scratch BO有效不证明栈对象有效 |
| [S08](#case-s08) 未复用UAF | free生效事件与poison状态 | Guest队列顺序驱动metadata失效，再验证回传 | 完成／故障与映射生命周期；不以Host调用时间判失效 |
| [S09](#case-s09) 复用UAF | 保存旧identity并与新epoch比较 | 验证Guest内存池复用、sidecar与映射重用相互独立 | 当前BO句柄或GPUVA不能重建旧指针身份 |
| [S10](#case-s10) Invalid free | allocator/owner及基址契约输入 | 运行真实Guest用户态释放路径，观察ioctl是否应发生 | 校验内核句柄和权限；逻辑子分配错误可能在UMD已被拦截 |
| [S11](#case-s11) Double free | 同一对象生命周期和释放事件去重 | 两次Guest释放与KMD资源引用、错误返回一致 | 内核资源引用计数与拒绝非法句柄；不混同不同allocator的逻辑ID |
| [S12](#case-s12) Leak | 明确检查点和逻辑活跃账本 | 在约定检查点、退出／context自动回收前保存UMD逻辑账本；退出／销毁后另核对内核资源清理 | 实际BO/context清理；OS自动回收不抹去程序检查点的逻辑泄漏 |
| [S13](#case-s13) Init | byte定义性、真实store及copy来源传播 | copy engine与Guest命令完成驱动位图更新 | DMA/copy完成和失败；不凭分配成功认定内容已定义 |
| [S14](#case-s14) Shared race | byte冲突、线程身份、barrier HB或适用FSM | 保持GPU同步模型，核对launch/resource配置 | 不提供GPU内部逐线程HB；驱动job按序也不消除kernel内部race |
| [S15](#case-s15) Global race | atomic宽度、scope/order及冲突历史 | GPU全grid语义与Guest提交一致，禁止检查器隐式串行化 | context/映射与外部同步；不能用每次DMA发生先后代替语言HB |
| [S16](#case-s16) 跨queue race | 多queue/event图和GPU访问历史 | 验证Guest UMD到KMD提交依赖，记录执行与完成 | 真实依赖与完成fence；不得把Host提交先后补成依赖 |
| [S17](#case-s17) Barrier/phase | live参与集合、完整phase来源与等待依赖 | 模型违规→IRQ/故障→Guest终止链；注入与真实检测分开 | 有效终止／reset和错误通知；超时不等于证明phase错误 |
| [S18](#case-s18) Subgroup mask | 动态lane/mask/exit及目标匹配规则 | GPU模型保留参与集合，Guest回传原PC与mask | 故障传播；看不到mask时不能自行补造诊断 |
| [S19](#case-s19) Async copy | copy group、完成、barrier phase与消费事件 | GPU内部异步引擎与系统DMA事件分别接入 | 外部copy引擎完成只说明该引擎，不代替kernel内部wait语义 |
| [S20](#case-s20) Async free | allocation/free生效和访问依赖 | 跑真实Guest队列、等待及内存池复用，核对开始／回收边界 | 提交完成、回收与错误证据；不擅自将free提交等同于全部访问失效 |
| [S21](#case-s21) Atomic越界 | 原子实际足迹、CAS成功/失败语义 | GPU模型保持原子性；shadow/descriptor和权限可达 | 原子故障及映射；不能用原子指令本身证明地址正确 |
| [S22](#case-s22) Mask/EVL | 只观察实际启用lane/元素 | 检查与GPU访存mask一致，避免QEMU桥接层强行全向量访问 | 无需逐元素新接口；通常不拥有向量EVL语义 |
| [S23](#case-s23) 权限／资源 | 资源格式、维度、坐标和边界模式 | Guest资源描述符/权限传入GPU模型，复现合法clamp | 映射权限与资源授权；padding和合法坐标需上层/ISA信息 |
| [S24](#case-s24) CPU/GPU别名 | 规范对象、CPU事件和系统同步模型 | 关联Guest CPU、GPU与外部映射；KVM下另有CPU观测方案 | 共享BO、导入映射、权限及外部同步；不能单凭同址推断所有权 |
| [S25](#case-s25) Atomic pointer | 指针值/身份配对协议；独立oracle需A2 | 同时验证Guest库边界与GPU原子协议；完整保存发布序号 | 只提供分配生命周期和映射，不能替代原子发布身份 |
| [S26](#case-s26) 库/JIT | 支持实际生成ISA；覆盖清单与原PC映射 | Guest loader执行compile/patch选择，Guest KMD合法加载 | 正规代码加载、可执行映射与资源校验；不默认允许任意patch |
| [S27](#case-s27) Graph replay | 执行ID、对象epoch及graph依赖 | Guest捕获/重放与设备侧launch接通，不能只盯Host回调 | 提交和资源生存期；每轮launch不自动产生新allocation身份 |
| [S28](#case-s28) 工具失败／终止 | 故障注入、错误状态持久化和模型停止 | 验证真实Guest报告、fence/IRQ、abort/reset、资源清理 | 可靠的终止范围和完成语义；只有queued-job cancel不够 |

**测试结果必须说明检查发生的位置。** 在GPU模型hook检出S01是路线A的证据；在同一模型上运行B插桩产物并由GPU检查库检出，是B的功能执行证据。若二者共享同一检查逻辑，不能将两次成功算作独立交叉验证。Q2的KMD测试还必须实际走Guest KMD的对应路径；在设备模型直接注入一个完成码，只验证该码之后的路径。

---

<a id="validation"></a>

## 19. 验证基准与验收标准

### 19.1 分四层验证

| 层 | 内容 | 检查重点 |
|---|---|---|
| V0：编译语义 | LLVM IR、MIR、反汇编和覆盖清单 | 检查没有被消除，新增目标访存可见 |
| V1：最小错误与合法反例 | 每个能力一个确定触发样例及无错对应样例 | 误报／漏报、边界和整数溢出 |
| V2：cmodel／QEMU／真机对照 | 相同语义用例，分别记录GPU执行与Guest驱动证据 | 区分共享模型和独立oracle，核对真实KMD路径 |
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

误报和漏报调查应先缩小输入／opcode，再使用 cmodel、反汇编和时间线定位。不能仅因为“加同步后错误消失”就认定原检测器正确。

### 19.7 功能级cmodel、QEMU全系统与真机的分层验收

【工程建议】使用相同S01～S28输入与规范对象定义，分开保留以下证据。每项记录所用路线、checker和支持子集，不以环境名称代替检测能力。

| 执行路径 | 可以验收的主要结果 | 必须保留的证据 | 不可据此宣称 |
|---|---|---|---|
| 独立功能级cmodel | GPU指令语义、模型侧A检查或B～F产物执行；小规模可控反例 | ISA/model版本、对象与命令输入、实际访存/同步轨迹、原始PC、oracle来源 | 真正运行了KMD；涵盖全部调度；真机性能已经达标 |
| Q2全系统GPU模型 | Guest应用→UMD→KMD→设备模型→GPU执行→故障/报告的系统闭环 | Guest内核/KMD/UMD版本、QEMU及GPU模型版本、CPU accelerator、映射/队列/IRQ/fence关联 | 默认拥有硬件缓存/弱内存/精确时序；模型成功等同真机成功 |
| 真机原生或Q3直通 | 实际硬件的访问检查、驱动回传、资源和性能表现 | 硬件/固件版本、Host/Guest驱动与VFIO配置、真实指令和错误记录、时间与资源原始数据 | Q3获得GPU模型内部hook；一次运行证明所有输入无错 |

QEMU与独立cmodel复用相同GPU模型时，系统接入路径得到更多验证，但GPU语义oracle并未因此独立。应保留受控IR/MIR/ISA夹具、独立小规模参考实现和真机对照，不把同一模型的两种启动方式当成两个相互独立的真值来源。

建议为现有错误协议增加 `execution_backend`（`functional-model`、`qemu-emulated-gpu`、`qemu-vfio`、`hardware-native`）、`cpu_accelerator`、Guest/Host KMD版本、GPU model build ID、上下文/地址域、对象/BO关联、命令与完成ID，以及“真实检测／故障注入／预期拒绝”标记。地址不应只记录一个无上下文的整数。

**KMD专项验收至少覆盖四组正反例。** 第一，metadata/报告缓冲映射成功与映射失败，检查器失败不能回退成clean；第二，队列依赖正确与缺失，完成通知不能早于实际完成；第三，真实运行中的错误及abort/reset，区分未执行job取消、context终止与整设备重置的影响范围；第四，导入/alias/context销毁，确认对象生命周期、引用清理和错误回传不重复。Q1可用于这些协议的局部故障注入，但不替代Q2或真机产生GPU内部违规的证据。

**可复现与性能的口径。** 若选择QEMU record/replay，目标GPU设备模型的异步事件、内部状态和外部输入也必须纳入可重放契约；保存Guest RAM并不自动保存模型的pointer identity、race历史或挂起copy。若暂未实现完整快照，就显式禁用该能力或从干净状态重跑。QEMU icount不是微架构周期模型，模型墙钟时间也不是目标GPU slowdown；真机性能仍按第17章的统一基线测量。[Q04][Q05]

<a id="roadmap"></a>

## 20. 分阶段研发计划和团队安排

### 20.1 推荐交付阶段

下表时间是有经验团队、已有硬件／基础栈条件下的规划区间，表示主动开发日历跨度，不包含等待硬件、长周期平台故障或所有阶段的简单串行总和。

| 阶段 | 范围 | 建议投入 | 预计跨度 | 退出证据 |
|---|---|---|---|---|
| M0 能力摸底 | 三组件契约、GPUVA/ISA/abort、allocator、cmodel及Q0～Q3现状 | 2–3人 | 2–3周 | 接口清单、实际限制、预算修订 |
| M1 窄原型 | 最小P＋B global＋A小用例oracle | 3–4人 | 6–10周 | 真机报错、确认B的地址reuse边界、首轮性能 |
| M2 内存检查内测版 | P＋B完善、KMD契约回归、shared/private、API、诊断与覆盖 | 5–7人 | 再10–16周 | 支持矩阵闭合、应用样例、资源可控 |
| M3 对象语义 | D1先行，再做受控D2与pool/view | 5–8人 | 16–28周 | 相邻对象／旧指针／atomic边界通过 |
| M4 独立checker | init、sync、shared race逐项推进 | 3–6人/工作流 | 每项8–16周量级 | 各自覆盖、误报和性能报告 |
| M5 生态补充 | E中binary fallback与JIT/外部库 | 4–6人 | 10–18周 | 混合边界无静默漏检 |
| M6 硬件研究 | profile＋cmodel／时序模型＋ISA/DV方案 | 跨架构团队 | 单独排期 | 与软件基线的同口径收益 |

M1 是有意缩小的原型，不能把 M1 的周期或代码量当成第16章完整 P＋B 的承诺。M3和M4部分工作可并行，但公共元数据和ABI需要稳定版本。若Q2尚未具备，先独立完成第16章的平台基础包；上表原有日历区间没有隐含包含从零建设QEMU GPU全系统平台的工期。

### 20.2 团队职责建议

| 角色 | 核心责任 |
|---|---|
| LLVM负责人 | 插桩时机、优化交互、目标指令覆盖、provenance |
| 编译器包内设备检查库负责人 | shadow／对象表、SIMT安全、错误通道和性能 |
| KMD负责人 | BO/GPUVA、提交完成、fault、abort/reset、上下文与内核接口回归 |
| UMD/runtime负责人 | 逻辑生命周期、queue/graph、模块、外部资源及用户态报告 |
| 功能级cmodel/架构负责人 | GPU ISA语义、独立oracle、调度和硬件检查功能模型 |
| QEMU全系统平台负责人 | GPU设备前端、地址/DMA/IRQ、Guest UMD/KMD接入、故障注入及状态管理 |
| 测试与性能负责人 | 反例、真实workload、可复现数据和回归 |
| 工具链集成人员 | CLI、符号化、安装、CI与用户文档 |

人数少时可以兼任，但不宜让一个人同时独自实现检查器并独自定义全部正确性 oracle。D2、异步生命周期和错误终止都值得交叉评审。

### 20.3 每个阶段必须留下什么

每阶段交付包括版本化文档、源码、测试清单、运行命令、输入hash、原始性能数据、已知限制及复现报告。公共 ABI 和 module capability manifest 的变更应记录兼容策略。

由于本任务已有明确的 GitHub 持续保存要求，建议在仓库以正文、证据笔记和进度记录共同维护；发生中断时从已提交的内容接续，不依赖聊天中某段未保存输出。

## 21. 主要风险与决策条件

| 风险 | 后果 | 对策与决策条件 |
|---|---|---|
| 指针身份在ABI或atomic边界丢失 | 时间错误漏检或误报 | D1/D2分级；unknown显式计数；不闭合就不宣称强保证 |
| GPUVA无法容纳线性shadow | 工具启动或运行失败 | M0原型确认；切换稀疏／对象内shadow |
| UMD按Host时间更新生存期 | 合法访问误报、真实race漏检 | 以queue/event和实际生效顺序为准 |
| 插桩导致SIMT死锁 | 工具把应用挂住 | 无锁／有限等待、目标前进性测试、统一abort |
| metadata和日志OOM | 检查中途失效 | 预算、显式降级、CI失败分类 |
| 二进制重写覆盖特殊指令不全 | 假clean或错误结果 | opcode清单、拒绝未知指令、差分执行 |
| 性能优化偷偷降低覆盖 | 看似加速但工具退化 | 性能与覆盖同时验收，采样模式单列 |
| 模型与真机内存语义不一致 | oracle不可信 | 目标litmus校验、事件差分、保留分歧 |
| QEMU设备桩被误作完整GPU模型 | kernel未执行却报告通过 | Q0～Q3能力声明、GPU实际访存与checker位置证据 |
| KMD只取消未运行job，却承诺运行中abort | 错误后挂死或扩大reset影响 | 分别验证queued cancel、dispatch/context abort和device reset [K01][K02] |
| Guest地址、GPUVA与Host指针混用 | 错映射、误报、模型越界 | 显式地址域、上下文和转换链，使用声明的QEMU内存接口 [Q03] |
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
8. 功能级cmodel已有的ISA/地址空间/atomic/async/sync覆盖、是否仅顺序调度；是否能够运行设备检查库，以及独立oracle的hook和身份输入。
9. 真正要支持的语言／框架与闭源模块比例。
10. 第一版是开发者工具、CI工具，还是更强的运行时防护需求。
11. 必须支持的地址空间和错误类型，特别是对象／子对象与复用UAF。
12. **QEMU已经属于Q2（用户确认可执行目标GPU ISA）**；需进一步核查现有GPU执行器的源码入口、CPU accelerator、设备前端、GPU MMU/VRAM/DMA/IRQ、Guest驱动、访存前hook、快照/重放和故障注入现状。
13. Q2 GPU ISA解释器是否与独立功能级cmodel共用代码；如果不共用，它们的ISA语义差分接口如何设计；GPUVA/Guest/Host地址域转换及Guest/Host KMD边界；Q3的VFIO能力单独核查。
14. 可投入团队、可用真机、允许的显存和性能预算；明确三组件、模型、QEMU平台及硬件项目各自负责人。

回答这些参数后，应优先修订第16章的估算和第20章排期，再进入具体实现设计。本轮结论已经足以决定首个原型的方向：公共P＋真机B＋独立A校验，以及明确预留D的对象身份协议。

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

**[K04] The Linux Kernel Documentation. VFIO — “Virtual Function I/O”.** [官方文档](https://docs.kernel.org/driver-api/vfio.html)。已读IOMMU group／device API、DMA map示例及IOMMUFD章节；支持设备地址域与Host映射参数的区分。本文不据此宣称QEMU自动提供GPU ISA检查或对象级sanitizer。

下列QEMU官方在线页面访问日期为2026-10-20；本次读取的master文档标示版本11.1.50。master页面会继续更新，工程落地应固定到实际采用的QEMU版本；这不是用户平台的安装版本。

**[Q01] QEMU. Introduction.** [官方文档](https://www.qemu.org/docs/master/system/introduction.html)。本次核验：系统仿真的CPU／memory／device构成、TCG与KVM等accelerator、设备模型及直通边界。

**[Q02] QEMU. QEMU TCG Plugins.** [官方文档](https://www.qemu.org/docs/master/devel/tcg-plugins.html)。本次核验：翻译／执行回调及vCPU上下文；成功load/store后触发memory callback，faulting access不触发该回调。不能据此假设自定义GPU内部执行自动可见。

**[Q03] QEMU. The memory API.** [官方文档](https://www.qemu.org/docs/master/devel/memory.html)。本次核验：MemoryRegion、AddressSpace、MMIO、RAM backing、IOMMU及address_space_rw；为设备DMA与地址域接入提供接口参照。

**[Q04] QEMU. TCG Instruction Counting.** [官方文档](https://www.qemu.org/docs/master/devel/tcg-icount.html)。本次核验：指令计数与虚拟时间；明确不是cycle-accurate，限定system emulation，且与multi-threaded TCG不兼容。

**[Q05] QEMU. Execution Record/Replay.** [官方文档](https://www.qemu.org/docs/master/devel/replay.html)。本次核验：icount依赖、非确定事件、虚拟设备确定性、异步bottom-half接入及状态保存／恢复要求；不证明外接GPU模型已自动满足这些要求。

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
| QEMU平台工程师 | Q01～Q05、K04、本文第7.7/8.8/19.7节 | Q0～Q3能力、Guest接入、地址域和GPU模型边界 |
| cmodel/架构工程师 | L06、N08/N09、P02/P03、P04、本文第18章 | 模型事件、同步语义、硬件前提 |
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

1.2版补充核验QEMU官方system/TCG plugin/Memory API/icount/replay文档与Linux kernel的GPU内存、共享缓冲、同步和reset契约。采用直接打开、页面定位及官方站内链接；新预算为工程假设，没有把公开文档当成用户KMD或GPU模型的能力证明。QEMU master文档版本与访问日期见Q01～Q05。

### 23.5 核验中保留的分歧与限制

- NVIDIA产品总览与新版initcheck章节存在更新不同步，正文采用新版手册和release notes。
- HiRace正式稿的一处百分比表述与Table I使用的分母不一致，本文没有引用“93%”作为positive recall。
- cuCatch和GPUArmor的“BST”含义不同，正文已明确；两篇的性能成本也不等于包含所有Host wrapper的应用端到端时间。
- GPUArmor作者页面、期刊online日期和文稿模板页眉不同，参考文献采用期刊元数据，性能机制采用已读作者全文。
- 对MUSA内部插桩机制，当前公开证据不足以确认用户参考描述的具体架构；正文保留可证实的功能，不补写内部实现。
- cmodel、真机和论文模拟器承担不同证据角色；本文未运行用户硬件，也未声称完成论文复现。
- 第16章按三组件重组并保留子项，KMD条件包和QEMU平台包分列；基础参考包与1.1总量可追溯，新增范围未伪装成原预算已包含。实际预算仍需用户仓库和接口支持。
- Linux DRM/VFIO与QEMU文档给出公开机制参照，不证明用户使用相同内核子系统或已经完成GPU模型。功能语义、Guest系统路径和真机时序的证据必须分开。

### 23.6 文档完成标准与后续入口

本版已详细说明项目背景和建设目标，覆盖友商、机制、量化结果边界、六条实现路线、编译器（含设备检查库）／KMD／UMD（含用户态工具）的改动，以及功能级cmodel／QEMU全系统平台的支持和修改条件、代码量、内存性能、验收和阶段计划。第18章的28个场景分别给出触发前提、具体操作或冲突事件、实现选项、组件信息、合法对照及报告边界，并与第19章测试矩阵关联。1.2版在每条A～F路线分别列出三组件支持与双平台能力，第18.29节补充逐场景的KMD和平台观测条件。

下一步若进入实现，应以第22章的实际平台参数收敛M0/M1设计；把选定路线转成具体目录、接口、opcode和测试工作包。本版是一份可用于该决策的调研文档，尚不是已实现的GPU sanitizer。
