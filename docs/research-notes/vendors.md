# GPU Sanitizer 友商官方证据核验笔记

> **厂商证据记录，而非厂商数量=算法数量。** 参见[3种基础空间参考配置与能力](../core-design-comparison.md)：重点NVIDIA与AMD；NV-R、AMD-B2、NV-C分别对照。Intel/MUSA保留功能/Device Runtime/报告比较，不因品牌不同宣称新的基础算法。实现细节见[主稿](../llvm-gpu-sanitizer-research.md)。

核验日：2026-10-20。用途：中文立项与架构调研的证据笔记。只核验公开一手材料，不作产品实测，不把文档声明当成完整覆盖证明。精确URL、版本和对应章节见同目录 vendors_sources.json。

## 1. 版本基线及纠正点

**区分厂商功能与内部算法**：NVIDIA默认运行时的基础地址等级与AMD-B2均主要检测当前地址有效性，但二者内部元数据结构不可推断为相同；NV-C通过来源追踪提升原对象Bounds。Intel oneAPI ASan/MSan/TSan与MUSA错误类型值得参考，但官方功能表不足以证明第四种新检测算法，也不能宣称它们与NV/AMD内部实现完全一样。

| 对象 | 本次可核验基线 | 不能混淆的旧结论 |
| --- | --- | --- |
| NVIDIA Compute Sanitizer | 在线 Release Notes 顶部为 2026.3；NVCC 在线手册 13.4；compile-time memcheck 的官方博客为 2025-12-10，明确始于 CUDA 13.1；release notes 对应 2025.4 | 当前 initcheck 已支持 shared/all，不再仅限 global。只有 memcheck 支持 compile-time instrumentation。不能拿 2023 cuCatch 论文的平均 19% slowdown 当成当前产品实测。 |
| AMD GPU ASan | ROCm 7.2.4 的版本化官方说明；其页首区分 7.13.0 technology preview 与生产使用 7.2.4 | LLNL 环境说明不是 AMD 全版本能力表；不能把 private/stack 或 HIP shared/LDS 写成完全同等的原生覆盖。 |
| LLNL El Capitan 指南 | 当前正文写 tested with rocmcc/7.0.0，未给该小节独立更新日期 | hipFree double-free、hipMemcpy invalid address、mismatched deallocation 的局限须标注“LLNL 此指南/环境仍列出”，不能断言 ROCm7.2.4 全部仍未修复。 |
| Moore Threads MUSA Compute Sanitizer | 当前版本列表最高 v1.1.1；v1.1.1 环境表是 Intel CPU、amd64/x86_64、MTT S5000；未显示可靠发布日期 | 国内公开产品样本只证明该产品，不代表“国内主流”；v1.1.0 的“全地址段”不能改写成覆盖全部 GPU 地址空间/指令。 |
| Intel oneAPI DPC++/C++ device sanitizers | 本次找到的当前 C/C++ 官方指南 2026.0，页首日期 2026-04-28 | 支持范围必须同时带上总章的 Linux/Ponte Vecchio 单卡配置及运行限制；Fortran 2026.1 文档不能当成 C/C++2026.1。 |

## 2. 能力比较（按文档实际范围）

“未建立”表示本次官方证据未建立能力，不表示断言完全不支持。

| 维度 | NVIDIA 当前 Compute Sanitizer | AMD ROCm7.2.4 GPU ASan | MUSA Compute Sanitizer v1.1.1 / 当前指南 | Intel oneAPI C/C++2026.0 |
| --- | --- | --- | --- | --- |
| 入口与路线 | 默认 runtime binary instrumentation；memcheck 可选编译期插桩；仍须配套 runtime | clang/LLVM 编译插桩 + ASan runtime/拦截器 + ASan 版 ROCm 库 | mt-compute-sanitizer 包装原程序运行；文档称 GPU 运行时调试工具；底层具体编译/二进制实现本次未建立 | -Xarch_device -fsanitize=address/memory/thread；配套 Unified Runtime sanitizer layer |
| OOB、对齐 | memcheck：global/local/shared、global atomic；区分 precise 与 hardware imprecise | 地址错误/OOB、有毒 shadow；private/stack 未插桩；HIP shared/LDS 转 global 建模报告 | 明确 global memory OOB；atomic、robust LSU、TME 仍在计划；private/shared/local/host pinned 不完整 | ASan：device global、USM、local、private；对齐、NULL、UAF；SYCL 另有 memory buffer |
| 时间/分配错误 | device malloc/free 错误、泄漏；memcheck 的 stream-ordered lifetime race 检查支持 use-before-alloc/use-after-free | 有 UAF；生命周期更新不完整影响漏报；不能从 ASan 名称推出所有 HIP allocation API 错误都被检查 | musaMalloc 泄漏需 --leak-check=full；API 返回错误；未建立完整 UAF/double-free 语义 | ASan UAF；SYCL bad-free/double-free/bad-context；OpenMP 对后三项列 n/a |
| 未初始化 | initcheck 默认 global；--initcheck-address-space shared/all 可扩展；2025.4 引入 shared | 该 ASan 文档不建立 MSan 能力 | 当前正式能力表不建立 initcheck/MSan | device MSan 自2025.1支持 OpenMP/SYCL，2025.2增 local/private |
| 竞态与同步 | racecheck 是 shared-memory data hazard detector（含 DSM/现代 barrier 支持），不能泛称任意 global race；synccheck 检同步原语；两者与 initcheck 仍需 runtime binary instrumentation | 该 ASan 文档不建立 TSan 能力 | 当前正式能力表不建立 racecheck/synccheck | device TSan 自2025.2支持 OpenMP/SYCL，文档重点 device USM；不能推成 NVIDIA synccheck 对等 |
| 报告 | thread/block/PC/address/access size；host+device backtrace 依错误类型/调试信息不同 | workgroup、wave lane及地址；GPU call stack 当前只有出错 PC 的单帧 | kernel/device/address/API error、host backtrace；v1.1.1 默认最多100条详情，summary仍统计 | 多错误、内存开销统计；ASan元数据与运行库状态 |

## 3. NVIDIA：可直接用于架构讨论的事实

### 3.1 编译期功能与对象边界

- 官方博客解释 fat pointer 为 pointer 加 base/bounds，指针运算更新 pointer 而保持所属对象的边界；可以发现越过 A 对象但落到相邻合法 B 对象内的访问。手册称 base-and-bounds analysis；分析失败回退到常规 allocation bounds，dynamic shared 的 size 可能缺失。不能承诺所有来源的指针均能保留 provenance。
- 正式入口：`nvcc -fdevice-sanitize=memcheck ...`，或 `ptxas -sanitize=memcheck ...`，运行 `compute-sanitizer --tool memcheck ./app`。产品在 CUDA13.1引入；NVCC13.4 §4.2.8.29 明确仅 memcheck 值受支持、编译工具与 sanitizer 同 CUDA Toolkit 版本，离开匹配 runtime 或换用别的 tool 运行均不受支持。
- hybrid 是不同 compilation unit 可分别编译期/二进制插桩；Release Notes 明确同一 module 中 function-level hybrid 不支持，未编译期插桩函数不会自动得到二进制补插桩。为了完整覆盖需整个 module 的所有函数都用该 flag。
- HMM 在 compile-time memcheck 模式不受支持且会误报。此限定不可扩大成 Compute Sanitizer 所有模式均不支持 HMM：旧版本已在常规模式加入过 HMM/ATS 支持。
- JIT 场景不能只依赖 `-fdevice-sanitize=memcheck`；release notes 要求 `-Xptxas=-sanitize=memcheck`。当前 compile-time 模式对 cluster shared memory access 仍列误报限制，部分 PTX sink (`_`) 用法也不支持。

### 3.2 内存与性能边界

- 博客只称某些案例运行时 sanitization 开销接近降低一个数量级，且明确不含重新编译时间；没有全工作负载保证。
- 插桩增加代码、寄存器、可能的 stack；可能使 kernel 超过 launch 资源限制。官方推荐定位用 `-lineinfo`，`-G` 会加重资源问题。文档建议 `__launch_bounds__`，博客也列 maxregcount。
- 追踪数据会使 host/device 内存上涨；大量并发 launch 时可因分配失败漏追踪 launch；force synchronization/blocking 或 lazy module loading 可缓解，但会改变并发行为。报告“检查零错误”必须与覆盖、是否掉记录、launch失败一起解读。
- `racecheck` 检 shared data hazards，与 `memcheck --track-stream-ordered-races` 的异步 allocation lifetime 分属不同语义，不能都归为完整全局 TSan。

### 3.3 公共 API 可借鉴的接口分层（非自研现状声明）

API Guide 提供 callback、patching、memory 三组核心接口。callback 能捕获 Runtime/Driver API、resource、synchronization、launch、memcpy、memset、batch memory 事件；patching 将 CUDA device 函数装入模块内指定指令事件，传 PC、ptr、accessSize、flags；memory API 是 callback 中可安全调用的内存替代接口。官方同时规定 callback 内通常不能重入任意 CUDA API，避免挂起；一个 subscriber；device-launched graph 没有 host launch callback，必须使用单独 device graph data 机制。这说明“宿主 API 事件”和“设备实际访存”应是两条互补观测链。

### 3.4 规范链接

以下是已核验可访问的规范正文地址，可替换被SVG或网页残片污染的链接：

1. 总览/手册： https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html
2. 编译期博客： https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/

## 4. AMD 与 LLNL：要把编译、运行库、系统能力一起说清

ROCm7.2.4 官方步骤要求 `-fsanitize=address -shared-libsan -g`、显式 `--offload-arch=...:xnack+`；xnack- 可以生成可运行程序但 device 不插桩并出 warning。运行还要 HMM（说明写 Linux kernel5.6+应足够）、`HSA_XNACK=1`、正确 ASan 版本 HIP/ROCm runtime；否则设备错误可能不报告。该版本说明对旧 gfx906/gfx908 另有内核模块 `noretry=0` 要求，但不可将旧型号示例当成当前通用支持清单。页面列 tested/supported OS Ubuntu22.04/RHEL8/SLES15，属于 ASan 页声明，不能代替整套 ROCm 支持矩阵。

其机制明确 load/store/atomic 前读取 shadow，对象有效区之外还需额外Shadow内存，文档警告约3倍或更高运行时间（依访存负载），这些是官方说明而非横向实测。跨越受标记无效区的访问可能漏检；private/stack 未插桩；LDS 被放到 global 建模；NULL shadow 未 poison，因此仍可能是原始 fault而非 ASan诊断。`ASAN_OPTIONS` 主要控制 host runtime，device runtime只支持部分相关项默认值，不可照抄 CPU ASan所有开关承诺。

LLNL 的 rocmcc7.0.0 示例额外给 ASan 库 rpath；运行用 `HSA_XNACK=1 ASAN_OPTIONS=detect_leaks=0` 避免语言runtime分配造成的退出LSan噪声。其 Known Issues 列 `hipFree` 双重释放、`hipMemcpy` 非法地址、CPU/HIP错配释放未被其环境可靠检出。建议引用格式：**“LLNL 当前 El Capitan 指南（正文示例测试版本 rocmcc7.0.0）仍列出这些限制；我们应把这些API列入自研验证集，而非假设编译插桩已经覆盖。”** 不能仅用该文献写“AMD现版本不支持所有这三项”。

## 5. MUSA：公开样本的价值与边界

当前文档确证 `mt-compute-sanitizer` 的 global OOB、MUSA API error、显存泄漏；泄漏默认关闭。v1.1.1改变 API error 默认为 explicit，只报告用户显式顶层调用错误，并过滤 NotReady/peer-access状态类返回，修复工具导致结果不正确的问题。v1.1.0曾重构 OOB并提示 Driver5.1.0+VMM仍可能误报、推荐5.2.0以上。当前 user guide 明确 atomic/robust LSU/TME在计划中，private/shared/local/host pinned不完整，工具拖慢运行并改变时序/内存布局。

架构启示可以作为“我们的判断”写：UMD分配/API拦截、状态返回过滤、错误去重/详情截断与计数一致性、版本配套应作为正式产品能力。不可写成“国内GPU普遍采用相同实现”；也不能把名称、命令行相似推断成 NVIDIA二进制重写技术或LLVM实现相同。

## 6. Intel：覆盖丰富，但配置与检查退化必须一起列

> 以下为 2026-10-20 历史核验记录。当前版本化详细资料见 [Intel oneAPI 设备端 Sanitizers 专题](../intel-oneapi-device-sanitizers.md)，2026-10-23 已复核 Intel 2026.0 官方说明。本文保留取证历史，不并行维护正式详细规范。

2026.0 device ASan 的正式表涵盖 global/USM/local/private OOB、misaligned、NULL、UAF、invalid argument、leak；SYCL另有 bad-context/bad-free/double-free/buffer，OpenMP不应照抄这些SYCL特有项。MSan自2025.1，local/private自2025.2；TSan自2025.2支持 device OpenMP/SYCL，侧重 device USM。

总章限制：Linux、Ponte Vecchio GPU单卡配置；ASan强制kernel顺序执行；private memory增加可降低最大workgroup并触发 INVALID_WORK_GROUP_SIZE；GPU workgroups多时可跳过ASan local/private OOB与MSan local/private UUM检查。GPU元数据大小和运行配置会影响资源及检测能力。OpenMP需 Unified Runtime plugin和UR_ENABLE_LAYERS，SYCL/CPU及GPU selector与OpenMP不可混用。

## 7. 对最终文稿的证据使用建议

1. 把友商能力表标成“公开文档能力与限制，核验于2026-10-20”，不用 SOTA、行业最强等排名。
2. 把编译期对象边界、runtime allocation lifetime、UMD异步事件、report completeness分开比较；API error返回与内存实参合法性是不同检查。
3. 以覆盖测试集验证自己的Compiler/Device Runtime/UMD/KMD真实契约；“零错误”不等于已检查全部语义，特别要显式记录未插桩module/function/address-space、未知或未覆盖的目标ISA指令、report溢出与fallback。
4. 本次产品调查的事实与由这些事实导出的架构建议应分别标注，论文性能数据不可移植到商业产品/自研目标。

## 8. 关键来源导航

完整机器可读清单见同目录 `vendors_sources.json`。每条包含标题、规范URL、版本日期、章节与适用范围。核心来源是 NVIDIA手册+Release Notes+NVCC13.4+2025博客+API Guide、AMD7.2.4版本化ASan页、LLNL现场指南、MUSA1.1.1发布说明+user guide、Intel2026.0总章+device章。
