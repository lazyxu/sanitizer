# S01 GPU Sanitizer厂商参考与目标组件实施：NV-R / AMD-B2 / NV-C

**版本 2.4 · 2026-10-23 · 适用对象：目标实体GPU、LLVM Compiler/Device Runtime、UMD、KMD，以及实际需要的Firmware/RTL。**

> 本文是面向研发落地的**厂商方案优先**设计说明，不是某个闭源 GPU Sanitizer 的反编译说明。凡“厂商官方确认”的机制都给出来源与限长英文原文；凡没有公开具体实现的数据结构、ISA 注入方法或内核接口，均明确写作**本项目建议实现**，不冒充 NVIDIA/AMD 的私有实现。
>
> 所有产品级访存判断必须由实体GPU执行的设备Checker指令或真实存在的Tag/Bounds硬件完成；CPU侧API错误不是设备侧检查。

> **统一计数**：主要参考厂商**2家**（NVIDIA/AMD），基础空间检查参考配置**3种**（①NV-R运行时二进制＋当前地址有效性、②AMD-B2 LLVM Shadow/Redzone、③NV-C LLVM Original-Object Bounds）。这**不是3种完全不同的空间谓词**：①②均主要判断当前地址是否有效，③加入原指针对象来源。B/C/D/E/F保留为工程交付标签，不等于5套独立算法。完整来源/能力对照只维护在[核心设计唯一分类表](core-design-comparison.md)。
>
> **命名约束**：NV-C是NVIDIA编译期Reference；仓库的路线C是Runtime Binary Rewriter，不可互相替代。B1（原I）是当前Live Allocation查询的编译期实现；F1/F2必须具备真正的Tag/Bounds硬件；E是集成策略。
>


## 0. S01 应交付的检测保证（先固定问题）

**S01 的唯一详细案例规范：** 请参见 [samples/S01/README.md](../samples/S01/README.md)，其中包含 64 B 对象的错误/合法访问对照、CUDA/HIP 可编译源代码、厂商预期及实卡验收要求。本指南只保留不同成熟技术路线在这一访问语义上的**机制比较及 Compiler/Device Runtime/UMD/KMD 落地实现**，不再重复维护场景参数表。

基础 S01 的可验收能力是“已追踪对象在有效布局中的 Global 字节可访问性”。**当前 GPUVA 有效≠原对象合法**。如果 A 尾部刚好邻接存活的 B，裸地址查询可能找到 B 并通过；必须保留访问的预期原对象 A（例如编译期 Base/Bounds、设备端真实携带的来源token）才可提供更强保证（主报告 S04）；单纯根据当前地址查表不足。

**判定边界：** S01 必须在真实 GPU Global Store 生效之前校验完整访问宽度与活跃 Lane，不能把 GPUVA 页已映射误认为原对象访问合法。溢出安全的对象 Bounds、缺失对象与工具故障的通用接口统一在 [组件实施指南 §2～3](real-gpu-components-implementation.md) 维护；S01 的具体合法/非法输入与预期报告只在 [唯一案例 README](../samples/S01/README.md) 维护。

### 需要改动的生产组件（含GPU检查库）

**通用组件职责、设备执行 ABI、资源生命周期和异常终止统一在 [真实 GPU 组件实施指南](real-gpu-components-implementation.md) 定义。** 本指南只解释三种成熟厂商参考配置在 Compiler 是否需要修改、UMD 是否需要 Binary Rewriter、Device Runtime 如何判定和硬件前提上的差异；不再单独维护第二份 Compiler/UMD/KMD 实现表。

---

## 1. 参考 NVIDIA：Compute Sanitizer Memcheck 到底怎样检测 S01？

### 1.1 官方确认的事实与未经披露的细节

NVIDIA 官方 [Compute Sanitizer User Manual — Compile-time patching](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compile-time-patching) 原文：

> Compute Sanitizer memcheck supports compile-time instrumentation as an alternative to the default runtime binary instrumentation.

**含义：** NVIDIA 默认 Memcheck 是**运行时二进制插桩**；编译期插桩是另一条路径，只有 Memcheck 当前支持该模式。Racecheck、Initcheck、Synccheck 仍有自己的运行时机制，不能把它们归为同一个 Bounds 算法。

NVIDIA 2025-12-10 [官方开发者技术文章](https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/)解释运行时检测是访问点附近的二进制插桩，按**当前有效分配地址范围**检查，可能漏掉 A 越界进入同样有效的 B。**不能据此推断 NVIDIA 内部使用 LLVM ASan 的1:8 Shadow布局或某种特定对象查找树**——默认 Memcheck 的完整私有实现并未公开。

### 1.2 路径 NVIDIA-R：运行时二进制插桩（不要求特殊编译）

**真实执行链：**

~~~text
用户 CUDA Kernel（普通 nvcc 编译）→ CUDA CUBIN/FATBIN
    ↓
Compute Sanitizer在模块装载/执行期间识别待检测的GPU访存点
    ↓
在真正生效的GPU load/store/atomic之前执行插入的检查逻辑
    ↓
工具Runtime关联当下CUDA allocation/释放/流与模块、PC等信息
    ↓
合法：原GPU访存生效；非法：诊断并按工具错误策略处理
    ↓
Host报告（kernel、PC、block/thread、GPUVA、字节宽度、allocation）
~~~

**能够确认的公开接口：** [Compute Sanitizer API Reference Manual — Patching API](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html#patching-api)。官方原文：

> The Compute Sanitizer Patching API allows you to load patch functions and insert them into user code.

NVIDIA 公开的 Patching API 示例使用 `sanitizerPatchInstructions`、`sanitizerPatchModule`、`sanitizerSetCallbackData`，其设备访问回调携带 `pc`、`ptr`、`accessSize`、`flags` 等。**这些是可公开使用的 NVIDIA工具API，不代表自研 GPU现成拥有该API；也不是 NVIDIA 内部完整重写器源码。**

#### 1.2.1 “不要求特殊编译”到底意味着什么？

**【官方事实】** [NVIDIA Compute Sanitizer 用户手册 — Compilation Options](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compilation-options)：

> The Compute Sanitizer tools do not need any special compilation flags to function.

准确含义是**用户程序按原有编译器生成二进制**；运行时工具在二进制已生成以后做插桩。对自研 GPU，**并非必须修改生产编译器、LLVM Pass、CodeGen、MC 后端或重新编译应用**。上版把“可选复用已有 Compiler/MC 解码库”误写成“Compiler/MC 必须交付新功能”，这是错误的必需依赖，已修正。

需要区分四种动作：

| 动作 | 是否要求修改生产编译器？ | 应由谁负责 |
|---|---|---|
| 使用现有编译器编译原始应用 | **否**，产物可以早已存在，甚至源代码不可得 | 原编译流程；Sanitizer无额外编译要求 |
| 编译新编写的设备 Patch Callback | **否**，使用已存在的目标编译器／汇编器编译一个**工具自己的模块**；不会修改原应用二进制生成规则 | UMD Runtime Instrumentation 项目的设备 Patch 库 |
| 解码现有机器码、插入检查、修正控制流并重新编码 | **否**，可由**独立的 Runtime Binary Rewriter** 在UMD工具域实现；**复用现有 MC 库是可选实现优化** | UMD／独立运行时插桩库 |
| 扩展 LLVM Pass、修改 CodeGen/MC 来支持新插桩语义 | **是**，但**仅为某个实施选择额外要求时才做**，不是 NVIDIA-R 风格的必要条件 | Compiler可选协作，不属于纯运行时方案的强制工作 |

NVIDIA 公开 [Patching API Guide](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html#writing-a-patch) 使用普通`nvcc --cubin --compile-as-tools-patch`生成工具 Patch CUBIN，再通过 `sanitizerAddPatchesFromFile`、`sanitizerPatchInstructions`、`sanitizerPatchModule`作用于**已经存在的用户模块**。这一示例恰好说明：**编译一个新工具模块≠修改编译器≠必须重新编译原应用**。[官方Patching API](https://docs.nvidia.com/compute-sanitizer/api/group__SANITIZER__PATCHING__API.html)

#### 若模仿 NVIDIA-R，自研 GPU 每个组件要改什么？

**核心区别：生产 Compiler 可以零修改。** 已编译用户 Binary 由 UMD 内独立的 Runtime Binary Rewriter 处理：识别目标 ISA 访存及谓词/宽度、插入真正 GPU 可执行的检查 Stub、重定位控制流及 PC、核算寄存器与 Scratch。现成 MC 解码器仅是可选复用，不能当成修改生产 LLVM 的强制条件。

**唯一通用实现代码：** [组件指南 §5（patchOnLoad、正规设备代码权限、未知 Opcode）](real-gpu-components-implementation.md)、[§3（设备 Checker 与报告）](real-gpu-components-implementation.md)。UMD 仍须关联逻辑分配与 Stream/Graph、Patch 缓存及原 PC，KMD 在映射、执行或终止契约确有缺口时才改动。

这是借鉴 NVIDIA 公开机制的**自研实现建议**，并非其私有重写器源码。基础 NV-R 检查当前有效地址，仍可能漏掉 S04 的 A 派生指针进入 Live B。

#### 用 NVIDIA 原生工具先验证 S01

~~~bash
# 运行时memcheck（无需特别使用 -G 或CPU ASan）
nvcc -lineinfo -O1 -o s01 samples/S01/cuda.cu
compute-sanitizer --tool memcheck ./s01 --good
compute-sanitizer --tool memcheck ./s01 --bad
# 本仓库cuda源码还允许单独测试头部变体：
compute-sanitizer --tool memcheck ./s01 --bad --head
~~~

**预期：** good无设备非法访存；bad在实际访问尾后4B时报告；head变体需记录是否真的有头部无效区和真实GPU错误输出，不能凭源码推断必定报错。

### 1.3 路径 NVIDIA-C：CUDA 13.1+ 的编译期 Base-and-Bounds

NVIDIA 官方技术文章说明编译器尝试使用带指针、Base和Bounds的 **fat pointer**。其短摘录：

> Pointer arithmetic on fat pointers only modifies the pointer component, leaving the base and bounds intact.

**含义：** 例如 `a[16]` 计算出B的地址，预期对象仍是A，因此可以在当前地址有效的情况下报告原对象越界。编译期分析若失败，官方手册说明会降级为常规分配范围检查；**不能对未知来源仍声称达到D1保证**。

**真实执行链：**

~~~text
CUDA源 + nvcc -fdevice-sanitize=memcheck
    ↓
编译器进行指针来源/原对象Base-Bounds分析
    ↓
GEP、算术、phi/select/call等已支持路径传播expected object
    ↓
PTXAS生成带设备检查/元数据的GPU Kernel
    ↓
Compute Sanitizer memcheck运行时提供分配/报告/元数据服务
    ↓
每次实际Global访问按原对象Bounds判断，来源未知时按声明降级
~~~

#### 若模仿 NVIDIA-C，自研 GPU 各组件要改什么？

这里反而**需要 Compiler 改动**：识别指针的原始 Root/Base/Size，在 GEP、phi/select、参数、调用与受支持的指针存储/载入路径传播 expected object；UMD 为已知 Kernel 参数提供对象 Sidecar，KMD 不能从裸 GPUVA 恢复原对象 A。

指针值和原对象元数据的通用表示、溢出安全 Bounds 检查与 ABI 只维护在 [组件指南 §2、§6](real-gpu-components-implementation.md)。来源 unknown 必须单独记为覆盖降级，不得宣称具备 S04 对 Live B 的 D1 保证。

官方用法：

~~~bash
# CUDA13.1+，PTXAS/Compute Sanitizer版本必须匹配同一Toolkit
nvcc -lineinfo -fdevice-sanitize=memcheck -o s01_ct samples/S01/cuda.cu
compute-sanitizer --tool memcheck ./s01_ct --bad
~~~

编译期插桩对现有工具和编译器绑定更深、需额外寄存器/stack，也可能让程序因资源超限无法发射；测试时不应仅追求少报错。参见 [Compute Sanitizer 编译期要求](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html#compile-time-patching-requirements)。

### 1.4 NVIDIA 两种路径的工程选择

| 问题 | NVIDIA-R / 运行时 | NVIDIA-C / 编译期 |
|---|---|---|
| 需要重编译应用并提供特殊选项吗 | 不需要（普通可执行二进制） | 需要启用编译器插桩 |
| 是否需要机器码decoder/rewriter能力 | **需要，但可由UMD独立构建/复用，不要求修改生产编译器**；已有合法Patch API时复用 | 不需要二进制rewriter作为首版前置 |
| 是否必须建设LLVM来源分析 | 非首版条件 | **必须做已声明范围的原对象分析** |
| 错误落入相邻live对象B | 当前有效区检查可能漏 | 来源已知时能检出 |
| 第一版更推荐哪条 | 仅闭源模块很重要且已有成熟rewriter时 | 想要原对象保证、能改LLVM时 |
| 最主要成本位置 | **UMD/独立Binary Rewriter**、设备Patch/Report、ISA语义和覆盖审计；Compiler源代码默认零改动 | Compiler SSA来源传播、对象ABI、Device Checker |

若想兼顾预编译库与可重编译自研模块，NVIDIA官方手册也有 **Hybrid Instrumentation**：对已编译期插桩的编译单元使用该机制，对没有插桩的单元使用运行时二进制检查。针对本项目需要自行设计module manifest与能力降级，**不能**声称混用后任意未插桩函数自然获得相同D1保证。

**版本限制**：[NVIDIA Compute Sanitizer 2026.3 Release Notes](https://docs.nvidia.com/compute-sanitizer/ReleaseNotes/index.html)指出，同一模块内同时存在编译插桩和未插桩函数时，未插桩函数不保证再获得Runtime Binary插桩。因而E应以实际模块/函数覆盖证据为准，不能承诺厂商Hybrid自动完成任意粒度补洞。

---

## 2. 参考 AMDGPU：ROCm GPU AddressSanitizer 怎样检测 S01？

**硬件依赖与替代方案：** 自研GPU无需复制官方ROCm的XNACK/HMM运行前提，但必须在真实GPU上执行设备检查指令。各组件修改、无XNACK/HMM时的显式GPU Shadow替代、无MMU/Tag/Bounds硬件时的可部署软件路径及精度限制，详见[AMD逐组件与硬件替代实施指南](amd-gpu-asan-hardware-fallback.md)。

### 2.1 官方确认的组成，不是“只有LLVM Pass”

AMD [官方 Using the AddressSanitizer on a GPU](https://rocmdocs.amd.com/projects/llvm-project/en/latest/conceptual/using-gpu-sanitizer.html)明确：

> ASan detection works by checking each address at runtime before the address is actually accessed

AMD文档说明真实GPU Load/Store/Atomic前有Shadow读取与Poison判定；编译器负责插入检查，Runtime负责堆/变量Redzone与Shadow Poison、GPU可用的元数据、Host↔GPU报告。AMD公开源码可供借鉴，但不能只打开CPU ASan开关就认为设备自动覆盖。

**真正的GPU链路：**

~~~text
HIP/C++ → Clang AMDGPU目标 + -fsanitize=address
           ↓
LLVM ASan Instrumentation：识别Global访存并插检查
           ↓
GPU ISA：先读Shadow并判断Poison → 再执行原Load/Store/Atomic
           ↑
Runtime/UMD：逻辑allocation、Redzone、GPU可见Shadow、poison/unpoison顺序
           ↓
Runtime/Reporter：设备错误PC/workgroup/lane、错误对象与源位置
           ↓
目标真实GPU执行上述检查器设备ISA
~~~

公开源码：
- [LLVM AddressSanitizer.cpp](https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html)：ShadowMapping、AMDGPU target判断、访存采集与插桩；
- [LLVM AMDGPUSwLowerLDS.cpp](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AMDGPU/AMDGPUSwLowerLDS.cpp)：某些LDS路径降低为Global后可按ASan机制插桩，说明并非全部地址空间天然统一；
- [LLVM compiler-rt ASan](https://github.com/llvm/llvm-project/tree/main/compiler-rt/lib/asan)：分配拦截、Redzone、Poison、诊断运行库基础；GPU移植仍需设备/Host路径；
- [ROCm GPU ASan官方文档](https://rocmdocs.amd.com/projects/llvm-project/en/latest/conceptual/using-gpu-sanitizer.html) 和 [ROCm LLVM项目](https://github.com/ROCm/llvm-project)。

### 2.2 1:8 Shadow 如何检查64 B对象？

经典 [AddressSanitizerAlgorithm](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm) 采用**每 8 B 应用数据映射 1 B Shadow**：0 表示完整 Granule 有效，1～7 表示有效前缀的字节数，Poison 表示不可访问。对完整有效的 64 B 对象，基础 Shadow 为 8 B；对象前后 Redzone 另需映射并设置 Poison。跨 Granule 的宽访存必须检查全部有效访问字节。

**一个 Shadow Byte 不能表达同 Granule 内任意不连续的合法子区域。** 因而 S05 的子对象问题与一般指针来源检查不能单凭 1:8 Shadow 自动解决。

具体 64 B 地址、合法反例和实际可执行程序在 [S01 唯一案例 README](../samples/S01/README.md)；通用按访问宽度检查与安全元数据访问代码在 [组件指南 §3](real-gpu-components-implementation.md)，而无 XNACK 的三种物理映射取舍在 [AMD 硬件专题 §2](amd-gpu-asan-hardware-fallback.md)。Shadow 缺失或未就绪不允许被默认为 VALID。

### 2.3 如果模仿 AMD，需要怎样修改每个组件？

与 NV-R 不同，**AMD-B2 由 LLVM 在设备访存前插入 Shadow 检查**，而不是 UMD 在装载 Binary 时重写指令。Compiler 必须正确识别 GPU 地址空间、宽度和活动 Lane；Device Runtime 在实体 GPU 读取设备可见 Shadow；UMD 管理 Redzone、Poison 与异步生命周期；KMD 负责必要映射与完成/fault 契约。已有系统接口满足时 KMD 可零改。

#### 可从 LLVM Pass 真正着手的最小入口

先选实际最终 ISA 中的普通 Global Load/Store 1/2/4/8 B，保证检查结果**控制原访存是否执行**，而非调用一个报告 Helper 后继续无条件写入。然后扩展 Atomic、Vector、Masked、Async，逐个记录最终机器访存的覆盖结果。

完整 IR/CFG 插桩及最终 ISA 审计只在 [组件指南 §2](real-gpu-components-implementation.md) 维护；Shadow/Allocator、可见性 Fence 和错误终止统一在 [§3～4](real-gpu-components-implementation.md)。不能以 BO 页大小代替 S01 的逻辑对象大小。

### 2.4 无 XNACK 时，AMD 式 GPU ASan 还能实现吗？

**官方 AMD ROCm 7.2.4 GPU ASan：** 公布的运行路径要求受支持的 xnack+、Linux HMM 与适配的插桩 HIP/HSA Runtime。xnack- 目标可能警告并跳过设备插桩；只设置 HSA_XNACK=1 不能给不支持的硬件增加可恢复缺页能力。

**自研 GPU 借鉴 AMD/LLVM Shadow 算法：可以不依赖 XNACK/HMM。** 但必须在设备访存前映射、初始化并发布全部需要读取的 Shadow/Report。XNACK 负责可恢复 GPU Page Fault 后重试访存，**不负责**为任意野指针生成 Poison。

无 XNACK 的三种核心取舍——全量物理预分配、随 gpuMalloc 分配且直接寻址、按需物理 Shadow 加安全索引——及虚拟 GPUVA/物理显存区别，仅在 [AMD 无硬件依赖专题 §2.1～2.2](amd-gpu-asan-hardware-fallback.md) 维护，此处不复制比较表。

当 GPU 设备元数据预映射且 Poison 已发布时使用 B2_SHADOW；若 Shadow 不可用但能够安全查询 Live 区间表，可以考虑 B1_CURRENT_LIVE_INTERVAL；来源已知时可增加 D1_ORIGINAL_OBJECT_BOUNDS。统一模式协商、元数据 Ready Fence、TOOL_FAILURE、UNSUPPORTED 与部分覆盖的通用 ABI 见 [组件指南](real-gpu-components-implementation.md)。不能把 Host 模拟检查或仅有 CPU ASan 的成功当成设备 Sanitizer 结果。

### 2.5 AMD 官方GPU ASan验证命令与条件

~~~bash
# 必须在真正支持该offload arch及XNACK的AMD设备上执行。
# 这里gfx90a只是官方示例，不能填一个设备并不支持的arch来伪造成功。
export ROCM_ARCH=gfx90a:xnack+
export HSA_XNACK=1
export LD_LIBRARY_PATH=/opt/rocm/lib/asan:/opt/rocm/lib:$LD_LIBRARY_PATH

hipcc -x hip -std=c++17 -O1 -g -fsanitize=address -shared-libsan \
  --offload-arch="$ROCM_ARCH" samples/S01/amd.hip.cpp -o s01_hip
ldd ./s01_hip        # 确认ASan runtime/插桩HIP库路径
./s01_hip --good
./s01_hip --bad      # 应搜索明确标记“on amdgpu device N”的设备报告
~~~

AMD官方文档明确要求 `--offload-arch=gfx...:xnack+`，并指出在 `xnack-` 的目标上编译器可能给出warning、设备代码**没有插桩**，即使二进制运行正常也不能算GPU ASan通过。GPU Debug模式使用的ASan运行库也需要与编译版本匹配，并核查实际加载的是 `/asan/` 的HIP相关库。[ROCm GPU ASan官方Compiling章节](https://rocmdocs.amd.com/projects/llvm-project/en/latest/conceptual/using-gpu-sanitizer.html#compiling-for-asan)

已知限制：有限Redzone会被wild访问跳过；Shadow状态若未正确维护可能误报/漏报；Private/stack设备地址空间并非天然全部覆盖，LDS/shared还有其降级处理。这些事实证明 **ASan不是KMD MMU检查，也不是完整来源/时间身份验证**。参考同一官方文档Known issues章节。

### 2.6 AMD 式移植比 NVIDIA-R 更适合什么条件？

你有**可修改的 LLVM 自研 GPU后端**，并希望先支持能重新编译的Kernel。此时复用公开LLVM ASan/AMDGPU机制，通常比从零建目标机器码完整rewriter更可控；UMD仍需实现目标GPU Shadow映射与设备库，不能直接编进CPU的compiler-rt就视为成功。NVIDIA-C方式更适合将来进一步补充原对象Bounds，而不需要替换ASan作为基础地址检查器。

---

## 3. 已有空间检查方案的实现变体与条件性辅助机制（不新增第四种基础配置）

独立“路线”至少要能够在**真实GPU**、声明的访问类型/空间与元数据生命周期内形成可归因的设备端检查和可靠失败状态。单一Tag比较、元数据索引布局、混合装载策略都不天然构成新的**完整字节级S01 Sanitizer路线**。

**统一参照[核心设计比较](core-design-comparison.md)**：①NV-R归运行时插桩C，②AMD-B2归B2，③NV-C归D1。当前Live查询B1不单列；D2时间安全、E集成、F硬件条件都不作为第四种基础空间检查配置。以下保留实现细节，不重复推导完整能力矩阵。

### 3.1 B1（原I）：编译期当前Live区间表，只作①NV-R地址检查语义的实现变体

B1 是由 Compiler 为目标 GPU 访存插入检查，GPU 在运行时查询**当前存活 Allocation 区间**。它的判断语义是当前 GPUVA 对应的完整字节范围是否有效，而不是指针最早来源的对象。即使 B1 使用 LLVM 编译期插桩，也不能因此获得 NV-C/D1 的 Original-Object Bounds。

在 S04 的 A→Live B 情形，B1 可能查到 B 并放行；地址复用后的历史指针又需要 D2 Allocation ID/Epoch。区间查询和发布同步是 [设备 Runtime、UMD 组件指南 §3～4](real-gpu-components-implementation.md) 的通用实现问题，不在 S01 厂商指南中重写第二段同义代码。B1 是**无 Shadow 的 B 实现变体，不是第四种独立基础算法**；其性能取舍需 GPU 实测。

### 3.2 条件性芯片能力：F1／F2

**F1 硬件 Tag：** 需要实体 GPU 的 Tag/访存比较机制，软件 Tag 属于设备插桩或 D2 身份表示。硬件 Tag 仍可能出现碰撞、Granule 内小幅越界漏检。[LLVM HWASan](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html) 是机制参照，不能作为目标 GPU 已存在硬件的证明。

**F2 硬件 Bounds：** 只有已有 ISA/RTL/对象元数据/异常与失效协议，或另行完成硬件研发，才是硬件方案；普通 GPU 指令算 Bounds 属于软件 D1。如果硬件只检查当前 Live 地址，也不会凭空获得指针原始来源。Firmware/RTL/KMD 的唯一实施代码及真实芯片验收在 [组件指南 §8～9](real-gpu-components-implementation.md)。

### 3.3 E：Hybrid模块覆盖与能力协商，不是第4种基础检查算法

E是混合交付策略：已经由 B/D 编译期插桩且检查 ABI 兼容的模块直接加载；未插桩但 ISA 可合法重写的模块由 UMD 的 C Runtime Binary Rewriter 补充；其余拒绝或显式报告 PARTIAL_COVERAGE。NVIDIA 2026.3 的同一模块混合限制不允许推断任意未插桩函数都自动补齐。

统一 Module Manifest、Kernel Launch/Graph、原 PC 映射和严格失败状态由 [组件指南 §5～6](real-gpu-components-implementation.md) 负责。E 不改变空间检查谓词，也不能自动把当前地址检查升级为 D1/D2 或覆盖 Init/Race/Sync。

## 4. 针对S01的最终选型与任务拆分

**首版推荐 AMD-B2 的设备 Shadow 检查**：目标已有可修改的 LLVM GPU 后端，先实现真实 Global Store 的设备检查，再增加 NV-C/D1 原对象 Bounds；只有不可重编译 Binary 存在实际缺口时再考虑 NV-R/C。

| 阶段 | S01 特定的可验证产物 | 正确状态 |
|---|---|---|
| S01-0 **真实GPU设备代码原型** | 最终 GPU ISA 在 Global Store 前执行检查，UMD 准备逻辑 64 B 对象、Redzone 与映射/发布好的 Shadow | 无实体目标 GPU 运行时仍为 GPU_NOT_RUN |
| S01-1 设备正反例闭环 | 与 [S01 唯一案例 README](../samples/S01/README.md) 的合法/非法输入对照，核对 PC、Lane、Byte Width、原始设备报告与终止策略 | 有可核验报告才记 GPU_DETECTED |
| S01-2 覆盖与故障扩展 | 宽访问、Masked/Atomic、Shadow 未映射、元数据不足和报告丢失的失败分类 | PARTIAL_COVERAGE / TOOL_FAILURE 不能当 CLEAN |
| S01-3 按需追加 D1/C/E | S04 A→Live B 检查来源、重写后的 Binary 等价性、跨模块覆盖清单 | 未知指针与未支持 Opcode 明确降级 |

**通用对象 ABI、SanAccessEvent/SanObject/SanReport 结构、错误终止和完整版组件职责仅在** [真实 GPU 组件实施指南 §1.1～§10](real-gpu-components-implementation.md) 维护。本文件不重复维护第二份 S01 测试矩阵；报告、合法反例、厂商预期只以 [S01 README](../samples/S01/README.md) 与 [manifest](../samples/manifest.json) 为准。

## 5. 现在仓库里的真实示例与这份设计怎样对应？

- CUDA完整设备源码：[samples/S01/cuda.cu](../samples/S01/cuda.cu)，AMD完整设备源码：[samples/S01/amd.hip.cpp](../samples/S01/amd.hip.cpp)。
- 一键Docker构建与GPU检测：[scripts/setup-docker.sh](../scripts/setup-docker.sh)、[scripts/run_samples.py](../scripts/run_samples.py)，以及[GitHub GPU Actions配置](gpu-actions-ci.md)。
- 原研究主稿 [§18.1 S01](llvm-gpu-sanitizer-research.md#case-s01) 和 [S01目录README](../samples/S01/README.md) 将链接本指南。

这些源码是**NVIDIA/AMD厂家现有工具的对照测试**，不是已经实现了自研GPU的Compiler/KMD/UMD Sanitizer。当前只有源码、文档与CPU静态CI的证据；没有真实GPU设备检出记录，不能把`detect`的预期写成已保证通过。

## 6. 官方资料与关键原文索引（正文只复述已公开机制）

| 来源 | 官方入口 | 能据此确认 | 不能据此断言 |
|---|---|---|---|
| NVIDIA Compute Sanitizer User Manual | [memcheck编译期/默认运行时与Hybrid](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html) | 两种插桩入口、编译期bounds、运行命令与限制 | NVIDIA默认运行时内部元数据一定是1:8 Shadow |
| NVIDIA技术文章（2025-12-10） | [Better Bug Detection](https://developer.nvidia.com/blog/better-bug-detection-how-compile-time-instrumentation-for-compute-sanitizer-enhances-memory-safety/) | 当前地址有效性漏掉邻接对象、fat pointer思路、resource开销与HMM限制 | 其私有编译器Pass与完整ABI源码可复制 |
| NVIDIA Sanitizer API Guide | [Patching/Callback/Memory API](https://docs.nvidia.com/compute-sanitizer/SanitizerApiGuide/index.html) | 用户工具如何装载patch、访存回调参数与可打补丁的指令ID | 这是目标自研GPU也现成拥有的API |
| AMD ROCm GPU ASan | [ROCm GPU AddressSanitizer](https://rocmdocs.amd.com/projects/llvm-project/en/latest/conceptual/using-gpu-sanitizer.html) | LLVM插桩、Shadow/Redzone、XNACK/设备库/运行报告、已知限制 | 把CPU compiler-rt直接链接就能检查任意自研GPU |
| LLVM实现 | [AddressSanitizer.cpp](https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html) / [AMDGPU SwLowerLDS](https://github.com/llvm/llvm-project/blob/main/llvm/lib/Target/AMDGPU/AMDGPUSwLowerLDS.cpp) | LLVM现有Pass和AMDGPU降级源码的复用位置 | 目标地址空间/ISA与AMDGPU必然一致 |
| Google ASan原始算法 | [AddressSanitizerAlgorithm](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm) | 1:8 Shadow、Redzone和Poison的基本数学关系 | 经典编码能表达同granule内任意非连续子对象 |

**本文落实结论：** I是B1 当前 Live 区间查询，NV-C/D1 保留原对象 Bounds；E是混合交付策略，软件 Tag 与缺少实体能力的 F1/F2 都不应另列新成熟算法。S01 细节归 [唯一案例](../samples/S01/README.md)，通用设备及驱动实现归 [组件指南](real-gpu-components-implementation.md)。
