# GPU Sanitizer 实体硬件部署与源码责任审计（v1.8）

**审计日期：2026-10-21；状态：文档已规定产品准入，目标自研GPU仍未完成源码或实卡验收。**

## 产品准入

只保留B（LLVM插桩的B2 Shadow/B1当前Live区间表）、C（UMD二进制运行期重写）、D（D1原对象Bounds/D2指针时间身份）、E（混合集成）、F（实际芯片具备的F1/F2 Tag/Bounds对象检查）。**F0 GPU MMU Guard Page只能辅助页级保护与fault测试，不能单独列完整Sanitizer方案**。每个检测结果必须在实体GPU执行的检查指令或真实硬件的MMU/Tag/Bounds路径中产生。CPU API拦截可用于Host非法free/leak，但不能据此声称GPU内部Load/Store越界已检查。

## 修改边界

| 路线 | GPU真正执行点 | 编译器新增源码 | UMD/Host Runtime | KMD与实体硬件 |
|---|---|---|---|---|
| B1/B2 | 设备ISA先检查Interval/Shadow再访存 | IR Pass、地址空间、后端opcode覆盖、设备库 | allocator、对象/Shadow发布、queue依赖、报告 | metadata BO/GPUVA、fence、fault/abort契约 |
| C | 由运行时改写过的GPU Binary执行设备检查 | **纯Runtime可以为0** | Decoder/Encoder、CFG relocation、Patch cache、PC映射、设备Stub | 授权代码映射、可执行资源和故障回传 |
| D1 | 实际GPU设备代码按原对象 token 做bounds检查 | pointer provenance、phi/select、参数传播 | base/size/sidecar和对象注册 | metadata隔离及生存期 |
| D2 | 实际GPU使用指针值与身份代次同步判定 | 已支持的指针copy/load/store/atomic传播 | object id/epoch、retirement、ABI | mapping、fault与metadata访问契约 |
| E | 模块级GPU设备检查器/patch共用一套ABI | Compiler manifest与通用Pass | 模式协商、统一对象表、部分覆盖明确标记 | 共享契约并集，只计缺失增量 |
| F0（辅助，非独立完整方案） | 实体GPU MMU页保护故障 | 通常不需要IR插桩 | GPUVA Guard Page策略和逻辑长度 | 实际页表/fault，**只看页粒度，不是S01任意4B对象边界** |
| F1/F2 | 实体GPU tag/bounds硬件逻辑 | 目标ISA选择或ABI | metadata/tag生命周期、失效 | 需要真实ISA/RTL/Firmware/硅片支持，缺失时UNSUPPORTED |

KMD复用已有契约时增量源码可以为零，必须仍验证实际映射、权限、完成和故障链路。设备检查库由Compiler或UMD交付但必须在GPU上运行；Firmware/RTL不是KMD源码的别名。

## 必须排除的错误产品假设

| 风险 | 正确判定 |
|---|---|
| 编译IR中已有check，所以GPU已检测 | 必须反汇编最终ISA，实卡执行并产生设备报告 |
| 只在UMD拦截分配就能发现GPU kernel内部越界 | UMD提供对象元数据，但访存检查必须由GPU执行 |
| 按GPUVA当前归属反查能发现所有旧指针 | 不行。地址复用后需要D2来源与epoch传播；D1可提供受控原对象边界 |
| Guard Page能检测每个4B头尾越界 | F0只在实际跨到受保护的GPU页时故障，**不构成独立S01产品路线** |
| F1/F2只需一个SDK属性 | 没有真实芯片Tag/Bounds机制即不支持，需独立RTL/固件项目 |
| Host提交顺序天然定义设备内所有HB | 不成立，Racecheck需实际设备scope/order/barrier事件 |
| 同一个GPU lane报错后立即return很安全 | 可能使其他lane陷入barrier等待；必须证明可靠dispatch/context终止 |
| 报告缓冲满时无错误记录就算clean | 必须保留sticky error/dropped计数并标明TOOL_FAILURE/PARTIAL_COVERAGE |

## S01实卡验收门槛

1. 真实GPU Global Store对应最终ISA中可执行的Checker/硬件检查路径，并在应用访存前触发。
2. UMD为64字节逻辑对象发布redzone，KMD保障metadata可达与正确的queue完成依赖。
3. 合法 `a[0]`、`a[15]` 不误报；非法 `a[-1]`、`a[16]` 在声明支持的配置下返回具体GPU设备错误。
4. 保存设备架构、模块构建ID、原始PC/补丁PC、lane、访问宽度、对象ID/epoch、错误覆盖状态。
5. 对unknown opcode、metadata OOM、报告丢失、abort不可用的情况明确拒绝或报告不完整，绝不假clean。

**未执行的真实设备测试应标记GPU_NOT_RUN。** Docker构建、源码审查、静态CI、软件日志合成均不得提升该状态。

## 仓库文档对应

| 资源 | 核验范围 |
|---|---|
| [正式研究主稿](llvm-gpu-sanitizer-research.md) | 全部正式路线、28场景、职责与真实硬件验收 |
| [逐组件关键实现代码](real-gpu-components-implementation.md) | Compiler/Device Runtime/UMD/KMD/Firmware/RTL/CLI逐项伪代码及路线对照 |
| [S01厂商实施指南](S01-vendor-implementation-guide.md) | NVIDIA/AMD与自研实际硬件路径 |
| [AMD组件及硬件替代指南](amd-gpu-asan-hardware-fallback.md) | 无XNACK/HMM/MMU/Tag/Bounds时真实GPU软件检查与不能恢复的保证 |
| [生产软件预算](estimates-v1.8.json) | 仅五条方案、P一次计费与缺失KMD契约 |
| [28组用例](../samples/) | 错例、合法对照、支持范围和真实GPU运行结果 |
| [CI验证指南](gpu-actions-ci.md) | 实卡Runner、错误证据、未运行分类 |

**审计结论：** 文档只描述可在真实GPU上生效的B～F路径，并把无法由当前芯片提供的F能力标为需另行交付；组件伪代码尚未落实到目标GPU实际源码，不能表述为开发完成或性能验收通过。
