# 全仓库真实GPU可部署性与组件职责审计

**版本：1.7 · 日期：2026-10-21 · 范围：lazyxu/sanitizer 的主研究文档、S01厂商指南、28份样例README、归档架构、预算JSON、Docker/CI及生产组件职责。**

## 1. 已确认的决议

**只有在真实GPU上执行的Sanitizer才是产品方案。** 以下为准入条件：
1. Compiler生成并加载到实际GPU的访存前检查ISA/Device Runtime（例如LLVM GPU Shadow、对象Bounds）。
2. UMD在运行时重写实际GPU二进制，使设备真的运行检查指令（NVIDIA-R式Binary Rewriter）。**不要求修改生产Compiler**，可选复用现有MC/ISA库。
3. **真实物理GPU**已有的MMU页映射/Tag/Bounds检查单元发生硬件检查。F0 Guard Page只覆盖实际越过GPU页的访问；F1/F2若实体GPU无对应特性，不能列为当前可部署功能。

**明确撤销：** 仅在功能级cmodel/QEMU的GPU解释器中增加`before_gpu_memory_effect`或某个Host C++地址表判定。该检查只会在模拟时被调用，真实GPU直接执行未插桩Kernel时不会运行它。因此它既不能充当产品Sanitizer、不能验收实体硬件，也不再作为独立候选、单独预算或P0里程碑。QEMU存在真实目标GPU ISA执行器，不改变这一结论。

**模型唯一职责：** 预先运行**同一份真实GPU可执行插桩产物**，验证设备helper/Shadow/元数据/行为，再核对Guest KMD/UMD协议；模型独立reference oracle仅用于对照，不能给硬件检查器凭空增加来源/时间身份能力。QEMU结果只能记`MODEL_PREVALIDATED`；真实GPU执行并有设备报告才可记`HARDWARE_DETECTED`。

## 2. 各路线的真实可部署性

| 正式候选 | 真机检查执行点 | 生产Compiler新增代码 | KMD新增代码 | UMD新增代码 | 模型/QEMU |
|---|---|---|---|---|---|
| **B：LLVM GPU插桩/Shadow** | GPU访存指令之前的Device Checker | **必需**：Pass及目标设备ABI（已有同等pass时可移植） | **按契约缺口**：GPUVA/BO/fence/fault | **必需**：Redzone、Shadow、Queue依赖与报告 | 执行同一个插桩Binary做预验证，不是产品checker |
| **B1：LLVM插桩/设备区间表** | GPU设备函数查询UMD发布的对象区间 | **必需**：IR/MIR检查插入，非模拟器查表 | 已有表Buffer映射足够可0 | **必需**：区间表不可变版本、发布/回收 | 验证同一GPU查询指令 |
| **C：UMD二进制插桩** | 实际重写后机器码的设备检查指令 | **0**（纯runtime；新设备Patch模块可用现有编译器编译） | **按契约缺口**：正规代码映射/资源/完成 | **必需**：Runtime Binary Rewriter/ISA decoder/encoder/patch、对象账本 | 原始/patch后ISA等价性与设备报告验证 |
| **D：原对象/时间身份** | GPU访存前的编译期Bounds/Epoch检查指令 | **必需**：provenance/SSA/ABI与设备helper | **按契约缺口** | **必需**：参数sidecar、对象身份与时序 | 执行同一Checker，来源参考仅辅助 |
| **E：Hybrid** | B/C/D在实际GPU执行的具体检查点 | 编译期部分必需，纯runtime fallback本身不要求Compiler变更 | 只按契约并集，去重 | 模块manifest、选择、loader和元数据共享 | 混合模块/降级路径验证 |
| **F0：GPU MMU Guard Page** | **实体GPU MMU页权限** | 通常0 | 根据真实GPUVA页映射/fault支持 | GPU VMM布局、权限、fault归因 | 模拟**已有硬件语义**；页级不等于字节级bounds |
| **F1/F2：Tag／硬件Bounds** | **真实芯片**已具备的相应比较/异常单元，或有明确GPU软件插桩实现 | 随真实目标ISA/设备检查需要 | 随硬件状态契约 | Tag/Descriptor生命周期 | 如果当前硬件不存在对应能力：仅未来研究，**不能列为已可部署产品** |

**不合格实现的典型反例：**

~~~text
【不可作为产品】
原始未插桩Kernel → QEMU解释器额外Host检查 → QEMU报告越界
原始未插桩Kernel → 实体GPU直接执行 → 没有上述Host检查

【可作为产品候选】
原始Kernel → LLVM插入GPU设备检查 → 编译成真实GPU ISA
          → 实体GPU实际执行设备Check和Store → GPU报告
          └→ QEMU执行同一GPU ISA做模型预验证

【可作为产品候选】
已有GPU Binary → UMD独立Runtime Binary Rewriter → 新GPU Binary
               → 实体GPU执行补丁设备Check → GPU报告

【条件性硬件产品】
已具备GPU页保护的芯片 → KMD/UMD布置真正不可访问GPUVA页
                      → 实体GPU越页访存触发MMU fault
不代表同页内64B对象尾后4B必定触发。
~~~

## 3. 类似的职责误写：已逐类审查

| 审计问题 | 修正后的强制边界 |
|---|---|
| “运行时插桩要改Compiler/MC” | **错误**。C纯runtime使用已编译机器码；decode/encode、Patch Stub、资源重算在UMD独立Runtime Binary Rewriter；复用MC是选项，编译自己的工具模块也不等于修改生产Compiler |
| “编译期LLVM Shadow不需要Compiler改动” | 除非已有可用GPU Pass/设备支持，否则**必须修改Compiler输出**，并部署能在GPU执行的Device Checker；不能拿Host侧shadow lookup替代 |
| “KMD属于每个Sanitizer必须改的部分” | **错误**。所有方案需要KMD提供真实映射、完成/异常等契约，但现有接口足够时新增源码可为0；KMD本身不知道任意逻辑tensor view |
| “cmodel/QEMU已有目标GPU ISA，所以可以不写GPU Checker” | **错误**。模型执行器已存在只节省测试平台建设；GPU的Checker必须由真实设备ISA或实体硬件机制实现 |
| “模型能独立检测所以可以作为真实GPU性能/覆盖结果” | **错误**。仅模型预验证；实体设备的覆盖、资源、时序/性能及错误报告必须单独实测 |
| “GPU Guard Page=完整地址越界Sanitizer” | **错误**。只有触发GPU MMU非法页访问才报告；任意64B内小越界可能仍在映射页内 |
| “硬件Tag/Bounds只要在QEMU新增ISA就能立即用” | **错误**。必须实体芯片已具备该功能或完成真实GPU软件插桩；模拟器的未来指令只证明模拟语义 |
| “User Runtime/API错误等同GPU访存检测” | **错误**。S10/S11的GPU API错误与S01 GPU设备OOB分别归类，不能以API返回码代替GPU Checker |
| “Shadow/当前分配表可保证S04原对象来源” | **错误**。需要Compiler D1传播指针原始对象身份；模拟器额外知道身份不能替代真机所需ABI/metadata |
| “UMD只需记录Host free调用时间就能检测GPU生命周期” | **错误**。必须按Queue/Event的设备生效顺序发布poison、free状态；模型记录只能对照真实协议 |
| “28个CUDA/HIP源文件都代表设备检测已验证通过” | **错误**。必须真实GPU实测且对应checker具备能力；当前代码只经CPU静态CI验证结构/日志分类 |

## 4. 全仓库范围、旧数据与更新结果

| 仓库部分 | 当前约束与处理 |
|---|---|
| `docs/llvm-gpu-sanitizer-research.md` | 第9章原仅模型检查方案移除；第1/4/7/8/16/18/19/20章收敛到真实GPU检查器及QEMU辅助验证 |
| `docs/S01-vendor-implementation-guide.md` | 删除“模型独立实现”候选章节，S01-0改为真实GPU B插桩；QEMU必须在产物构建后测试同一ISA |
| `samples/S01～S28/README.md` | 全部重新从当前场景规格同步，修复错误的cmodel/QEMU/KMD列映射；强调真机Checker、模型仅验证 |
| `docs/research-notes/architecture.md` | 移除旧模型候选/成本表，修正纯C Compiler=0的边界；研究笔记仅参考，现行准入由本文件定义 |
| `docs/estimates-v1.2.json` / `v1.3.json` | 删除模型独立产品键并标记历史算术过时；原数值保留在Git历史 |
| `docs/estimates-v1.7.json` | **当前可部署候选仅B/C/D/E/F**；模型/QEMU单列为验证设施；真实Tag/Bounds硬件必须另核支持 |
| `README.md`、进度与修订记录 | 更新当前实施顺序，历史旧方案明确已撤销 |
| `scripts/**` / `samples/**` / GitHub CI | 保留厂商设备Sanitizer代码和静态校验；不将CPU CI的success算作真实GPU检测成功 |

**自动防回归：** `scripts/validate_docs.py`用于检查当前主稿与S01指南没有独立模型候选标题/候选预算、28份README含正确硬件与模型辅助验收、当前JSON只保留B-F；GitHub静态CI执行它（仅结构检查）。旧Git提交不可删除也无需篡改；本次仅维护当前master文件。

## 5. 官方事实与信息边界

- [NVIDIA Compute Sanitizer User Manual](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)：原文“**The Compute Sanitizer tools do not need any special compilation flags to function.**” 与编译期替代默认运行时二进制插桩的说明。它证明纯C模式不强制修改编译器，但不证明未公开的NVIDIA内部重写算法细节。
- [ROCm GPU AddressSanitizer](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)：设备检查由LLVM插桩生成并在真实GPU执行，需要Shadow/Redzone和Runtime协同，不能单靠Host检测。
- [QEMU Device Emulation](https://www.qemu.org/docs/master/system/device-emulation.html)：原文“**A device front end is how a device is presented to the guest.**” QEMU负责模拟设备/Guest路径，不能自动让GPU硅片执行模型侧Host回调。
- [CUDA GPU Virtual Memory Management](https://docs.nvidia.com/cuda/cuda-driver-api/cuda_driver_api/group__CUDA__VA.html)：GPUVA reserve/map/access和设备页权限确实存在，**但只对实际映射粒度生效**，不是LLVM Shadow/原对象D1的替代品。

## 6. 实际完成范围与尚未完成的真机证明

**本轮只做仓库文档/候选/预算与静态结构校验，不是新增GPU算法代码。** 已有的56个CUDA/HIP样例和GPU Runner工作流仍保留，相关实验可验证厂商现有产品的各自支持场景。自研Compiler/UMD/KMD的正式GPU Checker、硬件设备执行、错误日志、性能数据以及现有QEMU模型运行都尚未在本会话执行。未达到真实设备条件时，状态应保留`SOURCE_AVAILABLE`或`HARDWARE_NOT_VERIFIED`，不得将模型成功、CPU静态CI通过或普通API错误称为GPU Sanitize成功。
