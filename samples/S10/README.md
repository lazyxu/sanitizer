# S10：非法gpuFree内部地址

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S10 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S10 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `api` | GPU Runtime/Driver API错误；不是GPU kernel内部Sanitizer命中。 |
| AMD HIP | AMD GPU AddressSanitizer | `api` | GPU Runtime/Driver API错误；不是GPU kernel内部Sanitizer命中。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.10](../../docs/llvm-gpu-sanitizer-research.md#case-s10)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.10 S10：Invalid free——内部指针与错误所有权

**前提与错误代码。** allocator 契约要求使用拥有者和原始基址释放。A 上下文拥有一个 64 B 对象；B 只导入其非拥有映射，导入句柄必须通过 `close_import` 关闭。以下两种误用分别在独立夹具中运行：

~~~cpp
uint8_t *p = owner_alloc(context_A, 64);
owner_free(context_A, p + 4); // 错误一：内部指针，不是 allocation base

// 独立运行第二例：重新创建 p，并建立非拥有导入映射 q。
uint8_t *q = import_nonowning(context_B, export_handle(p));
owner_free(context_B, q);     // 错误二：B 无权调用拥有者释放接口
~~~

**哪里不对、怎样检。** M6 需要验证原始基址、规范对象、allocator 类型、所有权和当前状态；“地址属于某个 live 对象”远远不够。A 可对 allocator／import 事件建立独立状态机，在模型重放中拒绝非法 free；B 或 E 的 runtime 拦截层可用同一类登记信息，在真正释放前报告并拒绝操作。后一能力来自 allocator 协议，单独给 load／store 加 shadow 并不能发现没有后续访存的错误。[L01][N02]

**组件观测。** compiler／loader 保留调用点；deviceRT 若支持设备堆，须把设备侧 free 接入相同事件；UMD 将导入别名归一到同一对象，但保留映射拥有权与资源拥有权的差别；cmodel 核对请求的 allocator、context、base 与合法关闭动作。API 和底层 driver 回调必须去重，不能把一次拒绝误记成一次成功释放。

**合法反例、修复及报告。** 内部指针可用于范围内访问，但释放时应传回 `p`；B 应 `close_import(q)`，最后由 A 在使用完成后释放 `p`。预期分别报告内部偏移 `+4` 和所有权／释放接口不匹配，附原 allocation site 与错误 API site。若 API 自身返回错误也应记录误用，不能要求一定发生设备 fault。未登记的自定义 allocator 或导入句柄只能标记覆盖缺口。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 传给释放接口的身份／所有权合法 |
| QEMU 已有 GPU ISA 执行模型 | M6/A1 |
| KMD | allocator、原始基址、owner |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S10.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
