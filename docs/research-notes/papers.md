# GPU sanitizer 论文证据核验（研究中间笔记）

> **研究证据，非新增方案目录。** 本轮只比较 NV-R、NV-C、B1、B2、cuCatch、CuSafe 六项软件配置；Intel 作为 B2 实现参考。NVIDIA/AMD 两家主要成熟厂商、NV-R/AMD-B2/NV-C 三种基础空间参考配置、两层空间语义的计数，仍以[核心分类与能力比较](../core-design-comparison.md)为准。本文把 cuCatch/CuSafe 的空间与生命周期证据放在主线，把 race、初始化及静态分析资料保留为补充背景。详见[主稿](../llvm-gpu-sanitizer-research.md)。

本轮核验日期：2026-11-02。cuCatch 与 CuSafe 已核对原文算法、实验章节及关键表格；其余条目保留 2026-10-20 的研究记录。本仓库未复现论文实验，不能把作者结果写成目标 GPU 验收结果。

## 0. 证据使用口径

论文原型、公开源码入口、现行产品是三类证据。论文的硬件/工具链、计时范围、失败样本和统计方式必须随数字一起保留；`1.19×` 表示运行时间增加 `19%`。不同论文之间没有同平台、同覆盖、同工作负载实测时，不按一个开销百分比排序。软件范围以本页开头六项为准，既有硬件研究不进入此次选型。

## 1. cuCatch（PLDI 2023）

### 书目信息与原始证据

Mohamed Tarek Ibn Ziad, Sana Damani, Aamer Jaleel, Stephen W. Keckler, Mark Stephenson. **cuCatch: A Debugging Tool for Efficiently Catching Memory Safety Violations in CUDA Applications**. *PACMPL* 7, PLDI, Article 111, June 2023, 24 pages. [DOI 10.1145/3591225](https://doi.org/10.1145/3591225)。采用 NVIDIA 页面标识的 Published Manuscript，未发现足以支持另一个早期版本差异的证据。

- [NVIDIA 论文页](https://research.nvidia.com/publication/2023-06_cucatch-debugging-tool-efficiently-catching-memory-safety-violations-cuda)
- [作者正式稿全文](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)：§3 为 Shadow TBB；§4 为编译器/runtime；§5/Table 2 为检出情况；§6 为性能；§7 为限制。

### 从 allocation 到访存的完整链条

**Shadow TBB 的 BST 是 Base and Size Table。** 每次分配在独立 BST 中记录 base、size 和随机 tag。原型假设指针有 `x=8` 个可用高位：`0` 留给不能携带 tag 的 host 可见分配；`16..255` 直接编码 BST index，共 240 个；`1..15` 为非零 4-bit 随机 tag，此时用指针地址查两级 shadow，取得完整 32-bit BST index。后备 shadow 每 32B 应用地址区域存 32-bit index，同一对象多个 entry 指向同一记录。因此，直接索引与 shadow 是两条取 metadata 的路径，不是每次访存都查两级表。[原文 §3.1–3.2](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)

**原对象边界来自编译器持续传播。** 后端 pass 以 forward iterative reaching-base analysis 找到 root，插入 `READMETADATA`，把 base/size/tag 放进寄存器并沿指针派生传播，形成有效的 fat pointer；内存中的指针宽度、对象布局和调用 ABI 不因此扩成 fat-pointer 结构。访存时执行 bounds/tag 检查，再移除 tag 进行实际访问。它能在 root 仍代表原对象时识别“对象 A 的派生地址落进有效对象 B”；若每次只从最终地址重新读取 B 的 metadata，这种能力就会消失。[原文 §3.2.2、§4.1、Fig.5–6](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)

**root 不保证是真正 allocation base。** 分析限于函数内部；load、call、多个来源的算术是 destructive redefinition。kernel 参数 `obj+offset`、函数返回值、派生指针存回内存后再 load 都可能截断来源；shadow 路径可能因此读到另一个对象的边界，只剩随机 tag 提供概率检查。不能把“保持 ABI”解释成任意未插桩模块、整数运算或调用边界都保留完整来源。[原文 §4.1.1、§7](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)

**runtime 是算法的一部分。** 分配 hook 维护 BST/shadow，释放清零 BST tag 及相应 shadow，修改 BST 后要求 GPU cache flush。编译优化可合并或外提 bounds checks，但 temporal tag 仍需随使用检查；论文也讨论将 tag check 延至对象最后一次访问的优化。寄存器中的旧 bounds 不替代运行时的释放状态。仅移植后端 check pass 不能获得整套生命周期能力。[原文 §3.2.4、§4.6–4.7](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)

### 空间与时间保障边界

| 对象/路径 | 论文原型的实际保障 | 不能扩大成的保证 |
|---|---|---|
| Global device allocation | 原对象 bounds；立即 UAF 通过失效记录检出；直接索引下新旧 BST index 不同时，地址复用后的 delayed UAF 可确定检出 | index 重用仍可能碰撞；shadow 路径只有 15 个非零随机 tag，不能写成统一的“256 个随机 temporal tags” |
| Unified/pinned host 可见内存 | 用 shadow 查询记录，保持 CPU 可用的未标记指针 | 不支持 top-byte-ignore 的 CPU 上无法给这类指针加 tag；原型漏掉复用后的 delayed UAF |
| Local | 每线程 31-entry BST，5-bit frame index + 3-bit tag；frame/`alloca` 生命周期；立即 UAS 与跨 frame 越界 | 同 frame 静态对象间越界漏检；延迟 UAS 为概率保障；BST 容量耗尽退回整个 stack 范围检查 |
| Shared | 静态 symbol 的 base/bounds，无 BST 和 temporal tag；dynamic shared 按整体 pool 检查 | pool 内多个逻辑分区、struct 字段等细边界没有自动恢复 |

依据：[原文 §4.2–4.4、§4.7、§5.2–5.3](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)。local 3-bit tag 在随机分配假设下单线程检出概率为 `7/8`，不是确定的代际身份。自定义 allocator 的内部对象、未截获的 CPU/ATS 分配也不在完整边界图中。2023 原型不支持 in-kernel `malloc`、dynamic parallelism，不插桩预编译 CUDA 库、host 执行、race、未初始化读取或 type confusion；这些应标为论文原型限制。[原文 §2.3、§6.1 注8、§7](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)

### 性能、覆盖与工程证据

| 指标 | 原文范围与口径 | 使用限制 |
|---|---|---|
| **1.19× / +19%** | 176 workloads，含 87 standalone kernels、PolyBench-ACC 和多数 CUDA Samples；geometric mean；RTX 2080Ti，GPU core 固定 1710MHz；自定义 ptxas/runtime，相对 baseline；global/local/shared 均插桩 | Samples/PolyBench 取内置 wall time，standalone 取 GPU-only 时间且排除 launch；不含分配 hook 和 metadata 管理开销。不能当端到端或现行 NV-C 成本 |
| Shadow-BB-only **1.25×，最大 3.22×** | 所有 metadata fetch 强制经 shadow 的消融配置 | 不是默认 Shadow TBB 最大值；默认最大值不从图自行补出精确数 |
| 固定 **32MiB + 128MiB**；后备 shadow **12.5%** | BST `2^20 × 32B`，一级表 `2^24 × 8B`；后备为 `4B/32B` 按需建立。论文单位写 MB，此处按其字节公式换算 MiB | 12.5% 只对应进入后备路径的区域；不是所有 payload 无条件都增加 12.5%，也不包括寄存器、spill、local BST 成本 |
| **40/56** 论文表内检出计数 | 自建错误集：global OOB 8/8、local 12/16、shared 10/12、字段内 0/8、UAF 2/4、UAS 4/4、invalid/double free 各 2/2 | 非实际项目总体 recall；GMOD/GPUShield 列是作者估计。invalid/double free 是转发底层 allocator 错误，延迟 temporal 结论仍受 tag 条件限制 |
| **14/176** occupancy 降低；**23** 个 kernel 增加 spill | 作者对资源压力的观测 | 低平均开销不保证高寄存器压力 kernel 适用 |
| 代码量/人月 **未披露** | 可确认改造 ptxas、CUDA runtime/driver、allocation metadata 与诊断链 | 不据论文反推每路线 LOC 或生产人月 |

以上来自[原文 §5/Table 2、§6/Fig.8–11](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)。176 项仅 5 项有至少 240 个 live allocations，评估明显包含大量可走直接索引的情形。论文相对当时 Compute Sanitizer 的平均 `63×` speedup 也不是同覆盖的现行产品对比：后者还检查 API/misalignment 等错误。[原文 §6.1、§6.2.6](https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf)

### 与 NV-C 产品的关系

[作者主页 2025-12 公告](https://mtarekibnziad.onrender.com/)说明该研究已进入 CUDA 13.1 的公开使用路径；[NVIDIA 2025-12-10 官方正文](https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/)介绍 `nvcc -fdevice-sanitize=memcheck` 后由 Compute Sanitizer runtime 执行，以及保持 base/bounds 的 fat-pointer 分析。故不能继续把该方向标成仅内部可用；但公开产品不等于 2023 原型源码发布，也不证明当前内部结构、支持范围或性能与论文完全相同。现行 NV-C 的边界以[厂商笔记](vendors.md)和主稿为准。

## 2. BARRACUDA（PLDI 2017；补充 race 背景）

### 书目信息

Ariel Eizenberg, Yuanfeng Peng, Toma Pigli, William Mansky, Joseph Devietti. **BARRACUDA: Binary-level Analysis of Runtime RAces in CUDA programs**. PLDI 2017, pp.126–140. DOI https://doi.org/10.1145/3062341.3062342 。ACM online date 2017-06-14（publisher search），会议报告2017-06-19。

- 作者稿全文：https://mansky-cs.red.uic.edu/wp-content/uploads/sites/862/2021/08/4.-barracuda.pdf 
- 会议：https://pldi17.sigplan.org/details/pldi-2017-papers/32/BARRACUDA-Binary-level-Analysis-of-Runtime-RAces-in-CUDA-programs
- ACM metadata：https://dl.acm.org/doi/10.1145/3062341.3062342

### 机制和可迁移部分

在 CUDA fat binary 注册处拦截，提取嵌入 PTX，移除已有架构机器码路径，再加 logging 并交给JIT。因此“binary-level”不是任意纯SASS文件均可处理；核心仍依赖可提取PTX。GPU记录 memory/synchronization/branch events，经GPU–CPU队列发送，CPU并行处理race detection。默认把50%GPU global memory预留给传输队列（§4.1），不要误说全部metadata在GPU上完成。

HB逻辑借鉴 FastTrack epoch/vector clock：有序访问用epoch，concurrent readers需要升级read vector clock。per-thread VC 按warp/thread hierarchy无损压缩为 Converged / Diverged / NestedDiverged / SparseVC，复杂point-to-point同步退回sparse thread map；“无损压缩”不意味着所有程序有常数内存。CPU shadow按应用每byte保留32B基础记录，包含read/write epoch、reader map等（Fig8、§4.3.3）。

它把branch/reconvergence纳入分析，并用邻接 fence + memory op / CAS / EXCH模式推断acquire/release。论文明确承认这属于heuristic：若误推断一个不存在的同步边，就可能把有race的执行判断为安全。单kernel范围，system fence按global处理。2017 warp-lockstep、当时CUDA语义假设不能直接作为现代独立线程调度/现行PTX语义的完整性依据。

### 数字

- 正确性：作者自建66个litmus tests（race或race-free，global/shared、warp/block、锁、grid barrier、flag），66/66判断正确；当时CUDA-racecheck19/66。不能说这是2026 ComputeSanitizer结果。
- 性能机：双 Xeon E5-2620v4，各8core/2.1GHz，128GB host；Maxwell GTX Titan X 12GB；Ubuntu16.04；CUDA7.5/8；driver367.48。26个standard benchmarks来自Rodinia3.1、GPU-TM、SHOC、CUDA SDK7.5和CUB1.4.1。每项10次均值，每次GPU reset。
- DWT2D最差相对开销约3700×（native90ms）；dxtc绝对运行最长20min；paper未在正文给可安全转引的跨项统一均值，不自行从图估算。

对自研LLVM backend的启发：将事件语义、检测状态机和code insertion分离；复杂同步的HB关系可用于梳理访问与同步事件语义。但不要以历史的warp隐式同步压缩规则直接覆盖当前架构。

## 3. HiRace（SC24；补充 race 背景）

### 版本和资源

John Jacobson, Martin Burtscher, Ganesh Gopalakrishnan. **HiRace: Accurate and Fast Data Race Checking for GPU Programs**. SC24, 2024. DOI https://doi.org/10.1109/SC41406.2024.00042 。

- 正式SC24作者稿：https://userweb.cs.txstate.edu/~mb92/papers/sc24.pdf （12页，已读正文并看Table I图像)
- conference：https://sc24.supercomputing.org/proceedings/tech_paper/tech_paper_pages/pap649.html
- arXiv早期标题 **HiRace: Accurate and Fast Source-Level Race Checking of GPU Programs**，arXiv:2401.04701v1，2024-01-09；https://arxiv.org/abs/2401.04701 。不能把早期摘要等同后期全部实验。
- 正式artifact：https://github.com/JohnJacobsonIII/HiRace-Artifact-SC24 （已打开仓库，包含 src、indigo、results、scripts 和固定 iGUARD submodule)

### 算法事实

对象是 global 与 block-shared memory races；源码层wrapper/header library + Clang LibTooling AST重写，不是LLVM backend插桩实现。

FSM有25 states：4 read-tracking、7 write-tracking、12 atomic-tracking、INIT、RACE。每state有48输入组合：4种访问（read、write、block atomic、global atomic）×3同步状态（unsynced、warp-sync、block-sync）×4线程关系（same、warp、block、grid）=1200 transitions。state只需5bit；与 thread ID、block/warp scalar clock等合并，默认每监测地址8B。transition编码为flat lookup table，shadow整值用atomicCAS重试，保证检查状态更新本身符合FSM；大量线程竞争同一shadow时仍会有CAS重试成本。

**不是无时钟算法**。正式稿§V.C明确：scoped-clock超过分配位宽时analysis被disable，用户可增大shadow；masked warp可能构成非层次同步，改用有限subwarp history（近似warp vector clock），机会性剪枝。常数空间保证受barrier次数以及非层次细粒度同步使用程度约束。cooperative groups、dynamic subgrids等文中论述为可扩展方向，不等于当前已完整实现。

每个data[k]默认8B shadow意味着选1B/4B/8B监测粒度会改变应用memory-to-shadow比例；总memory bloat必须注明粒度，不能把“8-byte metadata”误作“8% overhead”。

### 性能和正确性口径

- 平台：RTX2070 Super，Ryzen5 3600，Ubuntu22.04，CUDA11.7。
- 正确性主集：580个Indigo生成CUDA kernels，其中346个包含race；6个5–200节点小图，3480个code/input组合。HiRace和iGUARD此实验均未报false positive。HiRace在能实际触发所注入race的输入上检出全部race，不代表任意未走路径也可检测。
- 441个共同可测的大图执行上：iGUARD平均超过30×，HiRace低于3×（正文只称average，未标arith/geometric；不要擅改）。这支持该子集“超过10倍快”，不是任意kernel。
- 更广的HiRace性能集：作者报告5105 executions、464codes、11大图，average7.5×；原文counts有1项算术差（464×11=5104），如非必要可只转述“更大独立测试集约7.5×”，标作者平均。
- 正式稿TableI和§V.C存在更严重口径问题：TableI HiRace misses为78+137=215；总positive=346×6=2076。正文称“all but215 / completeness93%”，但215/2076推得positive recall约89.6%，(3480−215)/3480才约93.8%整体accuracy。推荐**不要引用93%为recall**；精确写215个未显现race的code/input组合漏报，并解释受输入触发限制。

对backend自研：FSM可以借鉴为层次barrier语义下的主要in-device算法；API lowering必须保留scope/mask和atomic属性，不能只用shared地址shadow +一个epoch冒充完整HappensBefore。可配置clock容量、overflow报告、非层次同步fallback必须写进设计。

## 4. CuSafe（USENIX Security 2026）

### 版本与实现入口

Hongyi Lu, Fengwei Zhang, Zhenkai Zhang, Shuai Wang, Yanan Guo. **CuSafe: Capturing Memory Corruption on NVIDIA GPUs**. 以 [USENIX Security ’26 官方页面](https://www.usenix.org/conference/usenixsecurity26/presentation/lu-hongyi)和 [USENIX prepublication 全文](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)为原始证据；[作者全文镜像](https://hongyi.lu/papers/cusafe-usenix26.pdf)关键算法与表格相同。本次不把研究网站的简化宣传语当作完整保障。

[作者项目页](https://cusafe.github.io/)公开 Source 入口，指向 [Figshare artifact DOI 10.6084/m9.figshare.30821396](https://doi.org/10.6084/m9.figshare.30821396)。论文称已发布源码；本次未成功取回该 archive，未逐文件检查许可证或编译，不能记作已复现。该 DOI 是软件 artifact，不是论文 DOI。

### 元数据、地址策略与检查顺序

**6-bit tag 编码 size class，8B 前缀提供精确 bounds。** allocator 把相同 `2^A` 对齐级别的对象放进相应 VA 区域，并在每个对象前保存真实 byte size。访存由 tag 得到对齐级别，清除低位定位前缀，再验证访问范围；因此既能检查大跨度跳转，也能检查“仍在同一对齐区间、但超过实际对象长度”的访问，不只是 coarse size-class 检查。它不采用 cuCatch 在寄存器中持续携带完整 base/size 的同一实现。[原文 §4.1–4.2、Fig.4–6](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)

**Global 的 tag 是有效 VA 的一部分。** 原型把 alignment 放在 `[46:41]`，利用既有 MMU 映射让带这些位的 VA 仍指向所需物理页；这是软件控制 VA 区域/映射别名的策略，不要求新增“硬件忽略高位”指令。该布局受 CUDA runtime 要求 `[63:47]=0` 约束，不能把 GPU 更宽的物理寻址能力直接当可用 tag 位。论文把 `2^A` 对齐施加于 VA，仅为实际使用的部分提供物理 backing；移植仍依赖目标平台可用的低层 GPUVA 管理接口。[原文 §2.2、§4.1–4.2、§6.2](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)

**Local/shared 使用编译器伪指针并在使用边界去 tag。** local alignment 在 `[53:48]`，bit 47 区分类型，高 10 bits 携带 stack epoch。因为这些地址由 NVIDIA runtime 管理，不能照搬 global 的映射策略；实际 dereference 或向未插桩函数传参前需去 tag。Global 带 tag 的有效 VA 可直接交给未插桩库，但库内访存没有因此被检查；返回的未标记 local/shared 指针也只能走兼容处理，不能据此声称全程序覆盖。[原文 §4.1–4.3、§8](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)

**先检查指针派生是否越出 size class，再检查精确长度。** 每次受插桩的 pointer arithmetic 比较运算前后不应变化的高位；若越过所属对齐区间，置最高 invalid bit。形成但未使用的无效指针不立即报错，实际解引用时再检查 invalid bit 与对象精确 bounds，并在失败时报告/终止。代价是临时越出 size class、随后减回原处的指针仍可能误报；只在同一 class 内暂时越过真实对象边界再恢复则可容忍。完整能力依赖相关算术和调用边界确实经过插桩。[原文 §4.2、§8/Fig.17](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)

### 生命周期与明确盲区

| 环节 | CuSafe 的实际机制 | 剩余边界 |
|---|---|---|
| Free / function return | global free 或 local 函数退出把 in-band size 清零；free hook 检查 double free | 清零不能独自解决原地址被新对象复用后的身份混淆 |
| Global delayed UAF | 随机化 VA 的 `[40:A]`，候选空间 `2^(41-A)`，降低旧指针落入新对象的概率 | 最多 37 个随机位只对应 16B class；1MiB 为 21 bits，8GiB 为 8 bits。仍为有限地址空间与概率保障，不是永不复用的确定身份 |
| Local UAS / stack reuse | 5-bit depth + 5-bit generation，检查指针所指 frame 的深度和代际；全局表记录每线程状态 | 同深度经过 32 次调用 generation 可回绕；depth 也有位宽边界，不能宣称无碰撞 |

依据：[原文 §4.3](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)。原型明确**不支持 dynamic shared memory、in-kernel malloc 和 JIT**；不检测同一 struct 内字段间越界；不能插桩闭源 CUDA 模块的内部访问。static local/shared 对象信息较早进入 LLVM pass，改善了 cuCatch 后端部分信息丢失问题，但不是所有内存类别、所有路径的完整保障。[原文 §5、§8](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)

### 实测口径、数字冲突与工程成本

| 证据 | 原文结果及条件 | 可用于评审的结论 |
|---|---|---|
| 原型规模 | LLVM 21 transform pass + `LD_PRELOAD` CUDA API library；**2,964 行 C/C++**；后续仍经 NVIDIA CUDA 编译链 | 这是作者原型 LOC；未给生产化人月。不能将“开放 LLVM pass”写成完全不依赖 NVIDIA 软件栈 |
| 测试平台 | Linux 6.12.21、driver 570.144、CUDA 12.8；Ryzen 9950X、RTX 5090 32GiB | 与 cuCatch 的 RTX 2080Ti 平台不同 |
| 正确性 | 自建 **33** 程序（15 spatial +18 temporal），CuSafe **33/33** 检出 | 测试集结果；cuCatch/GPUShield/LMI 列由论文推断，非同机运行，更非全程序 100% 保证 |
| 普通 workload 时间 | 整套作者报告 **44** 项，含 Rodinia/PolyBench-GPU/Tango/两项 LLaMA；每项 5 次；按时间报告的结果平均 **+13%=1.13×**，最大 **+83%=1.83×**（gemm） | 用 `cudaEventElapsedTime`，排除 host-device memcpy 和初始化；LLM 单独报告 throughput，runtime 平均分母及算术/几何方式未单独注明。不能写成含分配与端到端成本 |
| LLM throughput | LLaMA2-7B/LLaMA3-8B；平均 **−11%**，LLaMA3 **−18%** | throughput 百分比与 runtime slowdown 分开；不沿用项目页“LLaMA3 少于15%”的简化说法 |
| 内存结构 | 固定 **16.5MiB** epoch 表 + **8B/object** 精确长度 | 这是明确结构成本；平均百分比受对象大小/数量影响，不能拿 8B 代替整体内存成本 |

依据：[原文 §5–6、Table 3–6、Fig.15](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)。其中有三处必须随结果保留的证据限制：

- **样本数不一致。** 正文/摘要写 44，Table 4 分项标注 `19+19+6+2=46`；本次保留“作者报告44”，不自行修补分母或声称原始数据已经复核。
- **平均内存不一致。** 摘要/正文写 `0.3%` 与约 `16.5MiB`，Table 6 的 CuSafe Average 却写 `0.5MB (0.01%)`，与固定成本不相容且未解释。故 0.3% 只能记作作者摘要口径，现有表格不足以支持确定的整体平均；cuCatch/LMI 内存列也是按分配 profile 推算，不是运行其实现测量。
- **比较基线受限制。** Compute Sanitizer 的 Fig.15 漏去超过60分钟的 lu/gemm，正文又给出 gemm 的 153× 值，原始统计范围需进一步核对。cuCatch 19% 是引用另一篇论文；CuSafe 对 cuCatch“256个随机 tag”的概述也不能替代本文 §1 的直接索引/4-bit shadow/3-bit local 三种情况。[原文 §2.3、§6.1–6.2](https://www.usenix.org/system/files/conference/usenixsecurity26/sec26_prepub_lu.pdf)

**工程推论：** CuSafe 省去 cuCatch 的大规模 bounds shadow，但把工作移到对象布局、VA allocator、算术插桩、stack epoch 与跨模块兼容。低层地址接口、allocation/free 发布顺序、pointer-int roundtrip、generic 转换和未插桩库边界仍需目标 GPU 验证。2,964 行不能替代生产 runtime、诊断、并发 API 适配和测试成本；沿用 v1.8 预算，不据论文添加每路线人月。

## 5. SuperCollider（PLDI2026；补充 race 背景）

### 元数据与artifact

Mark Stephenson, Sana Damani, Mohamed Tarek Ibn Ziad, Anis Ladram, Michael Garland. **SuperCollider: Scalable and Effective Data Race Detection for CUDA**. PACMPL10, PLDI, Article261, June2026,25pp. DOI https://doi.org/10.1145/3808339 。

- 作者页：https://mtarekibnziad.onrender.com/publication/supercollider 
- 作者正式排版全文：https://mtarekibnziad.onrender.com/files/preprint_PLDI26_SuperCollider.pdf 
- Reproduction package，2026-03-17v1，https://zenodo.org/records/19058944 ，DOI https://doi.org/10.5281/zenodo.19058944 （已核到163.8MB zip和artifact条目；进一步核验确认 runtime `librc-release.so` 为专有评估许可，不能称整个工具开源。未运行artifact。）

### 机制和backend意义


对weak load做原load→随机delay→strong/system-scope重复load→值比对；若中间无本线程同步但值变了则形成race witness。对weak store做写后重复load检查lost update。另用warp match比较active lanes的store地址，捕捉同warp多线程同时写同地址（包括同值写）。通过block/warp/subwarp schedule扰动扩大暴露窗口，不依赖识别具体lock/fence协议。

只需per-thread状态，没有按全部访问地址分配的大shadow历史，但并非绝对0内存：仍有固定report buffers、变大的binary，且寄存器状态会增加。能够在shared/global/tensor memory和跨kernel/跨GPU/CPU-GPU等范围发现一些先前shadow工具难扩展的冲突；原型不完整支持所有异步方向（正文特别说 `cp.async.bulk` global→shared 尚不支持），对CPU读而GPU写的一些race只instrument GPU端不足。

与HB检测器不同，值未改变、多次a→b→a、窗口未碰中会漏；普通跨warp同值write不保证捕获；只检查weak operations，不能据此认定strong-operation协议正确。主要one-sided attribution，只有触发线程PC/line，缺少对端；intra-warp check可给参与lanes。不是race freedom证明。

### 可引用成本

平台：CUDA13工具链替换ptxas；RTX Ada6000/48GB；host i9-13900K、128GB；HeCBench/CUDA Samples/Gunrock/Indigo/CUTLASS4.3.0。

- 130个不触发race的HeCBench：rdelay=wdelay=1ns fast配置 geometric mean4.86×，median3.57×，max229×。
- 同130集balanced配置rdelay5000ns/wdelay1ns：geomean11.21×；检测窗口与性能tradeoff。
- 59个共同可完成的HeCBench子集：SuperCollider5.88×，iGUARD159×，Racecheck178×（geomean）；其余racecheck有67 workload超时、iGUARD39超时，HiRace因公开工具链需manual annotations未参加此比较。不能当全130项统一胜率。
- 文摘“发现over90 races”是作者跨库/基准发现数，若引用应写作者报告，非用户自有工作负载期望值。

对自研后端最直接建议（推论）：除主要shadow/HB检测外，可另设低metadata的随机扰动+重复访问模式；利用ISA明确的weak/strong语义、按mask工作的intrawarp address matching，明确其与完整HB检测路径的覆盖差别。需要明确probabilistic coverage与one-sided报告代价。

## 6. GMSBench（2026-09预印本；补充验证数据集）

Saurabh Singh, Jaewon Lee, Seonjin Na, Hyesoon Kim. **Towards Standardized Evaluation of GPU Memory Safety with GMSBench**. arXiv:2609.08871v1，2026-09-08，https://arxiv.org/html/2609.08871v1 ；DOI https://doi.org/10.48550/arXiv.2609.08871 。作者链接仓库 https://github.com/gthparch/GMSBench （本次open出错，未检查代码）。已读全文主要方法、表格，不仅摘要。

149个自包含CUDA测试，跨spatial/temporal/race与global/local/shared/constant/cross-space；每项聚焦一个错误。用实测地址建立攻击者/受害者布局，用sentinel或结果变化确立原生manifestation；setup条件不能建立时标记SETUP_FAIL，不应计作detector miss；有些语义错误无法稳定原生manifestation，列为detection-only。

在RTX A5000、A100、GH200三类平台比较native与ComputeSanitizer各模式，149总项中134项有native可观测manifestation；表中memcheck18/144、initcheck4/4、racecheck2/5，是**不同适用子集的分母**，不能把工具结果直接相加或把18/144当2026产品完整覆盖率。正文未在已核段落给完整工具版本/compile-time instrumentation配置，应避免拿它和CUDA13.1 CTI直接作同条件比较。

工程用途：可以作为allocation/sub-object/lifetime/scope测试矩阵种子；目前是研究者提出的开放benchmark，不可称已成为行业标准。race只有5个基础关系测试，不能替代Indigo/弱内存litmus/现代async专项测试。

## 7. SCuBA（Chasing Elusive...，2026-01预印本；静态补充）

Anubhab Ghosh, Ajay Nayak, Dhananjay Rao Thallikar Shyam, Arkaprava Basu. **Chasing Elusive Memory Bugs in GPU Programs**. arXiv:2601.21552v1，2026-01-29，https://arxiv.org/html/2601.21552v1 ；DOI https://doi.org/10.48550/arXiv.2601.21552 。已读全文方法和evaluation；未核到当前后续正式publication/可用artifact，不称已验证SOTA。

CGeist/MLIR同时分析host allocation size、launch维度与kernel访问offset的表达式关系，结合assert形成约束，用Google OR-Tools SAT solver检查可否越界；还跟踪dynamic shared allocation内部逻辑分区，帮助识别allocation边界sanitizer没有表示的子边界。

平台与样本：i5-14400F、64GB、RTX4080、Ubuntu24.10、CUDA12.1、LLVM18、CGeist截至2025-08commit；20 programs来自Kaldi、llm.c、CUDA Samples、ScoR、Rodinia、HeCBench、Indigo。TableII报告30真实+19人工引入OOB，SCuBA此集0FP/0FN；ComputeSanitizer此集检出4人工OOB，漏45。不是“45个全为新发现野外bug”；也不是对任意CUDA静态分析完整性的证明。

该文的“所有prior动态工具bug能力相同”或“cuCatch无公开实现”含时间/概括限制，不能原封不动用作2026现状；后者已被2025-12官方产品公告更新。正确启发是：动态sanitizer只能检查已执行路径；在host层保留size/shape/stride/partition关系，配合静态约束或有针对性的输入生成，可以扩大coverage。

## 8. 主文稿整合建议（工程推论，不新增软件选型）

- 对六项软件配置分别保存检查谓词、来源信息、生命周期与部署条件；不能以当前地址有效代替原对象 bounds，也不能以空间 bounds 代替无碰撞时间身份。
- cuCatch 的价值是后端 root 分析与 bounds 传播；CuSafe 的价值是较早的对象布局信息、size class 与精确 bounds 结合。选择哪一种都需要 allocation/free、metadata 可见性、指针跨边界和诊断链共同落地。
- HiRace、BARRACUDA、SuperCollider 只为 race 扩展提供背景；初始化/同步检查也单列覆盖，不算新增基础 ASan 配置，不以其开销和空间 sanitizer 排名。
- 评估先做语义矩阵，再做性能集。分开记录 FP/FN、未执行路径、SETUP_FAIL、timeout、OOM 和 instrumentation unsupported；保留每种开销的分母、baseline、计时边界和失败项。论文测试不能代替本项目 `GPU_NOT_RUN` 状态对应的实卡验收。
- 性能还应解释额外寄存器、occupancy、spill、metadata 访问与编译优化交互。v1.8 数字预算不因论文 2,964 行或 13%/19% 平均值而重估；生产工程量的未披露项保持未知。
