# S19：GPU Async Copy缺Wait

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S19 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S19 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `synccheck` | `conditional` | CUDA sm80+的cp.async语义，缺wait错误受调度影响；HIP源码仅功能对照。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | CUDA sm80+的cp.async语义，缺wait错误受调度影响；HIP源码仅功能对照。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


**当前实验子范围限制：** HIP文件为同步复制功能对照，没有与CUDA cp.async等价的AMD原生异步指令；不能据此宣称AMD S19检测已实现。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.19](../../docs/llvm-gpu-sanitizer-research.md#case-s19)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.19 S19：async copy缺wait、等待了错误批次或phase

**前提与错误。** 单线程block、支持所示pipeline primitive的目标，global源32字节已经完整初始化，源／shared目的满足16字节对齐。先把shared写为0，排除未初始化读取。mode 0在传输完成前读目的；mode 1等待的只保证较早批次，不能保护随后读取的最新批次。[N08]

~~~c
__shared__ alignas(16) unsigned char sm[32];
for (int i = 0; i < 32; ++i) sm[i] = 0;
__pipeline_memcpy_async(sm, src, 16);
__pipeline_commit();                     // batch 0
if (mode == 0) consume(sm[0]);            // BAD①：没有wait
else {
    __pipeline_memcpy_async(sm + 16, src + 16, 16);
    __pipeline_commit();                 // batch 1
    __pipeline_wait_prior(1);            // 仅保证batch 0完成
    consume(sm[16]);                     // BAD②：batch 1仍可能在途
}
__pipeline_wait_prior(0);                // 收尾等待不追溯修复过早读
~~~

**实现路线与观测点。** A把异步传输作为独立访问主体，追踪源读／目的写、commit组号、完成及消费者可见性；指令发出不等于目的已经可读。C拦截实际async opcode与wait，或E结合Compiler高层pipeline信息、runtime completion和Device checker；必须关联到同一个传输组／barrier phase，不能把任意wait当作全局完成。[N02][N03]

**修复、报告与边界。** 读取前`__pipeline_wait_prior(0)`保护本线程已提交批次；其他线程读取时，还需规定的参与同步与可见性。barrier版本要等绑定当前copy的phase：上一phase的token可能仍是合法参数，但成功等待它不证明当前传输已完成；比立即前一phase更旧的token则另属S17。报告copy发起PC、范围、组号／phase、等待实际覆盖范围及过早访问PC。预置0仍可发生传输竞争，不能只依赖I1位图。未观测copy engine、未支持bulk/tensor异步指令、仅观察一次幸运完成顺序均可能漏检；NVIDIA现有shared async-copy检查不能外推为全部引擎覆盖。[N02]

测试最终仍等待所有在途操作完成，以把过早读取与退出时资源生存期错误分开验证。

## 生产方案与两类模型辅助验证职责

**本场景只能由真实GPU设备代码或者已实现的硬件检查机制提供产品保证；模拟器和QEMU不能单独增加真机检测能力。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | 编译/运行时GPU检查ISA、GPU设备运行库、或受支持硬件MMU/Tag/Bounds；必须真机执行及报告 |
| 独立功能级cmodel（验证） | copy group、完成、barrier phase与消费事件；用真实checker产物做语义预验证 |
| **现有QEMU GPU ISA解释器（验证）** | GPU内部异步引擎与系统DMA事件分别接入；模型独有hook不得计GPU产品实现 |
| KMD（生产） | 外部copy引擎完成只说明该引擎，不代替kernel内部wait语义；原接口满足契约时不新增代码 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S19.*.log`日志及`summary.json`。Host API或模拟器独有检查不能计作GPU设备检测；硬件fault不能保证对象级边界精度。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
