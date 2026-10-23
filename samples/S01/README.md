# S01：Global对象头尾越界

## 从厂商实际实现理解 S01（优先阅读）

**[S01 厂商优先的完整实施指南](../../docs/S01-vendor-implementation-guide.md)**：

| 实际参考路线 | CUDA/AMDGPU现有机制 | 自研组件重点 |
|---|---|---|
| NVIDIA默认运行时Memcheck | 对已编译GPU二进制的访存点运行时插桩、检查当前有效分配；某些邻接live对象场景可漏检 | UMD独立Runtime Binary Rewriter处理ISA解码/编码、Patch和对象Registry；**生产Compiler零改动**（可选复用已有MC工具）；KMD按需提供映射/完成 |
| NVIDIA CUDA13.1+编译期Memcheck | Base-and-Bounds/fat pointer来源分析，运行时与GPU设备检查协同；未知来源降级 | Compiler SSA provenance与GPU检查库；UMD Kernel参数对象sidecar |
| AMD ROCm GPU ASan | LLVM访存插桩＋Shadow Memory＋Redzone/Poison＋ASan运行库 | Compiler LLVM Pass与设备checker；UMD shadow与对象生命周期；KMD按缺口改 |
| 其他GPU机制 | GPU端B1当前Live区间查询、D1原对象Bounds以及具备真实芯片支持的Tag/Bounds | 按对象语义、GPU访问元数据与硬件前提选择 |


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

**本 README 是 S01 的唯一详细技术与验收规范。** [主研究 §18.1](../../docs/llvm-gpu-sanitizer-research.md#case-s01) 仅保留场景摘要与稳定入口。本页伪代码与可编译 CUDA/HIP 程序按**相同违规语义**对齐，不保证逐行一致；真机验收必须确认目标访存确实执行，不能把被优化消除的操作当成已检查。

### 18.1 S01：Global 对象头部与尾部越界

**建议先按厂商实施路径理解本例**：详见[S01：NVIDIA运行时与编译期Memcheck、AMD GPU ASan、其余方案的逐组件实施指南](../../docs/S01-vendor-implementation-guide.md)。其中各路线分别说明为何检测本例、哪里可能漏检、生产组件的代码与接口、核心伪代码和官方原文；本小节保留原来的错误代码、数学判断及验收描述。

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

**哪里不对、怎样检。** 合法字节区间只有`[0x1000,0x1040)`；两次4B写分别落在尾后和头前。**真实GPU B方案**在应用store前查询已poison的Redzone，或**真实GPU C方案**使用已插桩的设备区间检查；两者都需UMD发布逻辑长度并检查完整访问范围，不能仅靠已映射GPU页。D1编译期设备检查器若保留原对象bounds也能检出。[L02][L05]

**组件观测。** compiler 保留 store 的谓词、4 B 宽度和源位置；deviceRT 在执行写之前检查，不能让其他 31 个未执行线程产生报告；UMD 提供逻辑长度 64 B，而非把 allocator 的对齐补齐长度当对象长度。

**合法反例、修复及报告。** 改为 `a[0]`、`a[15]` 应通过；循环边界应使用 `0 <= i && i < 16`。预期为 global 越界写，包含 M1／M3 对应保证、宽度 4、对象范围及偏移 `-4`／`+64`。若只有页级映射检查，两个地址仍位于已映射页时可能漏检；这不是对象检查已经通过。

#### S01真实GPU组件责任与检测路线

S01判断一次实际生效的4B Global访问是否越过64B逻辑对象；对象在GPUVA页内并不自动等于合法。

| 机制 | 真实GPU检查 | Compiler / Device Runtime | UMD | KMD / 硬件 |
|---|---|---|---|---|
| B1区间表 | GPU执行有序区间查询 | LLVM插入设备checker ISA | 对象表快照/事件fence | 真实metadata映射 |
| B2 Shadow | GPU检查访问区间对应Shadow | LLVM Pass与GPU库 | redzone、poison、生命周期、报告 | 实际GPUVA、completion、abort |
| C运行时Patch | GPU执行UMD重写后的Binary | **生产Compiler源码零改动可行**；Patch设备Stub | 独立Rewriter、模块cache与原PC | 合法装载、执行与故障 |
| D1对象Bounds | 设备指令使用原对象token | SSA/GEP/ABI来源传播 | base/size及sidecar | 元数据与资源可见性 |
| E混合 | 插桩/patch后由真实GPU执行 | manifest、受控Pass | 模式协商与覆盖降级 | 共用资源契约 |
| F1/F2 | 实体GPU Tag/Bounds | 按真实ISA编译 | Tag/Bounds元数据初始化与失效 | 无芯片支持则UNSUPPORTED |

**首批真实硬件验收：** 在实体GPU运行`a[0]`、`a[15]`合法对照和`a[-1]`、`a[16]`非法访问，获取设备checker报告、PC、访问宽度、lane、异步metadata顺序和真实失败终止。没有真实GPU结果不得报告为已检出。

[所有生产组件的关键实现代码/伪代码及汇总表](../../docs/real-gpu-components-implementation.md)

**从原主报告归并的补充边界：**

实际对象只有64字节，GPUVA页命中不等于逻辑对象访问合法。必须在目标GPU执行的Global Store之前做相应检查。

**第一交付工作包：** LLVM在真实Global Store前插入设备Shadow检查，UMD发布64B逻辑长度与redzone，KMD复用正规映射、提交及错误契约。直接在实体GPU运行 `a[0]`、`a[15]`、`a[-1]`、`a[16]`，核对PC、lane、byte宽度、报告和安全终止。完整代码见[逐组件伪代码](../../docs/real-gpu-components-implementation.md)。

## 验收需要留下什么证据？

在对应实体GPU运行`--good`和`--bad`，保留设备型号/驱动/工具链版本、目标ISA中真正执行的检查点、模块build ID、PC、线程/lane、访问范围、对象/queue、设备报告与原始日志。纯Host资源/API错误只记录`HOST_API_DETECTED`，不能冒充`GPU_DETECTED`；GPU内部错误只有设备实际执行并提供诊断才能记`GPU_DETECTED`。未运行必须记`GPU_NOT_RUN`，无硬件/功能前提记`UNSUPPORTED`；日志丢失、OOM、未覆盖模块/指令必须报告失败或部分覆盖，不能计作clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
