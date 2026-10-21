# S14：Shared RAW竞争

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S14 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S14 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `racecheck` | `detect` | NVIDIA racecheck检查GPU Shared hazard；ROCm GPU ASan只检查地址安全。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | NVIDIA racecheck检查GPU Shared hazard；ROCm GPU ASan只检查地址安全。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.14](../../docs/llvm-gpu-sanitizer-research.md#case-s14)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.14 S14：shared RAW、WAR与WAW竞争

**前提与错误。** 一个block至少2个线程；`mode`为全block一致的测试选择。先由线程0写入常量，再由完整block barrier发布，保证后续读取有已定义的旧值。三组分别覆盖写→读、读→写、写→写冲突；箭头表示待观测的访问顺序，不构成跨线程HB，运行时RAW/WAR标签可随观测次序变化。

~~~c
__shared__ volatile int x;               // volatile只用于保留访问，不提供同步
if (tid == 0) x = 7;
__syncthreads();                         // 初始化已完成
if (mode == RAW) {
    if (tid == 0) x = 1;                 // BAD①：T0 W(x)
    if (tid == 1) out[1] = x;            // BAD①：T1 R(x)，无中间同步
} else if (mode == WAR) {
    if (tid == 0) out[0] = x;            // BAD②：T0 R(x)
    if (tid == 1) x = 2;                 // BAD②：T1 W(x)
} else { if (tid == 0) x = 1; if (tid == 1) x = 2; } // BAD③：W/W
__syncthreads();
if (tid == 0) out[2] = x;                // 末尾barrier不能倒推消除先前冲突
~~~

**实现路线与观测点。** A按`launch/block/shared对象/byte`记录两端PC、线程及barrier epoch，检查重叠字节、至少一写、原子属性和HB。E由Compiler或C补齐实际shared访存与barrier事件，Device runtime使用HB历史或适用同步子集内的层次FSM；状态更新原子不能为应用制造HB。[P02][P03] 以线程32替换线程1、block设64，可扩展为跨warp测试；不同block相同shared偏移必须分离身份。

**修复、报告与边界。** 将完整block同步放在两段访问之间；WAW可明确先后或令各线程写独立位置。同warp也须按目标规则建立有效同步。报告两个访问位置、R/W类型、字节交集及缺失的HB，不能报成未初始化读取。NVIDIA racecheck的shared范围可作对照，但不推出global覆盖。[N02] 重复读、随机延迟或碰撞方案可能漏同值写、ABA或未命中窗口，应与HB/FSM的适用模型保证分栏。[P08]

验收同时加入同一word内不同byte的独立并发写，确认检测依据是实际字节交集，而非仅因共用metadata单元就误报冲突。

## 实体GPU生产组件职责

**只能由实体GPU实际执行设备检查器或已有硬件机制提供产品检测，实卡证据是验收依据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成并使GPU执行设备检查ISA；或实体GPU硬件检查单元/MMU触发，附真实设备报告 |
| KMD（生产） | 不提供GPU内部逐线程HB；驱动job按序也不消除kernel内部race；已充分支持的接口不需改源码 |

## 验收需要留下什么证据？

在实体GPU运行`--good`/`--bad`并保存硬件/驱动/编译器/模块build ID、真实设备ISA、PC/lane/宽度/对象/queue及原始报告。Host API资源/释放错误只能标记`HOST_API_DETECTED`，不能冒充设备内`GPU_DETECTED`。未运行记`GPU_NOT_RUN`，硬件能力不支持记`UNSUPPORTED`；部分覆盖、日志丢失或工具OOM必须标为`PARTIAL_COVERAGE`/`TOOL_FAILURE`而不是clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
