# GPU Sanitizer 研究进度

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
