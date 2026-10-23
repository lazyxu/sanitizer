# S18：Warp/Subgroup Mask错误

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S18 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S18 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `synccheck` | `conditional` | CUDA __syncwarp无效参与mask可能被synccheck识别；HIP builtin不等价。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | CUDA __syncwarp无效参与mask可能被synccheck识别；HIP builtin不等价。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。
**危险提示：** 该样例的错误分支可能造成GPU hang、context fault或reset；默认不自动执行bad，只有隔离硬件并明确传递`--allow-dangerous`才运行。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

**本 README 是 S18 的唯一详细技术与验收规范。** [主研究 §18.18](../../docs/llvm-gpu-sanitizer-research.md#case-s18) 仅保留场景摘要与稳定入口。本页伪代码与可编译 CUDA/HIP 程序按**相同违规语义**对齐，不保证逐行一致；真机验收必须确认目标访存确实执行，不能把被优化消除的操作当成已检查。

### 18.18 S18：warp／subgroup无效mask

**前提与错误。** 以下限定32-lane CUDA warp、完整32线程启动、`mode`全warp一致，`go`为已置0的合法原子。mode 0中lane 16调用同步却未把自身写进mask；mode 1中低16个lane声明全warp，但其余lane保持live，等待只有同步后才会发生的写入。退出lane与仍live但缺席lane必须区分。[N08]

~~~c
unsigned lane = tid % 32;
if (mode == 0) {
    if (lane <= 16)
        __syncwarp(0x0000ffff);          // BAD①：lane 16的self bit为0
} else {
    if (lane < 16) {
        __syncwarp(0xffffffff);          // BAD②：named live lanes 16..31未到达
        if (lane == 0) go.store(1, relaxed);
    } else { while (go.load(relaxed) == 0) {} }
}
~~~

**实现路线与观测点。** GPU设备Synccheck按动态warp、同步PC/实例记录每lane的mask、live/exit和到达状态：先检查self bit，再检查同组被点名且未退出的lane是否执行匹配调用。E由Compiler或C保存每lane原始mask与实际执行mask，Device runtime核对相交mask的一致性和等待关系；F只能在确有mask/到达状态观测接口时补充。不能把访存谓词mask直接当同步参与mask。

**修复、报告与边界。** 独立合法反例是仅lane 0–15调用`__syncwarp(0x0000ffff)`，其他lane不被点名；相互不重叠且满足目标规范的子组mask也不能误报。报告调用lane、self bit、声明mask、实际到达／已退出／仍待到达集合。CUDA要求被点名且尚未退出的参与线程以相同mask完成相应调用；具体收敛约束、wave宽度及退出语义须按目标ISA核验，不能照搬到其他GPU。[N08][L06] NVIDIA手册明确：synccheck可报self-mask错误，但不检测所有被点名线程未到达的情况；后者是自研目标，不可标成现产品保证。[N02]

合法mask应在参与者关系明确的程序点构造，不能把分支内瞬时执行mask直接当作算法预定的完整协作集合。

## 实体GPU生产组件职责

**产品检测必须由实体GPU执行对应设备Checker或真实硬件机制，正式验收需要实卡设备报告。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | 编译/运行时GPU检查ISA、GPU设备运行库、或受支持硬件MMU/Tag/Bounds；必须真机执行及报告 |
| KMD（生产） | 故障传播；看不到mask时不能自行补造诊断；原接口满足契约时不新增代码 |

## 验收需要留下什么证据？

在目标实体GPU运行错误与合法对照；保存硬件型号、驱动/Firmware、Compiler、最终目标ISA、设备原始PC与原始错误日志。发生在UMD/Host allocator API的错误仅能归类`HOST_API_DETECTED`；只有GPU内部实际执行并产生设备检测证据才能标记`GPU_DETECTED`。`GPU_NOT_RUN`、`UNSUPPORTED`、`PARTIAL_COVERAGE`、`TOOL_FAILURE`必须分别保留。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
