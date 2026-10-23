# AMD GPU AddressSanitizer：XNACK/HMM 硬件依赖、Shadow 映射取舍与软件替代

**版本：v2.5 · 2026-10-23 · 目标：在自研实体GPU上实现真实可执行的Address Sanitizer；不是移植仅限于某一厂商的GPU配置，也不是已有产线实现。**

本指南区分两件事：(1) AMD ROCm 7.2.4 当前官方 GPU ASan 的已支持环境；(2) 自研LLVM GPU后端可以采取的等效软件检查架构。两者不能互相推导硬件支持。产品检查的执行点始终是真实GPU上的load/store/atomic之前的设备指令或真实MMU硬件，绝非仅有Host API拦截。

**定位**：这是三种基础空间参考配置中的**② AMD-B2**的逐组件技术实施，主要借鉴AMD ROCm/LLVM Shadow与Redzone。其余**①NV-R Runtime Binary**、**③NV-C Original-Object Bounds**与能力比较在[核心统一分类](core-design-comparison.md)。本指南的B1当前区间表是实现变体（原I），**不等于第三种基础参考NV-C的Original-Object Bounds**；D2是时间身份增强，E是混合集成策略。没有XNACK的自研Shadow不是官方ROCm现成实现。

## 1. AMD当前实现的可核实事实

以 https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html 为版本固定依据（不同版本不能合并声称同一支持矩阵）：

- AMD通过LLVM ASan插桩和compiler-rt Host/设备报告、带Redzone的allocator及GPU可读Shadow配合完成访存检查。GPU Load/Store/Atomic之前查询Shadow；经典1:8编码有尾部前缀表达边界，约12.5% Shadow字节额外空间；Redzone、设备库、映射和错误日志还增加额外开销。
- 官方GPU ASan要求适配的xnack+设备目标；带xnack-的示例会警告GPU端插桩被忽略，应用可以继续运行但没有设备检查能力。HSA_XNACK=1只是启用真正具备该能力的设备配置，不会给不支持的GPU制造XNACK。Linux HMM、受支持GPU和ASan版HIP/HSA运行库也是该官方运行路径条件。
- 使用带插桩的HIP/HSA组件，核验运行期依赖例如libclang_rt.asan-x86_64.so以及/asan/libamdhip64.so，不能只看编译成功。官方GPU错误报告带 on amdgpu device、GPU PC、workgroup/lane和访问大小；普通CPU ASan或GPU API错误不属于设备访存检出。
- 目前文档列出private/stack访问未插桩的缺口；HIP Shared/LDS可以建模到Global内存中、并可能以Global类报告，因此不是完整精度的原生LDS对象诊断。宽访问、子对象、来源边界、redzone以外的跳越仍要按实际检查算法判断。
- 上游LLVM AddressSanitizer.cpp对AMDGPU有地址空间及generic指针动态分类路径；不能把通用CPU Pass启用视为自研Target所有global/LDS/private/texture/async opcode都已覆盖。
- AMD的性能、报告和兼容性结论仅代表官方已测试目标及版本；自研GPU不沿用这些数值作为实测结果。

参考：ROCm GPU ASan 7.2.4（上方URL）；https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html ；https://rocm.docs.amd.com/projects/HIP/en/latest/how-to/hip_runtime_api/memory_management/virtual_memory.html 。

## 2. 硬件依赖总表：缺失后的真实GPU替代方案

| 条件 | AMD官方GPU ASan | 自研GPU必需吗 | 缺失时可部署的实现 | 代价和不能补回的保证 |
|---|---|---|---|---|
| XNACK / retryable page fault | 官方 GPU ASan 要求受支持的 `xnack+` 目标；XNACK 提供可恢复缺页后的 GPU 访存重试 | **不是Shadow算法本身必需** | 将所需 Shadow 在设备访存前分配、映射、初始化并完成 ready fence | 不能依赖 Kernel 执行中缺页后补页并重试；不能直接当作官方 ROCm `xnack+` 运行环境 |
| Linux HMM / 共享虚拟内存 | 官方环境需HMM及兼容内核 | **不是自研离散GPU的普遍必需项** | Host维护对象账本，拷贝GPU Shadow/descriptor快照，按queue fence生效；设备端直接读固定GPUVA | 失去透明统一内存；须实现上传/下载与同步 |
| GPU能Load有效元数据 | GPU Device Runtime必需的基本能力 | **是** | 用global/constant/设备本地内存中映射的表查询，或可信硬件metadata通路 | 若GPU完全不能从合法设备内存读到元数据，软件Shadow无法部署；UNSUPPORTED |
| 专用 Shadow 检查硬件指令 | AMD GPU ASan 的基础 Shadow 检查由编译器插桩生成普通设备读取与判断，不要求专门指令 | **不是必需** | 使用现有 GPU Global Load、整数运算、比较和控制流 | 额外指令、寄存器与缓存开销，AMD 软件路径也有；不能把缺少专用指令视为相对 AMD 的独有缺陷 |
| 完整GPUVA预留 / Sparse VMM | 有助于大地址空间的 Shadow 布局 | 不是必需 | 根据已登记区域建立映射；可用软件 Radix/两级索引或硬件稀疏 GPUVA 映射 | 软件额外索引通常增加查询工作，Cache/TLB 和整体性能必须实测；未登记的合法来源应标 PARTIAL，不得误判为应用越界 |
| GPU硬件Tag比较指令 | 普通AMD Shadow方案不以专用Tag为前提 | 不是B/D必需 | D1传原对象base/limit；D2显式(pointer identity,epoch)及元数据查询 | RAW指针未知来源只能降级；不能宣称有硬件Tag隔离 |
| GPU硬件Bounds单元 | 普通AMD Shadow不要求 | 不是B/D必需 | 每条实际访存前插入设备Bounds/Shadow helper | 额外指令/寄存器/带宽，不能保证未插桩代码安全 |
| Device atomics和内存发布语义 | report ring/并发Shadow元数据需要同步原语 | 至少需要**一种可证明的并发协议** | 每lane或每block的独立固定报告槽、Host在完成后合并；shadow写更新使用有序命令 | 少量报告/高内存开销；缺少可证明发布语义时无法可靠报告 |
| SIMT级可靠trap/dispatch abort | 影响错误后停止 | 若模式要求阻止非法访问，则必须有可靠策略 | 条件谓词屏蔽无效访存+sticky fatal标记+统一dispatch完成策略，或受支持的硬件trap/context abort | 一lane提前return可能使barrier死锁；不能证明安全时拒绝该运行模式 |
| Private/Stack/LDS对象大小和生命周期 | 当前AMD覆盖不足或归类粗糙 | 要实现各空间对象级检测则必需 | Compiler记录frame/LDS布局；设备端使用(block,thread,frame,object)局部Bounds；可使用Global metadata backing | 未获得真实布局就只能声明未覆盖，不能把完整backing视为所有子对象有效 |
| 精确硬件fault PC | 用于硬件fault归因 | B软件报告不要求硬件精确PC | 插桩时放site_id、module hash、设备报告里写site和lane | 可定位受插桩访问，无法归因未插桩硬件Fault到每个lane |
| 足够设备显存 | Shadow、Redzone和报表都要资源 | 必须有可满足最小方案的存储 | 对象区间检查、紧凑descriptor、分配级Shadow、限量报告；限制支持集合 | 若资源不足则TOOL_FAILURE，不得静默回退成CLEAN |

**需要强调：**“没有XNACK”与“GPU没有访存能力”不是一回事。对自研目标，正常GPU global load能读取预映射设备Shadow就可以实现B软件检测；若既无法建立合法设备地址映射，也无设备load或可调用helper，则无法靠Host模拟替代实体GPU检查。

### 2.1 XNACK 的具体功能及其与 HMM 的区别

**XNACK 是 AMD GPU 的可恢复缺页重试能力（retryable page fault）**：当 GPU 执行一条访存指令，遇到**可由驱动/系统恢复**的缺页（例如有效页面暂时不驻留、需要建立映射或迁移）时，相关系统组件处理故障，然后 GPU 可以重试原来的访存。**Page Fault 不代表每次都新分配物理内存**；处理动作也可能是映射已有物理页或将已有页面从 CPU 内存迁移至设备。参考 [AMD HIP 统一内存/XNACK说明](https://rocm.docs.amd.com/projects/HIP/en/docs-6.3.3/how-to/hip_runtime_api/memory_management/unified_memory.html) 和 [ROCm GPU Memory](https://rocm.docs.amd.com/en/docs-6.1.5/conceptual/gpu-memory.html)。

**HMM（Linux Heterogeneous Memory Management）不是 XNACK 硬件，也不是 Shadow 分配器**。HMM 为设备镜像 CPU 进程地址空间、追踪 CPU 页表变化及内存迁移提供内核机制；它与支持可恢复缺页的 GPU 硬件和具体设备驱动协同使用。详见 [Linux 内核 HMM 文档](https://kernel.org/doc/html/latest/mm/hmm.html)。`HSA_XNACK=1` 只能在硬件和运行配置本来支持的前提下启用对应能力，不能为没有 XNACK 的芯片创造重试功能。

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

**显式映射的发布顺序：** `alloc backing → 建立 PTE/访问权限 → 初始化 Shadow/Poison → GPU fence 可见 → publish Ready/Present → launch`；Unmap/复用必须先结束可能读取旧映射的 GPU 操作，再撤销 Present 与物理页。若页大小大于逻辑 Shadow 字节，多个细小 Allocation 应尽量共用 Shadow 页，不能把每次 64B `gpuMalloc` 直接等同于 64KiB Shadow 物理分配。按页共享和 TLB 的真实性能必须实测。

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

**判定优先级必须清楚：** 没有真实目标 GPU 执行证据时为 `GPU_NOT_RUN`；Metadata/Report OOM 或日志丢失时为 `TOOL_FAILURE`；所需指令或模块未插桩则为 `PARTIAL_COVERAGE/UNSUPPORTED`；仅在确有对应设备报告时才可归类 `GPU_DETECTED`，完成执行但未发现错误不代表超出声明范围的 `CLEAN`。

报告中的设备型号、PC/Module Build ID、访问宽度、Lane、对象/Generation、Meta Ready 状态和 Context/Queue 应由统一 `SanReport` ABI 承载；Host API、CPU ASan、GPU Fault、GPU AddressSanitizer 的设备报告须分别归类。通用 Reporter/CLI 的唯一代码在 [组件指南 §1.1、§10](real-gpu-components-implementation.md)。

## 12. CI、S01样例、无XNACK实验及验收次序

### AMD官方环境对照（只适用于实际受支持的AMD设备）

~~~bash
# GPU真实支持xnack+、内核HMM以及对应/asan运行库时，才使用这一路径
export HSA_XNACK=1
export ROCM_ARCH=gfx90a:xnack+ # 示例，必须替换成真实目标架构
hipcc -x hip -O1 -g -fsanitize=address -shared-libsan \
  --offload-arch="$ROCM_ARCH" samples/S01/amd.hip.cpp -o /tmp/s01_gpu_asan
ldd /tmp/s01_gpu_asan   # 检查ASan Host runtime和/asan/下的HIP库
/tmp/s01_gpu_asan --good
/tmp/s01_gpu_asan --bad
~~~

如果设备不能提供该官方配置，记录UNSUPPORTED_OFFICIAL_ROCM_DEVICE_ASAN，不要简单设置环境变量绕过工具预检，也不能将CPU ASan作为设备检出。

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

**实现状态：本文全部为自研GPU实施设计/伪代码。此仓库尚无目标自研GPU生产Compiler/UMD/KMD源码与真实设备检测报告；静态CI成功不能代替GPU执行。**
