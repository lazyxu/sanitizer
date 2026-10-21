# S01：Global对象头尾越界

## 从厂商实际实现理解 S01（优先阅读）

**[S01 厂商优先的完整实施指南](../../docs/S01-vendor-implementation-guide.md)**：

| 实际参考路线 | CUDA/AMDGPU现有机制 | 自研组件重点 |
|---|---|---|
| NVIDIA默认运行时Memcheck | 对已编译GPU二进制的访存点运行时插桩、检查当前有效分配；某些邻接live对象场景可漏检 | UMD独立Runtime Binary Rewriter处理ISA解码/编码、Patch和对象Registry；**生产Compiler零改动**（可选复用已有MC工具）；KMD按需提供映射/完成 |
| NVIDIA CUDA13.1+编译期Memcheck | Base-and-Bounds/fat pointer来源分析，运行时与GPU设备检查协同；未知来源降级 | Compiler SSA provenance与GPU检查库；UMD Kernel参数对象sidecar |
| AMD ROCm GPU ASan | LLVM访存插桩＋Shadow Memory＋Redzone/Poison＋ASan运行库 | Compiler LLVM Pass与设备checker；UMD shadow与对象生命周期；KMD按缺口改 |
| 其他GPU方案 |**GPU设备可执行**的区间表插桩、GPU MMU Guard Page、已具备的Tag/Bounds硬件| 按厂商未提供的检测保证及项目GPU硬件能力选择 |

下文保留完整CUDA/HIP源码运行方式、原S01代码与数学判定，详细各组件代码和官方出处以独立指南为准。

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S01 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S01 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `detect` | 真实GPU heap边界访问；受支持工具应报告。 |
| AMD HIP | AMD GPU AddressSanitizer | `detect` | 真实GPU heap边界访问；受支持工具应报告。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.1](../../docs/llvm-gpu-sanitizer-research.md#case-s01)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.1 S01：Global 对象头部与尾部越界

**建议先按厂商实施路径理解本例**：详见[S01：NVIDIA运行时与编译期Memcheck、AMD GPU ASan、其余方案的逐组件实施指南](S01-vendor-implementation-guide.md)。其中各路线分别说明为何检测本例、哪里可能漏检、五组件的代码与接口、核心伪代码和官方原文；本小节保留原来的错误代码、数学判断及验收描述。

**前提与错误代码。** 受控夹具给 `a` 分配 64 B，即 16 个 `uint32_t`，首地址为 `0x1000`；前后各 16 B 是保留的不可访问区，不包含其他 live 对象。启动一个 32 线程 block，分别传入 `tail=false` 和 `tail=true`：

~~~cpp
__global__ void s01(uint32_t *a, bool tail) {
    if (threadIdx.x == 0) {
        int i = tail ? 16 : -1;
        a[i] = 7;  // 错误：分别写 [0x1040,0x1044) 或 [0x0ffc,0x1000)
    }
}
// 独立运行：s01<<<1,32>>>(a, false); s01<<<1,32>>>(a, true);
~~~

**哪里不对、怎样检。** 合法字节区间只有`[0x1000,0x1040)`；两次4B写分别落在尾后和头前。**真实GPU B方案**在应用store前查询已poison的Redzone，或**真实GPU C方案**使用已插桩的设备区间检查；两者都需UMD发布逻辑长度并检查完整访问范围，不能仅靠已映射GPU页。D1编译期设备检查器若保留原对象bounds也能检出。模型只负责验证同一GPU检查器而不能代替它。[L02][L05]

**组件观测。** compiler 保留 store 的谓词、4 B 宽度和源位置；deviceRT 在执行写之前检查，不能让其他 31 个未执行线程产生报告；UMD 提供逻辑长度 64 B，而非把 allocator 的对齐补齐长度当对象长度；

**合法反例、修复及报告。** 改为 `a[0]`、`a[15]` 应通过；循环边界应使用 `0 <= i && i < 16`。预期为 global 越界写，包含 M1／M3 对应保证、宽度 4、对象范围及偏移 `-4`／`+64`。若只有页级映射检查，两个地址仍位于已映射页时可能漏检；这不是对象检查已经通过。

#### S01真实GPU路线与组件落地

| 路线 | 真机检测位置 | 核心生产组件 |
|---|---|---|
| B1 区间表 | LLVM生成的GPU访问前检查 | Compiler／Device RT＋UMD对象快照＋KMD映射 |
| B2 Shadow | LLVM生成的GPU Shadow检查 | Compiler／Device RT＋UMD Shadow/Redzone |
| C Runtime Patch | GPU执行UMD重写后的ISA | Compiler源码0；UMD Binary Rewriter＋Patch Cache |
| D1 Bounds | GPU检查原对象Bounds | Compiler来源传播＋UMD参数sidecar |
| E Hybrid | 实际GPU运行B/D和C子集 | Compiler manifest＋UMD模式／覆盖管理 |
| F | 实体GPU MMU/Tag/Bounds | F0只覆盖页级；F1/F2需真实芯片具备硬件 |

P0在真实GPU运行S01的64B对象、合法a[0]/a[15]和非法a[-1]/a[16]，记录设备检查器报告及资源状态。

## 真实GPU生产职责

| 角色 | 正确职责 |
|---|---|
| **真实GPU检查／Host API** | Compiler/UMD在真实GPU上部署设备检查指令或使用实体GPU已有硬件检查；设备执行和报告构成证据 |
| **Compiler／Device Runtime／UMD** | 生成GPU检查代码、维护元数据与生命周期、解码设备报告及覆盖状态 |
| **KMD（生产）** | 有效映射与权限；页／BO边界不能代替逻辑对象长度；接口充分时新增代码可为0 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S01.*.log`日志及`summary.json`。GPU MMU fault仅证明符合其硬件页级规则，不等于对象级越界检测。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
