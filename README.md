# LLVM GPU Sanitizer：真实硬件实施调研

**版本 v1.8（2026-10-21）· 文档及工程伪代码阶段，目标自研 GPU 尚无实卡检出验收。**

面向自研 LLVM GPU 后端、GPU Device Runtime、UMD、KMD 及必要的 Firmware/RTL，研究内存越界、生命周期、Initcheck、Racecheck、Synccheck 的可部署方案。正式候选仅包括 B（LLVM Shadow）、C（运行时GPU二进制重写）、D（原对象Bounds/指针身份）、E（混合）、F（实体GPU硬件辅助）。F0 仅页粒度；F1/F2 必须具备真实芯片指令和元数据能力，否则标记不支持。

## 主要文档

- **[逐组件修改位置、关键代码/伪代码和B～F汇总表](docs/real-gpu-components-implementation.md)**：Compiler/LLVM、GPU Device Runtime、UMD DebugAllocator/Shadow、UMD Binary Rewriter、Host Runtime/Loader、KMD、Firmware、RTL/实体GPU MMU、CLI/CI，涵盖设备错误通路与硬件验收。
- [完整正式调研（23章、28个详细场景）](docs/llvm-gpu-sanitizer-research.md)：厂商比较、目标语义、五种实体GPU候选、对象与指针传播、场景矩阵、验收与性能。
- [实体GPU产品部署审计](docs/hardware-deployability-audit.md)：方案资格、组件职责、硬件前提和状态界限。
- [S01 厂商路径与逐组件实施说明](docs/S01-vendor-implementation-guide.md)：NVIDIA默认运行时/编译期Bounds、AMD GPU ASan/Shadow，以及设备区间表、GPU Guard Page与硬件扩展。
- [B～F生产软件与条件KMD预算（v1.8）](docs/estimates-v1.8.json)：仅生产三组件与新增检查器；单位kLOC，均是设计估算，不是目标源码实测。
- [S01～S28检测场景及生产职责](docs/llvm-gpu-sanitizer-research.md#detection-scenarios)、[实际组件对照表](docs/llvm-gpu-sanitizer-research.md#scenario-platforms)、[真实GPU验收](docs/llvm-gpu-sanitizer-research.md#validation)。
- [研究进度](docs/research-progress.md)、[修订历史](docs/revision-history.md)、[文档检查结果](docs/validation-report.json)、[研究资料](docs/research-notes/)。

## 可部署性与责任

| 路线 | 检查真正生效的位置 | Compiler生产源码 | UMD | KMD/实体硬件门槛 |
|---|---|---|---|---|
| B | GPU执行LLVM生成的Shadow检查ISA | 需要LLVM GPU Pass/后端适配 | Shadow、生命周期、报告 | 实际Metadata映射/完成契约 |
| C | GPU执行UMD改写后的设备Binary | **可以零修改** | 独立机器码重写器、装载/PC映射 | 正规可执行映射及设备终止语义 |
| D | GPU检查原对象Bounds及身份代次 | D1/D2来源传播必需 | sidecar、对象epoch、ABI | Metadata/报告可靠可见 |
| E | GPU分别执行经协商的编译或运行时检查 | 受控插桩与manifest | 混合模式与统一元数据 | 共享依赖不重复实现 |
| F | 实体GPU自身MMU/Tag/Bounds检查 | 视已存在ISA支持情况 | 硬件上下文和元数据初始化 | **必须有真实硬件能力** |

**关键界限：** Device Runtime虽可归Compiler交付，但实际在GPU上执行。既有KMD资源契约充分时可零新增；不意味着可以省去KMD契约验证。未能可靠处理元数据失败、GPU fault/abort、未知opcode或未覆盖模块时，必须报告 UNSUPPORTED / TOOL_FAILURE / PARTIAL_COVERAGE，不能默认为干净执行。

## 实卡样例与 CI

- [28组CUDA/AMD HIP独立样例](samples/README.md)：错误、修复及合法对照；源码存在不等于厂商工具全部能检出。
- [Docker环境](scripts/setup-docker.sh)：`./scripts/setup-docker.sh cuda`，或`ROCM_ARCH=gfx90a:xnack+ HSA_XNACK=1 ./scripts/setup-docker.sh rocm`；运行`./scripts/run-all.sh cuda|rocm`。
- [GPU硬件GitHub Actions](.github/workflows/gpu-hardware.yml)和[Runner配置说明](docs/gpu-actions-ci.md)：必须有实体GPU可见的Runner及匹配驱动/SDK；未调度显示 GPU_NOT_RUN。
- [CPU静态CI](.github/workflows/sample-structure.yml)：只验证样例、文档和合成日志，绝不是GPU实卡检测。
- [GPU真实Guard Page实验](samples/experiments/gpu-guard-page/README.md)：使用GPU VMM、GPU fault，不能拿CPU页保护代替。
- 文档一致性：`python3 scripts/validate_docs.py`；样例结构：`python3 scripts/validate_layout.py`。

## 实施顺序

先验收LLVM Global Store设备Shadow、UMD 64B逻辑对象/Redzone和KMD实际映射完成契约，在实体GPU执行S01正反例并获得PC/lane/宽度/报告和可靠终止；再扩展B到vector/atomic/shared/private；随后D1/D2；确有不可重编译模块时再做C/E。F的硬件变更以实体GPU架构接口和芯片交付计划另立项。

**状态：** 当前仓库是研究、设计、伪代码和厂商工具样例，不含目标自研GPU的LLVM/KMD/UMD/RTL实际补丁、实卡日志或性能改善数据。
