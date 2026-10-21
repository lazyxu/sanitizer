# 研究与实施进度（2026-10-21）

## 当前结论

**v1.8文档和工程伪代码已整理；目标自研GPU软件实现与真实芯片验收仍未完成。** 已明确五条实际可部署的B/C/D/E/F方案、三生产软件责任包、GPU Device Runtime实际执行位置，以及需要另行交付的Firmware/RTL条件。

| 事项 | 当前状态 | 证据/后续任务 |
|---|---|---|
| B～F产品准入定义 | 文档已完成 | [研究主稿](llvm-gpu-sanitizer-research.md)第9章 |
| 逐组件修改位置与关键伪代码 | 文档已完成 | [实现指南](real-gpu-components-implementation.md)覆盖Compiler、Device Runtime、UMD、KMD、Firmware、RTL和CI |
| S01 NVIDIA/AMD/自研差异比较 | 已更新 | [S01指南](S01-vendor-implementation-guide.md)，全程要求真实GPU执行 |
| 28组S01～S28错误与合法对照 | 已有源码和README | 实际厂商GPU编译/检出结果须运行后再更新 |
| B/C/D/E/F生产软件预算 | 已重算 | [v1.8预算](estimates-v1.8.json)；未测量目标GPU源码 |
| LLVM真实设备Sanitizer Pass与设备库 | **未实施** | 首先产生目标ISA检查指令和支持清单 |
| UMD Shadow/对象生命周期和Report | **未实施** | 明确逻辑长度、发布顺序、epoch、模块覆盖与失败状态 |
| KMD真实映射/完成/abort接口验收 | **未验收** | 按已有契约盘点，真实缺失时才增加源码 |
| 实体GPU S01正反例与性能基线 | **GPU_NOT_RUN** | 实卡核对设备PC、线程、地址、报告、终止与性能 |
| Firmware/RTL新能力 | **按目标硬件条件决定** | F0真实MMU现状；F1/F2缺少硅片功能则UNSUPPORTED |
| GitHub GPU CI | 工作流/样例入口已有 | 未有目标GPU实卡结果；其他厂商Runner须实际配置 |

## 下一实施顺序

先核对真实Compiler/UMD/KMD接口和目标GPU ISA访存清单；实现B Global Load/Store Device Runtime Shadow报告，在实体GPU运行S01合法和非法样例；补齐masked/vector/atomic/shared/private、稳定failure；再推进D1与受控D2。只有不可重编译/JIT覆盖缺口清晰时才引入C/E。Firmware与RTL是独立产品变更，不可以用软件状态假定存在。

## 文档校验

`python3 scripts/validate_docs.py` 检查23章、28场景、五条路线、伪代码指南、预算算术和禁用旧产物引用；`python3 scripts/validate_layout.py` 检查示例结构。这两个检查都是仓库一致性，不属于实体GPU功能或性能验收。

**原始性能数据：** 尚无针对自研GPU的数据。所有公开论文和厂商数据仅是其相应硬件/版本/输入上的引用，不作本项目测量值。
