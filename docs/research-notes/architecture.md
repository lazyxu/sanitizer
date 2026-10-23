# 自研 LLVM GPU sanitizer：架构与工程量工作笔记

> **历史取证记录，不是并行的架构规范。** 原稿形成于 2026-10-20；本次只保留阶段性判断、容易误解的技术边界与 R1～R9 一手资料索引。当前方案计数以[三种核心参考配置](../core-design-comparison.md)为准，算法与取舍以[23章主研究报告](../llvm-gpu-sanitizer-research.md)为准，**Compiler/Device Runtime/UMD/KMD/Firmware/RTL 唯一通用实现代码与 ABI** 以[组件指南](../real-gpu-components-implementation.md)为准，工程量以[预算 JSON](../estimates-v1.8.json)为准。GPU 实际代码和真机验收均不能由这份历史笔记证明（`GPU_NOT_RUN`）。

## 1. 范围、前提与必须保留的结论

历史规划假设已有可用 LLVM GPU 后端、目标代码装载、UMD/KMD、基础设备调用 ABI 和测试平台；这些是假设，**不是目标硬件已完成的审计结果**。内存安全判定至少区分 GPUVA 映射/权限、当前地址可访问性、产生指针的**原对象 Bounds**、以及随指针传播的 Allocation ID/Epoch 时间身份。当前地址/Shadow 命中合法页不自动证明原对象或旧指针合法。详细理论以[主稿 §6](../llvm-gpu-sanitizer-research.md)为准。

## 2. 真实GPU可部署的五类方案：组件修改面

B/C/D/E/F 是**工程交付包**，不是五种彼此独立的内存合法性数学谓词；三种成熟参考配置是 NV-R、AMD-B2、NV-C。已正式选择的部署路径与条件性硬件边界见[核心方案](../core-design-comparison.md)和[主稿 §9～14](../llvm-gpu-sanitizer-research.md)；此处不再重复完整方案/组件矩阵。

### C路线职责纠正（纯Runtime不要求修改Compiler）

**历史纠错必须保留：** NV-R 式 Runtime Binary Rewriter 可以独立运行在 UMD 工具域，生产 Compiler 新增源码可以为 **0**；可选只读使用现有 LLVM MC 库，并不代表必须修改 LLVM Compiler。完整 Patch/Resource/PC 映射代码见[组件指南 §5](../real-gpu-components-implementation.md)，历史归属的修订以[主稿 §16.5a](../llvm-gpu-sanitizer-research.md)为准。

### B：ASan 式 shadow 的实际移植点

上游 LLVM ASan 的 AMDGPU 特殊地址空间处理不能推出目标 GPU 的 LDS/Private/Stack 已经覆盖；CPU Compiler-RT 的 Host Allocator/OS 假设也不能直接搬进 GPU。**无 XNACK 的物理 Shadow 内存/未映射地址安全性** 归[AMD 硬件专题 §2](../amd-gpu-asan-hardware-fallback.md)，设备 Checker 和 Poison 生命周期归[组件指南](../real-gpu-components-implementation.md)。R1/R3/R6 是原始依据。

### C：runtime patching 的工程重点

历史审核特别关注 Predicate、向量 Mask、Branch Relocation、Code Signing/Executable Permission、寄存器和 Scratch 资源重算，以及 Original PC ↔ Patched PC 归因。无法识别的 Opcode **必须记录未覆盖或拒绝**，不能靠 Host API 回调假装已观察实际 GPU 访存。具体实现与审核要求统一在[组件指南 §5](../real-gpu-components-implementation.md)。

### D：身份设计与 ABI 工作量

**Object ID/Epoch 必须跟随指针而非复用 GPUVA**。非整体原子发布的 Pointer Value + Identity 可能配错；Pointer Shadow、Fat Pointer、Handle 及跨函数/外部 ABI 有不同成本，必须对 Pointer Load/Store/Copy/Atomic 证明保真。LLVM `pointers with external state`（R5）不保证任意 inttoptr/ptrtoint 往返能恢复身份。方案约束、Atomic 反例和历史估算见[主稿 §12、§16](../llvm-gpu-sanitizer-research.md)；通用 `PointerMeta` 在[组件指南 §2](../real-gpu-components-implementation.md)。

### E：跨模式契约

Hybrid 只是 B/D 编译期插桩与 C 受控 Runtime Patch 的**集成策略**。Module Manifest 必须记录实际检查模式、支持的 Opcode/AddressSpace、ABI 版本和 Unknown Provenance，不能把 C/B 当前地址检查提升为 D1/D2 保证。实现以[组件指南 §6](../real-gpu-components-implementation.md)为准。

### F：硬件是否存在是第一项门槛

不能仅由 CPU Pointer 宽度推断 GPUVA 高位可用于 Tag；Tag/Bounds 必须有真实 ISA、RTL、TLB/Cache、一致性及 Fault/Abort 协议。Tag 碰撞也无法保证字节级完全检出。芯片不存在相应能力时应标 `UNSUPPORTED`，软件预算不包含新 ASIC/RTL/DV，参考 R2 与[主稿 §14](../llvm-gpu-sanitizer-research.md)。

## 3. 公共编译器流水线：早期保语义、IR 检查、late 审核

早期传播对象来源、优化后插检查、最终 ISA 核对所有新生 Load/Store/Atomic/Masked/Async，是历史调研确认的关键约束。插桩 CFG 不能破坏 LLVM Convergence，也不能把 `convergent` 当 Memory Fence（R7）。统一实现以[Compiler §2](../real-gpu-components-implementation.md)及[主稿 §7](../llvm-gpu-sanitizer-research.md)为准。

## 4. lane mask、vector mask、poison 与原子的一致性

### 4.1 两级 mask 不可混淆

GPU Lane Active/Instruction Predicate、LLVM Vector Mask/VP EVL 与目标 Byte Enable 必须共同决定**实际发生的访存**。AMD 的 EXEC 可在 Lowering 中临时变化，不等于逻辑活动 Lane；Masked-off Poison Pointer 不应再产生 Shadow Load。LLVM 语义见 R4/R8，测试入口 [S22](../../samples/S22/README.md)。

### 4.2 atomic 两个问题要分别解决

一是**原 Atomic 指令的地址有效性**：检查前后仍保持一次原 Atomic 及 Ordering/Scope；二是**Atomic Pointer Value 与 ID/Epoch 的一致配对**，不能把两条普通原子写当作一次复合原子。目标缺少可证明配对机制时标 `PARTIAL_COVERAGE/UNSUPPORTED`，见 [S21](../../samples/S21/README.md)、[S25](../../samples/S25/README.md) 和 R9。

## 5. 1:8 shadow、子分配与 GPUVA

1:8 经典 Shadow 单字节仅能表达全有效、全无效或前缀有效；不能表示同 Granule 内任意交错有效岛，也不能独立检测指针跨入另一个 Live 对象。48-bit GPUVA 全量映射按比例需要 **32 TiB 逻辑 Shadow VA**，但预留 GPUVA 不代表提交 32 TiB 物理 HBM。无 XNACK 时三种分配选择和 Sparse GPUVA 的故障边界唯一详见[AMD 专题 §2](../amd-gpu-asan-hardware-fallback.md)及[组件 §3.1](../real-gpu-components-implementation.md)。

## 6. device runtime ABI 与报告格式

`SanAccessEvent`、`SanObject`、`SanReport`、`PointerMeta`、Module Build ID、原/重写后 PC 映射及发布顺序由[组件指南 §1.1～§3](../real-gpu-components-implementation.md)维护；不在本历史笔记重复新的 `SanContext` 结构体或 Report 字段。真正的设备报告/日志满/工具 OOM 须与 `GPU_DETECTED` 区分。

## 7. UMD 的异步生命周期与 stream/event 语义

Free Requested、Device Free Effective、Metadata Retired 与 Reusable 不是同一时间点。Host 返回不等于 GPU Command 已完成；同/跨 Queue 依真实依赖确定 Poison/Unpoison 状态。同 VA 复用必须给新对象分配新 ID/Epoch，不能复活 Old Pointer。唯一状态机、组件与实卡测试在[主稿 §8.4](../llvm-gpu-sanitizer-research.md)、[组件指南 §4/§6](../real-gpu-components-implementation.md)和[S20](../../samples/S20/README.md)。

## 8. 错误后的屏蔽、trap 与 barrier 死锁

仅让出错 Lane Return/Trap 可能使其他 Lane/Wave 等待 Barrier 而挂死。缺少平台可靠终止能力时，只能在**已证明可用的 Predicate Suppress + 整体 Dispatch Failure** 范围内启用；其余 Strict 模式必须拒绝。正式实施与工具故障状态在[组件指南 §3、§7～8](../real-gpu-components-implementation.md)、[S28](../../samples/S28/README.md)。

## 9. Firmware / KMD 边界

KMD 负责特权 GPUVA/BO、有效权限、提交、完成和 Fault 事实；UMD 负责逻辑分配、算法元数据与报告；Firmware/RTL 只按真实芯片能力协作。必须支持 ≠ 一定要新增 KMD 代码；已满足并经验证的原有接口可保留 **0 净改动**。详细依赖、Guest/Host 地址域隔离与合同缺口见[主稿 §8.7、§16.8](../llvm-gpu-sanitizer-research.md)，生产代码见[组件 §7～9](../real-gpu-components-implementation.md)。

## 10. 生产软件工程量：公共P加唯一选择路线

**全部 kLOC 数值及去重公式只有一个正式维护源：** [`docs/estimates-v1.8.json`](../estimates-v1.8.json)，中文解释在[主稿 §16](../llvm-gpu-sanitizer-research.md)。本历史笔记不再维护 P/B/C/D/E/F 总量表、Compiler/UMD 拆分表、E 的增量表或硬件条件价目表。所有数字是目标代码尚未核查时的**规划估计，不是已实现 SLOC**。

## 11. 必须进入验收套件的反例

案例已统一归 [S01～S28 唯一详细规范](../../samples/README.md) 与 [manifest](../../samples/manifest.json)；重点是 [S04](../../samples/S04/README.md) 跨 Live 对象、[S09](../../samples/S09/README.md) 同 VA 旧指针、[S18](../../samples/S18/README.md) Sync Mask、[S22](../../samples/S22/README.md) Masked-off、[S25](../../samples/S25/README.md) Atomic Identity 和 [S28](../../samples/S28/README.md) 工具资源耗尽。不能在笔记中复制另一套详细验收矩阵或用静态 CI 替代实卡结果。

## 12. 原始来源

下列 R1～R9 是当时调研的**一手材料索引**；它们的公开版本与新产品发布状态不一定相同。可复用的事实和自研推断必须分开记录。

| ID | 一手来源 | 用于核对的边界 |
|---|---|---|
| R1 | [AddressSanitizerAlgorithm](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm) | 1:8 前缀编码、Poison/Redzone |
| R2 | [HWASan Design](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html) | Tag/Short Granule/碰撞 |
| R3 | [LLVM AddressSanitizer.cpp](https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html) | AMDGPU AddressSpace 与 Stack Poison |
| R4 | [LLVM LangRef](https://llvm.org/docs/LangRef.html) | Masked/VP/Atomic/Poison |
| R5 | [LangRef: Pointers with external state](https://llvm.org/docs/LangRef.html#pointers-with-external-state) | 指针外部状态与复制、ABI |
| R6 | [Clang AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html) | Compiler + Runtime 分工 |
| R7 | [LLVM Convergent Operations](https://llvm.org/docs/ConvergentOperations.html) | 收敛控制流不是内存栅栏 |
| R8 | [AMDGPU Backend User Guide](https://llvm.org/docs/AMDGPUUsage.html) | AddressSpace、EXEC、Scope |
| R9 | [LLVM Atomics Guide](https://llvm.org/docs/Atomics.html) | RMW/CAS Ordering/Scope |

**历史状态：** 来源核验与架构研究材料；不代表自研 GPU Checker 已实现或在真实芯片运行，`GPU_NOT_RUN`。
