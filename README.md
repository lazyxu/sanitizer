# LLVM GPU Sanitizer 技术调研

本仓库保存面向自研GPU、LLVM后端、Host/Device runtime、用户态驱动（UMD）与cmodel的sanitizer技术调研和架构实施建议。

**当前版本1.1：调研文稿已完成，实际代码实现与目标硬件验证尚未开始。资料核验日期：2026-10-20。**

## 阅读入口

- [正式调研与实施方案](docs/llvm-gpu-sanitizer-research.md)：23章、30项参考来源，包含详细背景与目标、六条技术路线、跨组件接口、工程预算和28个检测场景。
- [项目背景](docs/llvm-gpu-sanitizer-research.md#background)：建设动因、故障定位困难、现有调试手段边界，以及不同使用者的工作流。
- [建设目标](docs/llvm-gpu-sanitizer-research.md#goals)：功能能力、质量目标、完成证据、P0/P1/P2优先级和明确的非目标。
- [检测场景与实例分析](docs/llvm-gpu-sanitizer-research.md#detection-scenarios)：S01～S28逐例说明错误位置、检测方案、组件配合、修复／合法对照、报告与限制。
- [验证基准与验收矩阵](docs/llvm-gpu-sanitizer-research.md#validation)：将场景映射到正反例和目标执行证据。
- [研究进度与下一步](docs/research-progress.md)、[版本修订记录](docs/revision-history.md)、[文稿验证记录](docs/validation-report.json)。
- [研究工作笔记](docs/research-notes/)：架构、论文及友商资料的过程记录与来源链接。

**正式判断以主稿为准。** 研究笔记保留1.0版调研过程，可能包含早期候选结论、旧章节编号或尚未统一的措辞，不能直接视为最终规格或承诺。场景代码为原理与验收设计的示意，尚未在用户GPU上编译或执行。

## 1.1版主要更新

背景与目标从原先的项目假设和能力分类扩展成明确的立项目的、使用者需求及可测目标。新增场景覆盖空间／对象边界、生命周期和API、初始化、竞争、同步、异步copy/free、原子与mask、资源权限、CPU/GPU别名、模块/JIT、graph replay及工具可靠性。

同时明确：异步free提交、开始生效和完成回收是不同节点；关闭mask的元素不能制造访存；atomic pointer的值与身份必须配对；日志满和元数据不足不得被当作clean。原有六路线代码量预算保持原口径。

## 后续工作

从[第22章平台参数](docs/llvm-gpu-sanitizer-research.md#platform-parameters)确认实际ISA、地址空间、GPUVA、LLVM版本、UMD/队列与cmodel条件，再选择最窄可验收原型。按第18章实例建立第19章要求的测试，以真实执行证据收敛覆盖与预算。文献性能数字不等于自研实现性能。

仓库保存原创概括与公开来源链接，不包含第三方论文PDF或提取的完整原文。所有修订通过正常提交保留历史，后续继续工作时先读取master最新版本。
