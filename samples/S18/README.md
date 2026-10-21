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

以下章节直接对应[主调研文档 §18.18](../../docs/llvm-gpu-sanitizer-research.md#case-s18)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

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

**实现路线与观测点。** A按动态warp、同步PC/实例记录每lane的mask、live/exit和到达状态：先检查self bit，再检查同组被点名且未退出的lane是否执行匹配调用。E由Compiler或C保存每lane原始mask与实际执行mask，Device runtime核对相交mask的一致性和等待关系；F只能在确有mask/到达状态观测接口时补充。不能把访存谓词mask直接当同步参与mask。

**修复、报告与边界。** 独立合法反例是仅lane 0–15调用`__syncwarp(0x0000ffff)`，其他lane不被点名；相互不重叠且满足目标规范的子组mask也不能误报。报告调用lane、self bit、声明mask、实际到达／已退出／仍待到达集合。CUDA要求被点名且尚未退出的参与线程以相同mask完成相应调用；具体收敛约束、wave宽度及退出语义须按目标ISA核验，不能照搬到其他GPU。[N08][L06] NVIDIA手册明确：synccheck可报self-mask错误，但不检测所有被点名线程未到达的情况；后者是自研目标，不可标成现产品保证。[N02]

合法mask应在参与者关系明确的程序点构造，不能把分支内瞬时执行mask直接当作算法预定的完整协作集合。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 调用者与命名参与者满足同步规则 |
| QEMU 已有 GPU ISA 执行模型 | S1 |
| KMD | lane ID、mask、目标语义 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S18.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
