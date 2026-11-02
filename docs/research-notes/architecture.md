# GPU Sanitizer 架构研究笔记：历史决策与原始技术证据

> **定位：2026-10-20 的研究取证与早期架构判断，2026-10-23 去重，2026-11-02 对齐正式组件契约。** 本笔记只保留历史判断的理由、反例和原始来源，不另行规定实现或预算。基础三配置唯一分类见 [核心方案](../core-design-comparison.md)，原理、算法与决策见 [23章主研究报告](../llvm-gpu-sanitizer-research.md)，唯一设备/UMD/KMD 伪代码及 ABI 见 [真实 GPU 组件指南](../real-gpu-components-implementation.md)，数值以 [工程预算 JSON](../estimates-v1.8.json) 为准。

**证据边界：** 已存在的是公开论文、LLVM 规范、原创设计和可编译的 NVIDIA/AMD 对照样例；自研目标 GPU Checker 未实现、未实机检测（`GPU_NOT_RUN`）。历史笔记中的 kLOC 和参考架构从未代表 LLVM/厂商已有的生产规模。

## 1. 范围、前提与必须保留的结论

当时的工程假设是已有基本 LLVM GPU Backend、Loader/UMD/KMD、Kernel 调度与 Debug Info；Sanitizer 从零起步。这一前提是预算条件，不能当成已审计目标代码。原研究区分四个不同保证：**GPUVA 映射/权限 → 当前地址可访问性 → 原对象 Bounds → 旧指针 Allocation ID/Epoch 时间身份**。后面两个不能凭当前 GPUVA 反查免费得到。正式数学语义见[研究 §6、§10～§12](../llvm-gpu-sanitizer-research.md)。

<a id="2-真实gpu可部署的五类方案组件修改面"></a>

## 2. 真实GPU工程包：组件修改面与历史修正

历史表格用 B/C/D/E/F 分工程工作，容易被误读成五套独立算法。正式口径是 **2 家主要厂商、3 种参考配置 NV-R/AMD-B2/NV-C、2 层空间保证**；B1 只是当前 Live 地址实现变体，D2 是时间身份，E 是集成，F 为条件性真实芯片接口。各包有工作量，不等于新增一个空间谓词。[核心分类](../core-design-comparison.md)

### C路线职责纠正（纯Runtime不要求修改Compiler）

早期表格把工具的 Device Patch Library 成本分摊在 Compiler 交付包，容易被错误解读为必须修改生产 LLVM。正确原则是 **纯 C Runtime 生产 Compiler 新增源码可为 0**；UMD 负责 ISA Decode/Encode、Patch、重定位、资源复核，已有 MC 库仅是可选的只读工具依赖。正式组件代码见[组件指南 §5](../real-gpu-components-implementation.md)，预算按[主稿 §16](../llvm-gpu-sanitizer-research.md)和 JSON 的正式归属。

### B：ASan 式 shadow 的实际移植点

LLVM AMDGPU ASan 的地址空间支持、Stack/Private 和 LDS Lowering 具有 Target 限制；不能把通用 CPU `compiler-rt` 开关直接宣称已经检查任何 GPU Kernel。具体版本化限制在[AMD 硬件专题](../amd-gpu-asan-hardware-fallback.md)，Pass/最终 ISA 审核在[组件指南 §2](../real-gpu-components-implementation.md)。原参考 R3。

### C：runtime patching 的工程重点

原始研究尤其强调保留 Predicate/EXEC、Atomic、Scoreboard/Wait、Branch Range、Constant Relocation、资源用量和 Original PC Map。新增关键修正是 **patch 必须用判定守卫原访存**，仅在前面插入日志 stub 不足以提供安全失败路径。未知 Opcode 标记 Coverage Gap；在途 Code Object 不直接热修改。实现只在[组件指南 §5](../real-gpu-components-implementation.md)维护。

### D：身份设计与 ABI 工作量

历史反例是：同一个 GPUVA 先属于 Old Allocation、Free 后又属于 New Allocation；当前区间查询无法区别旧、新指针。D2 必须将 Value 与历史 ID/Epoch 一同传播，处理 Pointer Load/Store、Memcpy/部分 Copy、ABI、整数往返、Atomic 和对象回收。**LLVM 外部状态指针规则 R5、Atomic Value+Identity 的原子一致性**已归[组件指南 §2.1](../real-gpu-components-implementation.md)和[研究 §12.2～§12.4](../llvm-gpu-sanitizer-research.md)维护；它们是工程语义约束，不代表目标语言 ABI 已经修改。

### E：跨模式契约

一个 Module 的 `coverage_manifest` 需要说明插桩/Runtime Patch、Opcode、AddressSpace、来源保证、ABI 与未知接口策略。C Fallback 不能把 Raw Address 升级为 D2 保证；Loader、Graph 和 Report 的统一实现仅在[组件指南 §5～§6](../real-gpu-components-implementation.md)。

### F：硬件是否存在是第一项门槛

早期把空余 GPUVA 位视为可用 Tag 位的设想没有硬件契约依据；位宽不能证明芯片会忽略、保存或比较 Tag。来源 R2 只用于理解 tagging 边界，不证明自研硬件已有功能。本笔记不扩展 F 研究，当前仅保留[主稿 §14](../llvm-gpu-sanitizer-research.md)的真实芯片准入和既有软件预算边界。

## 3. 公共编译器流水线：早期保语义、IR 检查、late 审核

历史设计把 Frontend/早期 IR 的原对象来源、常规优化后的检查插入，以及 MIR/MC 的**最终机器访存覆盖审计**分开。只在 IR 添加 `check`，不能证明 Target 后来生成的 Spill/Inline ASM/Async 访存都被检查；新插入的 CFG 还必须尊重 LLVM Convergence 语义（R7），`convergent` 本身不是 Fence。实现仅在[组件指南 §2](../real-gpu-components-implementation.md)维护。

## 4. lane mask、vector mask、poison 与原子的一致性

### 4.1 两级 mask 不可混淆

逻辑 Active Lane、机器 Predicate、Vector Element Mask、VP EVL 与 Byte Enable 联合决定**实际访存集合**，不能把不同维度直接当成一个 mask。Whole/Quad Wave 的临时 EXEC 未必等于应用活动集合；被屏蔽元素不能触发 Shadow/Pointer Metadata 读取，也不能因 checker 新增控制依赖而消费原本无效的 poison 值。LLVM 原始依据见 R4/R8；正式合约仅在[组件指南 §2.1](../real-gpu-components-implementation.md)。

### 4.2 atomic 两个问题要分别解决

Atomic 用户操作仍必须是**一次原子操作**，不能因 Sanitizer 插桩拆成普通 Load+Store；对 **Atomic Pointer** 还须保证 Value 与历史 Identity 是同一次发布。独立的两个 Atomic Store、普通 Seqlock Reader 配未插桩 Writer，都不足以提供复合原子快照。候选 Wide CAS / Immutable Descriptor Handle 及其全参与方约束见[主稿 §12.4](../llvm-gpu-sanitizer-research.md)与[组件指南 §2.1](../real-gpu-components-implementation.md)，依据 R4/R9。

## 5. 1:8 shadow、子分配与 GPUVA

1:8 Shadow 的单字节有效前缀无法同时表示一个 Granule 内不连续的两个有效岛；即使用更细粒度位图，也不能自动检测 A 来源进入仍存活的 B。48-bit 完整 App GPUVA 对应 32 TiB **逻辑** Shadow，不等于占用 32 TiB 物理显存。映射失效/野指针读取不可恢复 Fault 的讨论已集中到[AMD XNACK/HMM、稀疏 GPUVA 与三种取舍](../amd-gpu-asan-hardware-fallback.md)及[设备安全查找伪代码](../real-gpu-components-implementation.md)，依据 R1/R6。

## 6. device runtime ABI 与报告格式

历史要求包括 ABI Version、Module Build ID、Original/Patched PC、Lane/Mask、实际 Byte Width、Allocation/Epoch、Report Overflow 和 Metadata Failure。正式 ABI 将具体记录与运行级 sticky 状态分开，记录有界且显式提交，Host 读取同时满足真实终态与 cache 可见性；不能只看记录数为零就判 clean。结构与结果口径统一维护在[组件指南 §1.1、§3、§10](../real-gpu-components-implementation.md)。

## 7. UMD 的异步生命周期与 stream/event 语义

Host API 返回、Kernel Submit、Allocation/Free 真正生效和 Resource Retire 是不同事件。排队 free 不立即令先前合法访问失效，也不能等 backing 回收后才标 DEAD。应用 HB 来自实际 queue/event 依赖；工具不得自动等待全部 last-use 来掩盖缺失同步。Graph replay 更新执行 ID，仍 LIVE 的对象保留 allocation identity。模型及边界在[主稿 §8.3～§8.7](../llvm-gpu-sanitizer-research.md)，发布/失效代码只在[组件指南 §4](../real-gpu-components-implementation.md)。

## 8. 错误后的屏蔽、trap 与 barrier 死锁

错误 Lane 单独 Return/Trap 可能使其他参与者挂在 Barrier。严格模式需证明非法操作不执行、整次 launch 可靠失败；不能为错误 Load 填一个值后继续声称执行正常。UNKNOWN 来源/空间与 Metadata Failure 也不能回默认成功。Timeout、Device Lost、应用 OOB、Tool Failure 可以并存但须分别报告。实现只在[组件指南 §3、§7、§8](../real-gpu-components-implementation.md)。

## 9. Firmware / KMD 边界

已有 BO/GPUVA、权限、驻留、完成和失败契约充分时，KMD 新增代码可以为 0；缺少运行中 Abort 不能靠 Queued Job Cancel 或 Watchdog 名称冒充。请求终止不等于终止已完成，未证明设备不再访问前不能回收资源；Present 检查后也不能立即撤销 metadata 映射。完整契约见[组件指南 §7～§9](../real-gpu-components-implementation.md)，准入清单见[硬件审计](../hardware-deployability-audit.md)。

## 10. 生产软件工程量：公共P加唯一选择路线

原始 2026-10-20 预算是 kLOC 的**设计范围而非源码测量或交付承诺**。正式数字统一读取[生产预算 JSON](../estimates-v1.8.json)和[主研究 §16](../llvm-gpu-sanitizer-research.md)：共享 P 只计一次；E 的 B/D/C 公共底座去重；纯 C Runtime 的生产 Compiler 新增代码可以为 0；F 的软件成本不含另立项的 RTL/DV/硅验证；KMD 新增取决于明确缺失的契约。本笔记不再维护第二套可能漂移的数字表。

## 11. 必须进入验收套件的反例

正式的 28 项详细规范只在[案例 S01～S28](../../samples/README.md)及各独立 README，机器可读预期在[manifest](../../samples/manifest.json)。关键类别是跨 Granule 宽访存、A→Live B、UAF 后同 VA 复用、Masked-Off 无效地址、Atomic Pointer 身份撕裂、跨 Queue Free、SIMT Barrier 下错误退出，以及 Metadata/Report OOM。来源归本研究，不能把静态结构检查算作目标 GPU 检出。

## 12. 原始来源

| ID | 上游依据 | 当时用于校验的机制或语义 |
|---|---|---|
| R1 | [ASan Algorithm](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm) | 1:8 前缀/Poison 与跨度检查 |
| R2 | [LLVM HWASan](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html) | Tag、Short Granule 和概率碰撞边界 |
| R3 | [LLVM ASan 源码](https://llvm.org/docs/doxygen/AddressSanitizer_8cpp_source.html) | AMDGPU AddressSpace/Stack 支持条件 |
| R4 | [LLVM LangRef](https://llvm.org/docs/LangRef.html) | Masked/VP、Poison 与 Atomic Ordering |
| R5 | [LLVM External-State Pointers](https://llvm.org/docs/LangRef.html#pointers-with-external-state) | Pointer Metadata 跨 Load/Store/Copy 传播与 ABI 条件 |
| R6 | [Clang ASan](https://clang.llvm.org/docs/AddressSanitizer.html) | 编译器/运行时分层、映射/符号化前提 |
| R7 | [LLVM Convergent Operations](https://llvm.org/docs/ConvergentOperations.html) | 收敛/控制流约束不等于 Fence |
| R8 | [LLVM AMDGPU Usage](https://llvm.org/docs/AMDGPUUsage.html) | 地址空间、Wave/EXEC 及目标 Debug 规则 |
| R9 | [LLVM Atomics](https://llvm.org/docs/Atomics.html) | RMW/CAS 保持用户原子语义 |

**历史说明：** R1～R9 是该研究笔记的原始查证线索，而非自研实现的真实运行证明。厂商各版本的支持事实请使用独立 [厂商证据登记](vendors_sources.json) 和对应专题，避免将滚动 LLVM 文档与固定厂商产品版本混为一谈。
