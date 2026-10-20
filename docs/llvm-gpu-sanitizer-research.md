# 自研 LLVM GPU 后端 Sanitizer 技术调研与实施方案

**版本：1.0 · 资料核验日期：2026-10-20 · 文档语言：中文**

**用途：技术立项、编译器／驱动／架构联合评审、研发任务拆分与预算讨论。**

本文回答：在已有自研 GPU、LLVM 后端、用户态驱动（UMD）和 cmodel 的前提下，怎样建设可用于真机调试的 sanitizer；不同路线能检测什么、需要修改哪些组件、投入多少、如何验证，以及什么情况下值得进一步投入二进制插桩或硬件支持。

资料采用有针对性的技术调研与批判性比较，优先官方文档、原始论文及作者公开材料。本文不是声称穷尽全部文献的系统综述。正文中的方括号编号对应末尾可直接访问的参考文献；资料版本、证据范围和检索记录见附录。

**证据标识：**

- **【官方事实】**：产品文档、上游源码或厂商技术说明直接支持的结论。
- **【论文结果】**：特定论文的实现、实验或作者主张，限定于其版本、平台和实验范围。
- **【工程建议】**：针对自研 GPU 的设计判断，需要在实际 ISA、驱动和应用上验证。
- **【估算】**：本报告提出的工程预算，不是厂商内部代码量，也不是对现有仓库逐文件审计后的报价。

---

## 目录

1. 决策结论与推荐路线
2. 研究范围、假设与能力分层
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
16. 组件改动与代码量估算
17. 性能、显存和可观测性设计
18. 验证基准与验收标准
19. 分阶段研发计划和团队安排
20. 主要风险与决策条件
21. 架构评审时应确认的项目参数
22. 参考文献、阅读路径与核验记录

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

这一路线适合“编译器、UMD、cmodel 都能修改”的组织条件。它并不要求第一版同时完成四类检查器、全部 CUDA 类功能或完整二进制重写器。

### 1.2 为什么不能只做一个 LLVM Pass

一个 Pass 可以在访存前插入检查，但不能凭空知道分配何时创建、异步释放何时生效、某个裸指针是不是旧对象遗留的指针、导入外部内存对应哪个物理对象，也不能决定错误发生后如何安全终止一个正在等待 barrier 的 kernel。

因此，交付对象应是一套跨组件协议和工具链：

| 组件 | 必须承担的职责 |
|---|---|
| 编译器前端／LLVM 中端／后端 | 识别检查点、保留对象语义、产生检查和覆盖清单、传递必要元数据 |
| 设备 runtime | 执行检查、访问 shadow／对象表、维护检查器自身元数据、聚合错误 |
| Host runtime 与 UMD | 拦截并记录资源和命令、按设备执行顺序更新元数据、管理模块和诊断缓冲 |
| cmodel | 在实际语义访存和同步事件处提供独立观测，验证指令和检查语义 |
| 调试／报告工具 | 将设备 PC、对象、线程及主机调用栈关联为可操作的报告 |
| KMD／固件／硬件 | 按需提供终止、故障归因、地址映射和硬件检查支持；并非每条软件路线都必须修改 |

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

## 2. 研究范围、假设与能力分层

### 2.1 本报告采用的项目假设

用户尚未提供实际仓库、ISA、GPU 虚拟地址布局、编译器版本、执行模型和 cmodel 详细设计。为使方案和预算可讨论，本文采用以下可替换假设：

- 已有能编译并执行计算 kernel 的 LLVM GPU 后端，已有基本调试行信息。
- 已有 Host runtime、UMD，以及能够运行目标 ISA 的功能级 cmodel。
- 至少支持全局内存、共享／局部数据存储和线程私有／栈地址语义；具体 address-space 编号以目标定义为准。
- GPU 支持基本原子操作和可由驱动管理的设备缓冲；不假设具有 CPU 式虚拟内存缺页能力、Top-Byte Ignore、128-bit 原子或可靠线程级恢复。
- 第一阶段面向开发和 CI 调试，不将 sanitizer 当作恶意 kernel 的隔离边界。
- 代码量预算针对已有基础设施上的 sanitizer 建设，不包含从零开发 LLVM 后端、UMD、ISA 模拟器或整个 GPU。

如果这些假设不成立，应调整对应模块预算；不必因此推迟本报告提出的能力分层和接口设计。

### 2.2 检测能力不宜压缩成一个“支持 sanitizer”开关

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

### 2.3 地址空间及其身份

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
| cmodel 能给出真机 sanitizer 的性能结论 | 功能 cmodel 用于语义验证；真机性能需实测，硬件设计需校准的时序模型 | 不把模拟器运行时间当产品 overhead |

**官方来源之间也有更新不同步。** NVIDIA 产品总览仍保留 initcheck 仅 global 的概括，而当前用户手册与 release notes 已列出 shared 支持；本报告按后两者的版本信息处理这一冲突。[N01][N02][N05]

## 4. 友商产品与上游实现

### 4.1 NVIDIA：成熟工具套件与新增编译分析

**【官方事实】** NVIDIA 提供 memcheck、racecheck、initcheck、synccheck，并公开工具扩展接口。当前 memcheck 可选择编译期插桩；其价值包括利用原对象 bounds 改善相邻合法分配之间的越界检测。[N01][N02][N04]

**建议借鉴的分层是：资源／命令观测、代码检查点、元数据访问、分析与报告。** API 指南的 callback、patching、memory 部分可作为阅读入口；当前接口目录还有 barrier 和 stream 等模块，“三大组成”适合作概括，不宜当作全部接口清单。[N03]

对实际使用必须保留的版本条件是：编译器与工具匹配、按官方要求启动检查器，并核对 release notes 中 HMM、cluster、JIT 和混合模块等限制。调试行信息与插桩标志是两个独立配置。[N02][N05][N07]

**【工程建议】** 自研模块至少输出插桩 ABI 版本、检查器种类、覆盖的地址空间和指令种类。加载器发现不兼容时应明确报错或记录降级，不应静默运行并输出“没有错误”。

### 4.2 AMD ROCm：LLVM ASan 移植具有直接参考价值

**【官方事实】** ROCm GPU ASan 将编译插桩与运行库支持结合；稳定版文档给出 address sanitizer 和 shared runtime 的编译方式，并对 xnack、平台和运行环境作出约束。已知限制还涉及 private／stack、LDS 的建模方式、有限 redzone 和 NULL 地址；不能只从支持 ASan 推导全部空间均已覆盖。[A01]

LLNL El Capitan 指南提供实际部署中的编译包装器、环境设置和已知问题，示例注明 rocmcc/7.0.0；其中包括 hipFree double-free 和 hipMemcpy 非法地址的检测限制，适合构建现场回归清单。其结论不能替代其他版本和集群环境的支持声明。[A02]

**【工程建议】** 优先复用 LLVM 的指令识别、检查组织、ignorelist 和符号化经验；shadow 的地址布局、设备错误通道、设备 allocator 与队列生命周期按自研 GPU 设计。CPU compiler-rt 不能直接假定 GPU 具有同样的 mmap、信号、线程局部存储和系统调用能力。

### 4.3 Intel oneAPI：用于验证异构运行时职责是否齐全

Intel 的设备侧 sanitizer 文档提供 ASan、MSan、TSan 的产品路径，并明确具体平台和限制。其公开限制表说明：支持列表、执行串行化、某些规模下跳过检查，都属于产品规格的一部分。[I01][I02]

**【工程建议】** 参考它组织 Unified Runtime／USM 与编译器的协作方式，同时在自己的结果中公开“哪些 kernel／空间未检查”和“执行是否被串行化”。不要只比较 checker 名字，也不要把 Intel 的平台限定当作所有 LLVM GPU 后端的限制。

### 4.4 摩尔线程 MUSA：自研 GPU 产品化近邻

MUSA Compute Sanitizer 的公开指南展示了运行时工具、global 越界、Runtime／Driver API 错误和显存泄漏报告。本次可读取的官方材料不足以独立确认其内部是否采用用户参考描述中的“驱动 API 插桩＋指令级插桩”架构，因此本文不把该内部实现当作已证实事实。[M01][M02]

**【工程建议】** 近邻价值主要在工具命令、驱动集成和基础故障报告的产品形态。其公开版本不能证明国内厂商都采用同一路线，也不能推出“必须先完成二进制插桩才能做编译插桩”。

### 4.5 LLVM 上游：可复用的是框架，不是现成的目标支持

AddressSanitizer 官方文档将工具分为编译器插桩与 runtime。[L01] 核验时的 LLVM 在线源码页标为开发分支快照；AMDGPU 相关路径仍包含特定地址空间和 stack poisoning 条件，不能将“源码出现 AMDGPU”解释为已覆盖所有 GPU 存储类别。[L03]

HWASan 提供另一种思路：指针 tag 与内存 tag 对比，结合 granule 与短 granule 处理。但目标地址格式、tag 存放和剥离语义必须由 ISA 保证或软件明确实现，不能假设自研 GPU 可直接使用高地址位。[L04]

### 4.6 友商比较应采用的表格

| 对象 | 主要入口 | 最值得复用的经验 | 对自研方案的限制提醒 |
|---|---|---|---|
| NVIDIA Compute Sanitizer | 二进制检查＋可选 memcheck 编译插桩 | 模块混合、对象 bounds、完整报告和工具 API | checker、版本、地址空间必须逐项核对 |
| ROCm GPU ASan | LLVM 编译插桩＋异构 runtime | shadow 方案的跨组件接线 | 设备地址空间与平台条件并非通用 |
| Intel device sanitizers | 编译器＋异构运行时 | 多 checker 的运行时管理 | 检查跳过与串行化必须透明 |
| MUSA Compute Sanitizer | 运行器＋内存／API 检查＋报告 | 自研生态中的工具产品形态 | 内部插桩机制需另有公开证据 |
| LLVM ASan／HWASan | 编译 pass＋compiler-rt 体系 | 算法、诊断、工程结构 | 目标适配与设备 runtime 是实际工作 |

上述表用于架构比较，不表示这些产品在同一硬件、数据集上具有相同覆盖率或性能。[N01–N07][A01][A02][I01][M01][L01–L04]

### 4.7 本次采用的版本基线与操作入口

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

**本报告没有运行自研硬件、复现全部论文或完成友商同平台横测。** 因而不提供伪造的“自研方案预计快 3.7×”或统一厂商排行榜。第 18 章给出真正可复现的比较方法。

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

## 8. 跨组件公共架构与接口

### 8.1 推荐架构

~~~mermaid
flowchart TD
    C["LLVM 分析与插桩"] --> M["模块能力与诊断映射"]
    M --> U["Host runtime / UMD"]
    U --> O["对象与生命周期服务"]
    U --> Q["队列与事件依赖"]
    O --> D["设备检查 runtime"]
    Q --> D
    C --> D
    O --> S["cmodel 事件检查"]
    Q --> S
    D --> R["统一错误协议"]
    S --> R
    M --> R
    R --> T["符号化与 CI 报告"]
~~~

图中共享的是对象／命令语义与报告协议；cmodel 检查逻辑应保留独立实现，避免只用同一段错误代码自证。

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
    uint32_t state;            // pending/live/retiring/dead
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
| free request／retire complete | runtime／UMD | 对象身份、命令位置、完成证据 | quarantine、时间检查 |
| map／unmap／import／alias | UMD | 规范对象与映射、权限、device | 地址解析与 peer 检查 |
| launch／graph node | runtime／UMD | launch ID、module、queue、grid、参数元数据 | runtime、cmodel、报告 |
| copy／memset submit／complete | runtime／copy engine 接口 | 源目标范围、顺序、定义性 | init、生命周期 |
| barrier／fence／atomic | 插桩或 cmodel | scope、ordering、参与集合、phase | race／sync 分析 |
| module load／unload／JIT | loader | build ID、ABI、覆盖位图、code map | 混合模式与符号化 |

API 回调与底层实际命令必须去重。一个高级 API 触发若干内部 driver 调用时，不应把同一分配记录多次。

### 8.4 异步分配／释放是实际关键路径

“Host 已调用 free”与“GPU 上所有合法旧访问均已完成”不是同一时刻。状态机建议为：

~~~mermaid
stateDiagram-v2
    [*] --> Pending
    Pending --> Live: 分配命令生效
    Live --> Retiring: 释放命令进入依赖图
    Retiring --> Dead: 已满足退休条件
    Dead --> Reusable: epoch和元数据安全回收
    Reusable --> Pending: 新身份分配
~~~

**这张状态图不能直接解释成提交 free 时所有队列立即看见 Retiring＝不可访问。** 检查时必须结合访问命令与 free 命令的先后关系；同队列 free 之前的访问仍然合法，跨队列缺少依赖则可能是生命周期竞争。

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

## 9. 路线 A：cmodel 检测

### 9.1 原理和适用目标

**【工程建议】** 在 cmodel 真正解释访存、资源和同步语义的位置增加观察入口。每次即将生效的读写携带实际地址、宽度、byte enable、lane、作用域等信息，查询对象／有效性状态，随后决定报告或按检查策略阻止操作。

可分成三个层级：

- A0：实际地址、范围、对齐、分配 API、初始化位图。
- A1：结合编译器提供的 shared／stack 对象 map，检查对象边界。
- A2：为指针运算及寄存器／内存增加身份跟踪，并实现多个合法调度或 HB 分析。

**A0 不能自动获得 A1/A2 的保证。** 实际地址同样无法区分从 A 越入 B，也无法独立识别地址复用后的旧裸指针。

### 9.2 各组件改动

| 组件 | 修改内容 | 交付物 |
|---|---|---|
| 编译器 | 输出对象、frame、PC／源代码 map；可选语义标记 | 与机器码匹配的 sidecar |
| Device runtime | A0 可近乎不改；需要时输出 device allocator 事件 | 与公共协议一致的事件 |
| Host runtime／UMD | 将 alloc、free、copy、launch、事件依赖重放给模型 | 统一对象／命令输入 |
| cmodel | 访存 tap、对齐／权限／有效性判断、shadow、barrier 状态、停止策略 | 可重复的模型检查器 |
| 报告工具 | 合并模型 PC、对象和 Host site；稳定错误格式 | 与真机共享的诊断和 CI 出口 |
| KMD／固件 | 通常无新增要求 | 如模型执行依赖真机资源则单独评估 |

### 9.3 代价与局限

优势是易于观察语义、调试检查器、构造确定输入；不需要先解决真机上全部设备 runtime 性能问题。缺点是无法单凭功能 cmodel 推断真机 slowdown，也很难按真机规模运行数十 GiB 数据和全部调度。

不要在 cmodel 中强行重放它不能表达的 ISA 或内存序。出现与真机分歧时，应先确认模型、编译器和驱动的实际事件一致，再判断谁有错。

### 9.4 建议第一批验收

验证 scalar/vector 越界、mask、alignment、同地址不同 block、alloc/free 顺序和日志截断；为每种误报设计合法反例。A1/A2 另做身份传播及调度测试，不与 A0 的完成度合并。

预算见第 16 章：公共底座 P＋A 的核心实现约 18–38 kLOC，手写测试约 16–33 kLOC；窄范围 A0 探索原型可远小于此完整内测范围。

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

### 10.4 其他组件改动

| 组件 | 必做 | 容易低估的部分 |
|---|---|---|
| Device runtime | shadow 查询、任意长度检查、错误聚合、空间分类 | SIMD 前进性、helper 寄存器成本 |
| Host runtime／UMD | 分配／释放 poison、shadow 内存、参数 context、JIT 链接 | pool 子分配、异步命令时序、外部内存 |
| cmodel | 独立区间 oracle、shadow 建立和生效顺序核对 | 检查器本身访存与应用访存区分 |
| Tooling | alloc/free stack、PC 符号化、覆盖 manifest、忽略规则 | 部分检查和资源不足不能当通过 |
| KMD／固件 | 若已有安全终止／映射接口可复用 | 缺乏 dispatch abort 时是明确前置工作 |

### 10.5 能力边界与退出条件

完成 redzone 检查后，如果业务错误主要是对象 A 跳入 live B，应转向 D1，而不是只无限增加 padding。若地址复用 UAF 构成重要需求，应做 D2，而不是宣称更大的 quarantine 已解决全部问题。

只有在实际 shadow 布局、典型内存占用和真机 slowdown 均可接受时，才把 B 扩展成默认 CI 模式。

预算：P＋B 核心约 27–53 kLOC，测试约 23–44 kLOC。这里是具备多空间和异常路径处理的内测版本，不是插入几个 load/store helper 的演示程序。

## 11. 路线 C：运行时二进制插桩

### 11.1 原理和前提

UMD 在模块加载或 JIT 产物生成后取得目标代码，解析指令与控制流，为被选择的访存、atomic 和同步指令插入检查，再以新的资源用量和 PC map 启动 kernel。

它能覆盖无法重编译的模块，但“API 拦截”本身不能看到 kernel 内每次访存。也需要区分中间表示重写与最终机器码重写：例如 BARRACUDA 的 binary-level 路径依赖从 fat binary 提取 PTX，不能据此认为其能够处理所有纯机器码。[P02]

### 11.2 改动分解

| 组件 | 修改内容 |
|---|---|
| 编译器／MC 层 | 可复用解码／编码、指令语义表；生成原 PC 到新 PC 的 map；不强制应用重编译 |
| 设备 runtime | 高效 patch stub、寄存器保存恢复、metadata lookup、报告 |
| UMD／loader | 模块镜像复制、CFG／branch relocation、资源重算、patch cache、JIT 与卸载生命周期 |
| cmodel | 原指令与插桩版本差分执行；覆盖合法编码、谓词、寄存器依赖、异常路径 |
| Tooling | 还原原始 PC、代码版本、未支持指令和 patch 失败原因 |
| KMD／固件 | 遵循现有可执行代码加载／调试接口；缺失时需要正规新增接口 |

### 11.3 必须单独预算的工程风险

- 修改代码长度后，branch target、literal pool、重定位和调用约定都要更新。
- 额外寄存器和 scratch 会改变 occupancy；原 launch 资源声明未更新可能直接导致错误。
- patch 必须保留 predicate、scoreboard、barrier、依赖等待和特殊访存修饰符。
- 模块被多个 context／线程并发加载时，patch cache 需要绑定完整 code hash、ISA 和工具版本。
- 不能用未知指令的“近似解码”继续检查；应清楚拒绝或标成部分覆盖。
- 对不可识别对象 provenance 的代码，基础 C 只能保证地址／区间层面的检查。

### 11.4 什么时候不优先做 C

如果全部核心应用都能重编译，而项目尚无成熟的机器码重编码／调试 patch 设施，C 的基础建设通常会挤占最急需的对象与生命周期工作。可以先把模块未覆盖作为可见缺口，等实际闭源覆盖需求出现再补。

预算：在**已有成熟指令解码、编码和基础 patch 能力**时，P＋C 核心约 34–67 kLOC，测试约 30–58 kLOC。若这些前提不存在，还需追加第 16 章列出的基础设施投入。

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

### 12.5 UMD／runtime／cmodel 改动

UMD 负责对象 ID／epoch、kernel 参数 sidecar、模块能力协商、外部对象注册及回收顺序。设备 runtime 提供热描述符、pointer shadow、tag 检查、unknown provenance 路径和错误聚合。

cmodel 应在指针流经寄存器、frame、内存槽与原子发布时独立跟踪身份，用最小反例核对 D2。若只让模型再次调用同一身份查询函数，将无法发现实现共有的错误。

### 12.6 子对象和 tensor view

大块 GPU allocation 可能被框架划分成多个 tensor、workspace 和临时区。只检查最外层 allocation 无法判断逻辑 view 越界。推荐提供可选的 suballocation／view 注册接口，字段包括 parent、offset、length、stride、element size 和 lifetime。

第一版支持连续子范围即可；非连续 strided view 需要选择逐维 bounds 检查或明确只提供外包范围检查。后者不能声称完整 tensor 索引安全。

### 12.7 不直接照抄 cuCatch 的理由

cuCatch 中 BST 是 **Base and Size Table**，GPUArmor 的 BST 则可以指 **Binary Search Tree**，两者名称容易混淆。[P01][P04] 自研设计要分别讨论“表中存什么”和“怎样索引表”。

此外，能找到一个 reaching base pointer，不意味着它一定是最初 allocation base；跨函数或内存加载可能已经成为新的分析 root。本文 D2 的闭合身份传播要求比“局部后端分析恢复 bounds”更强，成本也更高。

预算：P＋D 核心约 50–98 kLOC，测试约 43–83 kLOC；只完成 D1 的受控原型应按任务单单独裁剪，不能承诺同时具有完整 D2 能力。

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

### 13.4 组件投入

编译器需要生成稳定 manifest 和边界摘要；runtime 需共享对象表并处理不同元数据级别；UMD 需要 patch 选择、版本校验和缓存；cmodel 要验证跨模式流转；报告工具必须显示每个错误和每段代码的覆盖等级。

预算见第 16 章的去重拆解：P＋E 核心约 76–146 kLOC、测试约 66–127 kLOC。这是“B 全量＋D 独有部分＋C fallback 子集＋集成”的估算，不是将 B、C、D 总量相加。

## 14. 路线 F：硬件辅助

### 14.1 可选硬件方向

| 方向 | 硬件／ISA 支持 | 软件支持 | 能力边界 |
|---|---|---|---|
| Tag compare | pointer tag、memory tag、同步 fault／屏蔽语义 | tag 分配、传播、清除／重用 | 通常有碰撞与粒度边界 |
| Bounds check 指令 | metadata handle／base-size 检查、准确错误 PC | 对象分析、descriptor 管理 | 无正确来源仍无法证明原对象 bounds |
| Metadata cache／MLB | per-SM 缓存、失效和一致性协议 | profile、lookup、free 生命周期 | cache 大小和工作集有关 |
| Watchpoint／保护页 | 地址范围、权限及 fault 归因 | GPUVA 管理、guard page | 粒度较粗，无法替代对象级检查 |
| 同步事件 trace | barrier／atomic／queue 事件输出 | race/sync 分析、流量控制 | 全访存 trace 的带宽可能不可接受 |

### 14.2 新指令不是全部工作

完整路线需要设计 metadata 的地址权限、cache coherence、free 后失效、context switch、peer memory、迁移、fault 重放和错误归因。尤其不能让缓存中旧的“live”信息在释放之后继续使访问通过。

UMD、固件和 KMD 要配套资源初始化、上下文管理和错误处理；cmodel 支持新语义，时序模型用于性能评估；RTL/DV 验证则覆盖流水线、异常、并发和一致性。

GPUArmor 提供了这类软硬协同的公开研究例子，且实测／模拟基线必须按第 5 章区分。[P04]

### 14.3 选择 F 的证据门槛

建议满足以下条件后再进入正式硬件投入：

1. 软件原型已有稳定错误分类和合理覆盖。
2. 至少一组代表性业务证明 metadata／检查指令是主要瓶颈，而非日志或 CPU wrapper。
3. 采集对象工作集、热点和缓存命中曲线。
4. 验证硬件方案不会让 free／alias／peer 的一致性成本抵消热路径收益。
5. 获得下一代芯片和软件发布节奏的一致资源安排。

### 14.4 预算解释

第 16 章的 P＋F 软件核心 30–60 kLOC、测试 29–56 kLOC，前提是所需硬件能力**已经存在或另有项目交付**。缺少这些能力时，不能用这张软件表报价完整 F。ISA／RTL／DV／固件的新增工作必须另行立项。

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

### 15.6 工程拆分建议

| 检查器 | Compiler | Device runtime | UMD／Host | cmodel | Tooling |
|---|---|---|---|---|---|
| Initcheck | 所有读写／copy 的定义性入口 | bitmap、合并与查询 | copy／memset／Host边界 | 独立定义性 oracle | 未定义范围与来源 |
| Racecheck | 访问、atomic、scope、barrier | HB/FSM 或观测算法 | queue/event/graph顺序 | 多调度及小规模参考检测 | 两端PC或单边证据、级别 |
| Synccheck | 同步指令和参与mask | barrier状态与错误聚合 | launch配置、完成／终止 | 同步状态和合法反例 | 参与者、phase、超时证据 |

每个 checker 都应独立开关、单独统计性能与覆盖，再讨论组合运行能否共享事件并节省成本。

## 16. 组件改动与代码量估算

### 16.1 估算口径与可信度

**本章全部为【估算】。** 未读取用户 GPU 软件仓库，也没有任何厂商内部 SLOC 数据。数值用于比较投入级别、预留人员和拆分工作，不应直接作为采购或交付承诺。

统一口径：

- 1 kLOC＝约 1,000 行新写或实质修改的手写代码；同一行重复修改不反复计入。
- 核心代码与手写测试分列；不计空行、注释、机械生成代码、第三方代码复制和未修改的 LLVM 依赖。
- 包含可发布给内部用户的工程化与失败路径处理；不包含成熟商业工具多代硬件的全部兼容与极限优化。
- 已有可用 LLVM 后端、UMD、cmodel 和基础调试能力；C 另假设已有成熟解码／编码及基本 patch 框架。
- A 预算以地址检查＋对象 map／同步观测为主，不包括通用全指令 provenance 模拟及所有调度穷举。
- B/C/D/E/F 表格主预算针对内存检查及其基础设施；完整 init／race／sync 的新增分析引擎在 §16.7 单列。
- F 软件表不包含缺失的硬件、RTL/DV 和硅片实现。
- 下限对应基础设施较好、接口受控、平台较少；上限对应更多边界及兼容工作。架构不确定性仍可能使实际值超出区间。

### 16.2 公共底座 P：只计一次

| 组件 | 核心 kLOC | 手写测试 kLOC | 主要内容 |
|---|---:|---:|---|
| Compiler | 1–2 | 1–2 | option、module manifest、诊断映射接口 |
| Device runtime | 1–2 | 1–2 | context、报告缓冲、基础 ABI |
| Host runtime＋UMD | 4–7 | 3–5 | allocation／queue／module 事件、生命周期 |
| cmodel | 1–2 | 1–2 | 事件适配和统一输入输出 |
| Tooling | 2–4 | 2–3 | CLI、符号化接线、覆盖和错误格式 |
| **合计 P** | **9–17** | **8–14** | 基础协议和共享实现 |

### 16.3 A／B／C 在 P 之上的新增量

表格中的每个单元写作“核心；测试”，单位均为 kLOC。

| 组件 | A：cmodel 增量 | B：shadow 增量 | C：binary 增量 |
|---|---:|---:|---:|
| Compiler | 1–2；1–2 | 7–13；6–11 | 0–2；0–2 |
| Device runtime | 0–1；0–1 | 5–9；4–7 | 6–11；5–9 |
| Host runtime＋UMD | 1–3；1–3 | 4–8；3–6 | 14–26；12–23 |
| cmodel | 6–12；5–10 | 1–3；1–3 | 2–5；2–5 |
| Tooling | 1–3；1–3 | 1–3；1–3 | 3–6；3–5 |
| **新增核心合计** | **9–21** | **18–36** | **25–50** |
| **新增测试合计** | **8–19** | **15–30** | **22–44** |

C 将二进制翻译和 patch 管理主要记在 Host runtime＋UMD 列。团队若把该实现放在独立工具库，应移动归属，不能同时记在 compiler 与 UMD 两列。

### 16.4 D／E／F 在 P 之上的新增量

| 组件 | D：bounds／provenance 增量 | E：去重后 hybrid 增量 | F：已有硬件的软件使能增量 |
|---|---:|---:|---:|
| Compiler | 18–34；15–29 | 23–44；19–36 | 5–10；5–10 |
| Device runtime | 10–20；8–16 | 17–32；14–27 | 4–8；4–8 |
| Host runtime＋UMD | 7–14；6–12 | 16–31；14–28 | 4–8；4–8 |
| cmodel | 3–7；3–7 | 6–12；6–12 | 6–12；6–12 |
| Tooling | 3–6；3–5 | 5–10；5–10 | 2–5；2–4 |
| **新增核心合计** | **41–81** | **67–129** | **21–43** |
| **新增测试合计** | **35–69** | **58–113** | **21–42** |

D 包含受控接口范围内的 D2；不能据此承诺任意 C++ ABI、所有第三方库和任意未插桩 writer 都保留身份。

### 16.5 从 sanitizer 零起点建设的总量

| 方案 | 核心 kLOC | 手写测试 kLOC | 两者合计 kLOC | 主要预算条件 |
|---|---:|---:|---:|---|
| P＋A | 18–38 | 16–33 | 34–71 | 已有 cmodel；非全部调度穷举 |
| P＋B | 27–53 | 23–44 | 50–97 | 可重编译、shadow 布局可行 |
| P＋C | 34–67 | 30–58 | 64–125 | 已有成熟 decode／encode／patch |
| P＋D | 50–98 | 43–83 | 93–181 | 受控 provenance 与 ABI 支持集合 |
| P＋E | 76–146 | 66–127 | 142–273 | B＋D 独有部分＋C 子集＋集成 |
| P＋F | 30–60 | 29–56 | 59–116 | 硬件能力另有交付，不含 RTL/DV |

**不能将这些“从零总量”相加。** 若采用 B 后再做 D，应剔除公共 P、shadow 通路、对象表和报告复用；E 已按这种方法单独计算。

### 16.6 E 的去重计算，供预算复核

| 构成 | 新增核心 kLOC | 新增测试 kLOC |
|---|---:|---:|
| 公共 P | 9–17 | 8–14 |
| B 完整增量 | 18–36 | 15–30 |
| D 在 B 上的独有新增 | 29–55 | 24–46 |
| C 的受控 binary fallback 子集 | 10–20 | 9–19 |
| 跨模式集成与边界协议 | 10–18 | 10–18 |
| **总计 P＋E** | **76–146** | **66–127** |

D 独有新增的五组件核心分别为 14–26／8–15／4–8／2–4／1–2，测试为 11–20／6–12／4–8／2–4／1–2。C 子集核心为 0–1／2–4／6–11／1–2／1–2，测试为 0–1／2–4／5–10／1–2／1–2。集成核心与测试各为 2–4／2–4／2–4／2–3／2–3。

这套 E 预算没有同时包含 C 的全部复杂 ISA、完整 A2 模型和任意语言 ABI 的 D2。需要这些能力时，在需求清单中明确追加。

### 16.7 其他 checker 的新增预算

以下估算以 P 和一个已稳定的访存事件／报告基础为前提。组件顺序仍为 Compiler／Device runtime／Host＋UMD／cmodel／Tooling。

| 新增能力 | 五组件核心 kLOC | 核心合计 | 测试合计 | 边界 |
|---|---|---:|---:|---|
| I1 内存初始化位图 | 2–4／3–6／2–4／1–2／1–2 | 9–18 | 8–16 | 不含完整寄存器值级 MSan |
| S1 基础 barrier／mask 检查 | 2–4／3–6／1–2／2–4／1–2 | 9–18 | 9–18 | cluster／复杂异步另扩展 |
| R1 shared HB／FSM | 4–8／7–14／1–3／3–6／2–4 | 17–35 | 18–36 | 明确层次同步子集 |
| R1 扩展至 global／跨kernel | 3–6／5–10／4–8／2–4／1–3 | 15–31 | 16–34 | 在前一 R1 基础上增量，不含全部跨设备模型 |
| 重复访问／碰撞实验模式 | 3–6／3–6／1–2／1–3／1–2 | 9–19 | 8–18 | 概率检测、目标内存语义明确 |
| 值级 MSan、通用跨CPU/GPU TSan | 需单独设计 | 不给伪精确合计 | 不给伪精确合计 | 数据传播和内存模型超出简单模块 |

多个 checker 组合可复用部分事件和 tooling。上表不是要求把每行全部实施后直接求和；在方案收敛后按最终工作包重新去重。

### 16.8 前提不满足时的条件追加

| 条件 | 可能新增工作 | 初始预算判断 |
|---|---|---|
| 缺少可用机器码重写基础 | 补齐 decoder/encoder、指令语义和基础重定位／资源接口 | 核心约15–35 kLOC、测试约15–40 kLOC；只计缺失基础，剔除C中已计的CFG／patch管理 |
| 硬件已有取消能力，但未接通安全 dispatch／context abort | 固件、KMD 与错误回传的软件接线 | 核心约4–10 kLOC、测试约4–10 kLOC；若硬件能力也缺失，须先验证可行性再估算 |
| 没有可控 GPUVA 或 shadow 预映射 | 地址管理、页表、元数据映射服务 | 先完成可行性原型，再估算；不默认只是常量 offset 修改 |
| 多 GPU／IPC／外部导入为首版硬需求 | 规范对象身份、别名、跨设备时序 | 独立项目包，按实际 API／一致性能力估算 |
| F 需要新硬件 | ISA、RTL、DV、性能模型、固件和硅片验证 | 不用软件 SLOC折算；纳入芯片计划 |

### 16.9 如何把宽区间变成可信的项目预算

建议用两周左右的技术摸底形成以下实际数据：待覆盖 opcode 数、kernel 类型数、allocator/API 数、现有 patch 支持情况、module ABI 边界数量，以及模型／真机已知差异。

随后以“接口＋算法＋失败路径＋兼容＋验证”的工作包估计人月，并与上述代码量交叉检查。不要按一个统一的“每人每天写多少行”直接推算项目时间。

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
版本：compiler / runtime / UMD / firmware / cmodel / ISA
编译：优化级别、sanitizer模式、debug配置、LTO、编译时间
工作负载：名称、输入hash、数据大小、launch数、并发队列
基线：未插桩；诊断NOP；完整检查（分别记录）
结果：kernel与端到端时间、p50/p95/max、重复次数
资源：寄存器、spill、VRAM峰值、shadow、Host RSS
覆盖：checked/skipped kernels、unknown provenance、unsupported ops
异常：errors、tool failure、overflow、timeout、sampled/dropped事件
~~~

诊断 NOP 基线用于解释编译扰动，不能替代未插桩基线；GPUArmor 的不同数字正说明了这一点。[P04]

## 18. 验证基准与验收标准

### 18.1 分四层验证

| 层 | 内容 | 检查重点 |
|---|---|---|
| V0：编译语义 | LLVM IR、MIR、反汇编和覆盖清单 | 检查没有被消除，新增目标访存可见 |
| V1：最小错误与合法反例 | 每个能力一个确定触发样例及无错对应样例 | 误报／漏报、边界和整数溢出 |
| V2：cmodel／真机差分 | 相同输入、相同对象和命令语义 | 模型、driver和真实ISA的一致性 |
| V3：真实应用与规模 | kernel库、框架内存池、长时间并发 | 性能、内存、时序、诊断可操作性 |

公开 benchmark 用来补充 V1/V3，不取代目标 ISA 的语义验证。[P03][P06]

### 18.2 最小测试矩阵

| 类别 | 必须包含的测试 | 合法反例／重要边界 |
|---|---|---|
| Global bounds | 前越界、尾越界、跨页、远距离跳越 redzone | 最后一个有效 byte、零长度操作 |
| 对象来源 | A 的指针落入 live B | B 的合法新指针 |
| 子分配 | 同一个 pool 内两个相邻逻辑对象 | 合法跨 view 范围的显式 API |
| Granule | 同 granule 的孔洞和多个子对象 | 编码可表达的有效前缀 |
| 宽访问 | 16／32／64 B跨边界、非对齐 | 指令明确允许的非对齐 |
| Vector／mask | lane掩码、元素掩码、EVL、gather/scatter | masked-off非法地址不得误报／fault |
| Atomics | 不同宽度、CAS成功／失败、混合访问 | 不同 scope 下合法同步 |
| Shared | static、dynamic、跨变量、block身份复用 | 不同 block 的相同偏移 |
| Private／stack | frame边界、递归／调用、返回后访问 | 相邻合法frame、spill地址 |
| Constant／resource | 只读权限、资源描述符、维度 | 合法clamp／border等地址模式 |
| Lifetime | free后访问、free排队但尚未生效 | 同queue中free之前的合法访问 |
| 地址重用 | 同GPUVA旧指针与新指针 | 新epoch正确传播 |
| Pointer传播 | phi/select、spill/reload、memcpy、call/return | 指针值与identity成对一致 |
| Atomic pointer | 并发CAS、ABA、value与metadata错配 | 所有writer遵循同一协议 |
| API | invalid/double free、失败分配、泄漏 | 对象仍被合法持有／导入但不拥有 |
| 初始化 | byte部分写、copy传播、async完成 | 有序memset和store后读取 |
| Shared race | RAW/WAR/WAW、同warp／跨warp | 正确barrier后的访问 |
| Global race | 同block、跨block、跨kernel | queue event和发布／获取 |
| 同步 | 无效mask、缺失参与、phase错误、退出 | 合法子组同步和早退语义 |
| 模块混合 | 已插桩→未插桩→已插桩 | 正确标记unknown，不伪造身份 |
| 特殊执行 | graph replay、JIT、device launch | 执行实例独立；仍live对象保留allocation epoch |
| 外部内存 | alias、peer、IPC、host-mapped | 同一对象不同VA不可被重复拥有 |
| 工具资源 | 报告满、shadow不足、clock溢出 | 覆盖降级明确且应用不会被隐瞒 |
| 终止 | 一lane错误但其他线程等barrier | dispatch/context可控终止 |

### 18.3 数据集与规模建议

**【工程建议】** 小用例做确定性诊断；规模用例覆盖 10^3／10^5／10^6 活跃对象或描述符、不同实际数据占用比例、1／多个 queue，以及长时间重复分配／释放。

具体建议包括：

- 算力密集：GEMM、卷积、attention／reduction，观察寄存器与 occupancy。
- 带宽密集：copy、scan、stencil、稀疏矩阵，观察 shadow 带宽。
- 指针／图结构：链表、hash、图遍历，观察 provenance 和 metadata lookup。
- allocator密集：小对象池、频繁free/reuse、多stream缓存。
- 特殊功能：async copy、tensor descriptor、graph和JIT。

100万对象测试可以采用小 allocation 或描述符模型，不必强行申请100万个大 buffer；应同时记录实际显存，以免把 API／驱动的原有限制误认为 sanitizer 回归。

### 18.4 检测率的正确分母

至少分开统计：

- 已生成的错误测试总数。
- 满足setup条件且实际执行到目标操作的测试数。
- checker声明支持的适用测试数。
- 在这些条件下的检出、漏报和合法样例误报。
- 未支持、编译失败、工具内部错误、timeout和未触发路径。

GMSBench 中不同 checker 使用不同适用分母，HiRace 的 code/input 组合也存在“错误未被输入触发”的情况。不能把这类不同口径合成一个总体 accuracy。[P03][P06]

### 18.5 建议验收标准

以下是拟议项目门槛，不是当前已经达成的测量：

1. 对声明支持的确定性错误集，要求全部预期诊断出现；有意概率检测单列，不按确定性要求评价。
2. 对合法反例集，要求无已知误报；所有 suppressions 必须有解释和独立记录。
3. 不允许 unsupported／tool OOM／记录丢失静默变成 clean。
4. 诊断必须至少给出 kernel／PC、空间、宽度、线程／block、对象信息；具备lineinfo时给源定位。
5. 按声明的保证等级验证地址重用、atomic pointer、mask、async free和barrier终止：B阶段记录其复用UAF边界，D2阶段要求身份链支持范围内的真实检出。
6. 性能阈值在完成第一轮真机基线后确定；每次优化都同时给前后覆盖和资源数据。

### 18.6 性能实验流程

固定频率／功耗配置并记录环境；先 warm-up 和单独记录首次 JIT，再做多轮重复。每轮同时收集 kernel 时间与端到端时间；长尾 workload 单独展示。

对两种方案只在相同输入、相同支持范围、相同并发模式下比较。超时不从总表消失；共同可完成子集可以提供单独表，但必须显示其占原始集合多少。

误报和漏报调查应先缩小输入／opcode，再使用 cmodel、反汇编和时间线定位。不能仅因为“加同步后错误消失”就认定原检测器正确。

## 19. 分阶段研发计划和团队安排

### 19.1 推荐交付阶段

下表时间是有经验团队、已有硬件／基础栈条件下的规划区间，表示主动开发日历跨度，不包含等待硬件、长周期平台故障或所有阶段的简单串行总和。

| 阶段 | 范围 | 建议投入 | 预计跨度 | 退出证据 |
|---|---|---|---|---|
| M0 能力摸底 | GPUVA、ISA、abort、allocator、现有代码覆盖 | 2–3人 | 2–3周 | 接口清单、实际限制、预算修订 |
| M1 窄原型 | 最小P＋B global＋A小用例oracle | 3–4人 | 6–10周 | 真机报错、确认B的地址reuse边界、首轮性能 |
| M2 内存检查内测版 | P＋B完善、shared/private、API、诊断与覆盖 | 5–7人 | 再10–16周 | 支持矩阵闭合、应用样例、资源可控 |
| M3 对象语义 | D1先行，再做受控D2与pool/view | 5–8人 | 16–28周 | 相邻对象／旧指针／atomic边界通过 |
| M4 独立checker | init、sync、shared race逐项推进 | 3–6人/工作流 | 每项8–16周量级 | 各自覆盖、误报和性能报告 |
| M5 生态补充 | E中binary fallback与JIT/外部库 | 4–6人 | 10–18周 | 混合边界无静默漏检 |
| M6 硬件研究 | profile＋cmodel／时序模型＋ISA/DV方案 | 跨架构团队 | 单独排期 | 与软件基线的同口径收益 |

M1 是有意缩小的原型，不能把 M1 的周期或代码量当成第16章完整 P＋B 的承诺。M3 和 M4 部分工作可并行，但公共元数据和 ABI 需要稳定版本。

### 19.2 团队职责建议

| 角色 | 核心责任 |
|---|---|
| LLVM负责人 | 插桩时机、优化交互、目标指令覆盖、provenance |
| 设备runtime负责人 | shadow／对象表、SIMT安全、错误通道和性能 |
| UMD/runtime负责人 | 生命周期、queue/graph、模块、外部资源 |
| cmodel/架构负责人 | ISA语义、独立oracle、终止和新硬件可行性 |
| 测试与性能负责人 | 反例、真实workload、可复现数据和回归 |
| 工具链集成人员 | CLI、符号化、安装、CI与用户文档 |

人数少时可以兼任，但不宜让一个人同时独自实现检查器并独自定义全部正确性 oracle。D2、异步生命周期和错误终止都值得交叉评审。

### 19.3 每个阶段必须留下什么

每阶段交付包括版本化文档、源码、测试清单、运行命令、输入hash、原始性能数据、已知限制及复现报告。公共 ABI 和 module capability manifest 的变更应记录兼容策略。

由于本任务已有明确的 GitHub 持续保存要求，建议在仓库以正文、证据笔记和进度记录共同维护；发生中断时从已提交的内容接续，不依赖聊天中某段未保存输出。

## 20. 主要风险与决策条件

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
| 把论文原型当可直接移植软件 | 计划低估、关键组件不可获得 | 核验artifact、实现开放范围和依赖 [P09] |
| 追逐最低平均overhead | 忽略长尾和实际业务 | 使用自己的应用分布与端到端基线 |
| 过早设计硬件 | 芯片投入与真正瓶颈错位 | 软件profile先证明需求 |

## 21. 架构评审时应确认的项目参数

以下清单用于后续把方案落实到用户实际 GPU；它不是本轮交付的阻塞问题。

1. LLVM／Clang 分支版本，是否使用自定义中间表示或二次后端。
2. GPUVA有效位数、generic pointer、不同空间aperture及可用tag位。
3. warp/wave大小、独立线程调度、谓词、whole-wave执行语义。
4. 全部scalar/vector/atomic/async/tensor访存指令及修饰符。
5. 原子宽度、memory order/scope和支持的同步原语。
6. Host分配、设备分配、pool、VMM、managed、IPC、peer、图和JIT API清单。
7. UMD／KMD／固件可提供的终止、fault、映射和完成证据。
8. cmodel是功能模型还是时序模型；能否表达多调度和弱内存。
9. 真正要支持的语言／框架与闭源模块比例。
10. 第一版是开发者工具、CI工具，还是更强的运行时防护需求。
11. 必须支持的地址空间和错误类型，特别是对象／子对象与复用UAF。
12. 可投入团队、可用真机、允许的显存和性能预算。

回答这些参数后，应优先修订第16章的估算和第19章排期，再进入具体实现设计。本轮结论已经足以决定首个原型的方向：公共P＋真机B＋独立A校验，以及明确预留D的对象身份协议。

## 22. 参考文献、阅读路径与核验记录

### 22.1 官方文档与上游源码

本节所有在线资料均在 **2026-10-20** 核验。未提供可靠单篇发布日期的动态文档，以版本与访问日为准；不把抓取日当成发布日期。

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

### 22.2 原始论文与可复现材料

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

### 22.3 建议阅读次序

| 读者／任务 | 优先资料 | 阅读后应产出 |
|---|---|---|
| 技术负责人做选型 | N02/N05、A01、P01、P04 | 覆盖目标与路线条件 |
| LLVM后端工程师 | L05/L06、L03、P01、P08 | 插桩点、目标指令清单、身份传播边界 |
| UMD/runtime工程师 | N03、A01/A02、I01/I02、本文第8章 | 分配和命令生效协议、资源预算 |
| cmodel/架构工程师 | L06、P02/P03、P04 | 模型事件、同步语义、硬件前提 |
| 测试/性能工程师 | P03、P06、P08、本文第17/18章 | 数据集分母、三基线、失败与跳过记录 |
| 产品化与部署 | N05/N07、I02、M01/M02 | 版本矩阵、已知限制、错误处理策略 |

### 22.4 检索方法和来源选择

实际使用互联网检索服务的 system1/system2 引擎、原始页面打开、官方站内链接追踪、论文全文检索及必要的PDF文本提取。权威产品条件优先使用官方来源；关键论文机制和数字回到作者全文；未使用个人博客作为设计事实依据。

以下为本轮实际执行的代表性查询原文；它们是检索日志节选，不是穷尽全部搜索或可用于PRISMA的统计：

| 日期 | 引擎 | 实际查询 | 目的 |
|---|---|---|---|
| 2026-10-20 | system1 | site.docs.nvidia.com/compute-sanitizer "Compile-time patching" | 定位编译插桩手册；首个site限制语法不规范，后续以规范URL直接核验 |
| 2026-10-20 | system1 | site:developer.nvidia.com/blog "Compile-Time" "Sanitizer" | 找到2025-12官方说明 |
| 2026-10-20 | system1 | site:arxiv.org GPUArmor memory safety GPU 2026 | 发现版本、近期基准与相关论文 |
| 2026-10-20 | system1 | site:clang.llvm.org AddressSanitizer HardwareAssistedAddressSanitizerDesign | 定位上游机制文档 |

搜索引擎没有为本调研提供可验证的全库命中总数，因此没有虚构“筛选了N篇论文”的统计。已选来源按同一论文的预印本／正式版本去重理解；GPUArmor P04/P05保留两个记录仅用于版本比较。

### 22.5 核验中保留的分歧与限制

- NVIDIA产品总览与新版initcheck章节存在更新不同步，正文采用新版手册和release notes。
- HiRace正式稿的一处百分比表述与Table I使用的分母不一致，本文没有引用“93%”作为positive recall。
- cuCatch和GPUArmor的“BST”含义不同，正文已明确；两篇的性能成本也不等于包含所有Host wrapper的应用端到端时间。
- GPUArmor作者页面、期刊online日期和文稿模板页眉不同，参考文献采用期刊元数据，性能机制采用已读作者全文。
- 对MUSA内部插桩机制，当前公开证据不足以确认用户参考描述的具体架构；正文保留可证实的功能，不补写内部实现。
- cmodel、真机和论文模拟器承担不同证据角色；本文未运行用户硬件，也未声称完成论文复现。
- 第16章估算已检查组件加总和复用关系；实际预算仍需用户仓库与ISA参数支持。

### 22.6 文档完成标准与后续入口

本版已覆盖研究背景、友商、机制、量化结果边界、六条实现路线、compiler／device runtime／UMD／cmodel／tooling改动、代码量、内存性能、验收和阶段计划。

下一步若进入实现，应以第21章的实际平台参数收敛M0/M1设计；把选定路线转成具体目录、接口、opcode和测试工作包。本版是一份可用于该决策的调研文档，尚不是已实现的GPU sanitizer。





