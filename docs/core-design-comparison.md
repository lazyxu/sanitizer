# GPU Sanitizer 核心设计分类与能力对照（v2.9）

**2026-11-02｜会议选型入口｜自研实现与目标性能尚无实卡证据：GPU_NOT_RUN。**

本页统一分类和横向结论；逐方案的执行链、元数据、约束、失败路径与开发任务直接写在[主报告既有第10～12章](llvm-gpu-sanitizer-research.md#software-route-details)。产品配置见[S01厂商指南](S01-vendor-implementation-guide.md)，通用接口以[组件指南](real-gpu-components-implementation.md)为准。**厂商公开能力、论文原型结果、自研设计和工程估算分别标示，不能相互替代。**

## 1. 唯一结论：2家主要厂商、3种核心参考配置，而不是5种检测算法

| 口径 | 数量 | 解释 |
|---|---:|---|
| 重点成熟厂商 | **2** | NVIDIA、AMD；Intel提供B2产品及运行时参照，MUSA只采用可核实的功能事实 |
| 基础空间参考配置 | **3** | NV-R：运行时二进制插桩；AMD-B2：编译插桩＋Shadow/Redzone；NV-C：编译期Original-Object Bounds |
| 基础空间判定 | **2层** | 当前地址可访问性；原指针来源对象边界。三种配置不等于三种完全不同的数学谓词 |
| 本轮详细比较 | **6条软件路线** | NV-R、B1、B2、NV-C、cuCatch、CuSafe；两篇研究提供不同元数据与时间检查设计，不增加成熟厂商配置计数 |
| 时间／集成／硬件 | D2／E／F | D2是指针时间身份扩展，E是模块集成策略；F1/F2仅保留既有硬件契约与预算条件，本轮不作芯片路线选型 |
| 历史预算标签 | B/C/D/E/F | 五个工程责任包，不是五套独立算法；B1/B2、D1/D2均未在v1.8拆出独立数字 |

**命名约束：NV-C不等于路线C。** NV-C是NVIDIA的NVCC/PTXAS编译期产品参考；路线C是自研运行时Binary Rewriter。自研D1选用LLVM实现，不能据此声称NVIDIA公开了相同LLVM Pass。完整自研产品交付数目前为0。[NVIDIA手册](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compile-time-patching)

## 2. 三种参考配置：参考谁、是否完全相同

| 配置 | 已公开的机制 | 自研需要实现的差额 | 可复用边界 |
|---|---|---|---|
| **NV-R** | Runtime Binary插桩，设备访问前检查当前地址、宽度、空间等 | 目标ISA译码/重编码、控制流与寄存器修复、PC映射、设备查询与生命周期 | 参考[NVIDIA Patching API](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html#patching-api)的职责；厂商私有重写器与索引布局未完整公开，**不是完整复制** |
| **AMD-B2** | LLVM设备插桩＋Shadow/Redzone＋配套运行库 | GPUVA映射、Shadow驻留、目标访存覆盖、队列顺序、设备报告和停止 | 参考[AMD版本化机制文档](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)与[LLVM ASan](https://clang.llvm.org/docs/AddressSanitizer.html)；算法同源，GPU软件栈仍须适配 |
| **NV-C** | NVCC/PTXAS分析指针来源、传播Base-and-Bounds；分析失败时回退普通分配检查 | LLVM SSA/MIR传播、指针存取与调用边界、unknown处理、设备Bounds检查 | 参考[官方Base-and-Bounds](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#base-and-bounds-analysis)；产品没有承诺任意ABI的完整D2 |

将六条路线放在同一组设计维度下，才能看清差异：

| 软件路线 | 检查注入点与GPU执行步骤 | 元数据和更新责任 | 首要部署约束 |
|---|---|---|---|
| **NV-R** | 装载/JIT后识别机器访存，在原指令前执行设备检查，再决定执行或终止 | UMD登记逻辑分配和实际生效顺序；内部索引未知，自研可选择B1类查询 | 无须重编译用户代码，但仅覆盖已支持的ISA、模块和指令；增加寄存器、代码与首次Patch成本 |
| **B1** | LLVM插桩；GPU在当前Live区间索引中查找候选对象，验证完整访问属于同一个对象 | UMD更新区间、权限与版本；设备查询只能看到已发布且可安全读取的快照 | 必须重编译；元数据成本主要随对象数变化；大对象内部的失效片段需额外登记 |
| **B2** | LLVM插桩；安全定位各Shadow Granule，对全部实际访问字节检查Poison/有效前缀 | Allocator维护保护区、Poison和Unpoison；UMD建立映射并按Queue发布 | 需Shadow资源和布局约束；自研可预驻留而无需XNACK，官方AMD支持条件另核 |
| **NV-C/D1** | 编译期为指针保留原对象Base/Size，经GEP、phi、参数等传播；设备按原对象检查 | Compiler传递来源；UMD提供初始对象描述符和生命周期 | 需重编译；跨调用、指针加载、整数转换可能丢来源；不能以当前地址反查冒充原来源 |
| **cuCatch** | 后端传播Base/Bounds；结合指针Tag和Base and Size Table（BST）比较空间与时间元数据 | 编译器、设备检查和修改后的CUDA驱动/分配链协作 | 论文原型的地址位、驱动与调用边界有假设；有限Tag和来源丢失限制保证，不等同当前NV-C产品 |
| **CuSafe** | LLVM插桩；从指针编码恢复对齐/对象候选，读取带内精确大小并检查；结合栈Epoch与Global地址随机化 | 分配拦截库管理大小类、对象头、CUDA虚拟映射与别名；局部对象由编译器布置 | 改变对象布局/指针解释；必须可控分配器与ABI；动态Shared和设备malloc不在论文原型支持内 |

最后两行依据原始[cuCatch论文](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)与[CuSafe论文](https://hongyi.lu/papers/cusafe-usenix26.pdf)；具体位分配、实验条件和原文分歧见[论文取证](research-notes/papers.md)。这些实现路径可以借鉴，不能视为自研设备上已可用的移植包。

## 3. 基础空间能力对比（不是厂商全部Checker矩阵）

**判定口径：** “支持”仅指元数据正确、目标操作确实被检查且满足该行前提；“条件”表示需要额外语义或仅概率覆盖；“无保证”不是断言所有具体样例一定漏检。NV-R与AMD-B2同属当前地址检查，但两个谓词未必逐例相等。

| 场景 | NV-R基础地址检查 | B1单对象区间 | B2逐字节Shadow | NV-C/D1 | cuCatch | CuSafe |
|---|---|---|---|---|---|---|
| S01：越界进入无效区 | 支持的空间/指令可报 | 完整访问不属Live对象时可报 | 命中Poison时可报 | 原来源已知或回退地址检查 | 受支持对象Bounds/Tag可报 | 受支持布局的精确Bounds可报 |
| S02：一次宽访问跨紧邻Live A/B | 厂商产品承诺按手册；自研取决于谓词 | **可报**：要求同一对象包含全部访问 | 若全部字节有效且无Poison间隔，**无保证** | 来源A已知可报 | Bounds恢复正确时可报 | 编码仍对应原对象且宽度检查完整时可报 |
| S04：A派生地址完全落入Live B | 无原来源保证 | 无原来源保证 | 无原来源保证 | **来源保留时可报** | 编译分析保留原Base时可报；边界丢失可降级 | 受支持对象且指针派生全插桩时可报；未插桩变换/字段子对象无完整保证 |
| 大分配内部局部Poison | 取决于额外子对象模型 | 仅登记外层范围时无保证 | **可表达的粒度内可报** | 需子对象/View来源 | 需对应布局或子对象支持 | 结构体字段内越界仍可漏检 |
| S08：Free生效后未复用 | 依赖生命周期撤销 | 依赖Live撤销 | 依赖Poison生效 | **Bounds自身不够**，需生命周期组合 | Tag/BST失效在支持链内识别 | 依赖对象失效、地址/栈身份机制 |
| S09：相同地址复用后旧指针 | 无保证 | 无保证 | 无保证 | 单独D1无保证，需D2 | Tag比较增强；有限身份/传播边界限制保证 | Global随机化、栈Epoch增强；碰撞/回绕限制保证 |
| S25：原子指针跨线程传递 | 地址层检查不验证历史身份 | 同左 | 同左 | D1/D2另需值与身份配对 | Tag/索引随指针值传递，但分析/有限身份/外部writer边界仍须验证 | 编码随指针值传递，但有限身份和外部变换仍须验证 |
| 闭源、不可重编译模块 | 支持的机器码可覆盖 | 无编译插桩保证 | 无编译插桩保证 | 无编译来源保证 | 论文不能保证预编译库 | 无插桩模块无检查保证 |
| Init/Race/Sync | 产品有其他Checker，不能归因于本列Bounds | 均需另加检查器 | 均需另加检查器 | 均需另加检查器 | 不是通用竞争/定义性检查 | 不是通用竞争/定义性检查 |

**决定性反例。** A=[0x1000,0x1040)，B=[0x1040,0x1080)。从A生成指针，访问[0x1040,0x1044)：当前地址有效，而原对象A不包含该范围；D1必须携带expected A。访问[0x103e,0x1042)则是另一问题：B1单对象包含检查能拒绝，纯B2若四字节都有效则可能放行。不能用一个“越界”勾选格掩盖两者。[NVIDIA原对象说明](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#base-and-bounds-analysis)

**时间身份反例。** A释放后B复用相同地址X，旧指针p和新指针q数值均为X；仅查当前表中的新Epoch无法区分p/q。cuCatch的有限Tag、CuSafe的有限栈Epoch或随机VA也不构成无限生命周期的确定保证；高概率缓解与闭合身份协议必须分列。

**Shadow比例与无XNACK。** 1:8、1:16、1:32只改变一字节Shadow覆盖的应用字节数；有效前缀编码还要求对齐和每个Granule的状态可表示，不能仅降低比例就承诺同样语义。XNACK负责可恢复缺页重试，不负责对象合法性。三种物理映射取舍见[AMD专题§2](amd-gpu-asan-hardware-fallback.md)，不新增第四种基础算法。

## 4. B1、C、D2、E、F是什么？为什么不能重复计数？

**B**提供编译检查与设备查询，B1/B2为替代实现；**C**只提供运行时注入方式，还要搭配查询谓词；**D1/D2**增加来源/历史身份；**E**整合B/D/C并保留各模块保证；**F**需要实体硬件契约。CuSafe不是增加了一个F，软件Tag检查也不自动要求修改RTL。

### 性能：比较成本来源，再比较同条件实测

| 软件路线 | 主要执行/内存成本 | 可引用的证据 | 对自研选型的实际含义 |
|---|---|---|---|
| NV-R | 首次解码/Patch、额外访存查询、寄存器保存、代码膨胀和PC映射 | 官方没有统一适用的慢速倍数；目标GPU未测 | 不用重编译的收益，要与ISA覆盖/启动成本一起评估 |
| B1 | 索引深度和不规则元数据读取；描述符约随对象数N增长 | 自研设计，未测 | 对象数少可能有利，随机访问和大量小对象可能很贵；不能仅以O(log N)判断GPU性能 |
| B2 | Shadow地址计算、元数据Load、分支和寄存器；经典逻辑Shadow约覆盖字节数/8 | AMD版本化文档提示部分负载3×或更高，**不是通用基准** | 顺序Shadow访问可能较友好；实际显存还含保护区、页面、日志，需测峰值 |
| NV-C/D1 | Base/Bounds活跃区间、寄存器/Spill、跨ABI传递；可优化冗余检查 | NVIDIA只给方向性改善，无通用平均承诺 | 更多寄存器可能抵消更少查询；Unknown比例必须随性能报告 |
| cuCatch | 编译传播、BST访问、Tag、驱动对象维护 | 论文176个workload，RTX 2080 Ti，几何平均1.19×；部分GPU计时不含Host管理成本 | 证明软件优化可行，不是自研端到端19%承诺 |
| CuSafe | 带内对象头Load、大小类对齐、VA/别名管理、栈身份 | 作者报告套件44项；普通程序时间平均+13%（1.13×），LLM吞吐另计；每项五次，不含初始化/H2D拷贝；跨项均值类型/时间分母未单列 | RTX 5090/CUDA12.8，不能与cuCatch横排快慢；原文样本数和内存表格存在分歧 |

cuCatch与CuSafe的数字分别来自各自论文，并非同平台复现。CuSafe正文44与Table 4分类合计46不一致，正文16.5 MiB/0.3%与Table 6汇总亦不一致，故保留“作者报告”且**不采纳为预算保证**。详见[主报告§5.8](llvm-gpu-sanitizer-research.md#paper-performance)。自研六条路线均无目标性能结论。

### 开发工作量：同一预算底座、不同新增任务

| 软件路线 | 必须新增的主要工作 | 条件工作/高风险处 | v1.8可采用的预算映射 |
|---|---|---|---|
| NV-R | UMD重写器、设备Stub、原PC/资源重算、对象服务与差分验证 | ISA基础设施、正规代码加载、可靠停止；生产Compiler可零修改 | P+C：核心31–60、测试27–51 kLOC；成熟重写基础缺失另核15–35/15–40 |
| B1 | LLVM访存覆盖、GPU区间查询、版本化索引与UMD发布/退休 | 子对象登记、索引一致性、大N随机查询 | P+B：25–48/21–39 kLOC是B家族估算；**没有独立B1报价** |
| B2 | LLVM访存覆盖、Safe Shadow Lookup、Allocator/ShadowManager、Poison顺序 | GPUVA/驻留、粒度布局、多空间/特殊指令 | 同P+B家族范围；不能说与B1逐模块成本相等 |
| NV-C/D1 | 来源分析、Companion数据、调用/存取边界、Bounds及覆盖报告 | 原子指针、指针整数转换、外部ABI；完整D2需另拆 | P+D：46–89/39–74 kLOC含受控D1/D2；**不是D1单独报价** |
| cuCatch | 把论文Base/Tag/BST协议落实到目标Compiler、Device与驱动 | 目标地址位、驱动接入、来源分析及时间Tag回绕 | 可参考D任务分解，**未有独立自研移植预算** |
| CuSafe | LLVM检查、对象布局/大小类、带内头、VA别名分配器和栈身份 | ABI兼容、地址资源、随机性、原型不支持的Shared/malloc | 原型2,964行C/C++仅是作者实现范围，**不等于目标产品工程量** |

表内成对数字均为“核心/手写测试”，公共P仅计一次；B1+B2不能按两个P+B相加，D也不能再与B整包简单相加。所有数字是[既有v1.8估算](estimates-v1.8.json)，不是厂商源码量。详细组件拆分、依赖和退出证据在[主报告§16](llvm-gpu-sanitizer-research.md#component-estimates)。无源码审计时给精确人月会制造虚假确定性；先核ISA覆盖、Allocator种类、ABI和KMD契约，再估工期。

## 5. Intel与摩尔线程的正确位置

厂商事实由[版本化索引](research-notes/vendors.md)及[结构化来源](research-notes/vendors_sources.json)统一登记。

- **Intel oneAPI ASan**是B2家族的产品对照，值得借鉴Compiler、Unified Runtime/Level Zero与分配生命周期协作；ASan/MSan/TSan各有不同能力。[Intel专题](intel-oneapi-device-sanitizers.md)保留2026.2的Linux/Ponte Vecchio、串行化和检查跳过条件，不能外推到所有Intel或第三方GPU。
- **MUSA**只纳入已公开功能与诊断体验参考；内部插桩/元数据机制未经公开资料证明，不另立算法。
- **F、Init/Race/Sync**保留原文必要接口和预算边界。本轮软件Memcheck决策不扩展Vulkan GPU-AV、GPUArmor或CHERI-SIMT方案比较。

## 6. 唯一建议实施顺序

1. **先P+B2**：可重编译应用、目标LLVM与驱动可控时，完成Global S01合法/非法、Safe Shadow Lookup、顺序Poison、报告和可靠停止。B1可在Shadow布局不适合且区间索引可控时替代，须补相同反例。
2. **再D1**：用A→Live B专项证明来源增益，记录来源丢失点；不是仅把普通地址表改名为Bounds表。
3. **按需求D2**：以同地址复用、跨存储/调用/原子链作为退出条件。cuCatch/CuSafe分别用于评估传播协议与布局方案，先验证约束，再决定是否吸收。
4. **闭源缺口明确才做C/E**：按模块列覆盖；同模块未插桩函数不自动获得Runtime补充，裸地址Fallback不升级为D1/D2。[NVIDIA限制](https://docs.nvidia.com/compute-sanitizer/ReleaseNotes/index.html#known-limitations)
5. 评审通过的应是**目标保证、可用条件、首个交付包和退出证据**。具体待定平台项集中在[主报告§22](llvm-gpu-sanitizer-research.md#platform-parameters)，本报告不把研究完整性写成产品验收完成。
