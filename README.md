# LLVM GPU Sanitizer：自研真实GPU技术设计

**版本 v2.7 · 2026-10-23｜设计与样例阶段；目标自研GPU真实检查器未实现，GPU_NOT_RUN。**

本项目为自研 LLVM GPU Backend、GPU Device Runtime、UMD/KMD及可能涉及的Firmware/RTL，研究GPU空间/时间内存错误、Initcheck、Racecheck和Synccheck。**先明确参考设计而不重复创造产品路线：重点参考2家厂商（NVIDIA、AMD），基础空间检查采用3种值得比较的厂商技术配置。**

## 先看唯一分类与能力矩阵

**[核心方案比较：2家厂商、3种参考配置、能力及实现关系](docs/core-design-comparison.md)** 为现行分类的权威入口：

| 核心空间参考配置 | 来源 | 主要检查方式 | 自研映射 | 与厂商完全一样吗 |
|---|---|---|---|---|
| **① NV-R** 运行期Binary检查 | NVIDIA Compute Sanitizer默认Memcheck | Runtime Binary patch + 当前分配地址有效性 | C（插桩方式），B1为相近检查等级的LLVM变体 | **不是**：厂商私有rewriter/元数据不可直接照搬 |
| **② AMD-B2** LLVM Shadow/Redzone | AMD ROCm GPU ASan、LLVM ASan | 编译期插设备Shadow/Poison检查 | B2（推荐首版） | **算法同源，目标GPU运行库/Shadow/KMD须适配** |
| **③ NV-C** LLVM原对象Bounds | NVIDIA CUDA 13.1+编译期Memcheck、cuCatch思路 | 保留Pointer Root/Base/Size，并在GPU检查原对象 | D1 | **思路一致，SSA/ABI与GPU实现需自研** |

**三个参考配置不是三种完全不同的判定谓词**：①和②都主要检查当前地址可访问性；③加入Original-Object Provenance。**NV-C ≠ 仓库路线C**：前者为NVIDIA编译期方案，后者是自研Runtime Binary Rewriter。

工程责任仍可按 **B/C/D/E/F** 预算与实施，但这是5个交付包，**不是5种独立算法**：B有B2 Shadow与B1当前Live区间（原I）；C运行期插桩；D1原对象Bounds与D2时间身份；E混合集成；F仅在目标实体GPU具备Tag/Bounds时为条件性硬件研发。

## 主要文档（避免重复方案计数）

**建议首先阅读 [统一文档导航](docs/README.md)**，再按方案、厂商实现、硬件部署、测试或预算选择权威文档；主研究报告和历史笔记不作为第二套分类依据。

- [Intel oneAPI 设备端 ASan/MSan/TSan 专题](docs/intel-oneapi-device-sanitizers.md)：官方 C/C++ 2026.0 设备能力、SYCL/OpenMP 编译运行示例、Unified Runtime 协作及限制，定位为成熟辅助参考。
- **[核心方案与能力比较](docs/core-design-comparison.md)**：现行计数、来源、是否完全一致、三种参考配置能力表、D2/E/F与Intel/MUSA定位。
- [S01厂商实施指南](docs/S01-vendor-implementation-guide.md)：NVIDIA运行时与编译期、AMD Shadow、B1/E及无XNACK支持边界，**只保留厂商机制与选型**。
- [完整技术研究（23章、28场景）](docs/llvm-gpu-sanitizer-research.md)：目标 ISA /内存语义、元数据、实现与性能；§4 保留厂商机制对比，版本清单和运行命令由证据登记及专题维护。
- [各组件唯一实现 ABI 与关键伪代码](docs/real-gpu-components-implementation.md)：Compiler、Device Runtime（含 safe Shadow Lookup、对象/Pointer 元数据及 Atomic 发布）、UMD、KMD、Firmware/RTL及CLI。
- [AMD无XNACK/HMM硬件适配与Shadow取舍](docs/amd-gpu-asan-hardware-fallback.md)：**XNACK 缺页重试、HMM 区别、三种 Shadow 映射及稀疏 GPUVA 安全性**；通用组件代码只在前述实现指南，绝非官方ROCm绕过开关。
- [版本化厂商证据](docs/research-notes/vendors.md)、[架构决策历史及 LLVM R1～R9 依据](docs/research-notes/architecture.md)、[机器可读证据登记](docs/research-notes/vendors_sources.json)、[实体GPU硬件部署审计](docs/hardware-deployability-audit.md)。
- [生产软件工程量估算（沿用v1.8数值）](docs/estimates-v1.8.json)：B/C/D/E/F按**责任包**估算，公共P一次计费，F不含RTL/ASIC成本。
- [研究进度](docs/research-progress.md)、[修订历史](docs/revision-history.md)、[结构审计记录](docs/validation-report.json)。

## GPU样例与CI

- [NVIDIA 官方独立回归组](tests/upstream/nvidia/README.md)：原样保存上游 Memcheck/Racecheck/Initcheck/Synccheck 与许可证；NV-* 单独编号、诊断与 GPU 证据分开记录，真实 GPU 未验证。
- [S01～S28 的CUDA与HIP设备程序](samples/README.md)：每种错误均区分非法示例、合法反例、工具能力/UNKNOWN；源码存在不等于GPU已检出。
- [GPU硬件Actions](.github/workflows/gpu-hardware.yml)与[Runner说明](docs/gpu-actions-ci.md)：需实际有GPU的Runner与受支持的工具链。NVIDIA检验既有CUDA工具；AMD官方ASan要求匹配的`xnack+`运行配置；这些并不等于自研GPU目标Checker已完成。
- CPU静态CI：[样例与文档检查](.github/workflows/sample-structure.yml)；可运行`python3 scripts/validate_docs.py`和`python3 scripts/validate_layout.py`，**不能替代真实GPU功能或性能验收**。

## 命令执行追踪：-v / --verbose

仓库 `scripts/` 内 **7 个自有 Bash / Python 脚本均支持 `-v` 和 `--verbose`**。默认输出不变，启用后可看到 Docker、`nvcc`、`hipcc`、`ldd`、`compute-sanitizer`、`nvidia-smi` 等外部命令；静态检查仅打印读取的文件路径。

~~~bash
# 主测试集 28 例：打印Docker和子进程命令
bash scripts/ci-gpu.sh cuda safe-all -v
# 已进入CUDA容器时，打印所有28例的编译与检测命令
./scripts/run-all.sh cuda -v
# 一键构建并进入容器；之后的Python脚本继承SAN_VERBOSE=1
./scripts/setup-docker.sh cuda --enter -v
# NVIDIA 官方独立回归集（自研检测器参数输出会脱敏）
python3 scripts/run_upstream_nvidia.py --case all --strict --verbose
# 不启动任何GPU内核的静态校验
python3 scripts/validate_docs.py -v
python3 scripts/validate_layout.py -v
~~~

Python 使用 Shell 安全引用输出命令，自研 checker 的命令行参数出于安全考虑隐藏，执行时仍传入原始参数。verbose 可能包含本机路径，不应分享包含秘密的日志。`-v` 不改变危险测试默认跳过、检测分类和 GPU 运行证据。

## 实施优先级

公共对象/报告/队列底座 P → **AMD-B2 Shadow** 在真实GPU完成S01报告 → 加入**NV-C/D1原对象Bounds**解决A→live B → 按需D2解决地址复用后旧指针 → 有不可重编译代码时再做NV-R式C、E模块级集成 → 软件性能数据足够后再研究F1/F2芯片加速。B1仅在适合当前Live GPU端区间查询时作为B的替代实现。

**当前状态**：仅研究与伪代码、CUDA/HIP厂商对照源码；无目标自研GPU的LLVM/KMD/UMD/RTL正式实现、无实卡诊断记录和真实性能改善数据。
