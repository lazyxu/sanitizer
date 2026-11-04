# GPU Memcheck 实施方案：B1 / B2 / B3 与条件注入 C

**版本：1.0｜日期：2026-11-04｜用途：内部技术评审与归档**  
**实现状态：自研设计与工程估算；目标 GPU 尚未实施、未实机验收（GPU_NOT_RUN）。**

## 0. 摘要与结论速览

本文将友商公开参照限定为 NVIDIA 的 NV-R（运行时 binary patching）与 NV-C（编译期 base-and-bounds，官方称 compile-time instrumentation），以及 AMD GPU AddressSanitizer 的公开形态；自研部分是 B 包的三个配置：B1 单对象 Live 区间、B2 字节级 Shadow（参照 AMD 方案）、B3 原对象 Base/Size（参照 NV-C）和可选注入方式 C。B1 是仓库自研设计，不参考 NV-C；B3 沿用仓库已有对象来源设计并在本文改用新代号。

| 约束组合 | 更匹配配置 | 理由 |
|---|---|---|
| 释放后访问与对象内局部 Poison 是主要缺陷 | B2 | 字节级状态可表达 Redzone、子分配与 Poison；依赖安全 Shadow 驻留 |
| 跨紧邻对象的一次宽访问是主要缺陷 | B1 | 整次访问必须落在同一个 Live 对象内，能拒绝跨界宽访问 |
| 来源对象 A 的派生地址完整落入另一个 Live B 是主要缺陷 | B3 | 检查使用指针携带的原对象 Base/Size，而不是重新按地址查当前对象 |
| 闭源模块确实无法重编译 | C + B1/B2/B3 之一 | C 改变注入位置，不独立提供检查谓词；只覆盖经验证可重写的机器码 |

B2 的交付必须分账：[A] Sanitizer 自身的编译插桩、Shadow/Poison、Allocator 与设备报告；[B] 另立项的 KMD 可恢复缺页与 retry 能力。当前目标 KMD 不具备为 Sanitizer 引入的可恢复缺页能力，[B] 不计入 [A]。自研所有性能判断均为 GPU_NOT_RUN，必须在目标 GPU 上测量后才能形成性能结论。[自研设计，GPU_NOT_RUN]

**范围：** 只讨论 NVIDIA、AMD 两家友商的公开 Memcheck 参照，以及自研 GPU 的空间访问、分配释放状态、泄漏与硬件异常归因。

## 1. 目标与验收口径：Memcheck 到底要抓什么

Memcheck 的交付目标是让每个受支持的设备访存，在原访存真正执行前得到可归因的判定；并让 UMD 对分配、释放、重复释放和泄漏形成一致的对象账本。仅有编译成功、插入了检查调用或 Host API 拦截，不代表设备访存已经受到保护。[自研设计，GPU_NOT_RUN]

| 缺陷类 | 裸跑为何抓不到 | 期望报错形态 |
|---|---|---|
| 空间越界、跨 allocation | 设备地址可能仍落在有效映射页内，硬件不会理解逻辑对象边界 | 越界类型、读写类型、地址、访问宽度、对象边界、lane、PC |
| 一次宽访问跨紧邻对象 | 首地址和末地址可能都在已映射区域，单看地址不能证明整次访问属于一个对象 | 完整访问区间、跨越的对象边界、来源对象（若配置具备） |
| 地址完整落入另一个 Live 对象 | 当前地址仍然可访问，裸地址无法说明它原来由哪个对象派生 | 原对象 Base/Size、实际地址与宽度、来源传播状态 |
| 未对齐访问 | 部分指令或硬件可能容忍未对齐，或只在特定访问宽度/地址空间触发异常 | 要求对齐值、实际地址、访问宽度、指令 PC |
| 释放后访问 | backing 可能仍映射且物理页未回收，普通执行不一定立即故障 | UAF、已释放对象范围、释放发布点、访问 PC/lane |
| double-free | 第二次释放可能落入分配器内部状态而不触发设备访存故障 | API 调用位置、对象句柄、重复释放状态 |
| 泄漏 | 进程退出时仍有 GPU allocation 不代表内核硬件会报错 | 未释放对象数、字节数、分配调用点及所属 Context |
| GPU 硬件异常 | 裸跑可能只返回异步设备错误，缺少触发访存的上下文 | Fault 地址、访问类型、队列/Context、设备 PC、可用的 lane 与模块映射 |

反例准入声明：只保留至少一个配置能抓、至少一个配置会漏的反例；所有配置都无保证的场景不写、不提。

### 1.1 反例：跨对象宽访问

下面的最小设备侧示意假定测试分配器将两个独立逻辑 allocation 紧邻布置：A 为前 32 字节，B 为后 32 字节。测试布局刻意不在两者之间插入 Poison 字节，使 Shadow 只看到两个相邻的可访问区域。该布局用于隔离检查谓词的表达能力，不代表所有生产分配器都会如此布局。

~~~cpp
#include <cstdint>
struct alignas(8) Wide16 { uint64_t lo; uint64_t hi; };

// 测试夹具：arena[0,32) 是 allocation A；arena[32,64) 是 allocation B。
alignas(16) unsigned char arena[64];

void probe_adjacent_allocations()
{
    unsigned char* A = arena;
    unsigned char* B = arena + 32;

    // A 派生地址 [A+24, A+40)，16 字节宽访问跨过 A/B 边界。
    volatile Wide16* cross = reinterpret_cast<volatile Wide16*>(A + 24);
    *cross = Wide16{0x11, 0x22};
    // B1：报。整次访问不属于同一个 Live allocation。
    // B2：漏。两侧 Shadow granule 均可访问时，Shadow 不表达 A/B 对象边界。
    // B3：报。来源为 A，访问末端超出 A 的 Base/Size。
    // NV-R：漏。按当前地址可访问性检查时，映射地址仍有效。
    // AMD 方案：漏。当前 granule 合法时不区分 A/B 对象边界。

    // 变体：仍从 A 派生，但实际访问完整落入另一个 Live allocation B。
    unsigned char* derived_from_A = A;
    derived_from_A += 32; // A 的 one-past 地址，随后访问落在 B。
    *reinterpret_cast<volatile uint32_t*>(derived_from_A) = 7;
    // B1：漏。按地址查到当前 Live B，完整访问包含在 B 内。
    // B2：漏。目标字节处于可访问 Shadow 区域。
    // B3：报。来源仍为 A，地址不属于 A 的原始范围。
    // NV-R：漏。当前地址可访问。
    // AMD 方案：漏。Shadow 只表示目标字节是否可访问。
}
~~~

这里 B2 与 AMD 方案的漏报是 Shadow 表达能力的固有边界：当 A、B 之间没有 Poison 间隔，且目标 granule 被标为可访问时，Shadow 无法仅凭字节可访问状态恢复两个独立对象的边界。这不是 AMD 方案实现缺陷，也不意味着常见 Redzone 布局下的所有跨界访问都会漏报。该反例只用于区分 B1 的“同一对象完整包含”与 B3 的“原对象来源边界”。[厂商公开] [自研设计，GPU_NOT_RUN]

### 1.2 反例：释放后访问

下面假定 free 已经按 API 队列依赖生效，且对象仍处于 Poison/退役状态、尚未还给复用池。该条件隔离的是释放状态判定本身。

~~~cpp
#include <cstdlib>
#include <cstdint>

void probe_use_after_free()
{
    volatile uint32_t* p =
        static_cast<volatile uint32_t*>(std::malloc(64));
    if (!p) return;

    p[0] = 1;
    std::free(const_cast<uint32_t*>(p)); // 释放已发布，块仍 Poison/退役且未复用。
    p[0] = 2;

    // B1：报。对象索引中的该区间已退役，不再是 Live。
    // B2：报。释放后对应 Shadow 已 Poison。
    // B3：单独不足以报。Base/Size 只说明来源范围，须组合 B1 或 B2 的释放状态。
    // NV-R：依赖对象撤销是否已被运行时检查元数据观察到。
    // AMD 方案：报。GPU ASan 的 Poison/运行库生命周期路径可报告该 UAF。
}
~~~

释放后访问反例只证明 Poison/退役发布完成且尚未复用时的判定能力；Poison/退役发布顺序归 UMD 负责。它不证明地址重新分配后还能仅凭裸地址恢复对象状态。[厂商公开] [自研设计，GPU_NOT_RUN]

### 1.3 验收维度与退出条件

| 验收维度 | 必须留存的证据 |
|---|---|
| 谓词层级 | 当前地址可访问性；地址是否落在原来源对象内。两层不得合并成一个“越界检查”勾选项 |
| 元数据载体 | B1：设备对象索引与已发布版本快照；B2：Shadow/Poison 字节状态；B3：随指针传播的原对象 Base/Size |
| 归因深度 | lane + PC + 越界字节数 + allocation site；不能提供的字段标 Unknown，不伪造 |
| 退出条件 | 两个反例均在目标 GPU 实测；分别记录报错配置、漏报配置和漏报的固有表达边界；合法对照不误报；工具故障不记为通过 |

首轮交付必须同时提供：两个反例的源码与构建记录、合法对照、目标 GPU 型号与驱动/工具链版本、最终设备指令覆盖清单、设备端原始报告、UMD 对象事件日志、GPU_NOT_RUN 或 GPU_DETECTED 状态。静态检查通过只证明文档/源码结构，不证明设备端判定。[自研设计，GPU_NOT_RUN]

## 2. 友商实现解剖：NVIDIA（NV-R / NV-C）× AMD（AMD 方案）

本章只记录官方手册、Release Notes 与公开 API 能直接支持的行为。闭源重写器内部数据结构、索引布局和未公开指令处理方式统一标注“依据公开行为反推”；推断不能证明厂商内部实现细节或目标 GPU 可直接复用。

### 2.1 NVIDIA · NV-R（运行时 binary patching）

NV-R 的公开外部行为是运行时对设备代码进行 binary instrumentation，在受支持的设备访存前执行检查，并结合运行时 API 与设备报告形成诊断。[厂商公开] 公开 Patching API 能支持工具注册回调、识别指令类别并进行插桩相关操作；它并未公开 Compute Sanitizer 内部完整的二进制重写器、对象索引结构或所有机器码变换细节。[厂商公开]

**增量落层简图**

~~~text
Compiler：复用既有编译产物、lineinfo；生产编译器无 Sanitizer 增量（0 kLOC）
    ↓
UMD：模块装载/JIT 入口 → ISA 译码/重编码 → 访存前设备检查 → 原 PC 映射
    ↓
KMD：复用既有页表/MMU/fault 上报；无增量（0 kLOC）
~~~

| 层 | 复用基线 | Sanitizer 增量 | 接口契约变化 | 增量量级 | 证据标签 |
|---|---|---|---|---|---|
| 编译器 | 既有设备代码与 lineinfo | 无；NV-R 插桩发生在运行时 | 无新增编译 ABI | 无增量（0 kLOC）；不要求重编译应用 | [厂商公开] |
| UMD | Loader、JIT、模块与分配 API 拦截 | ISA Decode/Encode、访存前插入检查、重定位、PC 映射、资源复核、对象登记与生命周期跟踪 | 模块缓存键、原始/插桩 PC 对照、Patch 覆盖清单 | 自研复刻为工程估算；厂商内部量不可知 | [厂商公开] / [推断，待核] |
| KMD | 页表、MMU、fault 上报、队列完成 | 无增量（0 kLOC）；当前地址检查在设备插桩代码中完成，不需改变页表语义 | 无 | 0 kLOC | [推断，待核] |

**ISA 译码与重编码。** 依据公开行为反推，运行时需要识别访存操作数、访问宽度、读写类型和地址空间，并在原访存之前插入检查代码；重写后还需修复分支目标、相对寻址、寄存器占用、谓词/执行掩码、等待依赖与代码位置。公开 API 不足以推断其内部采用哪一种 Decoder/Encoder、是否使用特定 MC 库、如何组织私有对象表，亦不能据此声称支持所有 ISA 修订。[推断，待核]

**检查与归因。** 检查必须真正控制原访存：失败时不能继续执行原访存。插桩代码要保存必要寄存器与原始 PC，并在报告中恢复原指令位置；若重写引入额外指令，需校验分支范围、寄存器压力与资源限制。具体修复序列属于闭源内部细节，不能写成已知事实。[推断，待核]

**对象服务与生命周期。** UMD 必须在分配成功后登记对象，在释放真正生效时撤销可访问状态，并保证设备检查读取到与队列顺序一致的元数据。NV-R 对当前地址的检查并不自动具备原来源对象语义。具体对象索引布局、是否采用区间树或其他索引、内部如何处理子分配，均不能从公开外部行为推出。[推断，待核]

**为何 KMD 无增量。** 在既有页表、MMU、队列完成与 fault 上报可用的基线下，NV-R 的空间谓词由插桩后的设备指令执行；不需要 KMD 参与每次对象边界判断。[推断，待核]

**Padding 的漏报本质。** 若分配器把逻辑对象尾部扩展到可访问的填充区，而检查器元数据也把填充区当作合法范围，则落在 padding 内的逻辑越界可能不会被当前地址检查识别。padding 不是原对象边界；它能降低相邻对象相互污染，却不能代替原对象 Base/Size 谓词。[自研设计，GPU_NOT_RUN]

**覆盖边界。** ISA 修订、可重写模块、JIT 产物、特殊访存指令、内联汇编、模块函数级混合覆盖等边界必须逐项验证；不得把“支持 binary patching”扩大解释为所有机器码均可安全重写。[厂商公开] 未列出的内部覆盖细节不能由公开 API 推出。[推断，待核]

### 2.2 NVIDIA · NV-C（编译期 base-and-bounds）

NV-C 是 NVIDIA Compute Sanitizer 的 compile-time instrumentation 能力：编译阶段分析指针来源并传播 Base/Bounds，在设备访存处执行原对象范围检查；当来源分析不可用时，官方说明会退回普通分配边界检查，而不是保证所有指针都保留原对象来源。[厂商公开]

**增量落层简图**

~~~text
Compiler：指针来源分析 → Base/Size 传播 → 访存前 bounds 检查 → unknown 回退
    ↓
UMD：编译选项/运行库兼容、对象初始描述符与生命周期
    ↓
KMD：复用既有页表/MMU/fault 上报；无增量（0 kLOC）
~~~

| 层 | 复用基线 | Sanitizer 增量 | 接口契约变化 | 增量量级 | 证据标签 |
|---|---|---|---|---|---|
| 编译器 | LLVM/目标后端、既有 ABI 与调试信息 | 指针来源分析、Base/Size 传播、检查插入、Unknown 降级与最终访存审计 | 需传递对象边界描述；传播失败必须保留 Unknown 状态 | 厂商未公开源码量；自研 B3 沿用 v1.8 家族区间，不作独立报价 | [厂商公开] / [工程估算，v1.8] |
| UMD | 分配器、模块装载、kernel launch | 对象描述符、参数侧信息、生命周期与运行库配合 | 描述符与模块 ABI 版本需匹配 | 与 B1/B2 共用对象服务部分 | [推断，待核] |
| KMD | 页表、MMU、fault 上报 | 无增量（0 kLOC）；对象来源是软件语义，不由页表推导 | 无 | 0 kLOC | [推断，待核] |

自研 B3 必须处理 GEP/指针加减、phi/select、函数参数与返回值、指针加载/存储、整数与指针转换、地址空间转换及优化器折叠。每种变换都要定义“可证明传播”“保守 Unknown”“拒绝编译/加载”的规则。Base/Size 应采用防整数溢出的区间检查，不能先计算可能回绕的 base+size 或 address+width。[自研设计，GPU_NOT_RUN]

官方 NV-C 的公开说明支持分析失败时回退普通分配检查这一方向；不支持据此推断任意丢失来源的操作仍可做原对象检查。自研 B3 必须将 Unknown 单独写入覆盖报告：若策略回退到 B1/B2 的地址谓词，报告中不得将其记为原对象检查通过。[厂商公开] [自研设计，GPU_NOT_RUN]

编译期分析只对实际经过支持编译流程且被插桩的代码成立。运行时生成代码、不可重编译模块、inline asm、特殊访存及 ABI 边界需要单独覆盖清单；不能从官方方向性描述推断任意 JIT 代码都自动获得 B3 检查。[推断，待核]

官方资料表达了编译期来源分析对检测能力与开销方向的改善，但没有适用于所有应用、GPU、工具链和访问模式的通用开销倍数。自研 B3 的寄存器压力、spill、代码尺寸与 Unknown 比例均为 GPU_NOT_RUN。[厂商公开] [自研设计，GPU_NOT_RUN]

本文自研对应见 4.5，代号 B3；仓库原代号 D1，改名只为避开已删除的时间身份 D 包，不是新造第三套机制。

### 2.3 AMD 方案：编译插桩 + Shadow/Redzone + 运行库（XNACK 条件）

AMD 方案是 AMD GPU AddressSanitizer 的公开形态，本文只称“AMD 方案”，不叫 B2；自研对其的参照实现见 4.4，代号 B2。

AMD GPU ASan 的公开实现组合了 LLVM 设备端插桩、Shadow/Redzone 与配套运行库；GPU 端运行需要满足对应 ROCm 版本、GPU 架构、XNACK/HMM 与运行库条件。[厂商公开]

**增量落层简图**

~~~text
Compiler：LLVM AMDGPU ASan instrumentation → Shadow 检查
    ↓
UMD/Runtime：Allocator、ShadowManager、Poison/Unpoison、GPUVA 映射与发布
    ↓
KMD/OS：XNACK+ 可恢复缺页与 HMM 相关系统能力（官方部署条件）
~~~

| 层 | 复用基线 | Sanitizer 增量 | 接口契约变化 | 增量量级 | 证据标签 |
|---|---|---|---|---|---|
| 编译器 | LLVM AMDGPU 后端与已有 IR/机器码流水线 | 设备访存插桩、Shadow 地址计算与状态判断、受支持地址空间的 Lowering | 编译目标与运行库 ABI/架构条件需匹配 | 自研 B2 估算沿用 v1.8 B 家族区间，不独立拆报 | [厂商公开] / [工程估算，v1.8] |
| UMD | HIP Runtime、Allocator、队列与模块装载 | Shadow/Redzone 分配、Poison/Unpoison、GPUVA 管理、元数据初始化与队列发布 | 分配/释放与 Shadow 状态需遵循同一异步队列顺序 | B2 [A] 的主要增量 | [厂商公开] / [自研设计，GPU_NOT_RUN] |
| KMD | OS HMM、GPUVA 与 fault 处理基线 | 官方部署依赖现有 XNACK+ 可恢复缺页；自研若要为 Sanitizer 新增 retry 能力则属于独立 [B] 项目 | 缺页 retry、Context 与 fault 完成契约 | 官方平台条件不等于自研 KMD 已有能力 | [厂商公开] / [推断，待核] |

经典 1:8 Shadow 将每 8 个应用字节映射到一个 Shadow 字节；Shadow 编码可表示整个 granule 可访问、不可访问或末端有效前缀等状态。它不是“每个对象 8 字节”，也不自动保留 allocation 身份。单字节有效前缀无法表达同一 granule 内任意两个不连续有效岛；更细的位图/分层布局需要额外存储与查询逻辑。[厂商公开]

Allocator 在逻辑对象周围布置 Redzone，并在分配、释放、重分配时同步更新 Shadow。Poison/Unpoison 必须先准备并初始化元数据，再通过队列依赖发布；设备端不能在 Shadow 尚未映射或初始化时执行检查。Shadow 发布顺序由 Runtime/UMD 负责。[厂商公开] [自研设计，GPU_NOT_RUN]

无可恢复缺页能力的目标 GPU 若要安全执行 Shadow Load，必须先保证 Shadow 根、目录、叶页和报告页已经映射、驻留并在检查期间保持有效。不能先盲读 Shadow，再把未知地址引起的 GPU fault 当成可恢复路径。[自研设计，GPU_NOT_RUN]

AMD 对应版本的 GPU ASan 文档要求满足其列明的 XNACK/HMM 和设备/Runtime 条件；具体支持架构随 ROCm 版本变化，应以版本化安装文档为准。非 XNACK 平台不能仅靠环境变量获得相同的可恢复缺页能力；没有设备端安全映射时，不能宣称 GPU ASan 检查完整。[厂商公开] XNACK 处理可恢复缺页及相关重试，不负责判断访问是否落在逻辑对象内，也不提供 Base/Size 来源语义。[厂商公开]

AMD 公开报告可定位设备错误与源码位置，但 Host 侧调用栈深度、符号化完整度及不同版本的报告字段需要按对应工具链验证；不能把设备 PC 回溯等同于完整 Host launch-site backtrace。[推断，待核]

在跨对象宽访问反例中，如果目标 granule 合法且访问字节没有被 Poison，Shadow 不区分相邻 A/B，因此会漏报。这是 Shadow 只表达可访问性的必然边界，不是缺陷；对应第 1 章反例。[厂商公开]

无 XNACK 的安全部署路径指向仓库专题 docs/amd-gpu-asan-hardware-fallback.md，本文不重写其映射取舍。

## 3. 三配置能力 × 成本对照

### 3.1 能力矩阵

| 场景 | NV-R | AMD 方案 | 自研 B1 | 自研 B2 | 自研 B3 |
|---|---|---|---|---|---|
| 越界进未映射区 | 可报，依赖支持的机器码覆盖 | 可报，依赖 GPU fault/运行库条件 | 可报，地址索引需完整登记域 | 可报，Shadow/元数据查找必须安全 | 回退地址检查可报；来源未知时不能给原对象结论 |
| 宽访问跨紧邻 A/B | 漏 | 漏 | 报 | 漏 | 报（来源在） |
| 派生地址完整落进另一 Live B | 漏 | 漏 | 漏 | 漏 | 报 |
| 释放后访问 | 依赖撤销状态已发布且检查可见 | Poison 可报 | 退役可报 | Poison 可报 | 需搭 B1/B2 的释放判定 |
| 大对象内部局部失效 | 需子模型 | 可表达，受 Shadow 粒度/编码限制 | 需子对象登记 | 可表达，受 Shadow 粒度/编码限制 | 需子对象 Bounds |
| 未对齐／硬件异常 | 产品其他能力或硬件 fault；不等同对象边界谓词 | 不负责通用对齐保证 | 不管 | 不管 | 不管 |
| 闭源不可重编模块 | 支持机器码可覆盖，仍受 ISA/模块边界限制 | 无插桩保证 | 无 | 无 | 无 |

**判据。** NV-R 与 AMD 方案同属“当前地址可访问性”层，对跨对象宽访问同样漏——这不是谁差，而是同一层表达力的上限。B1 与 B3 都属于对象级检查，但一个查表得当前对象，一个指针自带原对象：B1 抓跨界、不抓换对象；B3 在来源保留时两者都抓，代价是来源传播与 ABI。两者互补，不是强弱档。B1/B2 同属编译插桩，差异在元数据粒度（对象级 vs 字节级），不是两套完全不同的算法。[自研设计，GPU_NOT_RUN]

### 3.2 成本表

| 方案 | 成本来源 | 自研含义 | 证据标签 |
|---|---|---|---|
| NV-R | 机器码译码/重编码、控制流与寄存器修复、PC 映射、特殊指令覆盖和首次 Patch 成本 | 若不可重编模块数量少，需先统计覆盖收益；不把厂商私有实现量当作自研报价 | [推断，待核] |
| AMD 方案 | LLVM 设备插桩、Shadow/Redzone、Runtime、GPUVA 映射、XNACK/HMM 平台依赖 | B2 借鉴公开机制，但无 XNACK 自研目标需按安全预驻留设计 | [厂商公开] |
| NV-C | 指针来源分析、Base/Size 传播、设备检查、Unknown 回退与 ABI 维护 | B3 沿用 v1.8 家族区间，B1/B3 无独立报价，不编精确到千行的数 | [厂商公开] / [工程估算，v1.8] |

**预算口径。** v1.8 的 B、C、D 等数字是历史未拆项的家族估算，不能拆成 B1/B2/B3 各自的独立报价。本文写“沿用 v1.8 家族区间，B1/B3 无独立报价”；P 只计一次，B1/B2/B3 共享编译插桩与对象服务的公共部分，不能按三条全量路线相加。kLOC 指新增源码行，不含 vendoring；手写测试 kLOC 单列；所有数字均为粗估，不据此直接排工期。[工程估算，v1.8]

涉及性能的自研配置均未在目标 GPU 运行（GPU_NOT_RUN）。NV-R/NV-C/AMD 官方资料没有可直接套用到自研目标 GPU 的通用开销倍数；性能对比必须在同一 GPU、编译器、驱动、工作负载与检查覆盖条件下重新测量。[自研设计，GPU_NOT_RUN]


## 4. 自研落地方案：P + B 谓词组合（B1/B2/B3 平权，C 为条件项）

### 4.1 评估框架与责任划分

Compiler 负责访存语义、指令覆盖、来源传播和检查谓词；UMD 负责对象真值、元数据发布、报告 ABI、分配释放与模块集成；KMD 负责硬件可观测性与可靠终态。检查逻辑不下沉 KMD 充数。既有编译器 ABI/指令选择/lineinfo、UMD launch/heap/拦截框架、KMD 页表/MMU/fault 上报均视为基线；只有真实缺口才计 Sanitizer delta。[自研设计，GPU_NOT_RUN]

### 4.2 公共底座 P（只写一次）

| 层 | 公共增量 | 契约变化 | 粗估 |
|---|---|---|---|
| Compiler | 统一 Sanitizer target option、访存分类、插桩 ABI、Mask/EVL/byte-enable 传递、DebugLoc/PC 关联 | 每条实际访存必须声明是否插桩；Unknown opcode/空间必须显式覆盖缺口 | 沿用 v1.8 P 家族区间，不另造精确数字 |
| UMD | Report ABI、对象服务入口、模块/launch 标识、分配与释放事件、Guard/队列顺序、报告收集与可靠停止 | 分配发布、Poison/退役、设备检查与终态形成明确顺序 | 沿用 v1.8 P 家族区间，不另造精确数字 |
| KMD | 在现有 fault/完成能力上确认 fault 扩字段、wave/dispatch 冻结或等效可靠终态能力 | 必须区分 fault 已报告、终止请求、设备不再访问的终态证明 | 基线满足时无增量（0 kLOC）；缺失项单独核算 |

P 的报告记录至少包含 ABI 版本、Context/launch/module 标识、设备 PC、原始 PC 映射、lane、地址、访问宽度、读写类型、错误类型、对象边界/对象 site（可用时）、覆盖状态、报告丢失计数。Mask、元素 mask、EVL 与 byte-enable 不应假设固定宽度；以扩展记录描述真实活动访存。报告槽位写入需先预留、写完再发布；日志满时仍要置 sticky error 与 dropped 计数。Guard 必须保证检查失败后原访存不执行。

UMD 的队列契约：分配对象描述符和对应 B1/B2 元数据初始化完成后再发布可用状态；Free 在 API 依赖生效点发布退役/Poison；物理 backing、Shadow 页和对象记录只能在设备读者与报告消费者均结束后回收。工具内部准备元数据的 Fence 不得伪造应用本身缺失的依赖。[自研设计，GPU_NOT_RUN]

### 4.3 配置 B1：编译插桩 + 单对象 Live 区间／版本

不参考 NV-C。NV-C 的 base-and-bounds 是“指针携带来源”，归 B3；B1 是“地址查索引判同对象”，来源信息完全不参与。

| 层 | 增量 | 契约变化 | 估算口径 |
|---|---|---|---|
| Compiler | 覆盖受支持访存；原访存前插入 addr → 候选对象索引查询 → 整次访问包含于同一对象的谓词 | 输入只有实际地址、宽度、访问类型与地址空间；不读取指针来源元数据 | 共享 B 编译插桩区间 |
| UMD | 维护版本化区间索引；分配、Free、子分配按队列发布/退休；维护设备可读快照 | 快照在 kernel 执行期间稳定，更新有发布 Fence；登记域完整性可查询 | 与 B2 共享对象服务部分 |
| KMD | 不增加对象判断逻辑；复用已有页表/MMU/fault | 无新增 ioctl 的前提是现有资源与终态契约足够 | 无增量（0 kLOC）；理由：索引查询在设备侧完成 |

设备侧查找必须在正确 Context/address-space 域内定位候选对象，并以防溢出方式验证完整访问宽度包含在同一对象。索引可以是有序区间表、分层页表或其他 GPU 友好的结构；最终选择由对象数量、更新频率、访存局部性和并发读者决定，不能仅凭 CPU 上的 O(log N) 推断 GPU 延迟。[自研设计，GPU_NOT_RUN]

**最强场景：** 第 1 章宽访问跨 A/B 边界。B1 找到 A 后发现访问末端超出 A，即使后续字节落入 B 也拒绝。

**短板：** 派生指针完整落进另一个 Live B 时，B1 会按地址找到 B 并放行；该情况需要 B3。大对象内部局部失效需要额外子对象登记；释放判定依赖区间退役及时可见。

**预算：** 沿用 v1.8 P+B 家族区间；无独立 B1 报价，与 B2 共享编译覆盖与分配账本。[工程估算，v1.8]

### 4.4 配置 B2：字节级 Shadow/Poison（参照 AMD 方案）

B2 参照 AMD 方案的公开 Shadow/Redzone 形态，但属于自研实现，不代表 AMD 官方文档使用 B2 代号。

**[A] Sanitizer 增量**

| 层 | 增量 | 契约变化 | 粗估/证据 |
|---|---|---|---|
| Compiler | Safe Shadow Lookup 插桩；按实际访问字节覆盖全部相关 granule；处理 Mask/EVL 与地址溢出 | Shadow Load 前必须验证映射状态；不能将元数据 fault 当应用 OOB | 沿用 v1.8 B 家族区间；GPU_NOT_RUN |
| UMD | Allocator/ShadowManager、Redzone、Poison/Unpoison、Shadow 页管理、GPUVA 预驻留、队列发布与退休 | 元数据初始化完成才发布；Free 的 Poison 在正确队列依赖后生效 | 沿用 v1.8 B 家族区间；GPU_NOT_RUN |
| KMD | [A] 不增加缺页 retry；仅复用已有映射/完成能力 | 对缺失 Shadow 页返回工具错误或在提交前拒绝，不盲读 | 无增量（0 kLOC）+ 复用现有映射/故障能力 |

**[B] 前置缺页项目（另立项，不并入 [A]）**

| 子项 | 需实现/验证内容 | 验收证据 | 估算处理 |
|---|---|---|---|
| Retry 语义 | 识别可恢复 fault、保存 dispatch/wave 状态、完成页映射后精确重试，禁止重复副作用 | 同一访存只执行一次；fault/重试/完成状态可追踪 | 另行技术设计与报价 |
| Context 查找与限流 | 从 fault 定位 Context/队列/资源，限制并发缺页请求与内存压力 | 多 Context、fault storm、超时与取消回归 | 另行估算 |
| 可选 poison_range ioctl | UMD 能安全请求 KMD 建立/更新稀疏元数据映射；校验地址范围、权限、Context 与生命周期 | 非法请求拒绝、并发更新与释放顺序测试 | 作为可选接口，不预设一定采用 |
| 回归矩阵 | 缺页、映射失败、OOM、取消、设备复位、并发 Context、重试次数上限 | 所有路径均有明确终态，不出现无限重试或资源提前回收 | 单独列测试 kLOC 与设备资源 |
| 人月与量级 | 需先明确 KMD fault 架构、wave 可恢复能力、固件配合和芯片限制 | 由 KMD/硬件负责人给出接口审查与原型结果 | 当前无可靠数据，不编人月 |

[B] 不能与 [A] 相加形成一条路线总价；若 [B] 不立项，B2 只能采用 [A] 的全量预驻留与安全查找策略，不具备缺页兜底。[推断，待核]

**最强场景：** 释放后访问与大对象内部局部失效；Shadow 粒度允许表达的 Redzone/Poison 可逐字节检测。

**短板：** 对跨对象宽访问，若目标 granule 合法则漏，与 AMD 方案同源的固有边界；若该类是真实痛点，使用 B1（跨界）或 B3（换对象），不能把 B2 加厚成原对象谓词。没有 XNACK/自研 retry 时，Shadow 页必须预驻留；无法保证驻留就拒绝 launch 或报告工具故障。[自研设计，GPU_NOT_RUN]

### 4.5 配置 B3：原对象 Base/Size 随指针传播（参照 NV-C，仓库原 D1）

B3 参照 NV-C 的 base-and-bounds 思路。检查力来自“指针还记着从哪个对象来”，不是当前地址反查得到哪个对象。

| 层 | 增量 | 契约变化 | 估算口径 |
|---|---|---|---|
| Compiler | LLVM SSA/MIR 来源传播；覆盖 GEP、phi、参数/返回值、指针加载、整数转换与地址空间转换；插入原对象检查 | 来源丢失标 Unknown；每类变换有可证明/降级/拒绝策略 | 沿用 v1.8 B 家族区间；无独立 B3 报价 |
| UMD | 初始对象描述符与生命周期 sidecar；参数/模块 ABI 版本；收集 Unknown 派生率 | 不允许按当前地址查表后伪造原对象来源 | 与 B1/B2 共享对象服务部分 |
| KMD | 不做来源分析，不参与每次 Base/Size 判断 | 无新增 KMD 语义 | 无增量（0 kLOC）+ 来源是编译器/运行库语义 |

B3 与 B1 的分界必须保持清晰：B3 的检查力来自“指针还记着从哪个对象来”，所以能抓地址合法但换了对象的访问；代价是上述任一处丢来源就要降级成地址检查，并如实报告 Unknown 等级。B1 没有这个代价，也没有这个能力——互补，不是强弱档。[自研设计，GPU_NOT_RUN]

B3 传播必须区分指针数值与其来源描述符。对 phi/select，只有所有输入来源可合并为同一可证明范围时才保留精确来源；对参数与返回值，ABI 需传递或侧带描述符；对指针加载/存储，内存表示必须能保存对应来源，若目标 ABI 未提供安全载体则标 Unknown；整数转换仅在可逆且受控的变换中保留来源。对任意整数运算、外部函数、未插桩代码、inline asm 和不透明内存写入，不推断来源。[自研设计，GPU_NOT_RUN]

**释放边界：** B3 的 Base/Size 自身不足以判定释放后访问，必须组合 B1 的退役状态或 B2 的 Poison。分析失败降级只保住“地址是否 allocated”，抓不到“属于哪个对象”，这是固有边界，不是实现 bug。

### 4.6 条件项 C：运行时注入（参照 NV-R）

C 只在确有闭源/不可重编模块缺口时展开；必须搭配 B1/B2/B3 之一。裸地址 fallback 不冒充原对象检查；同模块未插桩函数不自动获得检查力。

| 层 | 增量 | 接口契约 | 量级/证据 |
|---|---|---|---|
| Compiler | 无增量（0 kLOC）；生产编译器不参与运行时 Patch | 不新增生产编译 ABI | 0 kLOC |
| UMD | ISA Decoder/Encoder、机器码访存识别、插桩、CFG/relocation 修复、寄存器/控制流/谓词保持、PC map、资源复核、Patch cache | 缓存键含原模块 hash、ISA、checker ABI、配置与 rewriter 版本；不支持的指令拒绝或报告覆盖缺口 | 沿用 v1.8 C 家族区间；GPU_NOT_RUN |
| KMD | 无增量（0 kLOC） | 复用已有可执行代码提交与完成契约 | 0 kLOC |

C 的每个插桩点必须执行选定的 B 谓词。若来源信息不存在，C 不能把原始地址升级成 B3；若模块仅部分函数被插桩，manifest 必须逐函数列出覆盖。运行时重写不能热修改在途代码对象；使用新代码副本并保留原 PC 到 patch PC 映射。[推断，待核]

### 4.7 条件式选型表

| 约束组合 | 更匹配 | 关键理由 | 依赖/前置 |
|---|---|---|---|
| 释放后抓 + 对象内局部失效（§1 释放后访问） | B2 | 字节级 Poison；参照 AMD 方案的 Shadow 机制 | [A] 预驻留；[B] 另立项 |
| 跨对象宽访问是主痛（§1 那个例子） | B1 | NV-R/AMD/B2 在该例都漏，B1 的对象包含谓词可区分 | 子对象需另登记 |
| 派生指针落进另一 Live 对象是主痛（§1 变体） | B3 | 只有指针带来源才抓，B1/B2 都漏 | Unknown 派生率待实测 |
| Shadow 驻留成本高 + 对象数可控 | B1 | 对象级元数据不需要逐字节 Shadow | 局部失效需子对象 |
| 闭源模块不可重编 | C + B1/B2/B3 之一 | 仅覆盖 ISA 内机器码；谓词仍由所选 B 配置决定 | 不冒充原对象检查 |
| [B] 不立项 | B2 无硬件兜底 | 只能采用安全预驻留策略；不具备缺页 retry | 对比 B1/B3 的元数据成本 |

### 4.8 验收与退出证据

首轮交付包覆盖第 1 章两个反例及其合法对照。跨对象宽访问：B1/B3 应报，B2/NV-R/AMD 方案按其表达边界允许漏；换对象变体：B3 在来源保留时应报，B1/B2/NV-R/AMD 方案允许漏。释放后访问：在对象仍处于退役/Poison 且未复用时，B1/B2 应报；B3 必须组合释放判定。每个允许漏报的情况都要写明是谓词表达边界，而不是测试未覆盖造成的假阴性。[自研设计，GPU_NOT_RUN]

退出条件 = 目标保证、可用条件、首交包、反例实测结果。目标保证必须按地址可访问性与原对象边界分别描述；可用条件必须列出 GPU/驱动/编译器/ABI/地址空间与可恢复故障依赖；首交包包括源码、编译产物、ISA 覆盖、对象事件、报告 ABI、合法对照和原始日志。GPU 未运行时标 GPU_NOT_RUN，不得写“验收通过”。

### 4.9 附录：各配置参考排期

以下均为示例排期，非既定路线；具体先后取决于目标 GPU、人员配置、现有 Compiler/UMD/KMD 缺口和设备可用窗口。

| 配置 | 工作序列 | 可评审里程碑 |
|---|---|---|
| B1 | P 的最小 ABI → Global 访存插桩 → 对象索引/版本快照 → 分配/Free 发布 → 两个反例与合法对照 | 同对象宽访问跨界能拒绝；换对象变体按边界允许漏 |
| B2 | P 的最小 ABI → Safe Shadow Lookup → Allocator/Redzone/Poison → GPUVA 预驻留 → 发布/退休 → 两个反例与对象内局部失效 | 释放后 Poison 报告；跨对象宽访问允许漏；无可恢复缺页时不盲读 |
| B3 | P 的最小 ABI → SSA 来源分析 → GEP/phi/参数传播 → 指针加载/整数转换 Unknown 处理 → ABI sidecar → 原对象反例 | 来源保留时换对象访问报；Unknown 率与降级报告可核 |
| C | 确认闭源模块清单 → ISA 覆盖审计 → Decoder/Encoder 原型 → Guarded Patch → PC map/资源复核 → 选定 B 谓词联测 | 仅对已验证机器码声明覆盖；未插桩函数列为缺口 |

### 4.10 待团队确认清单

- [ ] flat 还是分段地址空间？这决定 Shadow 映射与地址空间分类方式。
- [ ] B3 的 Base/Size 是否占用 kernel 参数 ABI 槽位，还是采用 sidecar？Unknown 派生率允许上限是多少？
- [ ] 是否要求检查未插桩路径的越界？若要求，需要明确其机器码覆盖与设备 fault 处理路径，不能把普通地址 fault 当作完整对象检查。
- [ ] 归因深度是 lane + PC，还是还需 Host launch-site backtrace？
- [ ] CI 允许的开销上限与峰值显存（Shadow + Redzone）预算是多少？
- [ ] 哪些二进制模块闭源且不可重编？是否有真实模块样本用于 C 的 ISA 覆盖审计？
- [ ] B1 与 B2 的目标负载对象数分布是什么？Shadow 驻留是否可接受？
- [ ] 第 1 章“跨对象宽访问”是否为真实负载痛点？若是，评估 B1；若痛点是地址完整落入另一个 Live 对象，评估 B3。
- [ ] [B] 可恢复缺页项目是否单独立项？目前已确认目标 KMD 不具备为 Sanitizer 引入的 retry 能力，需另定窗口与人月。

## 5. 参考与证据索引

以下仅列 NVIDIA、AMD 官方来源与仓库内部依据。产品版本、平台条件与来源 ID 应随目标版本更新；未能由官方文字直接支撑的内部实现细节继续标注推断。

### NVIDIA 官方

| 标题 | 版本/平台条件 | 来源 ID / 链接 | 本文使用 | 证据标签 |
|---|---|---|---|---|
| Compute Sanitizer User Manual：Memcheck、compile-time instrumentation、base-and-bounds | 对应 CUDA/Compute Sanitizer 发行版本与受支持 GPU | NV-MANUAL；https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html | §2.1、§2.2、§3 | [厂商公开] |
| Sanitizer API Guide：Patching API | 对应 Compute Sanitizer API 版本 | NV-API；https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html | §2.1 | [厂商公开] |
| Compute Sanitizer Release Notes | 按实际发布版本核对限制与修复 | NV-RELEASE；https://docs.nvidia.com/compute-sanitizer/ReleaseNotes/index.html | §2.1、§2.2 | [厂商公开] |

### AMD 官方（本文所称 AMD 方案）

| 标题 | 版本/平台条件 | 来源 ID / 链接 | 本文使用 | 证据标签 |
|---|---|---|---|---|
| AMD GPU AddressSanitizer 使用说明 | ROCm LLVM 7.2.4 对应机制文档；平台限制不得跨版本外推 | AMD-ASAN-724；https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html | §2.3、§3、§4.4 | [厂商公开] |
| AMD GPU ASan 安装与部署说明 | ROCm 10.1.0 对应发行包与支持设备 | AMD-ASAN-1010；https://rocm.docs.amd.com/en/docs-10.1.0/install/asan.html | §2.3 | [厂商公开] |
| LLVM AddressSanitizer 文档 | LLVM 上游公开实现；不等于所有 GPU 地址空间均受支持 | LLVM-ASAN；https://clang.llvm.org/docs/AddressSanitizer.html | §2.3、§4.4 | [厂商公开] |
| LLVM AMDGPU 后端文档 | 对应 LLVM/ROCm 分支与目标架构 | LLVM-AMDGPU；https://llvm.org/docs/AMDGPUUsage.html | §2.3、§4.4 | [厂商公开] |

### 仓库内部依据

| 标题 | 版本/平台条件 | 来源 ID / 路径 | 本文使用 | 证据标签 |
|---|---|---|---|---|
| 核心设计分类与能力对照 | 仓库 v2.9，2026-11-02 | INTERNAL-CORE；docs/core-design-comparison.md | B1/B2/B3 语义边界与反例判据 | [自研设计，GPU_NOT_RUN] |
| 主研究报告 §10.1 / §10.2 / §10.5 / §10.6 | 仓库 v2.9 | INTERNAL-RESEARCH-B；docs/llvm-gpu-sanitizer-research.md | B1/B2 语义、边界与预算口径 | [自研设计，GPU_NOT_RUN] |
| 主研究报告 §11 | 仓库 v2.9 | INTERNAL-RESEARCH-C；docs/llvm-gpu-sanitizer-research.md | NV-R 与 C 注入 | [自研设计，GPU_NOT_RUN] |
| 主研究报告 §12.1 | 仓库 v2.9 | INTERNAL-RESEARCH-B3；docs/llvm-gpu-sanitizer-research.md | 原对象 Bounds 设计 | [自研设计，GPU_NOT_RUN] |
| 真实 GPU 组件实现文档 | 仓库 v2.8/v2.9 ABI 口径 | INTERNAL-ABI；docs/real-gpu-components-implementation.md | 报告 ABI、Mask、Free 发布、Guard、终态 | [自研设计，GPU_NOT_RUN] |
| estimates-v1.8.json | 2026-10-21 历史估算；未拆项 | INTERNAL-BUDGET；docs/estimates-v1.8.json | P + B/C 家族区间及 KMD 条件项 | [工程估算，v1.8] |
| AMD 无 XNACK 硬件适配专题 | 仓库 v2.9 | INTERNAL-AMD-DEPLOY；docs/amd-gpu-asan-hardware-fallback.md | 无 XNACK 安全部署路径 | [自研设计，GPU_NOT_RUN] |
| 架构笔记与厂商来源登记 | 2026-11-02 复核快照 | INTERNAL-NOTES；docs/research-notes/architecture.md、docs/research-notes/vendors_sources.json | 语义历史与版本化证据线索 | [推断，待核] |

**证据使用规则。** [厂商公开] 只表示对应版本官方材料直接支持；[自研设计，GPU_NOT_RUN] 表示目标 GPU 尚无实测；[工程估算，v1.8] 表示沿用历史区间而非拆项报价；[推断，待核] 表示依据公开行为反推但公开材料不足以确认内部实现。性能相关结论必须明确 GPU_NOT_RUN，直到有目标设备、版本、输入、覆盖清单和原始日志可复核。
