# S17：Block Barrier参与不全

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S17 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S17 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `synccheck` | `conditional` | Divergent block barrier可能挂起GPU，需要显式允许危险测试。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | Divergent block barrier可能挂起GPU，需要显式允许危险测试。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。
**危险提示：** 该样例的错误分支可能造成GPU hang、context fault或reset；默认不自动执行bad，只有隔离硬件并明确传递`--allow-dangerous`才运行。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.17](../../docs/llvm-gpu-sanitizer-research.md#case-s17)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.17 S17：block barrier缺失live参与者与phase错配

**前提与错误。** 用例①启动64线程，`go`为已置0的block-scope原子，且只有等待barrier之后的线程0能置1；后半block持续存活并等待`go`，不会退出，因而缺失参与明确成立。用例②独立启动一个线程，`b`为正确分配的shared barrier；使用可保存整数token的低层primitive，避免混入C++移动后token的另一种误用。[N08]

~~~c
// 用例①：64线程；go在launch前已初始化为0
if (tid < 32) {
    __syncthreads();                     // BAD①：另一live warp永不参与
    if (tid == 0) go.store(1, relaxed);
} else { while (go.load(relaxed) == 0) {} }
// 用例②：独立的1线程block
__mbarrier_init(&b, 1);
auto t0 = __mbarrier_arrive(&b);          // 完成phase 0
while (!__mbarrier_test_wait(&b, t0)) {}
auto t1 = __mbarrier_arrive(&b);          // 完成phase 1，当前为phase 2
while (!__mbarrier_test_wait(&b, t1)) {}
__mbarrier_test_wait(&b, t0);             // BAD②：token早于立即前一phase
~~~

**实现路线与观测点。** A在同步指令执行处维护barrier身份、期望计数、live参与者、到达集合和完整phase；同时建等待依赖，区分循环等待证据与单纯超时。E由Compiler/C记录初始化、arrive/wait/drop、mask和退出事件，Device runtime维护同步状态，UMD提供block维度；F若能导出硬件barrier状态，可补充缺失参与证据，但仍需软件归因。

**修复、报告与边界。** 用例①把完整block barrier移到全员必经位置，或采用语义匹配的子组同步；用例②只使用当前或立即前一phase的有效token。报告barrier PC/ID、期望／实际参与集合、等待链或token/current phase。[N08] 不将普通barrier之前的任意return一概判错，退出线程必须按目标规范处理。NVIDIA当前synccheck的错误类、架构适用范围和barrier跟踪容量只是产品对照；自研“完整参与集合及phase验证”是目标能力。仅超时或缺失退出观测时，结论必须保留为疑似阻塞／覆盖不足。[N02]

若目标token仅编码phase parity，phase 0与phase 2可能具有相同值，纯token值或当前硬件状态不能保证识别这种ABA。A须额外保留token来源phase；E要保证旧token识别，须传播barrier身份和完整phase代数等sidecar。缺少该来源／epoch信息时，只能声明部分错配检测，不能宣称完整phase覆盖。

## 真实GPU生产职责

| 角色 | 正确职责 |
|---|---|
| **真实GPU检查／Host API** | 仅实际GPU设备指令或实体GPU硬件检查产生产品检测证据；Host API误用须另列 |
| **Compiler／Device Runtime／UMD** | 维护GPU检查代码、对象与生命周期、报告和覆盖状态 |
| **KMD（生产）** | 有效终止／reset和错误通知；超时不等于证明phase错误；原接口满足契约时不新增代码 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S17.*.log`日志及`summary.json`。Host API或模拟器独有检查不能计作GPU设备检测；硬件fault不能保证对象级边界精度。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
