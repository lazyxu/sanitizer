# S23：Constant资源符号边界

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S23 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S23 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | Constant符号边界与资源坐标/权限不能等同，ASan并不保证这类地址空间。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | Constant符号边界与资源坐标/权限不能等同，ASan并不保证这类地址空间。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


**当前实验子范围限制：** 当前可编译文件只检验constant符号边界子场景，尚未覆盖texture/surface的全部寻址模式与权限。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.23](../../docs/llvm-gpu-sanitizer-research.md#case-s23)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.23 S23：只读对象与 image／surface——检查权限、坐标及寻址模式

**场景与例子。** 对应 M1/M2/M3/A1。两个子例分别针对只读权限和资源坐标；资源均已创建且仍存活，排除无效 handle 的干扰。

~~~cpp
// 子例 A：模块登记 coef 为设备只读对象。
addr = address_of_module_constant("coef");
store_u32(addr, 7);  // 错误：示意 ISA 写入只读对象，不是合法的 Host 初始化

// 子例 B：64×32、每像素4 B的可写 surface，边界模式为 TRAP。
surface_store_u32(surface, x_byte=64*4, y=0, value=7, mode=TRAP);
// 错误：最后一个有效像素的 x_byte 为 63*4；这里已超出行宽。
~~~

**哪里不对、应检测什么。** A 的地址可能已映射且在 allocation 内，但写权限不允许。B 不能只检查底层 pitch allocation：行尾 padding 即使在物理分配内，也未必是合法像素。检查应理解维度、格式、坐标单位、mip/layer 和边界模式。PTX 对 surface 的边界处理提供明确模式；目标 ISA 的合法 clamp/zero 等行为必须按其语义处理，不能把所有超出原始坐标范围的操作都报为非法访存。[N09]

**实现方案与组件。** A 按资源描述符和权限在模型入口检查。Compiler＋UMD＋device runtime 可提供资源类插桩：加载时登记只读符号，创建资源时登记格式、维度与权限，在 texture/surface intrinsic 前检查参数。C/E 若覆盖资源 opcode，可查询同一描述符；只实现线性地址 shadow 的 B 不能自动获得这项能力。D 可以为显式资源 view 保留逻辑边界；F 的权限或坐标 fault 可作为硬件信号，但报告工具仍需关联资源 ID 和源码。

**修复、合法反例与预期报告。** A 应写可写对象或使用允许的 Host 更新接口；B 改为合法像素坐标。另设采用规定 clamp 模式的越界采样作为合法反例：它可以返回边界像素，不应报告通用 OOB；如果应用要求更严格的逻辑坐标，需作为独立约束声明。预期报告分别为 `write-to-read-only` 与 `surface-coordinate-out-of-range`，包含资源格式、坐标、单位和模式。资源 opcode 不受支持时必须显示覆盖缺口。

## 实体GPU生产组件职责

**实体GPU设备Checker或已有硬件机制必须实际产生检测与故障报告。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成可部署GPU ISA设备检查器或实体GPU硬件检查单元生效；真机记录对应错误 |
| KMD（生产） | 映射权限与资源授权；padding和合法坐标需上层/ISA信息；原有接口足够时零改动 |

## 验收需要留下什么证据？


**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
