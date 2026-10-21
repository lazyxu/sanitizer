# S07：Private/stack越界

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S07 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S07 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | AMD GPU ASan公开指出private/stack空间有覆盖缺口；NVIDIA视优化后的访问。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | AMD GPU ASan公开指出private/stack空间有覆盖缺口；NVIDIA视优化后的访问。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.7](../../docs/llvm-gpu-sanitizer-research.md#case-s07)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.7 S07：Private／stack 越界与返回后使用指针

**前提与越界代码。** 第一段传入运行时索引 4；第二段的 `make_bad` 确实使用独立栈 frame 并返回。只对最终存在相应 private／stack 访存的产物验收；源码数组若被标量替换、函数被消除或悬垂路径改变，须另建目标夹具。

~~~cpp
__device__ __noinline__ uint32_t read_local(int i) {
    uint32_t a[4] = {10, 20, 30, 40};
    return a[i]; // 错误：调用时 i=4，读取对象偏移 16 的 4 B
}
__global__ void s07_oob(uint32_t *out, int i) {
    if (threadIdx.x == 0) out[0] = read_local(i);
}
// s07_oob<<<1,32>>>(out, 4);
~~~

**返回后指针的独立代码。** 返回地址本身不能延长局部对象生存期；错误访问发生在 caller 解引用时：

~~~cpp
__device__ __noinline__ uint32_t *make_bad() {
    uint32_t a[4] = {10, 20, 30, 40};
    return &a[0]; // 产生即将悬垂的指针；不保证真实编译器保留此表示
}
__global__ void s07_uar(uint32_t *out) {
    if (threadIdx.x == 0) {
        uint32_t *p = make_bad();
        out[0] = p[0]; // 错误：被指向 frame 已返回，读取 4 B
    }
}
// s07_uar<<<1,32>>>(out);
~~~

**哪里不对、怎样检。** 第一段属于 M3，D1 检查 `a` 的 16 B bounds；第二段属于 M5，D2 需保留指针预期的 thread／frame／epoch 并发现 frame 已结束。另一种真正可部署的方案是由B/D编译器在实际设备代码中生成frame对象bounds、frame生存期与call/return身份检查。B的stack Redzone可以检查第一段；返回后检查还须实现延迟复用的 frame／fake-stack 等机制，普通栈地址重新变为有效后，单纯 poison 不再保证检出。[L01][L05]

**组件观测。** compiler 区分源级局部对象、spill 槽及真实 frame 生存期；deviceRT 按 logical thread 和 frame 管理状态；UMD 提供 launch／线程上下文，heap 分配表不能代替栈对象表。相同数值栈偏移属于不同线程时，必须保留不同身份。

**合法反例、修复及报告。** 索引改为 3；返回值改为标量，或写入 caller 持有且仍存活的输出数组。分别报告 private 越界读与 stack-use-after-return，列出对象范围、thread、frame 和返回位置。scratch 物理地址仍在某块合法 global backing 内，不是两例合法的证据。

## 实体GPU生产组件职责

**下表只描述GPU设备Checker、UMD及KMD真实生产职责；不能把软件静态检查代替设备端错误证据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成设备检查指令，或实际GPU硬件MMU/Tag/Bounds生效；GPU上的报告才是产品验收 |
| KMD（生产） | backing映射及资源管理；scratch BO有效不证明栈对象有效；接口充分时新增代码可为0 |

## 验收需要留下什么证据？

在对应实体GPU运行`--good`和`--bad`，保留设备型号/驱动/工具链版本、目标ISA中真正执行的检查点、模块build ID、PC、线程/lane、访问范围、对象/queue、设备报告与原始日志。纯Host资源/API错误只记录`HOST_API_DETECTED`，不能冒充`GPU_DETECTED`；GPU内部错误只有设备实际执行并提供诊断才能记`GPU_DETECTED`。未运行必须记`GPU_NOT_RUN`，无硬件/功能前提记`UNSUPPORTED`；日志丢失、OOM、未覆盖模块/指令必须报告失败或部分覆盖，不能计作clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
