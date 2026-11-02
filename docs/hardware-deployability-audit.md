# GPU Sanitizer 实体硬件部署与源码责任审计（v2.0）

**技术复核：2026-11-02；状态：产品准入设计已写明，目标自研 GPU 尚无源码实现或实卡验收（GPU_NOT_RUN）。** 本文用于评审时逐项决定“具备、缺失、不适用”，不替代厂商手册、通用实现或验收记录。

## 产品准入

统一分类以[核心设计比较](core-design-comparison.md)为准：**2 家主要厂商、3 种基础参考配置（NV-R、AMD-B2、NV-C）、2 层空间保证（当前地址、原对象）**。B/C/D/E/F 是工程包；B1 是当前 Live 区间变体，C 是运行时重写入口，D2 增加时间身份，E 集成已有检查，F 只保留既有预算和真实芯片接口边界。Intel/MUSA 是能力与接口参照。

GPU 访存诊断必须由实体 GPU 执行的检查指令，或芯片真实检查逻辑产生。Host API 拦截可报告 invalid free/leak；它不能证明 GPU 内部 Load/Store 已受检查。通过准入还要证明**检查控制原访存、元数据访问安全、错误后终止可靠、合法反例结果不变**。当前材料只规定这些门槛，尚未满足目标产品交付。

## 修改边界

| 工程包 | GPU 真正执行点与能力 | Compiler 新增源码 | UMD/Host Runtime | KMD及硬件契约 |
|---|---|---|---|---|
| B1/B2 | 访问前查当前 interval/Shadow；不恢复原对象 | Pass、空间/指令覆盖、设备库接线 | 对象登记、metadata 发布/失效、实际队列依赖、报告 | BO/GPUVA 驻留、权限、完成、失败出口 |
| C（NV-R插桩形式） | 改写 Binary 调用选定谓词，并守卫原访存 | **纯 Runtime 可以为 0** | Decode/Encode、CFG/relocation、资源重算、cache、PC 映射、设备 stub | 合法可执行资源、在途代码生存期、故障回传 |
| D1 | 已知来源的原对象 bounds；UNKNOWN 不承诺检出 | SSA、参数/调用/内存中的来源传播 | 可信来源 sidecar、逻辑对象注册 | metadata 访问与生存期 |
| D2 | 指针历史身份与设备 LIVE/代次一致性 | Value＋Identity 的 copy/load/store/atomic/ABI 闭合 | 身份发布/失效、代次回绕和记录回收 | 同一驻留/完成/失败契约 |
| E | 不同模块共用谓词、ABI 和报告 | manifest、跨模式接口 | 模式协商、覆盖缺口、禁止静默降级 | 共享契约并集，只计缺失增量 |
| 条件性 F1/F2 | 只有实际 Tag/Bounds 检查才可启用 | 真实 ISA/ABI 所需适配 | 硬件 metadata 生命周期接口 | 需要真实芯片功能；缺失为 UNSUPPORTED |

KMD 已有契约充分时新增源码可为零；“零新增”仍需真机审计映射、驻留、权限、完成和故障链。Device Runtime 随 Compiler 或 UMD 交付，但在 GPU 运行；Firmware/RTL 不是 KMD 源码的别名。通用代码仅维护在[组件指南](real-gpu-components-implementation.md)，工作量只采用[主稿 §16](llvm-gpu-sanitizer-research.md)及[v1.8 预算](estimates-v1.8.json)，不在本表重新累计。

## 必须排除的错误产品假设

| 风险 | 评审时应要求的判定或证据 |
|---|---|
| IR 已插 check，或 Binary 已插日志 stub，所以 GPU 已检测 | 最终 ISA 必须用返回判定守卫原访存；合法/非法实卡结果与具体 PC 对齐 |
| Shadow Present 位为真，所以任意查找都安全 | 根/叶地址和偏移有效，映射保持到全部读者结束；不能在检查 Present 后立即 unmap |
| 非 Global 空间或未知 D1/D2 来源默认成功 | 未支持空间/来源返回 UNKNOWN；严格请求拒绝，允许的较弱保证明确记录覆盖缺口 |
| 按 GPUVA 当前归属反查能恢复旧指针来源 | B1 只能得到当前对象；D1/D2 需要可信来源及随指针传播的历史身份 |
| Host free 请求一到即全局 DEAD，或 backing 释放完才 DEAD | 区分排队、按实际依赖生效、资源退休；同队列先前访问合法，生效后旧对象非法 |
| 检查工具自动等待所有 last-use 就能证明原程序同步正确 | 应用依赖与内部 metadata 同步分开；不能暗加应用 fence 让错误消失 |
| 发出 abort 请求就能通知完成并释放资源 | 必须确认任务不再访问资源；queued cancel、运行中 abort、device reset 分别验收 |
| 一个 lane 报错后 return，其他 lane 自行继续 | 必须证明整次执行可靠失败且 barrier 不悬挂；不能给错误 Load 造正常返回值 |
| 报告为空或溢出就算 clean | sticky error/tool failure、dropped/coverage 与真实终态一起判断；错误和报告丢失可同时发生 |
| F1/F2 只需 SDK 属性或空余地址位 | 无实际芯片检查则 UNSUPPORTED；不据此扩展软件产品承诺或虚报性能 |

## S01实卡验收门槛

1. 固定设备、ISA、Compiler/UMD/KMD/固件版本和所请求的 B1/B2/D1 等保证；确定最终 Global Store 的完整宽度、谓词与可执行 checker 路径。
2. UMD 发布 64 字节逻辑对象及所选模式需要的元数据：B2 为 Shadow/redzone，B1 为 Live 区间，D1 为可信原对象 bounds；KMD 保障实际映射、驻留和正确发布依赖。
3. 合法 `a[0]`、`a[15]` 不误报；非法 `a[-1]`、`a[16]` 在声明支持的配置下产生具体 GPU 错误，且被拒绝的 Store 不继续执行。详细用例只维护在[S01规范](../samples/S01/README.md)。
4. 保存 Build ID、原始/补丁 PC、launch、lane/元素、实际字节宽度和所用对象信息；没有 identity 的模式不能捏造 epoch 诊断。
5. unknown opcode、metadata OOM、报告丢失、失败出口不可用均须有明确状态；实际 fault/abort 完成与资源回收顺序可验证。
6. 首轮只建立真实时间与资源基线，不凭方案名称承诺更快。记录 kernel/端到端时间、首次 patch、metadata/报告峰值、寄存器及覆盖；性能阈值待设备基线确定。

**GPU_NOT_RUN 只有在对应真实设备证据产生后才能升级。** Docker 编译、源码审查、静态 CI、软件日志合成和厂商其他平台结果，均不能替代目标 GPU 的检出/无误报验收。

## 仓库文档对应

| 资源 | 唯一职责 |
|---|---|
| [正式研究主稿](llvm-gpu-sanitizer-research.md) | 机制、效果边界、选型、预算、性能方法及评审决策 |
| [逐组件关键实现代码](real-gpu-components-implementation.md) | ABI、设备查询/守卫、UMD 生命周期、KMD 终态和资源契约 |
| [S01厂商实施指南](S01-vendor-implementation-guide.md) | NVIDIA/AMD 的公开机制、命令、版本与自研差异 |
| [AMD组件及硬件替代指南](amd-gpu-asan-hardware-fallback.md) | XNACK/HMM、Shadow 映射与无相应硬件时的软件条件 |
| [生产软件预算](estimates-v1.8.json) | 公共 P 一次计费、五个工程包及缺失 KMD 契约；数值不变 |
| [28组用例](../samples/) | 唯一详细场景、合法对照、可执行子场景及预期 |
| [CI验证指南](gpu-actions-ci.md) | 实卡 Runner、具体错误证据、未运行与失败分类 |

**审计结论：** 文档规定了可供真实 GPU 实现的准入契约；目标 Compiler/UMD/KMD 产品补丁、设备报告与性能测量仍缺失。F 维持条件性接口边界，不能作为当前支持能力。后续应按选定配置逐项提交证据，当前状态保持 **GPU_NOT_RUN**。
