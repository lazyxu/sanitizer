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

以下章节直接对应[主调研文档 §18.2](../../docs/llvm-gpu-sanitizer-research.md#case-s02)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

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

**组件观测。** compiler 记录向量访存及后端拆分情况；deviceRT 的长度检查覆盖跨 granule 尾部；UMD 登记 30 B 逻辑长度，不能擅自扩大到 32 B；cmodel 观察指令的有效访问字节，不把缓存行预取或合并事务覆盖的所有字节都当作程序读取。若最终被拆成多个标量指令，验收按实际指令集合核对，不要求日志仍声称执行了一条 16 B 指令。

**合法反例、修复及报告。** `load16_req_align16(src)` 合法；尾部可用明确只读取 14 B 的操作，或在程序契约允许时将对象实际分配并初始化到 32 B。预期报告 global 越界读、原操作宽度 16、对象长度 30、首次非法偏移 30，以及执行线程。未覆盖的宽 opcode 必须标记部分覆盖，不能降成“未发现错误”。

## 生产方案与两类模型辅助验证职责

**下表只定义如何验证已能在GPU执行的B/C/D/E设备检查器或已存在硬件的F；绝不要求、也不认可将模型侧自定义访存检查作为生产Sanitizer。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成设备检查指令，或实际GPU硬件MMU/Tag/Bounds生效；GPU上的报告才是产品验收 |
| 独立功能级cmodel（验证） | 全部有效字节和跨granule判断；仅用于运行真实checker产物/参考核对 |
| **现有QEMU GPU ISA解释器（验证）** | DMA／地址翻译不可截短目标访问，保留GPU原始宽度；不得仅靠模拟器独有hook宣布真机检测完成 |
| KMD（生产） | 映射与fault访问范围；不只依赖一次页命中；接口充分时新增代码可为0 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S02.*.log`日志及`summary.json`。CPU ASan、Host API错误和模型独有hook不能冒充真实GPU检查器；GPU MMU fault仅证明符合其硬件页级规则，不等于对象级越界检测。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
