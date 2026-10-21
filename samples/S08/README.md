# S08：未复用UAF

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S08 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S08 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | 释放后的旧GPUVA可能硬件fault或被工具判断UAF，报告形式与窗口不保证。 |
| AMD HIP | AMD GPU AddressSanitizer | `conditional` | 释放后的旧GPUVA可能硬件fault或被工具判断UAF，报告形式与窗口不保证。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.8](../../docs/llvm-gpu-sanitizer-research.md#case-s08)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.8 S08：Free 已生效、地址尚未复用的 UAF

**前提与错误代码。** 受控 allocator 分配 64 B，退休后保留底层映射并禁止复用到本例结束；`out` 事先分配。`wait_host` 确认该 queue 上的 free 已生效，故本例没有“free 尚在排队”的歧义：

~~~cpp
__global__ void read_first(uint32_t *old, uint32_t *out) {
    if (threadIdx.x == 0) out[0] = old[0]; // 错误：已退休对象上的 4 B 读取
}
uint32_t *p = test_alloc(64);
uint32_t *old = p;
fill_async(p, 64, stream);
free_async(p, stream);
Event retired = record_event(stream);
wait_host(retired);
require(test_is_dead_and_not_reused(p));
read_first<<<1,32,0,stream>>>(old, out);
~~~

**哪里不对、怎样检。** 地址可能仍映射，但对象生存期已结束。B 在退休命令真正生效后 poison，对旧范围读做检查；**真实GPU C的运行时补丁/UMD对象账本**可以保留已退休对象Tombstone，并将生命周期状态作为设备检查输入；C必须真的在访问前执行设备检查，而不能只在模型里判断。这里尚无新对象占用该地址，因此“当前地址已 dead”足以判错；不能由此推导已具备 S09 的复用后时间安全。[L02]

**组件观测。** compiler 保留旧指针的实际 load；deviceRT 保证 poison 与后续访问具有正确可见顺序；UMD 提供 free 请求、完成事件及禁止复用条件；cmodel 依照 queue／event 顺序推进状态，不能把 Host API 的返回时间当成所有队列均已退休。测试启动前必须核对 dead 状态，不能靠一次 sleep 猜测。

**合法反例、修复及报告。** 同 queue 中先提交读取、再提交 free，读取仍应合法，即使 Host 很快看见 free API 返回。修复是把释放排在所有合法使用之后，并补齐跨 queue 的依赖；“再等待一次已完成的 free”不会使旧指针恢复有效。预期报告 UAF、对象 ID、4 B 读取、allocation／free／访问位置与次序。若释放时删掉全部元数据而映射仍在，只有映射检查的实现可能漏检。

## 生产方案与两类模型辅助验证职责

**模型及QEMU仅验证实际GPU设备检查产物/真实硬件机制，不可作为独立Sanitizer产品方案。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成并使GPU执行设备检查ISA；或实体GPU硬件检查单元/MMU触发，附真实设备报告 |
| 独立功能级cmodel（验证） | free生效事件与poison状态；只用于预验证真实checker产物和oracle差分 |
| **现有QEMU GPU ISA解释器（验证）** | Guest队列顺序驱动metadata失效，再验证回传；不能用模型独有访存hook代替真实设备检查 |
| KMD（生产） | 完成／故障与映射生命周期；不以Host调用时间判失效；已充分支持的接口不需改源码 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S08.*.log`日志及`summary.json`。CPU ASan、Host API错误或模型独有hook不能冒充真实GPU设备检测；GPU MMU fault限于页级证据。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
