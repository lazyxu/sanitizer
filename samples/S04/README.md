# S04：对象A跳入存活B

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S04 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S04 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `unsupported` | 同一pool中两个逻辑对象都处于合法大分配；缺乏provenance。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | 同一pool中两个逻辑对象都处于合法大分配；缺乏provenance。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.4](../../docs/llvm-gpu-sanitizer-research.md#case-s04)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.4 S04：从对象 A 越界，恰好落入仍存活的对象 B

**前提与错误代码。** 受控 allocator 建立两个独立、相邻且已初始化的 live 对象：A 为 `[0x1000,0x1040)`，B 为 `[0x1040,0x1080)`。记录并核对实际布局，不能假定两次普通分配必然相邻。`a` 的来源身份是 A：

~~~cpp
__global__ void s04(uint32_t *a, uint32_t *out) {
    if (threadIdx.x == 0)
        out[0] = a[18]; // 错误：A+72 = 0x1048，落在 live B 的偏移 8
}
// s04<<<1,32>>>(a, out);  // 读取宽度 4 B
~~~

**哪里不对、怎样检。** 目标四字节可访问，不代表从 A 推导的访问合法；应使用指针携带的 A 边界判断偏移 72。D1 可沿 `a` 的偏移计算传播 A 的 base／size，在访存处发现超出 64 B；A2 可独立跟踪参数、寄存器和指针运算的来源身份，再验证相同约束。两条路线都要求来源链没有被外部裸指针、整数往返或未受控内存槽破坏。只有分配表与实际地址的 plain A／C、以及只反映地址有效性的 B，都不能保证检出本例。[P01][N04]

**组件观测。** compiler 为 `a` 保留 A 的身份，不能在最终地址上反查到 B 后覆盖它；deviceRT 查询预期对象 A；UMD 发布 A／B 两份不同描述符和参数 sidecar；cmodel 同时记录“来源 A”和“地址当前落入 B”，用独立身份传播核对编译插桩结果。若 E 的 fallback 仅剩裸地址，应降级保证并计数。

**合法反例、修复及报告。** 若确实要读 B，应通过正确携带 B 身份的 `b[2]`；若要读 A，则索引应小于 16。预期报告 A 的越界读、偏移 72／宽度 4、A 的 64 B 边界，可补充实际落入 B。不能把“未发生硬件 fault”或“B 也存活”解释为通过；来源 unknown 时也不能伪造 A 级保证。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 地址有效但原对象越界 |
| QEMU 已有 GPU ISA 执行模型 | M3 |
| KMD | 原对象身份与 bounds |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S04.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
