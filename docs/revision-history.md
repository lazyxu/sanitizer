# 文稿修订记录

## v1.7 · 2026-10-21

1. 根据用户对**真实GPU可部署性**的要求，确认“直接在cmodel/QEMU解释器中加入访存检查”只在模拟环境起作用，实体GPU运行未插桩代码不会调用该检查。该模型独立产品路线、预算和前置交付里程碑全部撤销。
2. 主报告第9章改为[真实GPU准入与模型验证边界](llvm-gpu-sanitizer-research.md#hardware-deployment-gate)，当前候选仅B/C/D/E/F；QEMU/cmodel仅对GPU可执行检查代码与真实硬件语义提供预验证，不再是检测产品。
3. 重写S01厂商指南实施次序，删除独立模型方案章节，首阶段必须提交LLVM/设备库和UMD Shadow等可用于真机的检测代码；F0仍仅保证GPU页级，F1/F2未有真实硬件支持时不计已可部署。
4. 28份样例README从最新主场景重新同步，修正能力编号误列成模型/KMD职责的旧表；保留全部CUDA/HIP源文件与厂商GPU CI，未伪造任何新硬件检测结果。
5. 归档架构工作笔记中旧模型产品路线和金额移除；历史v1.2/v1.3结构化预算删除该路线键并标记历史算术失效，新增[五路线硬件预算](estimates-v1.7.json)，原历史可通过Git提交追溯。
6. 再次确认C纯Runtime Compiler可0修改，设备Patch与Runtime Binary Rewriter归UMD；B/D编译期仍需Compiler能力；KMD是否写新代码取决于真实接口缺口。
7. 添加[全仓库硬件可部署性审计清单](hardware-deployability-audit.md)及防回归CI脚本，后续若引入仅模拟器可生效的功能必须被标为测试工具，禁止重新列入产品路线。

**历史版本保留在Git中，下面旧修订事件里提到的已撤销概念不再代表当前产品方向。**


## v1.6.1 · 2026-10-21

针对“运行时二进制插桩不要求特殊编译，为何仍要求修改编译器”的正确质疑，纠正下列误导：

1. [NVIDIA-R厂商指南](S01-vendor-implementation-guide.md)区分原始应用编译、编译工具自己的Patch CUBIN、已有LLVM MC库可选复用，以及**UMD独立Runtime Binary Rewriter**；这四者不能统称为“修改Compiler”。
2. 运行时C路线的生产Compiler新修改明确**0（已有编译器足够时）**。设备Patch/ISA decoder/encoder/CFG/relocation和资源更新均可由独立UMD运行时工具负责；不要求新增LLVM Pass、CodeGen或MC。缺少合法代码加载权限、ISA工具时按实际平台缺口重新评估。
3. 主报告§11和§16及S01样例中的“Compiler/MC必须提供decoder”表述同步更正。原C Compiler预算是设备Patch库及可选MC的**历史交付包分组**，不是强制编译器源码修改。§16.5a展示从原C预算重新归属到UMD/Runtime的分组方法（总量不变；不是新测量），避免重复计费。
4. NVIDIA官方手册确认默认工具不需要特殊编译标志；Patching API确实可以使用**现有**nvcc把工具回调编译为补丁模块；这都不证明必须修改原始用户编译器。本轮仅文稿更正，没有编译或运行实际GPU检测器。


## v1.6 · 2026-10-21

1. 新增[独立 S01 按厂商实施指南](S01-vendor-implementation-guide.md)，而非用A/B/C抽象分类作首要目录：NVIDIA默认运行时二进制插桩与当前有效分配检查；NVIDIA CUDA13.1+编译期fat-pointer原对象bounds；AMD GPU ASan的LLVM插桩/Shadow/Redzone/设备运行库配合。
2. 每条厂商路径列出具体运行链路、Compiler/MC、KMD、UMD、功能级cmodel和**已有QEMU GPU ISA执行模型**的具体改动，包含设备检查和UMD分配/模块加载伪代码、前提与漏检边界。
3. 当时曾将模型参考检查错误列入候选（v1.7已撤销），其它LLVM设备区间表、GPU页级Guard、实体硬件Tag/Bounds与Hybrid分工，明确公开算法来源／厂商未知内部细节／针对自研GPU的原创工程接口。
4. 每条路径链接官方手册、上游代码、限长英文原文摘录和设备验证命令，说明NVIDIA默认Memcheck不等于AMDGPU的经典1:8 Shadow布局；核对XNACK+及device ASan运行库要求。
5. 在主研究报告第4章与§18.1、项目README、S01样例README增加导航；28场景、全部CUDA/HIP样例和现有GPU Actions保持不变。**新增内容是设计/文档，不是sanitizer实现或真机运行验收。**


## v1.5 · 2026-10-21

1. 在master增加`docker/Dockerfile.cuda`、`Dockerfile.rocm`与`scripts/setup-docker.sh`，采用已确认存在的CUDA 13.1.2/ROCm 7.2.4官方开发镜像。宿主必须真实提供GPU/驱动，不能用CPU sanitizer代替。
2. 增加`samples/S01～S28`每组CUDA `cuda.cu`、AMD HIP `amd.hip.cpp`以及详细中文README，合计56份主源文件，附`samples/manifest.json`逐GPU厂商能力判断和`scripts/run_samples.py` GPU专用编译/运行器。
3. 独立增加CUDA Driver API与HIP GPU VMM Guard Page实验，明确其使用GPUVA保留/映射/权限和GPU MMU fault，而非Host `mprotect`，并保留页粒度与字节级对象bounds的区别。
4. 新增`.github/workflows/gpu-hardware.yml`：GPU_CI_ENABLED显式开关、可信master、自托管NVIDIA/AMD GPU Runner，手动/可信push/周期触发；提供smoke/compile-all/safe-all，按manifest真实检测和上报Artifact；无GPU Runner时GPU任务显示SKIPPED。
5. 新增`scripts/ci-gpu.sh`、`tests/test_gpu_runner.py`，扩展CPU CI仅测试静态结构与合成日志类别；S01/S13/S14按特定设备错误类别验收，阻止CPU ASan、GPU API错误、其他设备诊断或无法分类的工具错误冒充目标检测成功。
6. 新增[GPU Actions与Runner使用说明](gpu-actions-ci.md)，强调AMD XNACK+、GPU权限、官方T4大型Runner准入条件、自托管公有仓库的PR安全限制。明确：28源文件均已加入，真实GPU nvcc/hipcc编译和sanitizer运行尚未证明，不可声称双厂商28个错误全部能够检出。
7. 主文档升为1.5，保留v1.3/QEMU和v1.4详细场景的机制设计。GPU CI新增只属于NVIDIA/AMD厂商样例验收，尚未实现自研LLVM GPU后端的sanitizer。


## 1.3 · 2026-10-21

本轮根据用户补充：“QEMU全系统虚拟平台已有GPU ISA执行模型”、要求深入展开友商产品实现原理，以及逐方案标明借鉴／融合／原创来源，完成以下变更：

1. 在第1.1、1.2、7.7、8.8、16.9、18.1、18.29、19.7、20及22章相关段落中，将本项目QEMU明确定为**已有目标GPU ISA执行模型的Q2**。其他Q0/Q1/Q3只是架构分类，不代表本项目缺少GPU执行器。cmodel是否同源、Guest链路完整性、checker/metadata接口仍需源码核实。
2. 第4章扩展为十个小节：NVIDIA memcheck默认运行时/可选编译期、patching/callback/memory API、race/init/sync机制；AMD GPU ASan的Shadow/Redzone及LLVM-runtime接线；Intel Unified Runtime和设备ASan/MSan/TSan分工；MUSA产品可证实功能与未知内部实现；LLVM ASan/HWASan源码；cuCatch/BARRACUDA/HiRace/GPUArmor/SuperCollider研究来源；全路线机制矩阵和官方源链接。
3. 第4.8节和第9～14章分别明确A、B1/B2、C、D1/D2、E、F0/F1/F2是直接借鉴公开机制、融合多个来源，还是本项目必须单独设计的工程实现。第15.7节增补init/race/sync算法溯源；“原创”不表示基本算法学术新颖。
4. 第7.7.6、8.8.5提供现有QEMU GPU ISA执行器的pre-access hook、对象/错误协议与最小实现示例；S01详细实例增补七类路线的来源和Compiler/KMD/UMD/cmodel/QEMU改动。
5. 第16.9.4重新界定预算：QEMU GPU ISA模型已经存在，不重计开发费；既有14–28kLOC Q2一次性基础包及各路线接入增量只是**对应接口全部缺失时的条件性预算参考**。新增[estimates-v1.3.json](estimates-v1.3.json)，保留原[v1.2](estimates-v1.2.json)以便追溯。
6. 官方参考新增[I03] oneAPI Unified Runtime，资料数更新至40；同步README、研究进度、验证记录。所有检查仅为文档结构/设计核对，未实现或执行目标GPU sanitizer。

## 1.2 · 2026-10-20

依据“按编译器、KMD、UMD划分组件，逐方案说明功能级cmodel及QEMU全系统平台的检测与修改支持”的修订要求：

1. 第1.2及8.7节明确三组件职责、内部子模块、事件生产方与底层结果、身份去重和所需支持；区分复用契约与必须新增实现。
2. 第7.7及8.8节定义功能模型、Q0～Q3全系统配置、GPU实际执行位置、TCG/DMA观测边界、Guest/Host地址域与驱动接入。说明Host ASan、Guest KASAN和GPU sanitizer分别检查什么。
3. 历史版本曾含已经撤销的模型独立方案；第9～14章当时为各候选加入三组件支持与修改表，以及cmodel/QEMU能力、修改与验证表；D1/D2分别列出组件要求。第15章补上init/race/sync的相同口径。
4. 第16章以Compiler/KMD/UMD重组原预算，同时保留设备库、Host runtime、CLI等可追溯子项及独立cmodel。旧预算曾包括现已撤销的模型独立产品成本；当前B～F预算已单独更新；KMD条件包、固件、Q2基础与路线接线单列去重，新增结构化估算数据。
5. 原S01～S28详细实例保留，新增18.29逐场景KMD／双平台对应表和19.7分层验收。区分模型执行、独立oracle、故障注入、Guest驱动证据和真机性能。
6. 路线图、团队职责、风险和待确认参数同步更新；新增Linux kernel4项及QEMU5项官方来源，总计39项。原友商、论文及内存模型章节保持。
7. 独立复核后收紧三个边界：终止能力取决于所选错误策略；C未必需要专门调试接口；泄漏账本须在退出／context自动回收前留存，清理结果另验。
8. 更新README、进度、验证记录并新增estimates-v1.2.json。所有代码量为设计估算，文稿检查不代表已完成GPU/驱动/QEMU实现或实验。

本轮以1.1提交`747eaac5cd1ed49d120868818b0fc9dbfc98e9ba`为基线；未改变23章主编号或28个场景ID。QEMU在线master文档本次标示11.1.50，固定版本与部署参数仍需实施时确认。

## 1.1 · 2026-10-20

依据“详细说明背景和目标，并逐类增加具体检测实例”的修订要求：

1. 第2章新增明确的项目背景与建设目标：建设动因、故障证据链、跨组件归因、已有工具盲区、使用者、功能与质量目标、完成证据、优先级和非目标。
2. 新增第18章S01～S28。每例说明触发前提、具体代码或事件、违反的约束、至少两种可选检测思路、组件信息、修复／合法对照、预期报告与覆盖边界。合法程序被错误插桩破坏的场景单独标识。
3. 第8章细化异步free提交、开始生效、完成及元数据安全回收，避免把物理回收时间当作应用生命周期终点。
4. 验收矩阵增加场景跳转；补充CUDA Programming Guide与PTX ISA两项官方参考，保留现有论文和版本比较。
5. 更新README、研究进度和文稿验证记录；六路线组件预算保持1.0口径。新增内容仍为调研与设计，没有宣称完成实际sanitizer实现、GPU测试或论文复现。

### 章节编号迁移

| 1.0版 | 1.1版 | 内容 |
|---|---|---|
| 2 | 2 | 扩展为背景、目标与研究范围 |
| 无 | 18 | 28个检测场景与实例 |
| 18 | 19 | 验证基准与验收 |
| 19 | 20 | 分阶段研发计划 |
| 20 | 21 | 主要风险 |
| 21 | 22 | 平台参数 |
| 22 | 23 | 参考文献与核验记录 |

主稿和README提供稳定锚点；历史研究笔记保留原编号，以主稿最新目录为准。

## 1.0 · 2026-10-20

完成首版调研：22章、28项参考来源，涵盖友商、原理、效果证据和当时的路线候选（模型独立方案后来撤销）、组件改动、工程预算、验证和阶段计划。首版正文提交为`11dc521d3707379271ecdc03ec45caca7ef05852`；README、研究笔记、进度和验证记录提交为`dabf36e7f84f290329374dbf950348680c1f0c20`。
