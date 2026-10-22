# S01 GPU Sanitizer厂商参考与目标组件实施：NV-R / AMD-B2 / NV-C

**版本 2.0 · 2026-10-22 · 适用对象：目标实体GPU、LLVM Compiler/Device Runtime、UMD、KMD，以及实际需要的Firmware/RTL。**

> 本文是面向研发落地的**厂商方案优先**设计说明，不是某个闭源 GPU Sanitizer 的反编译说明。凡“厂商官方确认”的机制都给出来源与限长英文原文；凡没有公开具体实现的数据结构、ISA 注入方法或内核接口，均明确写作**本项目建议实现**，不冒充 NVIDIA/AMD 的私有实现。
>
> 所有产品级访存判断必须由实体GPU执行的设备Checker指令或真实存在的Tag/Bounds硬件完成；CPU侧API错误不是设备侧检查。

> **统一计数**：主要参考厂商**2家**（NVIDIA/AMD），基础空间检查参考配置**3种**（①NV-R运行时二进制＋当前地址有效性、②AMD-B2 LLVM Shadow/Redzone、③NV-C LLVM Original-Object Bounds）。这**不是3种完全不同的空间谓词**：①②均主要判断当前地址是否有效，③加入原指针对象来源。B/C/D/E/F保留为工程交付标签，不等于5套独立算法。完整来源/能力对照只维护在[核心设计唯一分类表](core-design-comparison.md)。
>
> **命名约束**：NV-C是NVIDIA编译期Reference；仓库的路线C是Runtime Binary Rewriter，不可互相替代。B1（原I）是当前Live Allocation查询的编译期实现；F1/F2必须具备真正的Tag/Bounds硬件；E是集成策略。
>


## 0. S01 应交付的检测保证（先固定问题）

真实 GPU 算子中最常见的例子是长度为 1000 的向量，按 256 线程/block 启动 4 个 block，遗漏 `idx < N` 判断，造成线程 1000～1023 访问非法的数组元素。另一个例子是卷积/stencil 的首个线程访问 `input[-1]`，末个线程访问 `input[N]`。这不是 CPU 数组访问错误，而是 GPU Global Load/Store 真实生效时的访存问题。

可重复的 S01 基准：UMD 登记 64 B 逻辑对象，受控示例用户首地址 0x1000，合法区间为 [0x1000,0x1040)，4 B 访问如下：

| GPU 实际访问 | 结果 | 原因 |
|---|---|---|
| [0x1000,0x1004) | 合法 | 对象首元素 |
| [0x103c,0x1040) | 合法 | 对象末元素 |
| [0x0ffc,0x1000) | Underflow | 对象头前 4 B |
| [0x1040,0x1044) | Overflow | 对象尾后 4 B |

基础 S01 的可验收能力是“已追踪对象在有效布局中的 Global 字节可访问性”。**当前 GPUVA 有效≠原对象合法**。如果 A 尾部刚好邻接存活的 B，裸地址查询可能找到 B 并通过；必须保留访问的预期原对象 A（例如编译期 Base/Bounds、设备端真实携带的来源token）才可提供更强保证（主报告 S04）；单纯根据当前地址查表不足。

访问完整区间的溢出安全判断：

~~~cpp
enum class SanVerdict { VALID, OOB, UNKNOWN_OBJECT, TOOL_FAILURE };

bool within(uint64_t addr, uint64_t width,
            uint64_t original_base, uint64_t original_size) {
    if (width == 0) return true; // 只有调用方确认“实际没有访存”时才能使用
    if (addr < original_base || width > original_size) return false;
    uint64_t off = addr - original_base;
    return off <= original_size - width;
}
~~~

上式必须给定**正确的对象身份**；若按当前地址查询到邻接对象 B，它不能证明 A 的访问合法。所有检查必须在实际 load/store 生效前完成，保留活跃 lane、谓词、宽度、GPU PC、context 和访问类型；不能因为应用源码出现越界表达式，就断言最终 ISA 一定保留了该访存。

### 需要改动的生产组件（含GPU检查库）

**各组件逐条伪代码另见 [真实GPU组件实施指南](real-gpu-components-implementation.md)。**

| 层 | 作用 | 是否必须新增代码的判断 |
|---|---|---|
| Compiler | 为编译期方案插桩、保留原对象或调试映射；**NVIDIA-R纯运行时模式无需它参与** | **NVIDIA-R：生产编译器可保持完全不改（新增0）**。可选择复用现有MC库，或由UMD侧独立开发机器码解码器；AMD式LLVM ASan与NVIDIA编译期式Bounds才需要编译器改动 |
| KMD | GPUVA/BO映射、执行提交与完成、所选错误策略需要的fault/abort或reset | 有足够既有契约则可以新增 **0**；KMD不应代替UMD凭空推断每个tensor长度 |
| UMD | 用户逻辑 requested_size、分配/释放与异步顺序、module load/patch、Shadow/对象表、诊断解析 | 各真机方案都需要UMD侧可靠对象事件，但实现量不同 |

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

| 组件 | 需要交付的新增模块／具体接口 | 需要验证的关键边界 |
|---|---|---|
| **Compiler（生产编译器）** | **默认无新增修改：0。** 原流程生成GPU二进制即可；可读取既有目标ISA规范、现有反汇编工具或DebugInfo；不强制新增LLVM Pass/MC/CodeGen | 如果二进制是签名/加密/不可重写格式，运行时插桩需先验证允许的正规工具接口；不能把“改编译器”伪装成唯一解 |
| **Runtime Binary Rewriter（UMD侧独立库）** | **真正负责**机器码 decode/访存opcode与谓词/宽度/地址空间语义、插入patch stub、branch relocation、encode、reg/scratch/resource usage重算。可选择调用现有MC库但不修改它，也可以自建decoder/encoder | 每个实际Global opcode都有准确语义；未支持的明确记录PARTIAL_COVERAGE。重写后需要执行等价性测试及资源审计 |
| **UMD（主要工作包）** | 装载/调用Runtime Binary Rewriter，负责`SanModulePatcher`、`SanPatchCache`、`SanAllocationRegistry`、`SanReportDecoder`与设备Patch模块；拦截用户模块/JIT load、alloc/free、launch/event，维护原PC→新PC映射 | 用户requested_size与BO大小不同；代码签名与运行权限、缓存版本+ABI变化；多stream/graph/device-side allocator覆盖；不能绕过正常loader/KMD安全检查 |
| **KMD** | 复用合法的可执行代码映射、BO/GPUVA、提交/完成、出错context处理；若资源描述/运行中终止接口缺失，单列修改 | 不允许通过任意可写可执行映射绕过代码安全策略；部分硬件fault可能只有粗粒度定位 |

#### 核心代码应该是什么样？（自研伪代码，不是 NVIDIA 内部实现）

~~~cpp
struct MemorySite {
    uint64_t original_pc;
    uint32_t opcode, access_bytes, address_space, predicate, mask_kind;
    bool read, write, atomic;
};

// UMD Loader: 新旧PC映射与覆盖能力检查都是必要交付。
PatchedModule patch_module(const Module& module) {
    auto code = target_isa_decode(module);
    CodeBuilder result;
    for (auto& insn : code) {
        if (is_supported_global_access(insn)) {
            // 只在实际执行该指令的lane/element上查地址；
            // 不得为masked-off地址制造shadow访问或误报。
            result.emit_guarded_check(insn.predicate, insn.active_mask,
                                      insn.gpuva_operands, insn.access_bytes,
                                      insn.original_pc);
            result.emit_original_if_check_ok(insn);
        } else {
            if (may_perform_memory_access(insn)) result.mark_partial_coverage(insn);
            result.emit_original(insn);
        }
    }
    result.relocate_branches_calls_and_pcs();
    result.recompute_register_scratch_resources();
    return result.finish();
}
~~~

**上述 `target_isa_decode`、`CodeBuilder`、重定位与资源计算全部属于独立 Runtime Binary Rewriter/UMD 工具库，不是对生产 Compiler 的必需改动；既有LLVM MC库只是可选复用依赖。**

其中 `emit_guarded_check` **必须**落成目标 GPU 上真正能执行的检查指令序列或设备函数，不是 Host检查函数。需要失败动作（继续/终止）与barrier-safe策略，以免出错lane提前退出让其他lane卡在barrier。

Device Checker 使用 current-live-range 语义的示例：

~~~cpp
SanVerdict check_current_allocation(uint64_t addr, uint32_t width,
                                    const AllocationSnapshot& snapshot) {
    if (!width || width - 1 > UINT64_MAX - addr) return SanVerdict::OOB;
    auto region = snapshot.find_containing(addr); // device-friendly index
    if (!region) return SanVerdict::UNKNOWN_OBJECT; // 不允许视为合法
    if (!region->live) return SanVerdict::OOB;
    return within(addr,width,region->base,region->requested_size)
         ? SanVerdict::VALID : SanVerdict::OOB;
}
~~~

以上仅是**借鉴公开观察点、独立设计的候选实现**。NVIDIA真实Memcheck如何布置设备元数据、如何与驱动深层配合，不能凭公开Patching API推断细节。并且当前地址有效性检查可能把相邻B识别成合法，属于该等级的已知限制。

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

| 组件 | 具体交付 |
|---|---|
| **Compiler（最主要）** | `GPUProvenancePass`：识别Kernel参数关联对象root，跟踪GEP/phi/select/调用与ABI；`GPUBoundsInstrumentation`：在实际访存前查原对象base/size；输出来源是否已知、检查点/覆盖等级和site_id |
| **UMD** | `ObjectRegistry`维护allocation ID/原始逻辑范围；`KernelArgSidecar`对公开裸指针参数提供expected object ID/base/size；模块ABI版本、图重放和间接指针数据结构的支持集合 |
| **KMD** | 原有合法BO/GPUVA、同步及错误管理；无需在每条GPU访存时恢复指针来源；可重用接口时新增0 |

一个比较小、但完整表达关键保证的伪代码：

~~~cpp
struct PointerMeta {
    uint64_t current_gpuva;   // 指针算术修改该值
    uint64_t object_base;     // 派生后保持原根对象
    uint64_t object_size;
    uint64_t object_id;
    bool provenance_known;
};

PointerMeta gep(PointerMeta p, int64_t offset) {
    p.current_gpuva = checked_pointer_offset(p.current_gpuva, offset);
    return p; // 保留A的expected object，不从当前地址反查到B
}

SanVerdict check_d1(const PointerMeta& p, uint32_t bytes) {
    if (!p.provenance_known) return SanVerdict::UNKNOWN_OBJECT;
    return within(p.current_gpuva,bytes,p.object_base,p.object_size)
         ? SanVerdict::VALID : SanVerdict::OOB;
}
~~~

**真正难点：** 编译器不能只传播顶层 `kernel(int* a)`，还要明确覆盖指针载入/存储、整数往返、间接调用、模板/库边界、真实Atomic Pointer值与身份的一致性。S01最初可以仅支持已知Kernel指针参数；本报告S04/S09/S25则要求独立追加元数据传播和生命周期能力。

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

经典Google/LLVM ASan算法以8 B用户数据映射1 B Shadow。这是**借鉴的经典算法**，自研目标可选择不同比例、稀疏表或分区映射，不应直接复用CPU的固定Offset。[AddressSanitizerAlgorithm](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm)官方算法原文：

> AddressSanitizer maps 8 bytes of the application memory into 1 byte of the shadow memory.

以 0x1000 为用户Base、64 B逻辑范围、两边各16 B redzone为例：

| 应用 GPUVA | Shadow 状态 |
|---|---|
| [0x0ff0,0x1000) 前Redzone | Poison |
| [0x1000,0x1040) 用户64B | 8个0 Shadow，全部可访问 |
| [0x1040,0x1050) 后Redzone | Poison |

经典单Shadow字节：0表示整个8B有效；1..7表示只有前k字节有效；poison sentinel表示全部无效。一个granule内部同时存在多个不连续有效岛时，经典前缀编码不足以表示，需要改变对象布局或选择更细粒度元数据方案（S05）。

~~~cpp
// 目标无关GPU设备检查算法。lookup必须验证GPUVA->shadow存在/驻留；
SanVerdict check_shadow(const SanContext& ctx,
                        uint64_t gpuva, uint64_t access_bytes) {
    if (access_bytes == 0 || access_bytes - 1 > UINT64_MAX - gpuva)
        return SanVerdict::OOB; // 实际访存指令宽度应非零
    uint64_t cursor = gpuva, remaining = access_bytes;
    while (remaining > 0) {
        uint32_t offset = uint32_t(cursor & 7);
        uint64_t n = min<uint64_t>(remaining, 8 - offset);
        ShadowResult shadow = ctx.safe_lookup(cursor >> 3);
        if (shadow.untracked) return SanVerdict::UNKNOWN_OBJECT;
        if (!shadow.mapped) return SanVerdict::TOOL_FAILURE;
        bool valid = shadow.value == 0
                  || (shadow.value >= 1 && shadow.value <= 7
                      && offset + n <= shadow.value);
        if (!valid) return SanVerdict::OOB;
        cursor += n; remaining -= n;
    }
    return SanVerdict::VALID;
}
~~~

这是设计伪代码，正式GPU版必须解决Shadow索引合法范围、跨地址空间、GPUVA减法/加法溢出、Device Runtime自身访存不被再插桩、并发Shadow更新和SIMT访问mask。**Shadow映射失败或查不到对象不能默认为Valid**。

### 2.3 如果模仿 AMD，需要怎样修改每个组件？

| 组件 | 必须新增或确认复用的代码 | 具体要测试 |
|---|---|---|
| **Compiler（主要）** | LLVM Pass `GlobalLoad/Store/Atomic → check`；`TargetASInfo`确定Global地址空间；`ShadowMapping`、Device Helper ABI与site_id；后端最终访存指令coverage audit | 4B首/末/头/尾，宽访存跨granule，masked-off不误报，原Shadow helper不递归插桩，部分IR操作在后端产生访存不能静默缺口 |
| **UMD（主要）** | `SanDebugAllocator`给GPU对象做Redzone；`SanShadowManager`负责映射/poison/unpoison；`SanLaunchContext`传Shadow Root/Report Ring；`SanReportDecoder`符号化；在实际queue依赖中发布更新 | 用户requested_size≠BO页级size，poison可见性、不同context隔离、async free有效时刻和error工具OOM |
| **KMD（按需）** | 复用已有GPUVM/BO/map/fence/fault/错误完成；缺少shadow VA预留/映射或终止路径时补正当接口 | Shadow和Report Buffer映射权限、提交/完成顺序、多context清理；不用要求KMD遍历每条Kernel访存 |

#### 可从 LLVM Pass 真正着手的最小入口

这份示例刻意把**目标选择**、**插桩调用**与**CFG改造**分开。只是API风格的工程骨架，不是可以直接放入LLVM树编译通过的完整Pass；具体API签名需以你采用的LLVM版本为准。重要的是实际Load/Store**只有检查成功时**才能执行，不能只在它之前调用一个void报告helper却放行原访存。

~~~cpp
// 工程骨架：在Function内部收集待修改指令，避免遍历期间改CFG失效。
SmallVector<Instruction*> sites;
for (Instruction& I : instructions(F)) {
    if (is_device_sanitizer_helper(F) || is_internal_shadow_instruction(I))
        continue;
    if (isa<LoadInst>(I) || isa<StoreInst>(I)) sites.push_back(&I);
}
for (Instruction* I : sites) {
    Value* ptr = get_memory_pointer_operand(*I);
    Type* value_ty = get_memory_value_type(*I);
    unsigned addr_space = cast<PointerType>(ptr->getType())->getAddressSpace();
    if (!target_is_global_address_space(addr_space)) continue;

    TypeSize bytes = DL.getTypeStoreSize(value_ty);
    if (bytes.isScalable() || !supported_scalar_memory_access(*I)) {
        emit_coverage_gap(*I); // 不支持的指令不能静默标记“已检查”
        continue;
    }
    // 应把check编成真实的GPU设备端helper/inline检查。
    Value* verdict = insert_device_check(*I, ptr, bytes.getFixedValue(),
                                         get_original_pc_site_id(*I));
    // 改写CFG，让旧访存只出现在verdict==VALID的路径。
    // 不能让非法store在报告之后继续执行；合法lane必须保持原有barrier语义。
    split_and_guard_memory_access(*I, verdict);
}
~~~

建议首版只承诺普通非原子的Global Load/Store 1/2/4/8B，后续扩到宽向量、masked、atomic、memcpy/async与后端新增的ISA访存。结合目标反汇编审核最终 opcode 列表；LLVM IR已插桩不代表lowering后所有访存都在覆盖范围内。

**UMD 必要职责：** 为每个逻辑对象记录实际requested size、分配身份、设备端Poison可见性及发布顺序；Kernel启动前检查设备元数据确已就绪。具体接口与目标LLVM设备ABI同步定义。

**最关键的错误：** 不可把物理映射4KiB当作应用请求64B。也不能让poison操作还没有完成，下一条用户Kernel就开始加载Shadow；GPU Host调用返回时间不等于设备事件生效时间。

### 2.4 无 XNACK 时，AMD 式 GPU ASan 还能实现吗？

**先回答是与否**：

- **直接使用 AMD ROCm 7.2.4 官方 GPU ASan：没有受支持的 `xnack+` / HMM 环境时，不能按官方流程获得设备插桩。** 官方[编译与运行要求](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)明确说明，`xnack-` 目标可能警告并忽略设备 `-fsanitize=address` 插桩，程序能运行不意味着GPU访存受检查；只写 `HSA_XNACK=1` 也不会让不支持XNACK的芯片获得硬件功能。
- **借鉴AMD Shadow算法，在自研LLVM GPU后端重新实现：可以不依赖XNACK。** 真实必需条件是最终GPU ISA能执行检查（或受控原对象Bounds比较），所用metadata/Report在设备GPUVA中合法可达，UMD/KMD能够在dispatch前建立映射、驻留和fence顺序，且有安全的错误报告/访存屏蔽或终止协议。必须自研Pass、设备库与运行时，而不是修改一个ROCm环境变量。

**XNACK的准确功能及B2显存取舍：** XNACK支持可恢复GPU缺页后的访存重试，不是自动为任意野指针分配带正确Poison的Shadow；HMM属于Linux地址空间镜像及迁移机制。没有等效缺页重试时，Shadow主要有三类映射取舍：**全量物理Shadow预分配（显存浪费）**、**随`gpuMalloc`分配且直接寻址（野指针可能触发未映射Shadow Fault）**、**按需物理Shadow加安全两级索引（更多查询开销）**。大范围GPUVA虚拟预留不等于物理显存全量分配，硬件稀疏映射也可替代部分软件索引；详细限定、示例和官方引用统一放在[AMD指南第2节](amd-gpu-asan-hardware-fallback.md)。

| 无XNACK时的实现选择 | 必要的GPU/软件能力 | 可以提供的保证 | 代价与明确缺口 |
|---|---|---|---|
| **B2：预映射GPU Shadow（推荐）** | GPU能读取预驻留的Shadow BO；LLVM设备Load/Store插桩；UMD Poison/发布fence | 声明支持的字节范围地址检查、Poison期UAF | 显存/带宽开销；A→存活B可能漏；与官方ROCm运行库不同 |
| **B1：GPU Live Allocation区间表（下文原I）** | GPU能读不可变有序live对象descriptor；LLVM插GPU端区间查询 | 当前地址所在live allocation的范围有效性 | 无Shadow；有序查找较贵，仍可能漏A→B及复用旧裸指针 |
| **D1：原对象Bounds软件比较** | LLVM保留可证明的original base/size并在设备寄存器/参数或GPU sidecar传播 | 在来源闭合范围内发现A→存活B | 不支持的指针来源/跨ABI段降级，非完整D2 |
| **C：UMD机器码重写器** | 目标ISA可合法decode/patch/relocate，GPU stub/metadata可执行 | 可为不能重编译模块补特定设备访存检查 | 编译器可零改，但UMD重写成本高，不能凭裸地址恢复D1 |
| **F1/F2：硬件Tag/Bounds** | 真实芯片具有对应ISA/RTL/metadata/fault | 只在硬件实际声明的粒度/保证内有效 | 不存在这些功能时不能软件配置打开；转B/D软件方案 |

~~~cpp
// 无XNACK目标的能力协商：真实GPU运行的自研Device Checker，非官方ROCm开关。
Mode choose_gpu_checker(TargetGPU gpu, ObjectRegistry registry, Module manifest) {
    if (!gpu.can_execute_checked_isa) return UNSUPPORTED;
    if (gpu.can_read_device_metadata && registry.shadow_pre_mapped
        && registry.shadow_ready_fence) return B2_SHADOW;
    if (gpu.can_read_device_metadata && registry.intervals_pre_mapped)
        return B1_CURRENT_LIVE_INTERVAL;
    if (manifest.provenance_complete && manifest.original_bounds_in_args_or_registers)
        return D1_ORIGINAL_OBJECT_BOUNDS;
    return UNSUPPORTED; // Host API拦截不能伪装成GPU内部访存检查。
}
~~~

**组件级落地**：Compiler在实际GPU Load/Store前插入完整byte-footprint检查、保留active lane/mask、生成覆盖manifest并审核最终ISA；Device Runtime读取预映射Shadow/Descriptor或D1参数并写设备报告；UMD登记逻辑对象并提交GPU metadata BO、在queue/fence上排序Poison状态及async free；KMD保证GPUVA映射、权限、实际完成/fault；Firmware/RTL仅按真实abort或新硬件要求修改。无GPU按需缺页时，**不得执行过程中临时期望Shadow由CPU补页**，未驻留直接TOOL_FAILURE或拒绝launch。

**最窄的进一步退路**：即使没有一般的GPU动态元数据查表能力，若D1已经能将已知的original base/limit作为寄存器或Kernel实参送进GPU，仍可比较这些访问；但动态live对象、复杂指针load/store/alias和时间身份无法因此完整覆盖。没有GPU可执行检查指令或可靠失败协议则UNSUPPORTED。

**怎么验证而不自欺**：在真实目标GPU执行S01合法`a[0]`/`a[15]`和非法`a[-1]`/`a[16]`，另测A→live B、shadow缺页、metadata OOM、report满、masked-off、跨queue更新和非法访问后的SIMT前进性。保留GPU最终ISA、模块build ID、PC/lane/宽度、原始report及无插桩/NOP/完整检查性能对照。无实际芯片执行=GPU_NOT_RUN。进一步逐组件实现见[AMD无硬件依赖实施指南](amd-gpu-asan-hardware-fallback.md)。

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

**当前设计到底是什么？明确是B1，不是D1。** 为每一条声明支持的GPU Load/Store插入真实设备 `check_current_live_interval`。该Helper按访问后的实际**当前GPUVA**查找仍然LIVE的逻辑allocation，判定此次访问的整个字节范围是否包含于选中的live区间。**不保留产生这个指针的原对象身份**，也不传原始A的base/size。因此这种算法与NVIDIA传统运行时地址有效性判断属于相似的检查等级（不是宣称NVIDIA真实实现使用相同表）：只是插入时机改到编译期，不能因此自动获得NVIDIA新编译期Base-and-Bounds能力。

~~~cpp
SanVerdict check_current_live_interval(GPUVA addr, uint64_t n,
                                       DeviceLiveIntervals snapshot) {
    if (!n || n - 1 > GPUVA_MAX - addr) return OOB;
    if (!snapshot.gpu_mapped || !snapshot.published_for_launch) return TOOL_FAILURE;
    auto object = snapshot.floor_by_base(addr);  // 实体GPU执行的区间定位
    if (!object || !object->live) return UNKNOWN_OBJECT;
    if (addr < object->base || n > object->requested_bytes) return OOB;
    uint64_t delta = addr - object->base;
    return delta <= object->requested_bytes - n ? VALID : OOB;
}
~~~

- **Compiler**：插桩并审计mask/byte width/地址空间及最终GPU ISA；不携带original provenance时coverage仅为CURRENT_LIVE_ADDRESS。
- **Device Runtime**：GPU访问已映射的snapshot/descriptor，不在CPU端查表替代；unknown/缺表要输出UNSUPPORTED或TOOL_FAILURE，不能默认为合法。
- **UMD**：维护requested bytes、对象注册与free生效顺序、不可变device快照及跨queue fence；不允许GPU使用中原地无同步修改表。
- **KMD**：现有GPUVA/BO映射、提交、完成和fault契约满足则新增代码可以为0。

**边界示例**：指针源对象A为`[0x1000,0x1040)`，相邻对象B为`[0x1040,0x1080)`。若`a[16]`产生GPUVA `0x1040`，B仍LIVE，B1可能查到B并放行4B访问。**D1**必须携带`(expected_A_base, expected_A_size)`到访问点，再比较才可拒绝A→live B；**D2**需要旧指针自己的对象id/epoch才能区别同VA复用后的旧A和新B。B1也不能保证任意未登记地址或硬件特殊访存均能处理。

**为什么不单列方案I**：它是B的**无Shadow实现选项**，与B2经典1:8 Shadow/Redzone在数据表示和性能上不同；两者本质上都只判断当前GPU地址可访问性，不能凭“编译期”提升语义。第一版默认仍以B2 Shadow为主；目标缺少合适的Shadow GPUVA但能GPU查询预映射Live表时才选B1。每访存有序查表成本可能比Shadow高，必须同条件实卡测量。

### 3.2 条件性芯片能力：F1／F2

**F1 硬件Tag（原T；条件性能力）：**

参考[LLVM HWASan设计](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html)：将pointer tag与对象/内存granule tag比较。若真实GPU已有硬件Tag/访问检查，Compiler/UMD/KMD/Firmware需接入实际ISA、分配/释放Tag失效、context状态及真实fault；可以交付**其声明粒度内**的F1硬件检查。**Tag碰撞、同一granule内的小幅越界、未标记指针等并不必然可检出，不能宣称等价完整字节级S01。**

如果硬件没有Tag，软件Tag需要GPU端插桩、device sidecar和报告；这种表示应归D2的指针身份或相关软件检查算法，**不是给不存在的F1硬件开一个软件选项**。仅修改UMD allocator标签而没有GPU访存前的比较也不构成Sanitizer。

**F2 专用硬件Bounds/Metadata（原H；条件性未来交付）：**

如果目标GPU已经有GPU端Bounds指令/比较器、真实对象descriptor接口、异常/终止及metadata失效，则Compiler选择指令，UMD维护对象及版本，KMD/Firmware接通context和fault；可以作为具备该能力的硬件路线交付。**当前芯片没有就必须另立ISA/RTL/DV/Firmware/硅片工作包，不能当作可直接选择的现有产品方案。**

即使硬件查得快，如果只检查“当前GPUVA落入哪个live对象”，仍不能发现A→live B；想要更强的D1原对象界限，必须把expected object来源传至检查指令。没有硬件时用GPU普通指令实现B1/B2/D1软件检查，不得把软件Bounds称为F2。

### 3.3 E：Hybrid模块覆盖与能力协商，不是第4种基础检查算法

借鉴NVIDIA Hybrid机制：有B/D编译期插桩的模块直接加载，未插桩但ISA可重写的模块由C进行Runtime Binary Rewriter补充，未知/未覆盖模块声明PARTIAL_COVERAGE或按严格策略拒绝。UMD统一metadata、module manifest、debug PC和检查器ABI：

~~~cpp
if (module.compiler_instrumented && module.san_abi_compatible) load_checked();
else if (module.binary_patchable) load(rewrite_and_verify_real_gpu_isa());
else reject_or_mark_partial_coverage();
~~~

**E是有独立工程投入的完整产品集成形式，但自身不引入新的访问合法性定义**：B1当前区间查询不会因Hybrid自动具备D1来源，Binary补丁也不会从裸地址恢复旧指针epoch。Init/Race/Sync不随Hybrid自动获得。

## 4. 针对S01的最终选型与任务拆分

**交付条件：** P0必须产出目标实体GPU可执行的检查ISA及UMD/KMD资源协议；只有S01的错误/合法对照真机执行且报告正确，才能标记GPU_DETECTED。

| 交付阶段 | 具体实现 | 真实证据 | 是否优先 |
|---|---|---|---|
| S01-0 **真实GPU设备代码原型** | **先**实现LLVM Global Store设备插桩＋UMD 64B逻辑对象及**预映射Shadow/Redzone**，不要求自研GPU具备XNACK；KMD复用有效映射/完成，生成可在实体GPU运行的Binary | 目标GPU ISA确实包含Checker；在实体GPU可运行时记录原PC、lane、错误与合法对照 | **P0** |
| S01-1 **实体GPU设备检查闭环** | 实卡加载S01-0生成的插桩Binary，UMD发布Shadow/Report，KMD提交实际kernel并返回报告 | `a[0]/a[15]`通过、`a[-1]/a[16]`设备报错；PC/lane/访问宽度与实际ISA匹配 | **P0** |
| S01-2 优化与边界 | 跨Shadow granule、1/2/4/8/16 B、mask、atomic、异步poison顺序 | 扩展用例通过、工具资源不足显示不完整、三性能基线 | P1 |
| S01-3 NVIDIA编译期Bounds融合 | 直接Kernel指针参数base/size sidecar，GEP/phi/select传播；未知来源明确降级 | A越界落入B仍检出，合法B来源访问通过（S04） | P1/P2 |
| S01-4 NVIDIA运行期fallback | 仅当存在不可重编译Kernel且有成熟rewriter时建设 | patch前后合法行为、PC/资源重定位、覆盖manifest | 按需 |
| S01-5 条件性硬件研发 | F1/F2仅在实体Tag/Bounds硬件具备或另行交付后成立 | 没有真正ISA/RTL/芯片能力不能宣布完成硬件检查 | 可选条件性 |

**第一版真正需要交付到实体GPU的Checker与UMD ABI建议：**

~~~cpp
struct SanAccessEvent {
    uint64_t context_id, launch_id, gpu_pc, gpuva;
    uint32_t block_id, lane_id, bytes, kind, space, byte_enable;
    uint32_t site_id;
};
struct SanObject {
    uint64_t allocation_id, base_gpuva, requested_size;
    uint64_t generation;
    uint32_t state, owner_context;
};
struct SanReport {
    uint32_t checker_id, error_kind, coverage_status, dropped_count;
    SanAccessEvent access;
    uint64_t expected_object_id, object_base, object_size;
};
~~~

不要依赖裸PC作为唯一键（不同模块/重放可能冲突），实际报告应保留module build ID、原PC/patch PC转换和异步生命周期语义；生产ABI还要对齐与版本化、确保报告缓冲写入和完成次序、OOM/未支持时可观察。

**失败策略：** 若目标KMD/固件能可靠终止dispatch/context，可在设备报告完成后fail-fast；否则要明确选择可恢复的、不会破坏barrier前进性的语义或者拒绝不支持的模式。不能仅在非法lane本地return让其他lane永远等在barrier，也不能把shadow表未映射当作合法用户地址。

### S01最小验收矩阵

| 用例 | **实体GPU设备检查器**期望 |
|---|---|
| `a[0]`、`a[15]` | 正常、无报错 |
| `a[-1]`、`a[16]` | 在明确无效redzone/受控布局下报告Underflow/Overflow |
| 仅thread 0实际store，另外31 lane inactive | 只记录实际生效访问，不因其他lane计算地址而误报 |
| 访问起点合法，但16 B访问跨过尾部 | **S02延伸**：所有真实触及字节都检查，不能只查首地址 |
| A来源指针越界进入live B | 基础地址有效性可能漏；D1有来源时必须检出 |
| Shadow未映射或metadata不足 | TOOL_FAILURE/PARTIAL_COVERAGE，不能当CLEAN |
| 编译器IR已插桩但最终新ISA访存未覆盖 | 明确列为UNSUPPORTED/部分覆盖 |
| 目标实卡无法启动或设备上没有检查指令 | UNSUPPORTED / GPU_NOT_RUN，不能伪装成已检出 |
| 合法程序与报错程序的资源/性能差异 | 记录无检查、插桩空壳、完整检查三基线及reg/scratch/显存 |

---

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

**本文的落实结论（与[核心三参考配置](core-design-comparison.md)一致）：** I是B1 GPU端当前Live Allocation表，并非D1 Original-Object Bounds；F1软件Tag和F2缺硅片新硬件都不应成为独立现成路线；E是混合交付策略，不改变检查语义。没有XNACK时官方ROCm GPU ASan不能直接宣称设备插桩，但自研GPU可用预驻留设备Shadow/descriptor实现B2/B1/D1，并明确资源与覆盖缺口。S01首选借鉴AMD/LLVM的真实GPU Shadow检查：LLVM Pass生成目标GPU可执行ISA，UMD管理对象/Shadow和报告，KMD提供真实映射、提交、完成与故障契约。目标实卡直接对S01正反例进行验证；随后按实际需求加入D1原对象Bounds与UMD Binary Rewriter。若实体GPU没有Tag/Bounds硬件，则只将F1/F2视为待研发芯片机制。完整逐组件代码及矩阵见[实施指南](real-gpu-components-implementation.md)。
