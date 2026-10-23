# S26：未插桩设备函数

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S26 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S26 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | HIP可对特定函数禁用AddressSanitizer，CUDA同TU不能保证混合CU覆盖实验。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | HIP可对特定函数禁用AddressSanitizer，CUDA同TU不能保证混合CU覆盖实验。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


**当前实验子范围限制：** 当前文件的函数级插桩排除示意不等于跨编译单元/JIT二进制patch完全验收；需额外构建夹具。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

**本 README 是 S26 的唯一详细技术与验收规范。** [主研究 §18.26](../../docs/llvm-gpu-sanitizer-research.md#case-s26) 仅保留场景摘要与稳定入口。本页伪代码与可编译 CUDA/HIP 程序按**相同违规语义**对齐，不保证逐行一致；真机验收必须确认目标访存确实执行，不能把被优化消除的操作当成已检查。

### 18.26 S26：未插桩库与 JIT——没有观测到访问不能当作检查通过

**场景与例子。** 对应 M1/M3 的覆盖边界和模块契约。应用能够重编译，第三方 kernel 暂时只有预编译代码；`p` 分配 64 个 `int32_t`，末尾有已登记的 poison 检查区域。

~~~cpp
// 应用模块启用编译插桩。
p = alloc(64 * sizeof(int32_t));
launch(prebuilt_library_kernel, p, 64);

// 库内真实执行的逻辑；该模块没有编译期插桩。
kernel void prebuilt_library_kernel(int32_t* p, int n) {
    if (thread_id() == 0) p[n] = 1;  // 错误：p[64]越界
}
~~~

**哪里不对、应检测什么。** 应用确实有一次非法访问，但只在调用方插桩，无法观察库内每次访存。没有报告只能说明检查器没有看到错误，不能说明库安全。模块 manifest 至少应声明检查器、地址空间、指令覆盖及 ABI 版本；JIT 产物同样必须登记。NVIDIA 的 hybrid 也有编译单元／module 内函数粒度限制，不能据其名字假定任意未插桩函数都会自动补齐。[N05]

**实现方案与组件。** 首选重编译库并使用兼容B/D设备插桩；无法重编译时C/E在模块加载/JIT后重写受支持的目标GPU opcode并共享UMD对象表。**库模块必须在实体GPU上实际插桩并核对最终ISA，未检查的访问必须报告覆盖缺口。** UMD必须记录实际模块覆盖，区分成功加载与检查生效；工具显示未覆盖模块，Binary Fallback不能恢复原对象/时间身份时必须降级。

**修复、合法反例与预期报告。** 库中改用 `p[n-1]` 是合法反例。对可插桩的错误版，应报告库内 PC、模块名和 4 B 越界；对暂不支持的二进制，应明确 `unsupported-module` 或拒绝该检查模式，不能输出无条件 clean。验收同时运行“已插桩→未插桩→已插桩”的指针返回链，确认 unknown 身份不会被包装成强保证。

## 实体GPU生产组件职责

**实体GPU设备Checker或已有硬件机制必须实际产生检测与故障报告。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成可部署GPU ISA设备检查器或实体GPU硬件检查单元生效；真机记录对应错误 |
| KMD（生产） | 正规代码加载、可执行映射与资源校验；不默认允许任意patch；原有接口足够时零改动 |

## 验收需要留下什么证据？

在目标实体GPU运行错误与合法对照，保存硬件/固件/驱动/Compiler版本、实际ISA、模块build ID、device PC、lane/范围/对象/queue及原始日志。GPU内部真实检查成功记`GPU_DETECTED`；仅UMD/Host API识别的资源错误记`HOST_API_DETECTED`。未运行记`GPU_NOT_RUN`，不支持记`UNSUPPORTED`；未覆盖指令/模块、buffer丢失、tool OOM等记`PARTIAL_COVERAGE`或`TOOL_FAILURE`，不能算clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
