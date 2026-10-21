# S16：跨Queue Kernel竞争

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S16 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S16 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `racecheck` | `unsupported` | 两个stream之间没有HB，不等于工具提供跨Kernel race detector；事件影响真实调度。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | 两个stream之间没有HB，不等于工具提供跨Kernel race detector；事件影响真实调度。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.16](../../docs/llvm-gpu-sanitizer-research.md#case-s16)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.16 S16：跨kernel／queue竞争与event正确反例

**前提与错误。** `q0/q1`为不同的nonblocking stream，`x`已初始化并完成发布；`write_x`写`x[0]`，`read_x`读取相同4字节。排除legacy默认stream等隐式依赖。Host先提交writer再提交reader不建立跨queue顺序；最后一次全设备等待只等两者结束，不在二者之间补出HB。[N08]

~~~c
write_x<<<1, 1, 0, q0>>>(x, 7);
read_x<<<1, 1, 0, q1>>>(x, out);         // BAD：两次kernel访问无HB
cudaDeviceSynchronize();                 // 不追溯修复已发生的冲突
// 下方是独立合法反例，e已创建
write_x<<<1, 1, 0, q0>>>(x, 9);
cudaEventRecord(e, q0);                  // event覆盖writer完成
cudaStreamWaitEvent(q1, e, 0);            // wait必须位于reader之前
read_x<<<1, 1, 0, q1>>>(x, out);         // OK：writer -> event -> reader
cudaDeviceSynchronize();
~~~

**实现路线与观测点。** A同时模拟命令DAG与访存：将kernel内部线程时钟接到所属launch/queue节点，允许合法并行调度。E由Host runtime/UMD记录event版本、record/wait、launch与完成事件，再将B所承载的额外race插桩或C采集的地址历史接入HB引擎；不能在每个kernel结束时无条件清空global历史。只有Host API回调而没有设备访问事件或可信访问范围声明，无法确定两个kernel是否碰同一字节。[N03][P02]

**修复、报告与边界。** 上述event链及同一queue有序执行都是合法反例。设备内flag协议还须核对atomic scope与发布／获取，普通fence不能代替对端协议。报告应包含两个kernel、launch ID、queue、对象偏移及缺少的依赖边。驱动偶然串行、工具强制串行、默认stream配置改变或未执行目标路径，会影响实验覆盖；不能据此声称检查通过。基于随机延迟和值差异的发现仍是概率证据，须保留HB检测的独立口径。[P08]

event被重复record时必须按记录版本建边，不能把前一轮已完成的同名event错误套用到新一轮读写。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | launch 间依赖是否覆盖冲突 |
| QEMU 已有 GPU ISA 执行模型 | R1/A1 |
| KMD | queue/event 依赖图 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S16.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
