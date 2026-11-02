# GPU Memcheck 软件方案全维度对比：NV-R / B1 / B2 / NV-C / cuCatch / CuSafe

**研究增补：2026-11-02。性质：设计与证据比较，不代表自研 GPU 已实现；目标设备状态 GPU_NOT_RUN。**

> **范围与权威边界**：本页比较六种可在现有或自研 GPU 上通过软件实现的 Memcheck 路径；Intel oneAPI ASan 作为 B2 家族的成熟产品参照。**不纳入 Vulkan GPU-AV、GPUArmor、CHERI-SIMT 的选型或预算**。本页不是新的核心算法计数表：成熟厂商主参考仍是 NVIDIA 和 AMD，三种核心参考配置仍为 NV-R、AMD-B2、NV-C，基础空间判定仍分当前地址与原对象来源两层。分类冲突以[核心分类总表](core-design-comparison.md)为准，通用接口/代码以[组件指南](real-gpu-components-implementation.md)为准，正式软件数字始终以[v1.8 预算](estimates-v1.8.json)为准。不得把论文原型行数和预算混为一谈。

## 1. 阅读和证据约定

每种方案统一说明：**检查发生的阶段、GPU 实际检查算法、元数据来源与生命周期、能发现及会漏掉的错误、部署/ABI/硬件约束、运行与内存性能、各组件修改、工作量和可验收实验**。原理上可行、供应商已发布、论文实测、自研尚未实现，是不同状态。

| 标识 | 含义 |
|---|---|
| 厂商确认 | 对指定产品及文档版本有效；不能外推至自研 ISA 或未支持的设备 |
| 论文结果 | 仅对应论文公布的 GPU、工具版本、工作负载、检测集与统计口径 |
| 自研设计 | 需编写 Compiler、Device Checker、UMD 等目标代码，不能当作已开发 |
| 方案保证 | 仅限明确登记的内存类型、已插桩访存、具备设备可见元数据和可终止的错误路径 |
| GPU_NOT_RUN | 自研目标硬件没有实际运行并留下设备报告；CModel/全系统仿真通过不能替代芯片验收 |

缩写：UMD 为用户态驱动/运行时，KMD 为内核态驱动，GPUVA 为设备虚拟地址；Device Checker 是在真实 GPU 或执行其目标 ISA 的模型上运行的检查代码，**即使其开发预算归属 Compiler，也不是 CPU 侧静态检查**。本页的 NV-C 指 NVIDIA 编译期 Original-Object Bounds；仓库“路线 C”专指运行时 Binary Rewriter，二者不同。

## 2. 先把独立的设计维度拆开

**维度一——检查在哪里注入**：NV-R 在已编译机器码装载/运行阶段插入；B1、B2、NV-C、cuCatch、CuSafe 通常在编译工具链插入。注入阶段决定旧 Binary 能否覆盖、是否需要 Rewriter、编译器优化和 ISA 兼容范围，**不能单独决定空间检测的数学强度**。

**维度二——当前地址如何判定**：

- B1（可作为自研 NV-R 的同语义 Backend）：存在一个当前有效且权限允许的逻辑 Allocation A，使完整访问区间 [addr, addr+width) 包含于 [A.base, A.end)；设备索引可以是排序表、Radix 或其他结构，NVIDIA 内部索引不可据此假定。
- B2：访问涉及的**每一个实际字节**对应的 Shadow/Poison 状态均允许访问；可使用经典 1:8 前缀编码，也可在满足布局约束时研究 1:16/1:32，不是说每一字节查询一条 Allocation。
- 两者都不知道原指针来自哪次 Allocation。若 B1 强制完整访问落在同一 A，而 B2 只保证每字节有效，则跨越两个无保护间隔、同时存活对象的**宽访问**，B1 可比纯 B2 更严格；若有效大对象中间有显式 Poison，B2 可比仅有整块区间的 B1 更严格。

**维度三——原始来源（Provenance）**：NV-C/D1 让原对象 base/end（或关联 Token）随指针在受支持的 SSA、GEP、参数和 ABI 路径传播；即使地址跑进存活对象 B，也能识别“原来源 A”的非法访问。来源不明时必须记录降级，不能反查当前 B 并声称保留原来源。

**维度四——时间身份**：旧指针与新分配对象可有完全相同 GPUVA。仅保存当前 Bounds 或 Poison 的 B1/B2/NV-R、仅保存不带代际的 D1 都不能保证识别地址复用后的旧指针。cuCatch/CuSafe 的 Tag、代际、地址随机化等提供额外时间安全能力，但需明确 tag 碰撞、复用/别名及 ABI 范围，不能宣称不受条件限制的绝对 UAF 检出。

**统一前提**：合法地址与权限必须针对正确 Context、Memory Space、实执行 Lane/Element Mask、宽度及相应 Queue 顺序。未登记的外部内存不应假装 OOB；无元数据/未知指令/资源不足不能记 CLEAN。一次非法 Store 被报告后还无条件执行原 Store，不构成可靠的阻止型 Memcheck。

## 3. NV-R：运行时 GPU Binary Instrumentation + 当前地址检查

### 3.1 实现原理与执行链

普通 CUDA/GPU 源码经编译生成机器码 → Runtime 在 Module 装载/执行时识别 Global/Shared/Local 等实际机器访存及 Predicate/Width → 在访存前插入设备可执行的 Patch Stub → Checker 读取当时有效的地址元数据与权限 → 合法执行原指令，非法诊断并进入可靠的失败路径。目标 GPU 的 Rewriter 需要 Decoder、Encoder、CFG/跳转及重定位修复、寄存器/Scratch/资源重算、Patch Cache、原始 PC 映射。**生产 Compiler 可以不修改**，编译 Patch 模块不等于修改原 Compiler。

NVIDIA 官方确认 Memcheck 默认使用运行时二进制插桩，公开了 [Compute Sanitizer Patching API](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html#patching-api)，但**未公开完整私有 Rewriter 和全部 Allocation Lookup 数据结构**；自研 B1 式区间查找只能称为等价语义设计，不能声称就是 NVIDIA 的内部实现。[NVIDIA 手册](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compile-time-patching)。

### 3.2 检测效果与根本盲区

在已插桩、已登记且生命周期正确的访存中，可发现 Global 越界、访问宽度跨界、释放后尚未复用等当前地址无效事件。NVIDIA **完整产品**还公开支持 Global/Shared/Local 的越界和非对齐访问、部分硬件异常与泄漏报告，但不能将产品功能自动归功于单一 Bounds 谓词。来源 A 的指针若完整落入仍有效的 B，或地址复用后的旧指针完整落入新分配，基础当前地址检查无法保证发现。未知 ISA、外部/Peer/Pool 未登记区域或无法改写的指令必须明确部分覆盖。

### 3.3 部署、性能和开发约束

不需要重新编译用户 Binary，适合闭源或多编译器生态；代价是需精确支持目标 ISA 版本、调用/分支/Predicate/Atomic/Async 语义，且 Patch 增加寄存器和代码大小。每条动态访存仍要运行实际查询；每次 Module Patch 可能有启动/缓存成本。真实 NVIDIA 与自研性能不可直接相等；自研 NV-R 运行开销未测。基础 KMD/固件映射与 Abort 充分时可共用，若缺少受控可执行重映射接口需额外补充。

### 3.4 工作量与验收

正式预算映射到 **P+C**：核心 31–60kLOC，手写测试 27–51kLOC，均为三大生产软件组总量；**纯运行时方案生产 Compiler 新增可为 0**，设备 Patch/Rewriter 归 UMD/运行时工具。缺少成熟 Decoder/Encoder 基础时可能额外触发 v1.8 单列的 15–35k 核心及 15–40k 测试条件项。验收须保留原/改写 Binary 的控制流一致性、实际 ISA 访存清单、未知 Opcode 拒绝、合法/非法 S01/S02、PC 映射、Abort 及最终 GPU 设备报告。

## 4. B1：LLVM 编译期插桩 + Live Allocation 区间查询（自研变体）

### 4.1 实现原理与执行链

LLVM IR/目标后端确定每次 Memory Effect 的地址、宽度、地址空间、活动 Lane/Element → 插入设备 Checker → 在 GPU 上查询当前 Live Allocation Index（例如区间树/排序索引/分层索引）→ 获取候选对象 base/end、权限及执行时生命周期 → 验证整次访问位于**一个**被允许的逻辑对象范围内。UMD 的 DebugAllocator 在 alloc/free/pool/graph 节点更新 GPU 可见元数据，并按 Queue/Fence 发布版本；Checker 不应每次返回 CPU 查询。

### 4.2 检测效果与根本盲区

若当前地址集合与 NV-R 相同、查询谓词/元数据/访存覆盖相同，B1 与自研 NV-R 的**基础地址检测语义可以完全相等**。B1 的严格单对象范围检查可以拒绝从 A 尾部跨入紧邻 B 的一次宽访存；对一个完全落在活 B 的 A 派生地址，仍无法恢复来源 A。内部分块已失效但外层 Allocation 仍 Live 时，若 Registry 只登记外层对象会漏检；要求登记真实子对象或补细粒度状态。已复用地址的陈旧指针仍需要额外身份。

### 4.3 部署、性能和开发约束

要求能够重编译受保护的 GPU Kernel，并保证 LLVM IR/MIR 及最终 ISA 的访存覆盖一致。优势是无需独立 Binary Rewriter，可让 Compiler 优化多次索引查询（仅在能证明对象状态和实际访问区间不变化时）。索引开销主要随**活跃对象数量、分布及缓存命中**变化，单次普通访问可能包含多次随机元数据读取；不应未经实测断言一定比 B2 快。Debug Allocator、对象索引的并发版本、GPU 元数据可读和未知外部内存是主要 UMD 条件。

### 4.4 工作量与验收

现有 v1.8 只对 **B（B1/B2 的替代包）**统一估算 P+B：核心 25–48kLOC、手写测试 21–39kLOC，**没有独立 B1 数字**；不可声称与 B2 开发量相等。需分列 LLVM 检查插桩、GPU 区间 Lookup、UMD 索引更新/发布/退休和实际 Query Cache。验收重点：同 Allocation 末尾、跨相邻双 Live Allocation 的宽访问、对象数 1/1k/100k、非活动 Lane、异步 alloc/free、缺索引节点、精确原始 PC 与安全失败。

## 5. B2：LLVM 编译期插桩 + Shadow/Redzone/Poison（AMD 参考）

### 5.1 实现原理与执行链

LLVM 在最终会发生的设备 Load/Store/Atomic 前插检查 → 设备以映射规则找到本次实际访问字节对应的 Shadow Granule → 检查 0/有效前缀/Poison → 合法才执行原访问。经典 ASan 1:8 用 1 B Shadow 描述 8 B 应用内存；对象起点对齐、末尾部分 Granule 和保护区必须正确维护。UMD 除公共 Allocation Registry 外，还管理 ShadowManager、Redzone、Poison/Unpoison、Shadow GPUVA 映射和执行时同步。[LLVM ASan](https://clang.llvm.org/docs/AddressSanitizer.html)、[ROCm GPU ASan](https://rocmdocs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)。

### 5.2 检测效果与根本盲区

在正确插桩和 Poison 条件下能发现头尾越界、部分尾字节错误以及 Free 后未复用的访问；还可为一个大 Allocation 内局部失效区域建模。经典单字节前缀 Shadow **不能在同一 Granule 中表示任意有效—无效—有效组合**。若相邻 A/B 都有效且没有 Poison 间隔，纯每字节可访问性检查可能放行跨界宽访存；从 A 完整跳入有效 B、复用地址后的旧指针同样不能保证检出。实际 AMD 产品的 Private/Stack 与部分 LDS 覆盖限制属于**当前实现限制**，不等于 Shadow 算法数学上无法支持这些空间。

### 5.3 部署、性能和开发约束

GPU 每次检查增加 Shadow 地址计算、元数据读取、条件判断及寄存器压力；内存成本随 Shadow 逻辑覆盖范围、Redzone 和映射实现而变。在 AMD 官方受支持的 GPU ASan 环境，需匹配的 xnack+、运行库和平台条件；**自研** B2 不必复制 XNACK，但必须在 Shadow Load 前证明映射已建立、页面已驻留且 Metadata Ready。无可恢复缺页时可采用预映射或安全 Root/Leaf 索引，不能让 Checker 自身先 Fault。ROCm 官方文档指出某些工作负载会有 3× 或更高运行时开销，这是特定产品的经验范围，不是自研 B2 预测。[无 XNACK 专题](amd-gpu-asan-hardware-fallback.md)。

### 5.4 1:8、1:16、1:32 的正确对照

如果采用单字节**有效前缀编码**，1:16 和 1:32 在每个对象按相应 Granule 边界起始、各 Granule 只有全有效/全无效/前缀有效这三种可表示状态时可行；对象大小本身不必为 16/32 的整数倍，但尾部必须正确编码。**仅对象起点对齐仍不够**：子对象、栈、共享内存、多段映射及权限洞也必须满足相同状态约束，否则需更细的 Shadow 或额外对象描述符。比例为 1:G 时逻辑 Shadow 容量约为覆盖字节数的 1/G，**不等于整体 HBM 或 Allocator 内存必按相同比例下降**；对齐、保护区和物理页可能抵消收益。跨 Granule 的非对齐宽访问必须逐实际字节/范围检查。

### 5.5 工作量与验收

同 B1，现有仅有 **P+B** 的核心 25–48kLOC、测试 21–39kLOC 联合规划，没有独立 B2 数字。要额外明确 ShadowManager、对齐/Poison 更新、映射存在性以及跨 Queue 失效顺序。验收包括 S01 头尾、非 8 的整数倍对象、宽访问跨 Granule、失效 Shadow、极远野指针、未复用 UAF、Pool/External、错误时不执行原 Store、相同硬件上的 1:8/1:16/1:32 完整内存成本和速度。

## 6. NV-C：编译期 Original-Object Base-and-Bounds（映射 D1）

### 6.1 实现原理与执行链

编译期不仅插入检查，还将指针关联其最初来源对象的 base/end（概念 Fat Pointer/来源 Token），让这组信息沿受支持的 SSA/GEP/PHI/SELECT、调用、参数、存取与 ABI 传播；每次实际 GPU 访存检查 [addr,addr+width) 是否属于**原来那个对象**，而非任意当前有效对象。NVIDIA CUDA 13.1+ 的编译期 Memcheck 已公开 Base-and-Bounds 思路；具体私有编译器表示与 Runtime 不可直接照搬。[官方原理](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#base-and-bounds-analysis)、[NVIDIA 技术说明](https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/)。

### 6.2 检测效果与根本盲区

来源可靠时，A 派生指针越界后**完整落入另一个存活对象 B**仍可发现，这是与仅查当前地址的 B1/B2/NV-R 的根本差异。若来源丢失，NVIDIA 官方方案可回退普通 Allocation Bounds，必须把强度降级明确写出。纯 base/end **不等于时间身份**：对象 A 被释放，新对象 B 复用相同 base/end 时，旧指针仍可能通过；必须叠加 Allocation ID/Epoch 或其他 D2 方案。逻辑 Tensor View/结构体字段要获得子对象语义还需要单独的 shape/field 来源保证。

### 6.3 部署、性能和开发约束

要求重新编译用户程序、定义 Pointer Metadata 在 ABI/存储/外部库/整数转换中如何传播；不认识的路径不得标完整来源保护。扩充的指针状态可能提高寄存器、栈与代码大小，但编译器可做冗余检查优化。NVIDIA 公布某些工作负载编译期 Memcheck 比默认 Runtime Binary 更快，**此比较同时改变了插桩阶段与来源分析，不能当作单纯 B1 对 NV-R 的公平性能测量**。目标自研 GPU 速度与占用未测。

### 6.4 工作量与验收

正式预算的 **D 包含 D1 与受控 D2**：P+D 核心 46–89kLOC、手写测试 39–74kLOC。**不能把整个 D 当作纯 NV-C/D1 的单独成本**；当前没有可据以给出真实 D1 单独数值的源码审计。需将 Compiler Provenance、GPU Bounds Checker、UMD 参数 Sidecar/对象元数据、跨模块 ABI 与 D2 代际处理拆开评估。验收必须覆盖 S04 相邻存活对象、来源未知降级、参数与 PHI/SELECT、指针存储/载入和明确的地址复用反例。

## 7. cuCatch：Shadow Tagged Base-and-Bounds（论文研究原型）

### 7.1 实现原理与执行链

[PLDI 2023 cuCatch 论文](https://doi.org/10.1145/3591225)以 NVIDIA Backend 编译插桩和 Driver 协作为基础；通过 Base Pointer Analysis 尽早确定关联对象，使用 **Shadow Tagged Base-and-Bounds (Shadow TBB)**：常见设备分配可凭指针高位 Tag 索引 Base/Size/Tag Table（BST），超出可用 Tag 容量和 Managed Memory 等范围使用按指针值索引的 Shadow Map 回退，不要求捕捉全部“指针复制到哪一个变量”的事件。设备对原对象范围/标签进行检查，再将错误归因到访问位置。它的 Shadow Map **不是 B2 的每 8 字节可访问性 Poison 表**。

### 7.2 检测效果与根本盲区

在相关 Tag/Bounds 来源和生命周期正确时，提供强于纯当前地址检查的空间检测，并研究时间错误检出；论文区分确定性与概率性的检测保证，不能把所有 Tag/复用情形写成 100% 无条件捕获。自定义 Allocator 若只向 Runtime 暴露大块 Backing 而不暴露内部逻辑子分配，精准 Bounds 会缺失；CPU Allocation 经 ATS 直接迁入 GPU 的对象超出论文当时的完整实现覆盖，作者明确列为扩展工作。未插桩 Binary 不能自动获得论文的 Backend 检查。

### 7.3 部署、性能和开发约束

需要特定的 Backend 编译插桩、Tagged Pointer/BST 元数据协议、Driver 分配/释放 Hook、GPU 侧 Lookup 及 Tag 生命周期；如果目标 GPUVA 高位、指针表示或 Metadata 可见性不支持，必须重新设计 ABI/映射。论文测得**几何平均 1.19×（19%）运行时开销**；报告 BST 与 Shadow Map 第一层固定开销共约 **160 MiB**，回退二级表有依分配规模增长的成本；某些 Kernel 的寄存器 Spill/Occupancy 也会受影响。数字属于 PLDI 2023 的 NVIDIA 实验，不是自研 GPU 预测。[论文](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)。

### 7.4 工作量与验收

v1.8 **没有 cuCatch 单独 kLOC 预算**，不得将 D 包直接贴给它，也不得把论文算法视为可直接商用的现成开源 UMD。开发任务需单列 Compiler Backend/Base Pointer Analysis、BST/Shadow-TBB Device Checker、UMD Tag Allocator/Managed 回退、Custom Allocator 接入、ABI 和驱动契约。验收应包含 Tag 表容量边界、跨对象、随机指针、地址复用、Managed/Pool/ATS、未知来源降级、Pointer Copy、Registry 并发和性能。

## 8. CuSafe：Pointer Tag + In-band Exact Bounds（2026 研究实现）

### 8.1 实现原理与执行链

[USENIX Security 2026 CuSafe](https://www.usenix.org/conference/usenixsecurity26/presentation/lu-hongyi)使用 LLVM 21 Transform Pass 和 CUDA API 动态 Hook，把 Pointer Tag/对齐信息和**分配对象附近的 In-band 精确 Bounds**联系起来，利用 GPU 访存广播降低同一对象的元数据读取代价；对 Global 用虚拟地址布局/随机化处理时间身份混淆，对 Local/Stack 使用 Epoch；Compiler 插入访问检查及部分冗余消除。**与 cuCatch 的外置 BST/Shadow TBB 不是同一存储协议**，也不是仅按当前地址计算 B1 Bounds。

### 8.2 检测效果与根本盲区

论文 33 个安全测试（空间与时间）在所评估集合内取得较强覆盖，**不能外推为所有 GPU 程序、所有 UAF 永不漏报**。论文明确指出：仅记录对象级 Bounds 时无法发现同一结构体两个字段之间的内部越界；某些越出对齐包络、随后折返的合法指针运算会被判非法（误报边界）；未插桩闭源 Binary 仍存在检查覆盖缺口，外部库互操作需要 Tag 处理。地址随机化和 Epoch 缓解元数据混淆，但取决于具体复用、Tag 和地址空间政策。

### 8.3 部署、性能和开发约束

需要 Compiler/Pointer 语义适配、能够承载 Tag 的 GPUVA/MMU 约定、UMD 的 In-band 元数据与对齐 VA 布局、分配 Hook、Stack Epoch 及跨外部模块 ABI；**不等于换一个 Device Checker 即可复用现有 B1 Allocator**。论文报告 44 个程序的平均运行时间增加 **13%**、平均总体内存增加约 **0.3%**；其结构化元数据分析还给出约 16.5 MiB 固定 Stack Epoch 支出与每 Allocation 8 B In-band Size，不应将论文示意的虚拟地址对齐误读成同规模的物理显存浪费。结果依赖 NVIDIA GPU 与论文负载，不能和 cuCatch 不同基准下的 19% 直接排名。[CuSafe 原文](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)。

### 8.4 工作量与验收

论文原型报告 **2,964 行 C/C++**（LLVM Pass 与 CUDA Hook），**这是研究原型实现规模，不是目标自研 GPU 的全产品代码量**；不含把现有 ISA/UMD/KMD、错误终止、28 例覆盖和跨平台维护全部交付的预算。v1.8 没有 CuSafe 的正式 kLOC。应独立拆分 Tag/Pointer ABI、VA 约束核查、In-band Allocator、Epoch、GPU Device Checker、外部库兼容、SIMT/Queue 和错误终态。验收包含对齐边界、跨对象、反向指针算术、同 GPUVA 复用、Stack Epoch/溢出、闭源库交互、随机地址与报告。

## 9. Intel oneAPI Device ASan：B2 家族的产品级工程对照

[Intel oneAPI 2026.0 Device-Side Sanitizers](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/device-side-compiler-sanitizers.html)列出 SYCL/OpenMP GPU ASan 的设备 Global、Local、Private、USM 越界、UAF、非对齐等功能；[运行平台与局限](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/compiler-sanitizers.html)提示 Ponte Vecchio/Linux 等平台范围、ASan 时 Kernel 并行执行限制，以及寄存器/Private 资源增加带来的 Workgroup 约束。它是**成熟产品覆盖和 UMD/Unified Runtime 接线的参照**，不因另一家厂商具有更多工具功能就创建第四种基础空间数学谓词。本仓库的厂商 Profile 版本证据仍以[结构化登记](research-notes/vendors_sources.json)为准；这里不修改其版本基线，不用 Intel 的产品表推算自研 B2 已实现的地址空间。

## 10. 检测能力与效果矩阵：原理保证、额外条件和已知漏报

下表**仅比较各方案已声明支持并实际插桩的访存**。符号：✓ 机制在列明前提下可识别；△ 依赖附加元数据、适配或概率条件；— 单靠该基础机制不可保证。

| 场景 | NV-R 基础 | B1 严格区间 | B2 字节 Shadow | NV-C/D1 | cuCatch | CuSafe |
|---|---|---|---|---|---|---|
| 头尾越界，落入无效地址/Redzone | ✓ | ✓ | ✓ | ✓（有来源或正常回退） | ✓（已跟踪对象） | ✓（已跟踪对象） |
| 单次宽访问跨紧邻的两个 Live 对象、无中间 Poison | △（内部判定未公开） | ✓（单对象检查） | —（仅字节均可读） | ✓（来源已知） | ✓（Bounds/Tag 完整） | ✓（Pointer Tag/Bounds 完整） |
| 从 A 派生指针完整落入 Live B | — | — | — | ✓（来源已知） | △（来源/Tag 有效） | △（Tag/Bounds 有效） |
| 已知大 Allocation 内部人为 Poison 的子区间 | △（需细粒度表） | △（需登记子对象） | ✓（可表示的 Poison） | △（需子对象来源） | △（需子对象描述） | △（论文对象级 Bounds） |
| Free 后尚未复用、元数据已生效 | ✓ | ✓ | ✓ | △（需当前生命周期检查） | △（Tag/状态正确） | △（元数据失效正确） |
| 地址复用后旧指针、相同 GPUVA | — | — | — | —（纯 D1） | △（身份/Tag 有效且无碰撞） | △（随机化/Epoch/Tag 前提） |
| Global 以外的 Shared/Local/Private | 产品有能力，非该谓词自动提供 | △ | △ | △ | 论文支持其子集 | 论文支持其子集 |
| 未重编译的已有 GPU Binary | ✓（ISA Patch 支持） | — | — | — | — | — |
| Race / Init / Sync | —（需其他工具） | — | — | — | — | — |

**两个决定性反例**：A=[0x1000,0x1040)、B=[0x1040,0x1080) 都 Live。来自 A 的 4 B 访问若起始 0x103E，严格 B1 可因跨单对象拒绝，而无 Poison 间隔的纯 B2 可能通过；若起始 0x1040，则 B1 与 B2 可能都通过，只有真正保留来源 A 的 D1/Tag 方案才能识别。第二个反例是 Free(A) 后 B 复用完全相同 base/end，裸地址、普通 Shadow 和纯 Bounds 无法区别新旧指针。

不能把“实验中检测 33/33”替换成“全部真实程序均可检测”，也不能把 NVIDIA 完整 Memcheck 具有的硬件异常、对齐和 Local/Shared 诊断等功能等同于 NV-R 的单一 Allocation 谓词。

## 11. 性能差异：只比较机制，不跨论文做伪排名

| 方案 | 每次访问的主要增量 | 元数据/占用的主要来源 | 可引用的观察值 | 对自研 GPU 的判断 |
|---|---|---|---|---|
| NV-R | Patch Stub、寄存器保护、当前地址查询；装载时 Patch | 地址表、Patch Cache、报告 | 官方说明会增加运行时成本；不提供统一跨平台比例 | 未实测 |
| B1 | LLVM 检查指令 + Interval 索引查找 | 活对象描述符、索引/缓存 | **没有独立公开可比的自研测量** | 未实测 |
| B2 | Shadow 地址计算、Load、Poison 分支 | Shadow 逻辑覆盖区、保护区、实际映射页 | AMD 官方文档描述特定工作负载可慢 3× 或更多；非统一平均 | 未实测 |
| NV-C/D1 | Bounds/来源传播、比较、可能的寄存器和栈成本 | 指针来源 Sidecar/元数据 | NVIDIA 公布编译期 Memcheck 在部分负载显著加速；不是纯 B1 对照 | 未实测 |
| cuCatch | Tag/BST 或 Shadow Map 查询、检查、额外寄存器 | 论文示例固定约 160 MiB，回退映射增量 | PLDI 2023 几何均值 **1.19×** | 不可直接外推 |
| CuSafe | Tag 解码、In-band Size Load、检查；可编译期消冗余 | 论文示例固定约 16.5 MiB，加每对象 8 B | USENIX 2026 44 负载平均 **+13%**，整体内存约 **+0.3%** | 不可直接外推 |
| Intel GPU ASan（B2 参照） | Device ASan 检查及 Runtime 支出 | Shadow、私有资源、调度限制 | 官方没有可直接与其余方案同条件比较的统一数字 | 不可直接外推 |

**数字不能直接排序**：cuCatch 与 CuSafe 不同 GPU、论文测试集、优化、计时范围及 Compiler/Runtime 版本；AMD 的 3× 是可能的个别工作负载描述，不是总体几何平均；NVIDIA 新编译期和旧运行期的比较又同时改变算法/工具版本。对自研 GPU 必须在同一 ISA、相同输入、同一错误检测强度、相同优化/原型状态下采集真实性能。

建议至少报告：① 编译时间；② Module 装载/Patch 延迟；③ Kernel 执行时间相对基线；④ 设备指令数和 Shadow/Bounds 查询次数；⑤ 寄存器、Spill、Occupancy、Lane 分歧；⑥ Metadata/Allocator/实际 HBM 用量；⑦ Cache/TLB 未命中；⑧ 不同 Allocation 数量与分布；⑨ 同一时序的异常诊断与错误后完成。不要只用每访问算法复杂度推断实际运行性能。

## 12. 组件修改总表：共用什么、专属什么

| 方案 | Compiler/Backend | GPU Device Checker | UMD 专属部分 | KMD/Firmware/RTL | CModel/全系统仿真 |
|---|---|---|---|---|---|
| NV-R | 原生产 Compiler 可不改 | Patch Stub + 当前地址查找 | 独立 Binary Rewriter、Patch Cache、原 PC 关联；可共用 B1 Registry | 映射/可执行装载/Abort 有缺口才补；不要求专用 RTL | 能执行改写后 ISA 则通常不改 |
| B1 | LLVM 访存 Pass、最终 ISA 覆盖审计 | Live Interval Lookup | Object Index 创建、更新、退役 | 通用 Metadata BO/Fence/Abort 足够即不改 | 能执行 Checker ISA 则通常不改 |
| B2 | LLVM 访存 Pass、Shadow 地址计算 | Safe Shadow Lookup/Poison | ShadowManager、Redzone、Poison/Unpoison、映射/回收 | 无 XNACK 时必须安全预映射；现有接口不足才补 | 能执行 Checker ISA 与映射协议则通常不改 |
| NV-C/D1 | Provenance/Bounds SSA 与 ABI 传播 | Original-Object Bounds；来源未知降级 | 参数 Sidecar、对象描述符与能力协商 | 既有元数据/完成契约足够即不改 | 可执行新 Pointer/Metadata 语义则不改 |
| cuCatch | Backend Base Pointer Analysis/Tagged Checks | BST + Shadow-TBB 读与检查 | Tag 分配、BST、Managed 回退、Custom Allocator Hook | 如原 GPUVA/Tag 表能力不满足需补接口；不假设新 RTL | 若模型已有相关指针/元数据语义可不改 |
| CuSafe | LLVM Pass/指针 Tag 操作及优化 | In-band Bounds、Stack Epoch 检查 | 对齐 VA 策略、In-band Allocator、地址随机化、API Hook | 如果现有 GPUVA/MMU 别名语义不足则需改对应层；不假定总能零改 | 需要真实模拟目标 GPUVA/Tag 解释；已有语义时可不改 |

**共用底座**：UMD Allocation Registry、Context/Queue/Graph、元数据 Ready/Retire Fence、Report Buffer、Symbolizer/CLI、错误分类、GPU Device 检查 ABI、设备失败终止。**不得为了每种方案重复计算同一基础代码**。但 B1 对象索引、B2 Shadow/Poison、cuCatch BST、CuSafe In-band VA 布局属于不同的 Metadata Backend；不能仅统一接口就保证任意混用 Pointer ABI。NV-R 的主要独有投入是 Binary Rewriter。

**交付责任与执行位置分离**：B1/B2/NV-C 的 GPU Device Checker 通常归 Compiler 包预算但实际在 GPU 上执行；纯 NV-R 的 Patch/Checker 可归 UMD 运行时包。设备错误记录 Writer 随 Checker，Host Reporter、CLI 和 Symbolizer 归 UMD。KMD 若已有 BO/GPUVA/权限/完成/Abort 设施，新增源码可能为零；Firmware/RTL 不因软件 B1/B2/D1 自动需要修改。

## 13. 工作量：沿用正式 v1.8，不发明新报价

| 软件路线（公共底座 P 已计一次） | 核心 kLOC | 手写测试 kLOC | 解释 |
|---|---:|---:|---|
| P+B（B1 **或** B2） | 25–48 | 21–39 | 两者共用 B 预算，不代表 B1、B2 已独立量化 |
| P+C（NV-R 风格 Runtime Binary） | 31–60 | 27–51 | 纯 Runtime 生产 Compiler 代码可为 0，设备 Patch/Rewriter 计 UMD |
| P+D（D1 + 受控 D2） | 46–89 | 39–74 | **不是 NV-C/D1 单独报价**；D1 与 D2 尚未独立拆分 |
| P+E（B/D/受控 C 的共享集成） | 69–132 | 59–113 | 已去除部分共用代码；**不等于六种完整方案全部交付** |
| cuCatch | 未估算 | 未估算 | 现有 v1.8 无正式单独预算 |
| CuSafe | 未估算 | 未估算 | 论文 2,964 行原型 ≠ 完整自研 GPU 产品成本 |

### 13.1 现有正式预算可追溯的组件归属

为避免只给“整条路线”的总数而无法判断 Compiler 与 UMD 改动，把 v1.8 **已有的**公共 P 加路线增量拆到实际交付子项。表中格式为 **核心 / 手写测试**，单位 kLOC；这里是旧数字的责任重分类，**不是对 B1、B2、D1 的新估算**：

| 责任组件 | NV-R（P+C） | B1 或 B2（P+B，同一包） | NV-C 的参考 D 总包（P+D，仍含 D2） |
|---|---:|---:|---:|
| 生产 Compiler Pass/Backend | 0 / 0 | 8–15 / 7–13 | 19–36 / 16–31 |
| GPU Device Checker / Patch | 8–17 / 7–15（归 UMD） | 6–11 / 5–9（归 Compiler） | 11–22 / 9–18（归 Compiler） |
| UMD/SDK/Host Runtime/Loader | 18–33 / 15–28 | 8–15 / 6–11 | 11–21 / 9–17 |
| CLI / Host Reporter / Symbolizer | 5–10 / 5–8 | 3–7 / 3–6 | 5–10 / 5–8 |
| KMD（已有契约满足） | 0 / 0 | 0 / 0 | 0 / 0 |
| **核心代码总计** | **31–60** | **25–48** | **46–89** |
| **测试代码总计** | **27–51** | **21–39** | **39–74** |

上述“GPU Device Checker/Patch”是**实际 GPU 设备端运行代码**，只是交付预算归属不同：纯 NV-R 属 Runtime/UMD，B1/B2/D 属 Compiler 工具链。CLI 与 Host Reporter 均属 UMD；Device Error Writer 已包括在设备库，不再次计费。**NV-C/D1 没有独立数字、B1/B2 没有拆分数字，cuCatch/CuSafe 没有正式组件报价**，不能把这个表拆成未经审计的六份精确预算。

上表不含既有系统中确实缺失的 KMD/固件契约；对应项目的条件包详见[主研究 §16.8](llvm-gpu-sanitizer-research.md#168-kmd-条件增强与其他前提缺口)。也不含新建完整 GPU ISA/Compiler/KMD、芯片 RTL/DV 或硬件制造。

**若要真正比较六种的研发成本，仍须代码审计后单独拆出**：B1 索引 Backend、B2 Shadow Backend、NV-C/D1 不含 D2 的独立增量、cuCatch 的 BST/Tag、CuSafe 的 VA/In-band；再加上各自的 Compiler Pass、Device Runtime、UMD 和测试成本。不可未经拆分把 P+D 整包替换为 NV-C，或者将 P+B 的数值重复计算两次。行数只是规划辅助指标，实际复杂度还受目标 ISA、设备内存模型、Pointer ABI、同步和工具链成熟程度影响。

## 14. 三执行平台的验证链：何时可以不改 CModel

**前提**：功能性 CModel 和全系统仿真已经能执行包含 Checker 的**真实目标 GPU ISA**、Device Runtime、Predicate/Mask、GPUVA 元数据读取和报告写入。此时 B1/B2/NV-C 的主要产品改动可集中在 Compiler + Device Runtime + UMD，**没有必要为了使用 Memcheck 再往模拟器里增加第二套检查算法**。它们仍必须做验证：CModel 检查 ISA/逻辑功能，全系统仿真检查 UMD/KMD、映射/Queue/Fence 和错误后完成，真实芯片才检查真机 Fault、缓存/TLB/并发与实际性能。

| 阶段 | 必须检查的真实证据 | 不应声称 |
|---|---|---|
| CModel | 设备 ISA 实际执行了检查、S01 正反例、错误不执行原 Store、元数据可读 | “真实芯片已经通过” |
| 全系统仿真 | UMD/KMD、Metadata 映射、异步 Queue/Fence、报告与 Abort 语义在模型中一致 | “物理 HBM/cache/时序和实芯片性能已证实” |
| 实体 GPU | 原始 GPU 设备日志、版本、PC/Lane、同步/安全终止、性能基线 | “仅通过静态 CI 就是 GPU 检测成功” |

**例外**：cuCatch 的高位 Tag/BST、CuSafe 的 GPUVA/MMU 地址别名与指针位解释，若超出现有模型与实体 GPU 的地址语义，就需要更新模型/平台 ABI，不能因 CModel 能执行普通 Load 就默认支持。NV-R 需要模型可执行真实改写机器码，未知 Opcode 仍需拒绝。

## 15. 选择建议与可判定验收标准

| 主要需求/前提 | 首选或候选 | 为什么 | 何时不选 |
|---|---|---|---|
| 先在可控 LLVM 自研 GPU 上做 S01 的最小闭环 | B1 或 B2 | 无需独立 Rewriter；快速验证实际 GPU 检查链 | 元数据映射/可靠终止都缺失时先补公共底座 |
| 少量大对象、偏向精确完整 Allocation Bounds | B1 | Registry 的大小主要随对象数变化；宽访问可单对象验证 | 活对象数极多/索引随机读取开销过大时 |
| 需要 Redzone、Free 后 Poison、细粒度有效性 | B2 | Shadow 直接表示当前字节允许状态 | Shadow 物理映射和元数据生命周期代价不可接受时 |
| 要发现 A 指针落入另一个有效 B | NV-C/D1 或受支持 Tagged Bounds | 必须保留来源/关联对象，当前地址不足 | 来源 ABI 大量丢失且无法补齐时 |
| 地址复用后旧指针是重点 | cuCatch/CuSafe 或单独 D2 | 需要超过当前地址的时间身份信息 | 无法满足 Pointer Tag/ABI/VA 约束时不能承诺保证 |
| 大量未编译/闭源已有 Binary | NV-R | 运行时 Patch 覆盖有 ISA 支持的代码 | Rewriter/加载权限不可行或成本过高时 |
| 研究低额外显存的 Tagged In-band 模式 | CuSafe | 论文中有较低开销数据及真实 GPU 原型 | VA/Tag 约束不满足或闭源混合覆盖为硬需求时 |
| 参照多地址空间产品的实际功能列表 | Intel GPU ASan | 已发布 Global/Local/Private 等功能 | 不把 Intel 软件栈直接当作自研 GPU 实现 |

**统一验收矩阵**：S01 合法/头尾、S02 全宽度跨界、S04 A→Live B、S05 子对象或视图、S08 未复用 UAF、S09 同 GPUVA 复用、S25 受支持的 Atomic Pointer；还需专门验证非活动 Lane、跨 Granule、大小 1/2/4/8/16 B、Unmapped Metadata、日志满、Queue/Graph 重放、外部内存与所支持的 Address Space。每个测试都要标“机制保证 / 条件性 / 明确不支持”，保存原始设备报告与失败证据。

**推荐实施顺序**：先公共 P 与单一种 B 模式完成设备 S01，采用相同框架比较 B1 与 B2；取得同硬件性能后决定主 Backend；按需求加入 D1，地址复用问题再评估 D2/cuCatch/CuSafe；仅在必须支持旧 Binary 时投入 NV-R 式 C。此顺序是**工程建议，不是自研项目已经完成的事实**。

## 16. 原始材料与可复核范围

1. [NVIDIA Compute Sanitizer：默认 Runtime、Compile-Time 和 Base-and-Bounds](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compile-time-patching)；[NVIDIA 2025-12-10 技术文章](https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/)。
2. [ROCm LLVM GPU ASan](https://rocmdocs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)；[LLVM AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html)；本仓库[无 XNACK 的三种映射取舍](amd-gpu-asan-hardware-fallback.md)。
3. [Intel oneAPI 2026.0 Device-Side Compiler Sanitizers](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/device-side-compiler-sanitizers.html)与[适用平台/限制](https://www.intel.com/content/www/us/en/docs/dpcpp-cpp-compiler/developer-guide-reference/2026-0/compiler-sanitizers.html)。
4. [cuCatch，PLDI 2023 原文](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)；Tag/BST/Shadow Map、Custom Allocator/ATS 限制、几何平均运行开销。
5. [CuSafe，USENIX Security 2026 官方论文](https://www.usenix.org/conference/usenixsecurity26/presentation/lu-hongyi)及[原文 PDF](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)；33 个检测样本、44 个性能负载、In-band 元数据、ABI/误报约束。
6. [唯一分类与能力矩阵](core-design-comparison.md)、[唯一组件 ABI/伪代码](real-gpu-components-implementation.md)、[S01 真实场景](../samples/S01/README.md)、[v1.8 正式软件预算](estimates-v1.8.json)。

**最后的证据边界**：本页补齐的是“可行原理、已公开产品/论文证据与自研改动映射”，没有修改算法计数、正式预算、CUDA/HIP 样例或 CModel，也没有产生任何自研 GPU 的新实测。