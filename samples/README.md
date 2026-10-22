# GPU Sanitizer 28个完整案例（CUDA + AMD HIP）

**这是GPU设备侧Sanitizer测试仓库，不使用CPU ASan报告伪装GPU检测。** 每个`S01～S28`目录都包含完整可独立编译的`cuda.cu`、`amd.hip.cpp`和中文`README.md`；包含`__global__` GPU Kernel和独立`main`，可分别使用`--good`（合法对照）和`--bad`（错误／特定边界）。[manifest.json](manifest.json)逐平台声明预计使用的工具与保证级别。

**参考配置/能力矩阵**：[核心设计比较](../docs/core-design-comparison.md)统一2家主要厂商、3种基础空间参考配置（NV-R、AMD-B2、NV-C）。28组S01～S28覆盖不同错误类别，并不意味着每组由三种空间配置全部检出；D2是时间身份扩展，E是集成，F0为辅助GPU页fault实验。

## 0. 先确认现实边界

**不可能保证原文全部28个场景同时被两家GPU Sanitizer工具检出。** NVIDIA目前按工具类型分别检测global/shared内存、shared race、未初始化访问、barrier和设备泄漏；AMD GPU ASan主要提供设备地址有效性与部分UAF检测，需受支持的`xnack+`架构与插桩运行库。S04/S05（逻辑子对象/视图）和S09/S25（指针旧身份/atomic元数据一致性）不属于仅观察GPUVA有效性就能保证报告的错误；S15/S16（global/cross-kernel race）也不能冒充NVIDIA仅shared范围的racecheck。S22故意构造masked-off非法地址，反而**不应该报错**。

**新增代码只有静态结构审查，没有目标GPU上的nvcc/hipcc编译和Sanitizer运行结果。** 此仓库的样例状态是`SOURCE_ADDED_UNVALIDATED`，不是`28/28 validated`。

## 1. 在仓库根目录一键准备 Docker

需要Linux GPU宿主机、Docker和相应硬件驱动。容器不会提供不存在的GPU，也不会在普通CPU上模拟运行NVIDIA/AMD设备代码。

### CUDA

~~~bash
./scripts/setup-docker.sh cuda                 # 构建官方nvidia/cuda镜像并进入容器
# 容器中:
python3 scripts/run_samples.py --backend cuda --case S01 --mode both
./scripts/run-all.sh cuda                      # 一键编译并按manifest运行28例
python3 scripts/run_samples.py --backend cuda --case all --mode both --strict
~~~

NVIDIA容器需要宿主的NVIDIA Container Toolkit（`docker run --gpus all`）。默认用CUDA 13.1.2。必须有`nvcc`与`compute-sanitizer`；默认启动运行时工具，不启用CPU地址检查。若已使用匹配CUDA 13.1+版本，可为memcheck加`--nv-compile-memcheck`进行NVCC编译期插桩，其他race/init/sync仍依赖工具运行期方案。若架构与nvcc默认不符，设置`CUDA_ARCH=sm_90`等目标真实架构。

### AMD ROCm

~~~bash
# 注意先用rocminfo确认GPU与xnack配置，以下gfx90a只是官方文档示例，不能乱填。
ROCM_ARCH=gfx90a:xnack+ HSA_XNACK=1 ./scripts/setup-docker.sh rocm
# 容器中:
python3 scripts/run_samples.py --backend rocm --case S01 --mode both
./scripts/run-all.sh rocm
~~~

AMD镜像固定为`rocm/dev-ubuntu-24.04:7.2.4-complete`，使用`hipcc -fsanitize=address -shared-libsan -g -O1 --offload-arch=$ROCM_ARCH`。必须确保`HSA_XNACK=1`、受支持的GPU，且`ldd`能看出`libclang_rt.asan*.so`和`/asan/libamdhip64.so`。不能在`gfx...:xnack-`或设备不支持时，仅因编译生成了CPU ASan二进制就宣称“GPU检测成功”。宿主设备至少需要`/dev/kfd`和`/dev/dri`。

### 只构建镜像，不声称执行GPU

~~~bash
./scripts/setup-docker.sh cuda --build-only
./scripts/setup-docker.sh rocm --build-only
# 进入Docker后也可仅编译（不会声称GPU实际检出）：
python3 scripts/run_samples.py --backend cuda --case all --compile-only
# ROCm compile-only仍需显式支持的ROCM_ARCH
~~~

## 1.5 GitHub Actions 真正GPU验证

已提供 **[NVIDIA/AMD GPU硬件工作流](../.github/workflows/gpu-hardware.yml)** 和 **[详细启用指南](../docs/gpu-actions-ci.md)**。自托管Linux GPU Runner额外标签分别为`gpu-nvidia`与`gpu-amd-xnack`，且必须在仓库Actions Variables中明确设置`GPU_CI_ENABLED=true`；AMD另设`GPU_ROCM_ARCH=gfx...:xnack+`。未启用时GPU工作流`Skipped`，不能算通过。

GitHub Actions的`workflow_dispatch`允许选择`backend=cuda/rocm/both`以及`suite=smoke/compile-all/safe-all`。默认smoke：CUDA检测S01、S13、S14；AMD检测S01；全部包含`--good`合法对照及`--bad`应有设备报告。脚本`[ci-gpu.sh](../scripts/ci-gpu.sh)`自动Docker构建、确认GPU真实可见、执行并将`summary-*.json`和逐案例原始日志上传Artifact。常规CI绝不启动危险的S17/S18/S20/S27坏例。

CPU-only静态工作流有成功记录，仅表明清单、语法和虚构日志解析单元测试通过；**未验证nvcc/hipcc在真实GPU上的执行或工具实际检出**。自托管Runner禁止从不可信PR运行代码，建议使用隔离、一次性设备。

## 2. Runner如何区分检测结论？

| 标识 | 精确定义 |
|---|---|
| `DETECTED_GPU` | NVIDIA Compute Sanitizer实际GPU报告，或AMD ASan日志明确包含`on amdgpu device N` |
| `CLEAN` | 合法`--good`样例执行且没有GPU错误报告；只代表这次执行 |
| `CLEAN_AS_EXPECTED` | S22等masked-off合法对照没有报告 |
| `NOT_DETECTED` | 故障版本运行了，工具没有发现GPU错误；**不是成功** |
| `UNSUPPORTED_BY_VENDOR` | 该工具在本轮预设下没有对应保证，默认只编译、不跑坏例 |
| `GPU_API_ERROR_NOT_GPU_KERNEL_SANITIZER` | GPU Runtime/Driver API返回非法free等错误；不能算设备kernel访存检测 |
| `CPU_ASAN_ONLY_NOT_GPU` | AMD log只有Host CPU ASan报告，没有明确GPU设备报告 |
| `PREREQUISITE_NOT_MET` | 架构、GPUVA复用或相关功能不满足，代码显式`SKIP:` |
| `BUILD_OR_INSTRUMENTATION_FAILURE` | nvcc/hipcc或GPU插桩runtime检查失败 |
| `MISMATCHED_GPU_DIAGNOSTIC` | 仅出现了其他GPU错误类别（如S01只有设备泄漏，而非global OOB），不能计作正确检出 |
| `UNCLASSIFIED_SANITIZER_ERROR` | GPU工具报告非零错误但没有可归类的设备违规证据，不能标记为CLEAN |
| `SKIPPED_DANGEROUS` / `TIMEOUT` | 危险Barrier/Graph/Free默认不自动执行；显式开启仍受超时控制 |

`--strict`会使预期确定检出的故障未检出、工具失败或合法用例误报导致运行器非零退出。输出保留在`results/{backend}/{Sxx}.{mode}.log`和`summary.json`。如果设置`--attempt-unsupported`会执行无厂商保证的坏例，只用于调查，不提高所宣称的覆盖等级。S17/S18/S20/S27需单独加`--allow-dangerous`；实验GPU可能hang或reset。

**这些分类代表预期而非实测：** 对每个支持集的`detect`，仍必须在真实目标硬件上获得日志才能确认。

## 3. 场景和检测机制对应

| 场景 | 内容 | CUDA主要工具 | AMD GPU ASan | 注意 |
|---|---|---|---|---|
| S01 | Global头尾越界 | memcheck（预期） | GPU ASan（预期） | 需真实设备报告 |
| S02 | 16B宽访问跨界 | memcheck（预期） | GPU ASan（预期） | 核对实际ISA宽度 |
| S03 | 指令对齐 | memcheck（条件） | 条件 | 取决于lowering |
| S04 | 从A越界进入live B | 不保证 | 不保证 | 需原对象provenance |
| S05 | 子分配/tensor view | 不保证 | 不保证 | 需逻辑view约束 |
| S06 | Shared/LDS越界 | memcheck（条件） | 条件 | 空间支持取决于平台 |
| S07 | Private/stack | memcheck（条件） | 不保证 | GPU私有地址空间 |
| S08 | 释放后使用 | memcheck（条件） | 条件 | GPU旧VA/硬件fault |
| S09 | 地址复用旧指针 | 不保证 | 不保证 | 未复用时SKIP |
| S10–S11 | Invalid/double GPU free | API错误 | API错误 | 不算kernel GPU检测 |
| S12 | GPU leak | memcheck leak（条件） | 不保证 | 需要定义检查点 |
| S13 | 未初始化GPU Global读 | initcheck（预期） | 不支持本类检查 | 并非地址越界 |
| S14 | Shared数据竞争 | racecheck（预期） | 不支持本类检查 | AMD ASan不是race checker |
| S15–S16 | Global和跨queue race | 不保证 | 不保证 | 需要HB检查器 |
| S17–S18 | Barrier、warp mask错误 | synccheck（条件） | 不支持本类检查 | 可导致hang |
| S19 | GPU Async Copy缺wait | CUDA synccheck（条件） | 非等价功能占位 | CUDA需SM80，HIP无同等指令demo |
| S20 | 异步释放次序 | memcheck（条件） | 不保证 | 真实stream时序 |
| S21 | GPU Atomic越界 | memcheck（预期） | 条件 | Atomic同样检查范围 |
| S22 | 无实际访存的Mask | **应无错误** | **应无错误** | 测试假阳性 |
| S23 | Constant/资源边界 | 条件 | 不保证 | 不是完整texture权限测试 |
| S24 | Host/GPU shared alias | 不保证 | 不保证 | 缺跨CPU/GPU race checker |
| S25 | Atomic pointer value/identity | 不保证 | 不保证 | 需D2协议 |
| S26 | 未插桩函数/覆盖缺口 | 条件 | 不保证 | 单TU演示，非完整混合多CU |
| S27 | Graph replay旧指针 | memcheck（条件） | 不保证 | 可能API拒绝或GPU fault |
| S28 | 多线程大量越界/日志 | memcheck（条件） | GPU ASan（条件） | 不证明工具元数据OOM |

以上部分样例**仅验证原28场景的一个子情况**；源文件README明确限定，例如S19的HIP源码不具备与CUDA`cp.async`等价的硬件原语，S23未覆盖texture/surface所有边界模式，S26真正混合编译单元需要另加构建夹具，S28尚没有工具内部故障注入能力。不能把“源文件存在”写成“完整场景已经验收”。

## 4. GPU Guard Page辅助页保护：能不能取代完整Sanitizer？

**不能取代。只有真实GPU支持VMM时才可作为辅助页级保护及KMD fault验证，不独立计完整S01字节级Sanitizer方案。** 本仓库额外提供[CUDA+HIP GPU VMM Guard Page完整实验源码](experiments/gpu-guard-page/)。不是调用CPU`mprotect`，而是GPUVA保留两页、只映射前一页，后一页保持不可访问，让真正的GPU Kernel越过页尾时产生GPU MMU fault。

- [CUDA Driver Virtual Memory](https://docs.nvidia.com/cuda/archive/13.4.2/cuda-driver-api/cuda_driver_api/group__CUDA__VA.html)：`cuMemAddressReserve / cuMemCreate / cuMemMap / cuMemSetAccess`。
- [HIP GPU Virtual Memory Management](https://rocm.docs.amd.com/projects/HIP/en/latest/how-to/hip_runtime_api/memory_management/virtual_memory.html)：`hipMemAddressReserve / hipMemCreate / hipMemMap / hipMemSetAccess`。

**限制：** 支持取决于实际GPU/Driver/VMM属性，映射粒度为硬件页granularity，不是任意4B对象边界；错误有时表现为GPU硬件fault而非精确ASan报告。bad实验可能reset设备，需显式`--allow-fault`，不放入默认28例批量运行。

## 5. 官方技术入口

- [NVIDIA Compute Sanitizer User Manual](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)：四工具分工、编译期memcheck、工具参数。
- [NVIDIA Compute Sanitizer Release Notes](https://docs.nvidia.com/compute-sanitizer/ReleaseNotes/index.html)：shared initcheck、混合编译已知限制。
- [ROCm GPU ASan 7.2.4](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)：xnack+、shared runtime、已知漏检。
- [AMD ROCm Docker官方文档](https://rocm.docs.amd.com/projects/install-on-linux/en/docs-7.2.3/how-to/docker.html)。
- [NVIDIA CUDA容器支持版本列表](https://gitlab.com/nvidia/container-images/cuda/blob/master/doc/supported-tags.md)。
- [项目主研究报告](../docs/llvm-gpu-sanitizer-research.md)：模型路径、检测器算法与28场景的技术分解。
