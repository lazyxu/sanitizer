# GitHub Actions 真实 GPU 验证：NVIDIA Compute Sanitizer / AMD GPU ASan

**配置状态（2026-10-22）：GPU工作流、Docker入口、S01/S13/S14 smoke、28例全量编译及安全执行模式已提交；仓库未确认存在可调度的GPU Runner，GPU_CI_ENABLED未由本会话设置。因此当前GPU job显示SKIPPED，不是GPU验证通过。** CPU工作流只负责脚本与清单、合成诊断分类测试。

**方案口径**：[核心设计比较](core-design-comparison.md)列①NV-R、②AMD-B2、③NV-C。硬件CI只是对现有NVIDIA/AMD厂商工具的实际设备运行验证；不能从这些运行推断三种参考配置全部已验证，更不能宣称目标自研GPU端Check已实现。NVIDIA编译期memcheck需要匹配工具链与显式开启；同模块函数级Hybrid不自动完整补洞。

## 1. 为什么默认不在 ubuntu-latest 执行GPU检查

[GitHub标准Runner规格](https://docs.github.com/en/actions/reference/runners/github-hosted-runners)列出的普通ubuntu-latest不是GPU实例。已有[GitHub larger runner](https://docs.github.com/en/actions/reference/runners/larger-runners)提供NVIDIA Tesla T4（Ubuntu/Windows）的GPU配置，但[larger runner权限](https://docs.github.com/en/actions/concepts/runners/larger-runners)要求符合组织/企业账号套餐与Runner配置条件；它**不是**个人公开仓库自动获得的免费NVIDIA GPU，也不提供通用AMD ROCm环境。

因此现有仓库选择**明确启用的GPU自托管Runner**。未来若有已购买和授权的GitHub托管T4 runner，可将NVIDIA job的\`runs-on\`替换成配置的具体大型Runner标签；仍需确认NVIDIA驱动、Docker、Container Toolkit及CUDA 13.1.2宿主兼容性。

GPU验证绝不能因为CPU-only工作流success就打成\`GPU_DETECTED\`；仓库使用两个分开的工作流：

| 工作流 | Runner与条件 | 验证范围 |
|---|---|---|
| [.github/workflows/sample-structure.yml](../.github/workflows/sample-structure.yml) | GitHub普通CPU Runner，push/PR | Python/Bash语法、28案例清单、合成GPU日志解析单元测试；不运行GPU |
| [.github/workflows/gpu-hardware.yml](../.github/workflows/gpu-hardware.yml) | 启用后仅可信\`master\`，NVIDIA/AMD GPU Runner | GPU容器构建、真实GPU预检、CUDA Compute Sanitizer或ROCm设备ASan、原始GPU日志与JSON结果Artifact |

## 2. 必须配置的GitHub Actions Runner

在GitHub仓库设置中的 **Actions → Runners → New self-hosted runner**，分别给有对应GPU的独立Linux x64机器注册：

- NVIDIA节点：默认标签\`self-hosted\`、\`linux\`、\`x64\`，额外标签\`gpu-nvidia\`。
- AMD节点：默认标签\`self-hosted\`、\`linux\`、\`x64\`，额外标签\`gpu-amd-xnack\`，且实际ROCm设备必须支持\`xnack+\`。

GitHub官方说明支持在注册时用\`./config.sh --labels gpu-nvidia\`等方式添加自定义标签，也支持之后在设置页面管理。不要把注册Token、凭证、驱动私钥、任何生产秘密提交到仓库。[官方自托管标签说明](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/apply-labels)

**Runner环境要求：** Linux GPU专用主机，Docker Engine，实际宿主GPU驱动；NVIDIA额外安装[NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/sample-workload.html)并确认\`docker run --rm --gpus all ... nvidia-smi\`可用；AMD宿主\`/dev/kfd\`与\`/dev/dri\`可访问，并具备[ROCm Docker设备透传](https://rocm.docs.amd.com/en/latest/install/docker-containers.html)条件。容器中工具链无法代替宿主显卡或Kernel驱动。

**安全：** GitHub[自托管Runner安全指南](https://docs.github.com/en/actions/reference/security/secure-use)明确警告公有仓库不能让未受信任PR在持久化自托管Runner执行任意代码。因此GPU workflow**没有pull_request和pull_request_target触发器**；仅受信任\`master\`的push/schedule/手动运行，且\`GITHUB_TOKEN\`仅赋予\`contents:read\`。仍建议使用**一次性隔离GPU Runner**，无生产访问、无持久敏感凭证，禁止和主要办公/服务器GPU共享，并人工审查合入master的代码。Docker socket本身是高权限接口。

## 3. 在GitHub仓库设置中配置变量（不是密钥）

路径：**Settings → Secrets and variables → Actions → Variables**。

| 变量 | 示例值 | 作用 |
|---|---|---|
| \`GPU_CI_ENABLED\` | \`true\` | **必须显式设置**，否则真实GPU jobs会跳过；防止无GPU环境误报成功或长时间排队 |
| \`GPU_CUDA_ARCH\` | \`sm_75\`（Tesla T4示例） | 按真实NVIDIA GPU架构设置；其他GPU用对应\`sm_XX\`，留空按工具默认 |
| \`GPU_ROCM_ARCH\` | \`gfx90a:xnack+\`（支持的MI2x0示例） | 必须用实际GPU支持的\`gfx...:xnack+\`。不能仅仅为了编译而冒充架构或GPU XNACK能力 |

脚本为ROCm GPU ASan设置\`HSA_XNACK=1\`，但这不能启用硬件本身不存在的XNACK。务必通过[AMD GPU ASan文档](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)和实际设备\`rocminfo\`确认适配，且确保容器内\`/opt/rocm/lib/asan\`插桩运行库被链接/加载。设备不支持则明确FAIL/UNSUPPORTED，不退回CPU AddressSanitizer。

启用变量前，不必配置不存在的GPU Runner；启用后若缺少匹配标签，job会留在queued，而不是通过。

## 4. 启动验证

进入GitHub **Actions → GPU Sanitizer hardware validation (NVIDIA / AMD) → Run workflow**，选\`master\`，再选：

| 输入 | 选择 | 实际行为 |
|---|---|---|
| backend | \`cuda\` / \`rocm\` / \`both\` | 使用不同GPU Runner和不同Docker镜像，两种GPU不会混跑 |
| suite | \`smoke\` | NVIDIA运行S01 Global OOB、S13 GPU initcheck、S14 Shared racecheck；AMD运行S01 GPU OOB；每项有\`--good\`与\`--bad\`、严格匹配设备报告 |
| suite | \`compile-all\` | 仅在真实Docker内编译所有28份本平台源码并检查ROCm runtime链接；**不宣称错误检测成功** |
| suite | \`safe-all\` | 编译28份本平台源码，执行合法对照和不需要危险权限的bad；默认跳过S17/S18/S20/S27的危险bad、无厂商能力保证的bad |

push到可信master且修改\`samples/**\`/\`scripts/**\`/\`docker/**\`/\`gpu-hardware.yml\`时触发**smoke**（只有启用变量才调度）；另设每周定时smoke。**危险GPU reset/hang样例不会自动运行**，要在单独隔离设备上明确使用\`--allow-dangerous\`，不包含在常规GitHub GPU CI内。

GPU Runner上也可使用同一入口：

~~~bash
bash scripts/ci-gpu.sh cuda smoke
ROCM_ARCH=gfx90a:xnack+ HSA_XNACK=1 bash scripts/ci-gpu.sh rocm smoke
bash scripts/ci-gpu.sh cuda compile-all
bash scripts/ci-gpu.sh cuda safe-all
bash scripts/ci-gpu.sh cuda safe-all -v  # 输出Docker、nvcc和检测命令
~~~

或使用原本[Docker一键配置脚本](../scripts/setup-docker.sh)进入容器后运行\`./scripts/run-all.sh cuda\`和\`./scripts/run-all.sh rocm\`。编译器与报告工具同样在容器内执行；不会静默退回普通Host ASan。

## 5. 怎样理解结果与Artifact

每个GPU job在**真正有GPU的Runner**上先执行宿主预检、构建容器，再在容器内确认GPU可见性和工具链；没有设备、驱动、Docker、XNACK或工具链时应失败，而不是显示pass。

运行器按[manifest.json](../samples/manifest.json)逐例记录\`DETECTED_GPU\`、\`NOT_DETECTED\`、\`UNSUPPORTED_BY_VENDOR\`、\`CPU_ASAN_ONLY_NOT_GPU\`、\`GPU_API_ERROR_NOT_GPU_KERNEL_SANITIZER\`、\`CLEAN\`、\`FALSE_POSITIVE_OR_OTHER_ERROR\`等，严格模式下预期必检出而无GPU报告、合法对照误报、编译失败、超时、不可归类的工具错误均算失败。

Artifacts：\`nvidia-gpu-results-<run_id>\`与\`amd-gpu-results-<run_id>\`，即使任务失败也上传（若已创建日志）。包括\`results/<backend>/*.build.log\`、\`*.good.log\`、\`*.bad.log\`、\`summary-*.json\`、\`runner-environment.txt\`及ROCm的\`rocminfo.txt\`。\`summary.json\`只代表最近一次runner调用，smoke还会另外保存S01/S13/S14独立summary。

\`smoke\`通过意味着**在该次记录的具体GPU、CUDA/ROCm版本和工具范围内**成功检出其预期设备错误、且合法样例无误报；它不等于28个案例全部支持或验证完毕。要对28例声称GPU编译通过，需\`compile-all\`无失败；要对某错误场景宣称被检出，必须提供该场景的设备日志，不可以用普通\`ERROR SUMMARY\`、CPU ASan 或GPU API拒绝码冒充。若GPU job是\`skipped\`，明确记录\`GPU_NOT_RUN\`。

## 6. NVIDIA 官方上游独立回归组

新增 [tests/upstream/nvidia](../tests/upstream/nvidia/README.md)，使用 NVIDIA 官方 commit `1e1fab8e25148127f5dec10210ac70b799d1c36f` 的 BSD-3-Clause 原样样例和 LICENSE；7个NV-*逻辑检查项（6个CUDA源码）与S01～S28分开。常规CPU静态CI核对上游各Git Blob SHA、许可证、manifest与Python合成报告分类，不代表实卡已运行。

NVIDIA独立模式仅从**可信 master 的手动 workflow_dispatch**以 `suite=upstream-compile` 或 `suite=upstream-safe` 启动（`backend=cuda` 或 `both` 时只跑 NVIDIA job；AMD job自动跳过）：
- `upstream-compile`：编译官方6个CUDA程序，不启动GPU kernel、不产生检测结论。
- `upstream-safe`：对非危险案例运行 `compute-sanitizer`，同步危险场景只编译不执行。
- 不启动自研 Checker 时 `custom_diagnostic=NOT_CONFIGURED`；`vendor_diagnostic`、`custom_diagnostic`、`gpu_evidence` 各自记录，绝不合并计数。
- GPU日志上传同一 NVIDIA Artifact 中的 `results/upstream/nvidia/`。GPU检测没有实测记录时维持 `GPU_NOT_RUN`。

手工执行：
~~~bash
python3 scripts/run_upstream_nvidia.py --validate-only
bash scripts/ci-gpu.sh cuda upstream-compile
bash scripts/ci-gpu.sh cuda upstream-safe
~~~

## 7. 当前支持边界

\`S04\`逻辑子对象/\`S05\`tensor view、\`S09\`时间身份、\`S15/S16\`global race、\`S25\`atomic pointer identity等不能由当前两家工具默认完整检出。S22的masked-off合法地址**不应**产生错误。S19 HIP源码不含AMD目标原生异步copy指令，不具备CUDA cp.async等价保证；后续需要为目标GPU另写ISA原语测试。


## 8. 还未完成的真实GPU验收

目前能核实的仅是：源码/脚本/工作流已提交，GitHub CPU静态CI可报告结构/测试结果，GPU工作流的自托管任务受变量开关保护。此会话没有NVIDIA/AMD硬件、没有GPU Runner配置权限，也没有设备端编译和真实Sanitizer执行记录。

**后续验收必须逐台验证**：GPU型号、驱动/ROCm target/XNACK、镜像tag、编译日志、设备报告与PC、合法反例、失败退出码、错误汇总及Artifact，再据此将\`SOURCE_ADDED_UNVALIDATED\`升级为\`GPU_COMPILED\`或\`GPU_DETECTED\`。不能根据本文件声称已完成此步骤。
