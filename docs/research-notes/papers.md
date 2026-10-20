# GPU sanitizer 论文证据核验（研究中间笔记）

> **研究过程记录，非最终规范。** 本笔记保存早期调研内容，可能包含未更新的候选结论。正式判断以[主稿](../llvm-gpu-sanitizer-research.md)为准。

核验日期：2026-10-20。下述机制和实验结论来自论文全文、作者页面或作者代码；非全量系统性综述。采用原始URL与章节定位保存证据；未复现实验。

## 0. 最需要立即纠正的结论

1. **GPUArmor 的 2.3% 与 8% 是不同性能基线，已在 2026 全文直接找到解释。** 2025 arXiv §8 采用 hardened binary，并将安全指令当作 NOP 的基线；2026 论文正文主结果改为 vanilla binary 基线。2026 §10.1、Fig.12 明确写：使用前一种基线，GPUArmor 从 8% 降到 2.3%；SoL 从 7% 降到 1.4%。编译插桩妨碍原有优化约贡献 6%，metadata/check 约 2%。不是 GPUArmor 2026 平均只增加 2.3% 的完整运行成本。
2. **HiRace 的 25 states / 1,200 transitions / 默认 8-byte shadow / CAS 全部在正式 SC24 作者稿全文得到确认。** 但 8-byte 是每个被监测地址/数据元素的默认 shadow，不是“整个地址空间仅 8-byte”，也不是固定百分比内存开销；逻辑时钟有界、非层次 masked-warp 同步需额外近似历史。不能扩展成“任意 GPU 同步在固定 8-byte 下完整处理”。
3. **cuCatch：BST 在该论文指 Base and Size Table，不是 binary search tree。** 高位直接索引 + 两级 shadow 是互补路径。原型 x=8 时前 240 个索引可直接编码；后备 shadow 路径用 1..15 非零 4-bit 标签、32-bit shadow index。不是每次访问都做两级 shadow walk。
4. **不能将 cuCatch 一直写成未公开内部原型。** 作者主页 2025-12 公告指向 NVIDIA 2025-12-10 blog，说明该方向已通过 CUDA 13.1 `-fdevice-sanitize=memcheck` 对外提供。2023 原型限制应标记为“论文原型”；当前产品支持范围须以现行文档核对，不能直接照搬论文限制或 2025 blog 限制。
5. **2026 应补充 SuperCollider（PLDI2026），比泛列未核验 G-San/GPUDrano/SAGE 更直接相关。** 它是 backend compiler 的 weak-memory 值重复检查与调度扰动路线，覆盖现代 scope、异步操作的工程启发很强；它牺牲完备性，不是另一个完全 happens-before detector。

## 1. cuCatch（PLDI 2023）

### 书目信息和版本

- Mohamed Tarek Ibn Ziad, Sana Damani, Aamer Jaleel, Stephen W. Keckler, Mark Stephenson. **cuCatch: A Debugging Tool for Efficiently Catching Memory Safety Violations in CUDA Applications**.
- *Proceedings of the ACM on Programming Languages*, 7, PLDI, Article 111, June 2023, 24 pages. DOI: https://doi.org/10.1145/3591225
- 作者官方页面（明确 Published Manuscript）：https://research.nvidia.com/publication/2023-06_cucatch-debugging-tool-efficiently-catching-memory-safety-violations-cuda
- 全文：https://d1qx31qr3h6wln.cloudfront.net/publications/PLDI_2023_cuCatch_2.pdf
- PLDI program 的 “Link to Preprint” 实际指向上述 NVIDIA 页面；本次未找到并证明另一个独立早期版本的内容差异，因此采用官方 Published Manuscript，不编造版本变化。
- 主要定位： 全文；§3 pp.7–8；§4 pp.9–14；§5 pp.15–16；§6 pp.17–19；§7 p.20。

### 算法与后端分析

Shadow TBB 把每个 allocation 的 base/size/random temporal tag 放入 disjoint BST。指针高位能容纳 index 时直达 BST；否则地址经两级 shadow map 找 BST index。x-bit 高位空间分为：0 保留 host allocation；1..15 标记后备 shadow 路径；16..2^x−1 直接索引。原型假设 x=8，直接索引容量240。后备 shadow 对每32B应用区域保留32-bit index，同一 allocation 对应多个 shadow entry，最终指向同一 BST 记录（§3.1–3.2）。

Backend pass 采用受 reaching-definitions 启发的 forward iterative dataflow，求 reaching base pointers。破坏性重定义包括 load、call、带多个 reaching-base 的算术等。它在识别出的 root 处插入 READMETADATA，base/size/tag 经寄存器沿 pointer arithmetic 传播，随后每个 access 执行 SAFETYCHECK；实现上相当于在函数/内核可分析范围内构造 fat pointer，不改变存储中的 pointer layout/ABI（§4.1）。

关键限制：kernel 参数本身可能已是 `obj+offset`；函数返回或从内存加载的指针可成为新的分析 root，而不是真实 allocation base。跨分析边界的越界若已落入另一个有效 allocation，会降低“原对象”识别能力。因此“后端总能恢复真实 base”不成立。动态 shared pool 的逻辑子对象、静态 local 帧内具体对象边界、struct 字段仍有精度缺口。

### 可复核数字

| 指标 | 原文范围与口径 | 限制 |
|---|---|---|
| 1.19× / 19% | §6，176 workloads；87 standalone kernels + PolyBench-ACC + 多数 CUDA Samples；geometric mean；RTX2080Ti，core1710MHz；替换 ptxas 与 CUDA runtime | host CPU未插桩；standalone 用 GPU-only 时间排除 launch；不含分配接口拦截及metadata管理时间 |
| Shadow-BB-only 1.25×，最大3.22× | 同一评估，禁止利用 pointer高位直接索引、所有 metadata fetch 经shadow | 不是默认ShadowTBB |
| 32MB + 128MB 固定开销，shadow部分12.5% | BST为2^20个32B对齐条目；一级shadow2^24个指针；二级32bit/32B按需分配 | 不能只写固定12.5%或无条件“160MB+应用全部12.5%”：后备shadow按需要分配 |
| 40/56=71.4% | §5 Table2 自建56个错误测试；cuCatch确定检出计数；ComputeSanitizer20/56=35.7% | GMOD/GPUShield列来自作者按论文估计，非本平台实测；这一人工测试比率不代表野外总体recall |

具体覆盖：global OOB8/8；local OOB12/16；shared OOB10/12；intra-allocation0/8；UAF2/4；UAS4/4；invalid-free2/2、double-free2/2（后两项转发底层allocator错误，不是新独立检查）。delayed UAF 的直接索引路径在新旧BST index不同条件下确定检测；index重复或shadow随机tag路径存在概率漏检。论文没有保证所有temporal错误无漏检。

论文原型不做host检测、uninitialized-read/type-confusion、race/sync检测；不支持 device malloc/free、dynamic parallelism，性能集排除预编译CUDA库（未做binary instrumentation）。这些是2023原型边界。

### 产品化证据（不要把论文性能数字当成2026产品保证）

- 作者主页：https://mtarekibnziad.onrender.com/ （2025-12公告）。
- NVIDIA作者blog：https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/ （2025-12-10）。
- blog正文说明 CUDA13.1先 `nvcc -fdevice-sanitize=memcheck`，再在 Compute Sanitizer runtime 下运行。指针base/bounds分析可识别跳入相邻有效allocation的越界；仍是动态检查，必须触发问题路径。extra registers/stack和优化交互是实际工程风险。

## 2. BARRACUDA（PLDI 2017）

### 书目信息

Ariel Eizenberg, Yuanfeng Peng, Toma Pigli, William Mansky, Joseph Devietti. **BARRACUDA: Binary-level Analysis of Runtime RAces in CUDA programs**. PLDI 2017, pp.126–140. DOI https://doi.org/10.1145/3062341.3062342 。ACM online date 2017-06-14（publisher search），会议报告2017-06-19。

- 作者稿全文：https://mansky-cs.red.uic.edu/wp-content/uploads/sites/862/2021/08/4.-barracuda.pdf 
- 会议：https://pldi17.sigplan.org/details/pldi-2017-papers/32/BARRACUDA-Binary-level-Analysis-of-Runtime-RAces-in-CUDA-programs
- ACM metadata：https://dl.acm.org/doi/10.1145/3062341.3062342

### 机制和可迁移部分

在 CUDA fat binary 注册处拦截，提取嵌入 PTX，移除已有架构机器码路径，再加 logging 并交给JIT。因此“binary-level”不是任意纯SASS文件均可处理；核心仍依赖可提取PTX。GPU记录 memory/synchronization/branch events，经GPU–CPU队列发送，CPU并行处理race detection。默认把50%GPU global memory预留给传输队列（§4.1），不要误说全部metadata在GPU上完成。

HB逻辑借鉴 FastTrack epoch/vector clock：有序访问用epoch，concurrent readers需要升级read vector clock。per-thread VC 按warp/thread hierarchy无损压缩为 Converged / Diverged / NestedDiverged / SparseVC，复杂point-to-point同步退回sparse thread map；“无损压缩”不意味着所有程序有常数内存。CPU shadow按应用每byte保留32B基础记录，包含read/write epoch、reader map等（Fig8、§4.3.3）。

它把branch/reconvergence纳入分析，并用邻接 fence + memory op / CAS / EXCH模式推断acquire/release。论文明确承认这属于heuristic：若误推断一个不存在的同步边，就可能把有race的执行判断为安全。单kernel范围，system fence按global处理。2017 warp-lockstep、当时CUDA语义假设不能直接当现代独立线程调度/现行PTX模型证明。

### 数字

- 正确性：作者自建66个litmus tests（race或race-free，global/shared、warp/block、锁、grid barrier、flag），66/66判断正确；当时CUDA-racecheck19/66。不能说这是2026 ComputeSanitizer结果。
- 性能机：双 Xeon E5-2620v4，各8core/2.1GHz，128GB host；Maxwell GTX Titan X 12GB；Ubuntu16.04；CUDA7.5/8；driver367.48。26个standard benchmarks来自Rodinia3.1、GPU-TM、SHOC、CUDA SDK7.5和CUB1.4.1。每项10次均值，每次GPU reset。
- DWT2D最差相对开销约3700×（native90ms）；dxtc绝对运行最长20min；paper未在正文给可安全转引的跨项统一均值，不自行从图估算。

对自研LLVM backend的启发：将事件语义、检测状态机和code insertion分离；精确HB复杂同步是有价值的“参考语义”或小规模oracle。但不要以历史的warp隐式同步压缩规则直接覆盖当前架构。

## 3. HiRace（SC24，正式稿与arXiv区分）

### 版本和资源

John Jacobson, Martin Burtscher, Ganesh Gopalakrishnan. **HiRace: Accurate and Fast Data Race Checking for GPU Programs**. SC24, 2024. DOI https://doi.org/10.1109/SC41406.2024.00042 。

- 正式SC24作者稿：https://userweb.cs.txstate.edu/~mb92/papers/sc24.pdf （12页，已读正文并看Table I图像)
- conference：https://sc24.supercomputing.org/proceedings/tech_paper/tech_paper_pages/pap649.html
- arXiv早期标题 **HiRace: Accurate and Fast Source-Level Race Checking of GPU Programs**，arXiv:2401.04701v1，2024-01-09；https://arxiv.org/abs/2401.04701 。不能把早期摘要等同后期全部实验。
- 正式artifact：https://github.com/JohnJacobsonIII/HiRace-Artifact-SC24 （已打开仓库，包含 src、indigo、results、scripts 和固定 iGUARD submodule)

### 算法事实

对象是 global 与 block-shared memory races；源码层wrapper/header library + Clang LibTooling AST重写，不是LLVM backend插桩实现。

FSM有25 states：4 read-tracking、7 write-tracking、12 atomic-tracking、INIT、RACE。每state有48输入组合：4种访问（read、write、block atomic、global atomic）×3同步状态（unsynced、warp-sync、block-sync）×4线程关系（same、warp、block、grid）=1200 transitions。state只需5bit；与 thread ID、block/warp scalar clock等合并，默认每监测地址8B。transition编码为flat lookup table，shadow整值用atomicCAS重试，保证检查状态更新本身符合FSM；大量线程竞争同一shadow时仍会有CAS重试成本。

**不是无时钟算法**。正式稿§V.C明确：scoped-clock超过分配位宽时analysis被disable，用户可增大shadow；masked warp可能构成非层次同步，改用有限subwarp history（近似warp vector clock），机会性剪枝。常数空间保证受barrier次数以及非层次细粒度同步使用程度约束。Murphi验证针对建模的动作/有限状态空间，不是任意CUDA API正确性证明。cooperative groups、dynamic subgrids等文中论述为可扩展方向，不等于当前已完整实现。

每个data[k]默认8B shadow意味着选1B/4B/8B监测粒度会改变应用memory-to-shadow比例；总memory bloat必须注明粒度，不能把“8-byte metadata”误作“8% overhead”。

### 性能和正确性口径

- 平台：RTX2070 Super，Ryzen5 3600，Ubuntu22.04，CUDA11.7。
- 正确性主集：580个Indigo生成CUDA kernels，其中346个包含race；6个5–200节点小图，3480个code/input组合。HiRace和iGUARD此实验均未报false positive。HiRace在能实际触发所注入race的输入上检出全部race，不代表任意未走路径也可检测。
- 441个共同可测的大图执行上：iGUARD平均超过30×，HiRace低于3×（正文只称average，未标arith/geometric；不要擅改）。这支持该子集“超过10倍快”，不是任意kernel。
- 更广的HiRace性能集：作者报告5105 executions、464codes、11大图，average7.5×；原文counts有1项算术差（464×11=5104），如非必要可只转述“更大独立测试集约7.5×”，标作者平均。
- 正式稿TableI和§V.C存在更严重口径问题：TableI HiRace misses为78+137=215；总positive=346×6=2076。正文称“all but215 / completeness93%”，但215/2076推得positive recall约89.6%，(3480−215)/3480才约93.8%整体accuracy。推荐**不要引用93%为recall**；精确写215个未显现race的code/input组合漏报，并解释受输入触发限制。

对backend自研：FSM可以借鉴为层次barrier语义下的主要in-device算法；API lowering必须保留scope/mask和atomic属性，不能只用shared地址shadow +一个epoch冒充完整HappensBefore。可配置clock容量、overflow报告、非层次同步fallback必须写进设计。

## 4. GPUArmor（2025预印本 → TACO2026）

### 版本、DOI、公开稿

Mohamed Tarek Ibn Ziad, Sana Damani, Mark Stephenson, Stephen W. Keckler, Aamer Jaleel. **GPUArmor: A Hardware-Software Co-Design for Efficient and Scalable Memory Safety on GPUs**.

- 2025 arXiv v1：2502.17780，2025-02-25，https://arxiv.org/html/2502.17780v1 ；arXiv DOI https://doi.org/10.48550/arXiv.2502.17780 。该稿还有conference模板字样，不应据此认定已发表于ISCA2025。
- TACO23(3),2026,25pp，publisher online date2026-07-23，DOI https://doi.org/10.1145/3815783 。Primary ACM issue：https://dl.acm.org/toc/taco/2026/23/3 。
- NVIDIA作者页日期2026-05-30（与publisher正式online date不同）：https://research.nvidia.com/index.php/publication/2026-05_gpuarmor-hardware-software-co-design-efficient-and-scalable-memory-safety-gpus
- 此页提供2026全文：https://d1qx31qr3h6wln.cloudfront.net/publications/TACO2026_GPUArmor_preprint.pdf?VersionId=Ob0eQ3K1kRgq3riGLP3adyVyQZxJf6wE  。注意PDF页眉仍有Vol1/No1/January2026占位，不能抄作最终bibliographic metadata。
- 2026稿脚注明确本TACO文章扩展cuCatch2023，新增工作负载刻画、ISA/hardware/compiler co-design。

### 机制

沿用cuCatch的root-pointer propagation思想，但用新ISA指令 LOADMETA/MEMCHECK.G 和 Metadata Loading Unit 硬件处理。meta记录base/size/tag，与一个小的 per-SM Metadata Lookaside Buffer 缓存配合，允许metadata主存结构选择按allocation组织的binary search tree等省空间布局。这里 **BST指Binary Search Tree，与cuCatch的Base and Size Table缩写不同**。

MLB典型8 entries；缓存最近allocation metadata与其位置。LOADMETA按范围查询；MEMCHECK按已知metadata位置取回当前记录。释放时失效相关缓存；MEMCHECK重取当前tag避免把寄存器中的旧tag当作并发free之后仍有效。软件负责分配/释放hook与标记pointer，硬件需理解新操作/忽略tag bits。没有现售GPU硬件即不能照代码开关直接获得论文成本。

“总体live allocations很大但短时object working set小”是28个GPU样本的经验观察，不能当作用户自有kernel必然性质。需要先profile，不能从算法复杂度直接推出8-entry总足够。

### 2.3% / 8% 对照证据

| 稿件/图 | baseline | 结果 | 意义 |
|---|---|---|---|
| 2025 arXiv§8/9 | hardened binary，将memory-safety instructions当NOP | GPUArmor2.3% | 排除了大部分编译扰动造成的成本；文中称hardened vs vanilla<5% |
| 2026§9，Fig10/14 | vanilla binary | GPUArmor binary-search-tree +8entry MLB约1.08×；cuCatch同平台约1.36× | 应作为主性能结论 |
| 2026§10.1，Fig12 | hardened binary，将check ops当NOP | GPUArmor从8%降至2.3%；SoL7%降到1.4% | 明确基线差异归因：约6%编译优化损失、约2%metadata/check |
| 2026§10.3 | vanilla binary，无重编译版本 | GPUArmor-Binary2.2%，LAK10% | 采用memory-tagging概率保障，不能混成同等完整base/bounds保障 |

平台：nvcc -O3，改造ptxas；真实NVIDIA GA100采集vanilla/hardened traces，NVIDIA Architectural Simulator模拟GA100，每trace至少40M dynamic instructions。主性能集28 CUDA kernels，现实输入（Table2）；memory-management wrapper时间不计。geomean明确标在Fig10/14。**模拟结果，不是stock GA100上安全硬件实测**。2025变体称HWOnly，2026称Binary。

保障：跨root-analysis范围或delayed-UAF依赖随机tag，论文采用57bit地址空间、7bit高位并保留2个值，126有效tag → 1−1/126≈99.2%单次概率检测，前提是相应随机分配模型；不是端到端安全概率。真实base可识别时的非相邻intra-scope OOB为确定检查；字段内越界未覆盖。不能给GPUArmor“full spatial+temporal无条件保证”。

## 5. SuperCollider（PLDI2026，建议列作最新主线之一）

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

对自研后端最直接建议（推论）：除主要shadow/HB检测外，可另设低metadata的随机扰动+重复访问模式；利用ISA明确的weak/strong语义、按mask工作的intrawarp address matching，避免把现代异步/跨kernel问题都挤进一个全局HB模型。需要明确probabilistic coverage与one-sided报告代价。

## 6. GMSBench（2026-09预印本；验证基准）

Saurabh Singh, Jaewon Lee, Seonjin Na, Hyesoon Kim. **Towards Standardized Evaluation of GPU Memory Safety with GMSBench**. arXiv:2609.08871v1，2026-09-08，https://arxiv.org/html/2609.08871v1 ；DOI https://doi.org/10.48550/arXiv.2609.08871 。作者链接仓库 https://github.com/gthparch/GMSBench （本次open出错，未检查代码）。已读全文主要方法、表格，不仅摘要。

149个自包含CUDA测试，跨spatial/temporal/race与global/local/shared/constant/cross-space；每项聚焦一个错误。用实测地址建立攻击者/受害者布局，用sentinel或结果变化确立原生manifestation；setup条件不能建立时标记SETUP_FAIL，不应计作detector miss；有些语义错误无法稳定原生manifestation，列为detection-only。

在RTX A5000、A100、GH200三类平台比较native与ComputeSanitizer各模式，149总项中134项有native可观测manifestation；表中memcheck18/144、initcheck4/4、racecheck2/5，是**不同适用子集的分母**，不能把工具结果直接相加或把18/144当2026产品完整覆盖率。正文未在已核段落给完整工具版本/compile-time instrumentation配置，应避免拿它和CUDA13.1 CTI直接作同条件比较。

工程用途：可以作为allocation/sub-object/lifetime/scope测试矩阵种子；目前是研究者提出的开放benchmark，不可称已成为行业标准。race只有5个基础关系测试，不能替代Indigo/弱内存litmus/现代async专项测试。

## 7. SCuBA（Chasing Elusive...，2026-01预印本；静态补充）

Anubhab Ghosh, Ajay Nayak, Dhananjay Rao Thallikar Shyam, Arkaprava Basu. **Chasing Elusive Memory Bugs in GPU Programs**. arXiv:2601.21552v1，2026-01-29，https://arxiv.org/html/2601.21552v1 ；DOI https://doi.org/10.48550/arXiv.2601.21552 。已读全文方法和evaluation；未核到当前后续正式publication/可用artifact，不称已验证SOTA。

CGeist/MLIR同时分析host allocation size、launch维度与kernel访问offset的表达式关系，结合assert形成约束，用Google OR-Tools SAT solver检查可否越界；还跟踪dynamic shared allocation内部逻辑分区，帮助识别allocation边界sanitizer没有表示的子边界。

平台与样本：i5-14400F、64GB、RTX4080、Ubuntu24.10、CUDA12.1、LLVM18、CGeist截至2025-08commit；20 programs来自Kaldi、llm.c、CUDA Samples、ScoR、Rodinia、HeCBench、Indigo。TableII报告30真实+19人工引入OOB，SCuBA此集0FP/0FN；ComputeSanitizer此集检出4人工OOB，漏45。不是“45个全为新发现野外bug”；也不是对任意CUDA静态分析完整性的证明。

该文的“所有prior动态工具bug能力相同”或“cuCatch无公开实现”含时间/概括限制，不能原封不动用作2026现状；后者已被2025-12官方产品公告更新。正确启发是：动态sanitizer只能检查已执行路径；在host层保留size/shape/stride/partition关系，配合静态约束或有针对性的输入生成，可以扩大coverage。

## 8. 主文稿整合建议（基于上述论文的工程推论）

- 第一阶段保留独立diagnostics与allocation lifecycle接口，把spatial/temporal与race policy分开；仅用地址有效性不够表达“这个pointer应属于哪一个对象”。
- 早IR保留对象/子对象、scope/mask/async语义；后端侧确保实际memory opcode全覆盖，检查所有lowered instruction类型和generic地址空间。cuCatch的local/shared缺口正说明边界信息可能在后端前丢失。
- race架构可以有“层次FSM主模式 + 复杂HB参考/fallback + 随机witness模式”；每种模式的clock上限、history截断、随机遗漏和scope边界都必须可观察。
- 评估先做semantic matrix（GMSBench/Indigo/litmus），再做性能集。分别统计FP/FN/unexecuted-path/setup-fail/timeout/OOM/instrumentation unsupported；不要只对成功运行子集报几何均值而省略淘汰数。
- GPUArmor说明必须同时记录original vanilla、instrumented-but-check-NOP、完整检查三个成本；否则runtime check的便宜会掩盖寄存器/occupancy/codegen扰动。
- 不宜将表内不同时代GPU/编译器/样本上的19%、8%、3×、5.88×画作统一性能排名。
