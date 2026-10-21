# S03：真实指令对齐错误

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S03 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S03 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | 实际opcode与lowering可能使未对齐访问合法或变为字节访问。 |
| AMD HIP | AMD GPU AddressSanitizer | `conditional` | 实际opcode与lowering可能使未对齐访问合法或变为字节访问。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.3](../../docs/llvm-gpu-sanitizer-research.md#case-s03)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.3 S03：范围正确但违背指令对齐约束

**前提与错误代码。** `bytes` 是按 16 B 对齐的 64 B global 对象。测试 ISA 明确规定 `load_u32_req_align4` 必须按 4 B 对齐；其读出的 4 B 均在分配内，因而本例单独考察 M2：

~~~cpp
__global__ void s03(const uint8_t *bytes, uint32_t *out) {
    if (threadIdx.x == 0)
        out[0] = load_u32_req_align4(bytes + 1);
        // 错误：地址低两位为 01，访问 [base+1,base+5) 本身没有越界
}
// s03<<<1,32>>>(bytes, out);
~~~

**哪里不对、怎样检。** 应验证的是该目标操作要求的对齐，即 `address % 4 == 0`，并另行验证范围。真实GPU上的B可在已声明对齐要求下插入设备对齐检查；C可以针对已准确解码的目标指令插入设备端模运算／位检查。两者均须使用指令契约，不能因为源码类型为 `uint32_t` 就断言任何机器访问都必须对齐。B 的普通可访问性 shadow 和 D1 的 bounds 查询会接受这四字节，只有额外插入对齐条件才具有本例能力。[L05][N02]

**组件观测。** compiler 必须区分目标硬约束、IR 的 `align` 承诺和实际降级生成的操作；若向 IR 提供了错误的强对齐承诺，优化器可能先利用未定义行为，不能靠事后插桩恢复被删去的访问。deviceRT 输出要求值与实际余数；UMD 提供分配基址。

**合法反例、修复及报告。** 改用 `bytes+4`，或使用目标明确允许非对齐的字节读取／拷贝路径，应通过。预期报告 `MISALIGNED_ADDRESS`、global、宽度 4、要求对齐 4、实际地址与 PC，而不是对象越界。若该 ISA 本来允许此操作非对齐，原错误前提就不成立，应换用确有对齐约束的 opcode。

## 实体GPU生产组件职责

**下表只描述GPU设备Checker、UMD及KMD真实生产职责；不能把软件静态检查代替设备端错误证据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成设备检查指令，或实际GPU硬件MMU/Tag/Bounds生效；GPU上的报告才是产品验收 |
| KMD（生产） | 转交已发生的对齐fault及上下文；若硬件不报则依赖检查器；接口充分时新增代码可为0 |

## 验收需要留下什么证据？

在对应实体GPU运行`--good`和`--bad`，保留设备型号/驱动/工具链版本、目标ISA中真正执行的检查点、模块build ID、PC、线程/lane、访问范围、对象/queue、设备报告与原始日志。纯Host资源/API错误只记录`HOST_API_DETECTED`，不能冒充`GPU_DETECTED`；GPU内部错误只有设备实际执行并提供诊断才能记`GPU_DETECTED`。未运行必须记`GPU_NOT_RUN`，无硬件/功能前提记`UNSUPPORTED`；日志丢失、OOM、未覆盖模块/指令必须报告失败或部分覆盖，不能计作clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
