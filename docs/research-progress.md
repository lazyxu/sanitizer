# GPU Sanitizer 研究进度

**1.2版资料调研与架构文稿已完成；实际实现、目标GPU实验及工程验收尚未开始。**

## 当前正式版本

- 主稿：[llvm-gpu-sanitizer-research.md](llvm-gpu-sanitizer-research.md)。
- 版本及资料核验日期：1.2，2026-10-20。
- 范围：23章、28个详细检测场景、39项参考来源；区分官方事实、论文结果、工程建议与工程量估算。
- 主稿校验：276973字节，2924行，SHA-256 `77e0bda05cc2f7b446f3158fd92256c3b08d2ddaf40b701c28f9924b836b11fe`。
- 本轮基于master提交`747eaac5cd1ed49d120868818b0fc9dbfc98e9ba`更新。归档研究笔记保留1.0调研过程，正式判断及章节引用以主稿为准。

## 本轮完成范围

生产软件统一按[编译器、KMD、UMD](llvm-gpu-sanitizer-research.md#component-contracts)组织。前端/Pass/后端与设备检查库分别说明，用户态SDK/runtime、driver/loader和报告工具仍按层实现；KMD明确负责的映射、提交完成、故障及所选终止策略需要逐项验收，已有接口充分时可以不新增代码。

[功能级cmodel和QEMU平台](llvm-gpu-sanitizer-research.md#model-platforms)逐一比较能否执行插桩、是否具备独立oracle、需要什么模型修改。Q0/Q1/Q2/Q3定义、Guest/Host地址域、TCG/DMA边界、全系统驱动接入、record/replay及真机性能边界均补齐。第9～14章分别增加六路线三组件表和双平台表，第15章补充init/race/sync的对应职责。

[第16章](llvm-gpu-sanitizer-research.md#component-estimates)重新组织组件代码量，保留原核心／测试区间和可追溯子项，分列KMD缺口、固件、Q2基础与路线接线。三生产组件小计与含cmodel研发参考包分别展示；共用模型、KMD契约和混合路线只计一次。配套[estimates-v1.2.json](estimates-v1.2.json)保存重组、去重和算术数据。

原28个详细场景完整保留，[第18.29节](llvm-gpu-sanitizer-research.md#scenario-platforms)逐例新增KMD及双平台条件，[第19.7节](llvm-gpu-sanitizer-research.md#validation)增加功能模型、Guest全系统、真机的分层验收。路线图、风险、参数清单、阅读路径和参考文献一并更新。新增Linux kernel4项和QEMU5项官方来源。

## 文稿验证证据

本轮验证通过：23章及小节编号连续；28个详细场景与1.1版保持一致，并全部映射到验收矩阵和平台矩阵；6条路线具备三组件／双平台支持表；39个参考编号唯一且引用可解析；74张表列数一致；40个代码围栏完整闭合；内部锚点与跳转一致。友商、论文及内存基础模型章节保留原稿。

主审独立完成163项范围重组、预算加总及正文表格核对；预算专项复核另报告171项核对。KMD职责、GPU模型观测、Guest系统证据和平台成本经独立审查，已修正终止策略、专用调试接口和泄漏检查点三处歧义。完整记录见[validation-report.json](validation-report.json)。

以上是文稿一致性和架构设计审查，不是sanitizer实现测试；没有编译／执行场景代码，没有运行目标GPU、cmodel或QEMU，也没有复现论文性能或对用户代码库测量SLOC。

## 下一步实施输入

1. 依据[平台参数](llvm-gpu-sanitizer-research.md#platform-parameters)冻结ISA、地址空间、mask/atomic/sync语义、LLVM分支及模块ABI。
2. 分别盘点编译器、UMD与KMD已有契约，区分可复用且已验证、拟复用待验、确实缺失；确认运行中终止能力与影响范围。
3. 明确功能cmodel支持的ISA和并发语义、独立oracle能力，以及QEMU属于Q0/Q1/Q2/Q3哪种配置；登记Guest/Host地址域和实际参与的驱动。
4. 选择最窄可验收原型，以第18章正反例驱动第19章测试，逐工作包核定组件修改和条件预算。
5. 在真机测量时间、资源与覆盖；模型测试用于功能语义和系统路径，硬件方案另做时序、RTL/DV及性能验证。

## 归档记录

- [架构与工程量笔记](research-notes/architecture.md)
- [论文核验笔记](research-notes/papers.md)
- [友商核验笔记](research-notes/vendors.md)
- [友商来源与适用范围](research-notes/vendors_sources.json)

笔记保存原创分析与公开来源链接，没有第三方PDF或全文提取。后续工作从仓库最新master继续，并通过正常提交保留修订历史。
