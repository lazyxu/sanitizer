# AMD GPU AddressSanitizer：XNACK/HMM 硬件依赖、Shadow 映射取舍与软件替代

**版本：v2.6 · 2026-11-02 · 目标：明确AMD官方部署条件，以及自研实体GPU在缺少XNACK/HMM时实现B2软件检查所需的条件。**

本指南分别使用**ROCm 7.2.4机制文档**与**10.1.0发行/安装文档**，不把不同版本拼成一个支持矩阵。自研LLVM后端借鉴B2时，检查由真实GPU指令读取Shadow并判断；Host分配拦截、GPU页表已映射都不能单独证明字节级访存合法。**无XNACK可设计独立B2实现，但不能据此声称AMD官方GPU ASan支持该环境。**

**定位**：这是三种基础空间参考配置中的**② AMD-B2**硬件部署与软件后备专题；其余配置及能力比较在[核心统一分类](core-design-comparison.md)。本指南的 B1 是当前 Live 区间的实现变体，不等于 NV-C/D1 原对象 Bounds；D2 是时间身份增强，E 是集成策略。官方 ROCm `xnack+` 支持范围与自研 Shadow 的无 XNACK 方案必须分开。**版本化证据仅在** [`amd-rocm-gpu-asan` Profile](research-notes/vendors_sources.json) 及其 AMD/LLNL/预览来源中维护。

## 1. AMD当前实现的可核实事实

**2026-11-02版本复核：** [官方发布历史](https://rocm.docs.amd.com/en/docs-10.1.0/release/versions.html)列出ROCm **10.1.0于2026-10-05发布**；其[ASAN安装说明](https://rocm.docs.amd.com/en/docs-10.1.0/install/asan.html)限定预构建包为`gfx942`、`gfx950`及包含二者的`all`版本，独立安装于`/opt/rocm/core-asan-10.1`。这是一项**发行包约束**，不能用7.2.4中的`gfx90a:xnack+`示例推导10.1也提供对应包。10.1仍要求HMM、`HSA_XNACK=1`、编译插桩和插桩Runtime。

机制与具体缺口仍以[固定的7.2.4 GPU ASan文档](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)为依据：

- **执行机制：** 编译器在Load/Store/Atomic前插入Shadow读取和Poison判断，Runtime维护Redzone/Shadow并报告错误。经典1:8编码的裸Shadow比例为12.5%；页粒度、Redzone和其他元数据使总开销更高，不能用12.5%充当整套工具显存预算。[Runtime overhead](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html#runtime-overhead)
- **启用条件：** 该文档要求显式受支持的`xnack+`目标；其他目标会警告且不插桩设备代码。运行时还需HMM及匹配的ASan库，尤其是插桩HIP/HSA组件；编译成功不代表设备检查生效。[编译和运行要求](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html#compiling-for-asan)
- **报告识别：** GPU报告含`on amdgpu device`、PC、访问宽度、Workgroup及Lane地址，文档中的设备调用栈仅有一层。CPU ASan堆栈或API返回错误应另行分类。[Runtime reporting](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html#runtime-reporting)
- **已知缺口：** 有限Redzone可能被跳过；Private/Stack访问未插桩；HIP Shared/LDS通过Global建模并按Global报告；极野地址可能先在Shadow Load上Fault，NULL的Shadow也未被Poison。[Known issues](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html#known-issues-with-using-gpu-sanitizer)

[LLVM AddressSanitizer.cpp](https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html)提供AMDGPU目标与地址空间处理入口，但其上游当前源码不等同于某个ROCm发行版的完整实现，更不能证明自研Target已覆盖所有访存。LLNL 7.0.0现场指南及7.13.0预览材料保留为[历史证据](research-notes/vendors_sources.json)，其问题不能自动归入10.1的已知缺陷。

## 2. 硬件依赖总表：缺失后的真实GPU替代方案

| 条件 | AMD官方GPU ASan | 自研GPU必需吗 | 缺失时可部署的实现 | 代价和不能补回的保证 |
|---|---|---|---|---|
| XNACK / retryable page fault | 官方 GPU ASan 要求受支持的 `xnack+` 目标；XNACK 提供可恢复缺页后的 GPU 访存重试 | **不是Shadow算法本身必需** | 将所需 Shadow 在设备访存前分配、映射、初始化并完成 ready fence | 不能依赖 Kernel 执行中缺页后补页并重试；不能直接当作官方 ROCm `xnack+` 运行环境 |
| Linux HMM / 共享虚拟内存 | 官方环境需HMM及兼容内核 | **不是自研离散GPU的普遍必需项** | Host维护对象账本，拷贝GPU Shadow/descriptor快照，按queue fence生效；设备端直接读固定GPUVA | 失去透明统一内存；须实现上传/下载与同步 |
| GPU能Load有效元数据 | GPU Device Runtime必需的基本能力 | **是** | 用global/constant/设备本地内存中映射的表查询，或可信硬件metadata通路 | 若GPU完全不能从合法设备内存读到元数据，软件Shadow无法部署；UNSUPPORTED |
| 专用 Shadow 检查硬件指令 | AMD GPU ASan 的基础 Shadow 检查由编译器插桩生成普通设备读取与判断，不要求专门指令 | **不是必需** | 使用现有 GPU Global Load、整数运算、比较和控制流 | 额外指令、寄存器与缓存开销，AMD 软件路径也有；不能把缺少专用指令视为相对 AMD 的独有缺陷 |
| 完整GPUVA预留 / Sparse VMM | 有助于大地址空间的 Shadow 布局 | 不是必需 | 根据已登记区域建立映射；可用软件 Radix/两级索引或硬件稀疏 GPUVA 映射 | 软件额外索引通常增加查询工作，Cache/TLB 和整体性能必须实测；未登记的合法来源应标 PARTIAL，不得误判为应用越界 |
| 同步、报告与可靠终止 | Runtime、设备代码和驱动共同提供 | **必须满足选定模式的契约** | 使用目标已有发布/完成Fence、报告协议和Abort或安全抑制机制 | Metadata未就绪、报告丢失和Barrier死锁不能记作成功；通用设计见[组件指南](real-gpu-components-implementation.md) |
| 硬件Tag / Bounds | 普通AMD Shadow不以此为前提 | 不是B2必需 | 软件原对象Bounds属于D1，时间身份属于D2；F1/F2另需真实硬件 | 不改变Shadow的当前地址语义，不自动覆盖未知来源或未插桩代码 |

表中自研列为**工程设计选择**。GPU能读取预映射Shadow是部署软件B2的基本条件；还必须满足访存覆盖、元数据生命周期和可靠报告契约。没有这些条件时只能声明`UNSUPPORTED`，不能把Host侧结果记为实体GPU检测。

### 2.1 XNACK 的具体功能及其与 HMM 的区别

**XNACK提供GPU访存重放，可用于可恢复缺页后的重试、按需分页和页面迁移。** 当缺页可由驱动/系统处理时，建立或恢复所需映射后重试访存；**Page Fault 不代表每次都新分配物理内存**，也可能只是映射已有页或迁移页面。[AMDGPU XNACK代码对象约定](https://rocm.docs.amd.com/projects/llvm-project/en/latest/LLVM/llvm/html/AMDGPUUsage.html#target-features)、[HIP统一内存说明](https://rocm.docs.amd.com/projects/HIP/en/docs-10.0.0/how-to/hip_runtime_api/memory_management/unified_memory.html)。

**HMM（Linux Heterogeneous Memory Management）不是 XNACK 硬件，也不是 Shadow 分配器**。它提供设备内存管理、CPU页表镜像与迁移辅助机制，与实际驱动/设备能力协作。[Linux内核HMM文档](https://docs.kernel.org/mm/hmm.html)。`HSA_XNACK=1`还受运行环境限制；例如[HIP文档](https://rocm.docs.amd.com/projects/HIP/en/docs-10.0.0/how-to/hip_runtime_api/memory_management/unified_memory.html)明确CDNA2的SR-IOV配置不支持XNACK，不能只看芯片系列就判定环境可用。

**XNACK 也不能自动判断 Shadow 的合法性。** 如果非法指针映射到一个没有受管理的 Shadow 地址，GPU 即使具有重试能力，也不一定存在可恢复的页面；驱动更不会自动知道应当写入 `0x00`、`0x05` 还是 Poison。ROCm 官方 GPU ASan 依赖支持的 `xnack+`/HMM 环境，是**官方实现的部署要求**，不能推出每个 Shadow 物理页都必然通过第一次 GPU Page Fault 才分配。没有 XNACK 的自研 GPU 可以在访问前显式准备 Shadow，但不能依赖执行中的故障补页、透明换入或自动重试。

### 2.2 没有 XNACK 且没有等效缺页重试机制：Shadow 的三项核心取舍

以下只比较**受自研 `gpuMalloc/gpuFree` 管理的 GPU Global Memory**；不讨论与 XNACK 无关的 LLVM 插桩覆盖、算法语义或 GPU 错误报告。先区分 **GPUVA 虚拟地址预留**和**Shadow 物理显存实际提交**：两者不是一回事。

| 选择 | 显存与性能 | 未跟踪非法地址的后果 | 约束与适用情况 |
|---|---|---|---|
| **① 全量物理 Shadow 预分配** | 为整个目标 GPUVA 覆盖区提前提交物理 Shadow，寻址通常简单，但巨大空闲区域浪费 HBM | 如果全部查询区域确实有映射且正确 Poison，可直接读取；不能仅凭物理容量推断合法性 | 例如 48-bit GPUVA 有 256 TiB 虚拟范围，经典 1:8 Shadow 对应 **32 TiB 逻辑 Shadow**，整块实存预分配通常不可行。只**预留虚拟地址**并不等于提交 32 TiB 物理显存 |
| **② 随 `gpuMalloc` 分配 Shadow，按直接地址公式读取** | 只为受管理对象及必要的保护区域准备物理 Shadow，显存节省、常见有效地址的读取路径简单 | 野指针落在**未建立 Shadow 映射**的区域时，检查器直接执行 Shadow Load 可能先发生不可恢复的设备 Fault；同页已映射但状态不正确时也可能误判 | 64B 对象对应 8 个基础 Shadow 字节，但并非每个额外字节都需要单独一个物理页；未映射地址不能盲读 |
| **③ 按需物理 Shadow + 安全的两级 Shadow 索引** | 根目录及必要索引节点始终有效映射；先判定索引和对象覆盖状态，再访问叶子 Shadow。软件多一次或多次元数据访问，可能增加延迟和缓存压力 | 未登记范围在 Shadow Load 之前被识别，因此不会先盲读未映射叶子页 | 通常更安全、节省物理 Shadow，但性能要做同条件实测；不是保证一定比所有平坦映射慢 |

**对比 S01：** `gpuMalloc(64)` 的有效范围及右侧保护区域如果已经映射、Poison 已就绪，则 `a[16]` 对应的 Shadow 可被正常读取并报告越界，**不需要 XNACK**。但访问远离任何已登记区域的野指针（如示意地址 `0xDEADBEEF`）时，②的直接公式可能映射到不存在的 Shadow 页，③则先检查始终可访问的目录，避免非法 Shadow Load。若本次检测范围不限于 `gpuMalloc`，对尚未登记但可能合法的外部内存应标记 **UNSUPPORTED/PARTIAL_COVERAGE**，不能自动定性为应用 OOB。

**额外选择，不应误算成独立第四种检查算法：** 如果 GPU MMU 本来支持稀疏 GPUVA，可以预留广阔的**虚拟** Shadow 范围，仅为已管理区域提供物理后备；部分场景能利用页表/TLB 避免显式的软件两级查找。不过，没有可恢复缺页时仍需保证真正发出的 Shadow Load 对应有效映射，或在读取前安全拒绝未映射地址。页表稀疏映射并不能凭空让任意野指针对应合法可读的 Poison 数据。

**归纳：** ①主要付出物理显存，②可能在未映射 Shadow 上直接 Fault，③通常增加显式索引开销。已驻留 Shadow 若在 Kernel 仍可能访问时被透明换出，也不能再依赖 XNACK 缺页重试；要么保持驻留，要么在后续使用前显式迁回。三种方式均可用于不具备 XNACK/HMM 的自研 B2，但都不等于能够直接运行 AMD 官方 GPU ASan。

### 2.3 无 XNACK 下的 Shadow 页有效性前置检查

关键顺序是 **先证明 Shadow 页可读，再执行 Shadow Load**：软件两级 Directory 或始终有效的 Present 元数据区可预先判定是否映射；GPU MMU 稀疏映射本身不能自动处理未映射的任意野指针。示意 `safeShadowLookupNoFault` 与结果区分只在[通用组件指南 §3](real-gpu-components-implementation.md)维护；这里规定硬件前提与错误归属。

**自研映射契约：** Backing/PTE和权限就绪后初始化Poison，以设备可见的Fence发布Ready，最后才允许读取；撤销映射/复用须先阻止新的使用，并等待所有旧读者完成。页大小大于逻辑Shadow字节时，可合并小对象的Shadow物理页，但须维护页内Poison及引用生命周期；一次64B分配不等于必须增加一个64KiB物理页。按页共享和TLB开销须实测。

## 3. Clang/Driver与构建系统

**AMD 官方与自研 Target 不能混为同一配置：** ROCm GPU ASan 的受支持路径使用 `xnack+` 目标、HMM 和配套 ASan Runtime；自研 LLVM 后端不必照搬 `:xnack+` 编译门槛，但必须声明实际设备能否 Load 元数据、映射 Shadow、发布 Queue Fence、可靠处理错误。若当前模式不满足这些契约，Compiler/Loader 必须标明 `UNSUPPORTED`，不能生成“只检查 CPU”的假成功。

**唯一通用实现位置：** [组件指南 §2](real-gpu-components-implementation.md) 的 LLVM Pass、Target 最终 ISA 覆盖清单；[§6.1](real-gpu-components-implementation.md) 的 `B2_SHADOW` / `B1_CURRENT_LIVE_INTERVAL` / `D1_ORIGINAL_OBJECT_BOUNDS` 能力协商及 `metadata_ready`。本专题只保留**缺 XNACK/HMM 时的硬件选择条件**，不再复制独立 `chooseSanitizer` 代码。



## 4. LLVM IR Pass：访存前检查（Compiler，方案B）

**AMD 特有注意点：** LLVM 上游 AddressSanitizer 的 AMDGPU 分支存在 Target AddressSpace 与 Generic Pointer 分类条件；LDS/Shared 和 Private/Stack 是否插桩还受真实 Lowering 和官方版本限制。启用 CPU ASan Pass **不能**证明自研 GPU ISA 的 Load/Store/Atomic、Masked/EVL、Async/Image 均被检查。

自研编译插桩、有效 Lane/元素 Mask、范围宽度、原子操作不降级、合法/非法访问的 CFG Guard 与最终覆盖 `Manifest` 的**唯一通用示意代码**在[组件指南 §2](real-gpu-components-implementation.md)。AMD 官方具体已知能力与不足在本指南 §1；未支持的地址空间/Opcode 明确 `PARTIAL_COVERAGE` 或 `UNSUPPORTED`。

## 5. Target Backend / MC：最终机器访存覆盖

**缺少专用 Shadow 硬件指令不阻止 AMD-B2 类软件检测。** GPU 能执行正常 Global Load、整数运算、比较和控制流，即可实现已映射 Shadow 的设备端判定。实际限制是额外指令、Cache/TLB、Register/Scratch/Occupancy 成本，而非检测算法丧失合法性。

Target 可能在 LLVM IR 插桩后 Lower 出新的访存；最终 ISA 的字节足迹、Predicate、AddressSpace、Atomic、Image/Async 必须复核。统一 Machine Opcode Coverage Audit 与严格拒绝机制见 [组件指南 §2](real-gpu-components-implementation.md)，此处不重复维护机器码检查循环。

## 6. GPU Device Runtime：Shadow和对象区间两种可部署实现

**没有 XNACK 的关键专属约束：** 对应用地址计算出 Shadow 地址之后，**绝不能在未验证页映射或 Metadata Ready 之前直接发出 Shadow Global Load**。可能合法但未登记的外部地址应返回 `UNKNOWN/UNSUPPORTED`；已声明跟踪的对象却缺少所需 Shadow，则是 `TOOL_FAILURE` 或启动前拒绝，不能把不存在的页当成 `0x00`。对于 1:8 Shadow，必须覆盖宽访问穿过的全部 Granule，并防止整数回绕。

[组件指南 §3](real-gpu-components-implementation.md) 是统一的设备端安全查询、Bounds、错误标志和 `Report` 实现位置。本指南 §2.2 的三种 Shadow 物理分配策略是**内存映射取舍**，不是三种不同的检查语义。B1 是设备端当前 Live Allocation 区间查询；A 派生指针进入仍存活的 B 时不能无来源元数据直接获得 D1 保证。

## 7. UMD：DebugAllocator、ShadowManager和异步生命周期

UMD 需要区别应用请求的 `requested_size`、实际 BO Backing Bytes 和保护区大小；在 `gpuMalloc/gpuFree`、Pool Suballocation、External Import、Graph Replay 等对象生命周期节点，确保 Shadow 映射、Poison 和 Ready Fence 的发布顺序正确。**无 HMM 时，不能直接把未映射的普通 Host Pointer 当作 GPU 指针**；必要 Registry/Descriptor 应通过正式 DMA/Copy 路径上传设备 BO。

元数据缺页不能在 Kernel 执行期间依赖 Host 自动补映射；`gpuFree` Host 返回也不代表设备端 Poison 已生效。详细 `SanObject`、`sanMalloc`/`sanFreeAsync`、跨队列依赖和 `Launch` 路径只维护在[组件指南 §1.1、§4、§6](real-gpu-components-implementation.md)。地址复用的旧指针需要 D2，而不是只更新 B2 Poison。

## 8. KMD：真实GPU映射、权限和完成，不强制每条路线都改

**没有 XNACK/等效恢复机制：** KMD 应在 Kernel 可执行之前建立合法 Shadow/Report/Descriptor GPUVA 映射与必要的 PTE/权限（Shadow 读、Report 写）并保证 TLB 和 Fence 顺序；绝不能预期执行中的 Fault 会自动恢复。若不支持稀疏 VMM，可采用事先证明能够合法访问的固定设备 BO 或软件安全索引，但**未映射 Shadow 的直接读取依然不安全**。

KMD 已有 BO/GPUVA、Code Permission、Submit/Completion/Fault/Abort 接口满足需要时，新增源码可以为零；缺少可靠设备完成 Fence 就不能声称 Shadow 初始化或异步释放顺序正确。通用提交/权限检查的唯一代码见 [组件指南 §7](real-gpu-components-implementation.md)。

## 9. Firmware / Trap / 错误终止

发现非法 GPU Store 后需要**真正阻止非法访存**，并向整个执行/Context 发布失败；仅让出错 Lane `return` 可能破坏 Barrier 参与集合并造成死锁。可以依赖已有可靠的 Dispatch/Context Abort，或在明确支持范围内使用谓词屏蔽与统一的错误完成协议；二者都无法证明时应拒绝该 Strict 模式，而不是无条件报告成功。

Firmware/KMD 和设备 Checker 的通用错误执行代码、Report 可见性和恢复/终止状态机已在 [组件指南 §3、§7～8](real-gpu-components-implementation.md) 定义。本节保留**缺可恢复缺页时不能依赖故障补映射、缺可靠 Abort 时不能承诺设备继续正确运行**的硬件边界。

## 10. RTL/实体硬件：仅F1/F2条件性Tag/Bounds硬件

**F1 硬件 Tag 与 F2 硬件 Bounds** 只有在真实目标 GPU 具备所需的 ISA/RTL/元数据、失效和 Fault 协议时才能声明硬件实现。缺少专用 Shadow/Tag/Bounds 指令时使用 LLVM 插桩和正常 GPU Global Load/Compare/Branch，不能把软件检测称为硬件能力。即使硬件只看当前有效 GPUVA，也不会凭空获得 D1 原对象来源。

条件性 RTL/Firmware/Tag/Bounds 的唯一通用源码与真实芯片验收在[组件指南 §8～9](real-gpu-components-implementation.md)；专用硬件是否值得研发，须在自研设备软件 B2/D1 的同条件显存和性能结果出现后再评估。

## 11. Reporter、CLI与失败分类

**判定优先级必须清楚：** 没有真实目标 GPU 执行证据时为 `GPU_NOT_RUN`；Metadata/Report OOM 等资源失败记为 `TOOL_FAILURE`；日志丢失时保留已有命中证据，并将不完整结果标为 `PARTIAL_COVERAGE`；所需指令或模块未插桩则为 `PARTIAL_COVERAGE/UNSUPPORTED`。仅在确有对应设备报告时才可归类 `GPU_DETECTED`，日志丢失或覆盖不完整不能记为 `CLEAN`；完成执行但未发现错误也不证明声明范围之外的能力。

报告中的设备型号、PC/Module Build ID、访问宽度、Lane、对象/Generation、Meta Ready 状态和 Context/Queue 应由统一 `SanReport` ABI 承载；Host API、CPU ASan、GPU Fault、GPU AddressSanitizer 的设备报告须分别归类。通用 Reporter/CLI 的唯一代码在 [组件指南 §1.1、§10](real-gpu-components-implementation.md)。

## 12. CI、S01样例、无XNACK实验及验收次序

### AMD官方环境对照（只适用于实际受支持的AMD设备）

以下保留**7.2.4机制文档对应的构建范式**，假设已选中匹配版本的Compiler和ASan运行库。`gfx90a`只是该版示例；10.1用户须先按[10.1 ASAN安装说明](https://rocm.docs.amd.com/en/docs-10.1.0/install/asan.html)选择`gfx942/gfx950`发行包并完成其环境设置，不可直接复用旧版路径/设备列表。这里的命令是待执行操作，尚无本仓库实卡结果。

~~~bash
# GPU真实支持xnack+、内核HMM以及对应/asan运行库时，才使用这一路径
export HSA_XNACK=1
export ROCM_ARCH=gfx90a:xnack+ # 示例，必须替换成真实目标架构
hipcc --version
rocminfo
hipcc -x hip -O1 -g -fsanitize=address -shared-libsan \
  --offload-arch="$ROCM_ARCH" samples/S01/amd.hip.cpp -o /tmp/s01_gpu_asan
ldd /tmp/s01_gpu_asan   # 检查ASan Host runtime和/asan/下的HIP库
readelf -d /tmp/s01_gpu_asan | rg NEEDED
/tmp/s01_gpu_asan --good
/tmp/s01_gpu_asan --bad
~~~

必须核对`libclang_rt.asan-x86_64.so`和插桩HIP/HSA库的实际加载位置；缺失时先修正环境再运行，不能把缺少设备报告解释成无错。`ASAN_OPTIONS`不代表设备端支持全部Host选项。[7.2.4运行要求](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html#preparing-to-run-an-instrumented-application)。设备不能满足该配置时记录`UNSUPPORTED_OFFICIAL_ROCM_DEVICE_ASAN`。

### 自研GPU（无需XNACK的真正实现路径）

~~~text
1. 读取目标ISA/设备元数据Load能力/队列与KMD映射能力
2. UMD创建设备可访问的Object Registry和Shadow segment，全部映射并完成初始化
3. LLVM为S01 Global Store编译设备check + report指令，反汇编核对最终实际ISA
4. 设置Strict覆盖/报告/资源合约，启动前检查metadata_ready和abort/suppress能力
5. 在实体GPU执行S01 good: a[0], a[15]；bad: a[-1], a[16]
6. 同时验证shadow初始化未完成、metadata映射失败、report溢出、非法lane关闭等反例
7. 收集设备PC/lane/访问宽度/对象范围/模块hash/原始日志和真实性能开销
~~~

### 缺硬件功能的逐项验收矩阵

| 测试 | 硬件条件 | 预期结果 |
|---|---|---|
| B Global + 显式Shadow | 无XNACK但GPU可读固定Shadow | 真实设备报告；确认GPU ISA真的执行check |
| B Global + HMM关闭 | 设备BO与fence上传可用 | 正反例结果一致，不能偷偷读Host地址 |
| 无Tag/Bounds硬件 | 软件D1/D2实现完整来源/epoch | D1/D2受控范围可用，F1/F2标UNSUPPORTED |
| 无可靠context abort | 有安全谓词屏蔽与错误完成协议 | 失败dispatch被拒绝或整体失败，不出现barrier挂死 |
| Shared/Private无精确布局 | 未实现新target插桩和资源协议 | 对应space标PARTIAL/UNSUPPORTED |
| Shadow OOM/报告满 | GPU真实执行失败注入 | TOOL_FAILURE且sticky/dropped状态可见 |
| 真实GPU性能 | 固定同一ISA/输入/调度/驱动条件 | 报告原始Kernel时间、显存、occupancy、覆盖及前后差异 |

推荐先做B的Global Load/Store和实际GPU报告，再扩展masked/vector/atomic/async、LDS/private的受控软件Bounds；D1解决原对象跨活对象问题，D2解决同VA复用后旧指针，C/E处理无法重新编译的模块。软件B/D在真实GPU可靠运行前，不优先立项F1/F2硬件扩展。

**实现状态：厂商部分是文档证据，自研部分是实施设计。此仓库尚无目标自研GPU生产Compiler/UMD/KMD实现和真实设备检测报告，状态仍为`GPU_NOT_RUN`；静态CI成功不能代替GPU执行。**
