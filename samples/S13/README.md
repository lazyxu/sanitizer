# S13：未初始化Global读

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S13 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S13 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `initcheck` | `detect` | CUDA initcheck可报告未初始化GPU Global读取；AMD GPU ASan不检查定义性。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | CUDA initcheck可报告未初始化GPU Global读取；AMD GPU ASan不检查定义性。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.13](../../docs/llvm-gpu-sanitizer-research.md#case-s13)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.13 S13：未初始化读取、部分字节写入与copy传播

**前提与错误。** `src/dst`来自真实分配，初始定义性位均为0；两者8字节均可寻址且满足4字节读取对齐。所有命令在同一stream，排除顺序错误。`fixed=false/true`两版独立运行，不依赖错误后继续执行。`read32`实际消费偏移0的4字节并写入有效输出；它不能被优化掉。按byte复制含未定义字节的对象表示，本身不一概构成程序错误；copy应传播定义性，是否同时报告copy源未定义属于工具策略。错误断言落在随后消费未定义byte 3的读取。

~~~c
cudaMalloc(&src, 8);                      // I(src[0..7]) = 0
cudaMalloc(&dst, 8);                      // I(dst[0..7]) = 0
cudaMemsetAsync(src, 0, 3, s);            // 只定义src[0..2]
if (fixed) cudaMemsetAsync(src + 3, 0, 5, s); // 独立修复版补齐真实写入
cudaMemcpyAsync(dst, src, 8, D2D, s);     // 传播源定义性，不能把dst全标已定义
read32<<<1, 1, 0, s>>>(dst, out);         // BAD：fixed=false时消费未定义byte 3
cudaStreamSynchronize(s);
~~~

**实现路线与观测点。** A在allocation时置未定义位，按实际byte enable更新store/memset，copy按源位图传播；B额外建设独立初始化bitmap，由Compiler插入load/store检查，Device runtime原子合并共享metadata word。E接入runtime/UMD及copy engine的提交、完成、依赖事件，不能在Host提交copy时提前定义目的端。H2D若只采用“Host来源已定义”假设，须明确其覆盖边界；这属于第15章I1设计，不是普通地址ASan的附赠能力。[N03][L05]

**修复、报告与边界。** 独立运行`fixed=true`版本，完整有序memset后的读取应无报；另测仅补写byte 3后读取4字节合法、读取8字节仍非法。报告至少给出`dst+3`、读取宽度、未定义byte mask、源allocation和copy链。不得把“向内存store一个来源未定义的寄存器”当作值级定义性已正确实现；完整MSan传播另立能力。若copy engine不可见、只按word标记初始化、未跟踪Host来源或实际指令被消除，均需单列漏检边界。

## 实体GPU生产组件职责

**只能由实体GPU实际执行设备检查器或已有硬件机制提供产品检测，实卡证据是验收依据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成并使GPU执行设备检查ISA；或实体GPU硬件检查单元/MMU触发，附真实设备报告 |
| KMD（生产） | DMA/copy完成和失败；不凭分配成功认定内容已定义；已充分支持的接口不需改源码 |

## 验收需要留下什么证据？

在实体GPU运行`--good`/`--bad`并保存硬件/驱动/编译器/模块build ID、真实设备ISA、PC/lane/宽度/对象/queue及原始报告。Host API资源/释放错误只能标记`HOST_API_DETECTED`，不能冒充设备内`GPU_DETECTED`。未运行记`GPU_NOT_RUN`，硬件能力不支持记`UNSUPPORTED`；部分覆盖、日志丢失或工具OOM必须标为`PARTIAL_COVERAGE`/`TOOL_FAILURE`而不是clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
