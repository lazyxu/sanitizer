# S05：Tensor view逻辑子边界

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S05 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S05 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `unsupported` | Tensor view的列索引非法但物理GPUVA在合法pool里。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | Tensor view的列索引非法但物理GPUVA在合法pool里。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.5](../../docs/llvm-gpu-sanitizer-research.md#case-s05)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.5 S05：内存池子分配、granule 孔洞与 tensor view 边界

**前提与错误代码。** 一个 128 B pool 全部可寻址；同一 8 B granule 中，子对象 A 占偏移 `[0,3)`、B 占 `[5,8)`，中间是未分配孔洞。另在 `pool+32` 注册 float view：2 行、3 列、行步长 4 元素。下列两个分支分别运行，`view_store` 的示意实现暂未检查索引：

~~~cpp
uint8_t *a = register_suballocation(pool, 0, 3);
uint8_t *b = register_suballocation(pool, 5, 3);
View v = register_view(pool + 32, /*rows=*/2, /*cols=*/3,
                       /*row_stride=*/4, /*element_bytes=*/4);
__global__ void s05(uint8_t *a, View v, int which) {
    if (threadIdx.x != 0) return;
    if (which == 0) a[4] = 1;           // 错误：进入逻辑孔洞，仍在 pool 内
    else view_store(v, 0, 3, 1.0f);     // 错误：列 3；写到行间 padding
}
// 两次独立 launch：<<<1,32>>>，which 分别为 0、1。
~~~

**哪里不对、怎样检。** M4 需要子对象／view 契约。前一写超出 A 的 3 B；后一写位于 view 外包区间内，却不满足 `row<2 && col<3`。D1设备端检查可以为子对象收窄bounds，并把view形状、步长、坐标验证嵌入实际GPU指令；UMD/框架必须在运行时注册逻辑view。若降成裸指针且索引语义丢失，仅有外包范围不能还原任意 view 的逻辑约束。[P01][L05]

**组件观测。** compiler 保留子对象身份与行列操作；deviceRT 查询对应逻辑描述符；UMD／框架登记 parent、子范围及生存期。B 可用更细位图表达特定孔洞，但经典 1:8 的单字节编码不能表示本例“有效三字节、无效两字节、有效三字节”。全局 poison 也不能区分同地址上的多个重叠 view 权限。[L02]

**合法反例、修复及报告。** `a[2]` 与 `view_store(v,0,2,...)` 应通过；合法的 pool 管理操作可持有 parent 范围，不能因逻辑孔洞被永久误禁。预期报告 parent／子对象 ID、错误偏移，或 `(row=0,col=3)` 与形状、步长。未登记子分配时只能报告 pool 级覆盖，不能将“地址在 128 B 内”当作 tensor 索引安全。

## 实体GPU生产组件职责

**下表只描述GPU设备Checker、UMD及KMD真实生产职责；不能把软件静态检查代替设备端错误证据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成设备检查指令，或实际GPU硬件MMU/Tag/Bounds生效；GPU上的报告才是产品验收 |
| KMD（生产） | 支撑父映射；同一BO中的tensor边界由上层登记；接口充分时新增代码可为0 |

## 验收需要留下什么证据？

在对应实体GPU运行`--good`和`--bad`，保留设备型号/驱动/工具链版本、目标ISA中真正执行的检查点、模块build ID、PC、线程/lane、访问范围、对象/queue、设备报告与原始日志。纯Host资源/API错误只记录`HOST_API_DETECTED`，不能冒充`GPU_DETECTED`；GPU内部错误只有设备实际执行并提供诊断才能记`GPU_DETECTED`。未运行必须记`GPU_NOT_RUN`，无硬件/功能前提记`UNSUPPORTED`；日志丢失、OOM、未覆盖模块/指令必须报告失败或部分覆盖，不能计作clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
