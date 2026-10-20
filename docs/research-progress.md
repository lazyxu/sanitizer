# GPU Sanitizer 研究进度

**1.1版资料调研与架构文稿已完成；实际实现、目标GPU实验及最终工程验收尚未开始。**

## 当前正式版本

- 主稿：[llvm-gpu-sanitizer-research.md](llvm-gpu-sanitizer-research.md)。
- 版本及资料核验日期：1.1，2026-10-20。
- 范围：23章、28个检测场景、30项参考来源；区分官方事实、论文结果、工程建议与工程量估算。
- 主稿校验：189699字节，2346行，SHA-256 `d373c9a6d4bb0dce6476f416b0a4dc7e76ea7606799306c249d8b7ac897e72a7`。
- 正式判断以主稿为准。归档研究笔记保留1.0调研过程，可能有早期候选判断和旧章节编号；后续引用优先核对主稿及参考文献。

## 本轮已完成

详细展开[项目背景](llvm-gpu-sanitizer-research.md#background)与[建设目标](llvm-gpu-sanitizer-research.md#goals)：说明动因、调试盲区、使用者、功能／质量目标、验收证据和优先级。新增[第18章S01～S28](llvm-gpu-sanitizer-research.md#detection-scenarios)，逐例解释哪里错误、可选方案、组件观测、修复／合法对照和报告边界，并关联[第19章验收矩阵](llvm-gpu-sanitizer-research.md#validation)。

原有友商与论文比较、六路线、组件改动及工程量均保留。公共生命周期架构补充free开始生效与完成回收的区别；新增[N08] CUDA Programming Guide和[N09] PTX ISA官方来源。完整修订范围及旧新章节对应见[修订记录](revision-history.md)。

## 文稿验证证据

本轮检查通过：23章与小节编号连续；28个场景唯一且均包含示例、修复／合法对照和诊断预期；全部场景映射到验收矩阵；30个参考编号唯一且引用可解析；45张表列数一致；围栏闭合；内部显式锚点及跳转一致。异步生命周期、graph身份、初始化copy与phase token边界另经独立技术复核。

第16章与1.0版逐字一致，沿用已经复核的代码量、测试量和混合路线去重算术。结构化结果见[validation-report.json](validation-report.json)。这些检查是文稿一致性和设计审查，没有编译或运行场景代码，不代表已运行sanitizer测试、完成目标硬件验证或复现论文。

## 下一步：从第22章确认实施参数

1. 依据[平台参数](llvm-gpu-sanitizer-research.md#platform-parameters)确认ISA／地址空间／对齐规则、lane与mask、GPUVA和映射、LLVM版本及后端基础设施。
2. 确认Host/Device runtime、UMD队列和异步生命周期、终止／报告通道、cmodel观测和调度能力。
3. 从第18章场景选择首个可验收原型，登记实际支持集合与触发证据，再按第19章建立错误正例、合法反例和覆盖退化测试。
4. 在真机测量时间、寄存器、stack、显存及检查完整性；根据实际缺口收敛组件预算。硬件辅助进入独立可行性及模型校准流程。

## 工作记录

- [架构与工程量笔记](research-notes/architecture.md)
- [论文核验笔记](research-notes/papers.md)
- [友商核验笔记](research-notes/vendors.md)
- [友商来源与适用范围](research-notes/vendors_sources.json)

笔记只保存原创摘要、工程分析和公开来源链接，没有第三方PDF或全文提取文件。后续工作先读取仓库最新内容，再通过正常提交保留修订历史。
