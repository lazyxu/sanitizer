# GPU Sanitizer 研究进度

## 2026-10-21 · v1.6 S01按厂商实现的工程拆解

已提交独立[S01厂商优先实施指南](S01-vendor-implementation-guide.md)：按照NVIDIA默认运行时二进制插桩、CUDA13.1+编译期原对象Base/Bounds、AMD LLVM GPU ASan＋Shadow/Redzone三种公开实现路径展开，逐路径给出Compiler、KMD、UMD、功能级cmodel、已有QEMU GPU ISA模型的具体修改、核心伪代码与公开官方来源。然后单列模型原生oracle、实时对象区间表、真实GPUVA Guard Page、Tag、硬件Bounds和Hybrid，明确自研实现与厂商未公开内部机制不能混同。

文档与原S01 CUDA/HIP样例README/主调研报告已互相链接。**当前仍只有厂商公开资料与架构设计；没有在用户自研GPU上实现或验证Sanitizer，也没有CUDA/AMD实卡检测通过证据。** 建议首个代码原型：直接复用已存在QEMU GPU ISA执行器的Global Store生效前hook，构造64B头/尾及合法对照；之后实现LLVM Shadow真机路径，最后追加原对象D1。


## 2026-10-21 · v1.5 新增真实GPU验证基础设施

**完成：** 28组CUDA+HIP样例共56份独立源文件及中文README；NVIDIA/ROCm官方Docker镜像配置；GPU设备端Sanitizer Runner、28案例能力清单、GPU Guard Page CUDA/HIP实验；[GPU硬件工作流](../.github/workflows/gpu-hardware.yml)和[完整GPU Runner配置文档](gpu-actions-ci.md)。CPU静态CI增加合成GPU日志分类测试与Bash/Python校验，且已取得成功记录。

**关键能力边界：** GitHub Actions标准ubuntu CPU Runner不能执行GPU检测；NVIDIA GPU大型Runner需符合组织/企业准入条件，AMD依靠可用ROCm GPU Runner。GPU工作流由`GPU_CI_ENABLED=true`显式启用，缺省跳过、不冒充成功。smoke实际要求CUDA S01/S13/S14或AMD S01的设备违规报告及无误报合法对照，并保留Artifact。`compile-all`仅编译28组本平台源文件，`safe-all`对可安全运行的坏例做检测并显式保留unsupported/未检出状态。

**尚未完成：** 这里没有可访问的真实NVIDIA/AMD GPU；GPU Runner/Actions变量未由本会话配置；没有目标GPU的nvcc/hipcc编译、设备检测报告或性能数据。部分样例覆盖28场景的子范围（例如S19 HIP异步指令、S26真正混合库、S28工具OOM）；不能宣称28/28双厂商可检出。F0 Guard Page使用真实GPU VMM页映射，不等于任意4B逻辑对象边界检查。

**执行入口：** [样例说明](../samples/README.md) → [Docker脚本](../scripts/setup-docker.sh) → [真实GPU CI脚本](../scripts/ci-gpu.sh) → [Actions Runner配置](gpu-actions-ci.md)。下一项真实验收依赖可调度GPU Runner及其完整输出，不能根据CPU CI通过自动标记GPU完成。


**当前正式文档：v1.3（2026-10-21）；设计研究与方案映射已更新。Sanitizer尚未在用户GPU、QEMU/cmodel和KMD/UMD中实际实现或测试。**

## 当前已确认的事实

- 生产组件依然按 **Compiler、KMD、UMD** 分工；设备检查库的预算归编译器包，实际执行于GPU。
- 用户明确确认：**基于QEMU的全系统虚拟平台已经具有目标GPU ISA执行模型，现有部署归类Q2。** 因此不应重复开发GPU ISA解释器；真正需修改的是尚缺的访存前hook、对象/Shadow/身份接线、Guest驱动链路及报告。QEMU与独立cmodel是否共用同一模型、实际ISA覆盖和Guest接口，尚未审计。
- 正文包含23章、28个详细检测场景及40项唯一参考资料；三组件与功能cmodel/已有QEMU模型分别说明。本文原创指工程适配设计，并非宣称基础算法首次发明。

## 本次v1.3完成内容

1. [第4章深度对标](llvm-gpu-sanitizer-research.md#vendor-deep-dive)：NVIDIA四checker、runtime patch与编译期base/bounds和hybrid；AMD GPU ASan shadow/redzone；Intel device-side sanitizer/Unified Runtime；MUSA公开功能及内部机制未核实部分；LLVM ASan/HWASan代码入口；研究论文与产品的关系。官方链接及短摘录直接附于对应机制。
2. [第4.8节来源矩阵](llvm-gpu-sanitizer-research.md#route-provenance-matrix)及第9～14章各自的来源小节：A、B1、B2、C、D1、D2、E、F0/F1/F2及init/race/sync均标出“来源＋融合＋本项目需实现的原创工程部分”，不虚构任何厂商未公开实现细节。
3. [第7.7节已有QEMU模型接线](llvm-gpu-sanitizer-research.md#model-platforms)与[第8.8节系统接入](llvm-gpu-sanitizer-research.md#qemu-integration)：不再视QEMU缺乏GPU ISA执行能力，明确现有Q2执行器的模型pre-access hook、对象/错误接口及独立oracle的边界。
4. [S01七路线案例](llvm-gpu-sanitizer-research.md#case-s01)新增溯源、五组件改动和现有QEMU执行路径的最小验收建议；原S01～S28场景均保留。
5. [第16.9.4节预算](llvm-gpu-sanitizer-research.md#component-estimates)明确QEMU GPU ISA解释器重新建设成本为0（已有），其余Q2历史条件包**仅按确认缺口**计算；[v1.3预算快照](estimates-v1.3.json)保留完整旧表并附当前事实，v1.2原始数据仍在[历史版本](estimates-v1.2.json)。

## 结构与版本校验

- 正文：3245 行（按末尾换行口径）、166464个JavaScript字符；GitHub blob SHA `12f28486054c67e5fa3b75995966507714e1df0d`。
- 对引用、内部锚点、表格列数、围栏闭合、场景ID和A～F来源章节执行程序化检查；详细计数见[validation-report.json](validation-report.json)。
- 所有实现片段均为目标无关的工程示意，**没有实际编译/运行，也没有真实GPU/cmodel/QEMU性能数据**。原论文与厂商结果有各自的限定平台。

## 下一步实现入口

首先定位现有QEMU GPU ISA执行器中真实Global Load/Store的生效前访存hook，取得GPU PC/width/mask/space，同时核对Guest UMD/KMD如何传递逻辑对象大小与完成状态；以S01的首/末合法、头/尾越界及mask-off反例实现独立A0 oracle。随后在LLVM后端实现B2插桩和设备Shadow库，并让现有QEMU执行真正插桩产物；最后在真机核对边界、失败控制与性能。对QEMU已存在子模块不重复计费。

## 研究与修订入口

- [完整主稿](llvm-gpu-sanitizer-research.md)
- [修订历史](revision-history.md)
- [验证记录](validation-report.json)
- [预算数据1.3](estimates-v1.3.json)
- [归档架构笔记](research-notes/architecture.md)
- [归档论文笔记](research-notes/papers.md)
- [归档厂商笔记](research-notes/vendors.md)
