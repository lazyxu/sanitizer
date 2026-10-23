# S02：宽访存跨越尾界

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S02 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S02 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `detect` | 16字节设备宽load跨逻辑堆对象末端。 |
| AMD HIP | AMD GPU AddressSanitizer | `detect` | 16字节设备宽load跨逻辑堆对象末端。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

**本 README 是 S02 的唯一详细技术与验收规范。** [主研究 §18.2](../../docs/llvm-gpu-sanitizer-research.md#case-s02) 仅保留场景摘要与稳定入口。本页伪代码与可编译 CUDA/HIP 程序按**相同违规语义**对齐，不保证逐行一致；真机验收必须确认目标访存确实执行，不能把被优化消除的操作当成已检查。

### 18.2 S02：访问首地址合法，宽访问的末端越界

**前提与错误代码。** `src` 是首地址按 16 B 对齐、逻辑长度恰为 30 B 的 global 对象，尾部已 poison；`dst` 是独立的 16 B 对齐、16 B 有效对象。下面的示意 intrinsic 明确对应一个要求 16 B 对齐、实际读取 16 B 的目标操作：

~~~cpp
__global__ void s02(const uint8_t *src, uint8_t *dst) {
    if (threadIdx.x == 0) {
        u8x16 v = load16_req_align16(src + 16); // 错误：读偏移 [16,32)
        store16_req_align16(dst, v);
    }
}
// s02<<<1,32>>>(src, dst);  // src 的偏移 30、31 两字节无效
~~~

**哪里不对、怎样检。** 起点 `src+16` 合法且对齐正确，但 16 B 读取中只有前 14 B 属于对象。应检查完整访问范围，不能用“起始地址在对象内”代替范围条件。B设备检查器必须检查访问涉及的全部shadow granule并识别有效前缀；C的**真实GPU二进制补丁**也必须在设备上按最终ISA宽度逐有效字节检查。若只有 4 B 标量模板、只查首字节或错误忽略尾部，均会漏检。[L02][L05]

**组件观测。** compiler 记录向量访存及后端拆分情况；deviceRT 的长度检查覆盖跨 granule 尾部；UMD 登记 30 B 逻辑长度，不能擅自扩大到 32 B。若最终被拆成多个标量指令，验收按实际指令集合核对，不要求日志仍声称执行了一条 16 B 指令。

**合法反例、修复及报告。** `load16_req_align16(src)` 合法；尾部可用明确只读取 14 B 的操作，或在程序契约允许时将对象实际分配并初始化到 32 B。预期报告 global 越界读、原操作宽度 16、对象长度 30、首次非法偏移 30，以及执行线程。未覆盖的宽 opcode 必须标记部分覆盖，不能降成“未发现错误”。

## 实体GPU生产组件职责

**下表只描述GPU设备Checker、UMD及KMD真实生产职责；不能把软件静态检查代替设备端错误证据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成设备检查指令，或实际GPU硬件MMU/Tag/Bounds生效；GPU上的报告才是产品验收 |
| KMD（生产） | 映射与fault访问范围；不只依赖一次页命中；接口充分时新增代码可为0 |

## 验收需要留下什么证据？

在对应实体GPU运行`--good`和`--bad`，保留设备型号/驱动/工具链版本、目标ISA中真正执行的检查点、模块build ID、PC、线程/lane、访问范围、对象/queue、设备报告与原始日志。纯Host资源/API错误只记录`HOST_API_DETECTED`，不能冒充`GPU_DETECTED`；GPU内部错误只有设备实际执行并提供诊断才能记`GPU_DETECTED`。未运行必须记`GPU_NOT_RUN`，无硬件/功能前提记`UNSUPPORTED`；日志丢失、OOM、未覆盖模块/指令必须报告失败或部分覆盖，不能计作clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
