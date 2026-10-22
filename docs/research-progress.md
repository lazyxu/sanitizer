# GPU Sanitizer 研究与实施进度（2026-10-22）

## 当前方案分类

**现行唯一分类**：[核心三种空间参考配置与能力对比](core-design-comparison.md)。

- **NV-R**：NVIDIA运行时GPU Binary插桩＋当前地址有效性。
- **AMD-B2**：LLVM设备Shadow/Poison，可在满足设备资源契约的自研GPU上实现，无需复制官方ROCm的XNACK硬件配置。
- **NV-C**：编译期Original-Object Bounds，来源可证明时检查原对象范围。

NVIDIA、AMD是主要参考厂商；三种参考配置不等于三种互不相同的空间谓词。B/C/D/E/F是工程包而非五种独立检查算法：B1是当前Live区间表实现，D2是时间身份扩展，E是混合集成，F1/F2是真实硬件存在时的条件性研发。

**仓库当前只有设计、伪代码和CUDA/HIP厂商工具样例，自研目标GPU Sanitizer代码未实施、真机GPU_NOT_RUN。**

| 工作项 | 当前状态 | 依据与下一步 |
|---|---|---|
| 厂商来源、3种配置与能力矩阵 | 文档已完成 | [核心对照](core-design-comparison.md)、[S01指南](S01-vendor-implementation-guide.md) |
| 无XNACK的自研GPU软件检查路径 | 设计已完成 | [AMD实现与设备依赖](amd-gpu-asan-hardware-fallback.md)，编译器/UMD/KMD需要适配 |
| Compiler/Device Runtime/KMD/UMD实施责任 | 设计已完成 | [逐组件指南](real-gpu-components-implementation.md)与[主研究](llvm-gpu-sanitizer-research.md) |
| B/C/D/E/F生产预算 | 仅估算，尚无实测 | [估算JSON](estimates-v1.8.json)，数值与公共P去重口径保留 |
| 28组CUDA/HIP测试 | 测试源码已提交 | [样例说明](../samples/README.md)，未核实目标GPU检测率 |
| 目标LLVM设备Checker、Poison更新和报表 | **未开发** | 首先建立B2真实Global Store检查与错误/合法对照 |
| D1来源和D2历史身份传播 | **未开发** | 分别通过S04、S09等适用场景验证 |
| C Runtime Binary Rewriter / E Hybrid | **未开发** | 仅在存在不可重新编译/JIT模块覆盖需求时实现 |
| 真正的F1/F2 Tag/Bounds芯片能力 | **条件性，未开发** | 芯片缺少相应ISA/RTL时保持UNSUPPORTED |
| 真实GPU设备检测与性能基线 | **GPU_NOT_RUN** | 最终ISA、PC/lane、报告、错误终止及同条件性能数据 |
| CPU静态CI | 已具备 | 检查文档/28例/预算一致性，不属于GPU功能验证 |

## 实施顺序

公共对象/Queue/报告底座 → AMD-B2设备Shadow与S01真实GPU闭环 → NV-C/D1来源Bounds → D2时间身份按需实施 → NV-R式C与E覆盖存量二进制 → 有明确实体硬件与性能证据后再评估F1/F2。

## 测试与文档校验

`python3 scripts/validate_docs.py` 核对2家主要参考厂商、3种基础配置、B/C/D/E/F工程包、23章研究、28场景、预算及文档链接；`python3 scripts/validate_layout.py` 检查CUDA/HIP测试结构。这两个检查都不实际运行自研GPU内核。

**真实性能数据：** 未采集；引用的厂商/论文数字不是本项目实测。
