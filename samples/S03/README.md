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

**哪里不对、怎样检。** 应验证的是该目标操作要求的对齐，即 `address % 4 == 0`，并另行验证范围。A0 在解释实际 opcode 时检查对齐；C 可对已明确解码的同类指令插入模运算／位检查。两者均须使用指令契约，不能因为源码类型为 `uint32_t` 就断言任何机器访问都必须对齐。B 的普通可访问性 shadow 和 D1 的 bounds 查询会接受这四字节，只有额外插入对齐条件才具有本例能力。[L05][N02]

**组件观测。** compiler 必须区分目标硬约束、IR 的 `align` 承诺和实际降级生成的操作；若向 IR 提供了错误的强对齐承诺，优化器可能先利用未定义行为，不能靠事后插桩恢复被删去的访问。deviceRT 输出要求值与实际余数；UMD 提供分配基址；cmodel 在指令生效前比对 ISA 规则，不能只看宿主机模拟读写是否成功。

**合法反例、修复及报告。** 改用 `bytes+4`，或使用目标明确允许非对齐的字节读取／拷贝路径，应通过。预期报告 `MISALIGNED_ADDRESS`、global、宽度 4、要求对齐 4、实际地址与 PC，而不是对象越界。若该 ISA 本来允许此操作非对齐，原错误前提就不成立，应换用确有对齐约束的 opcode。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 是否满足该指令的对齐要求 |
| QEMU 已有 GPU ISA 执行模型 | M2 |
| KMD | opcode 与对齐契约 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S03.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
