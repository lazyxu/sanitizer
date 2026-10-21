# S11：重复GPU Free

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S11 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S11 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `api` | 重复释放属于GPU Runtime/API检查；不是设备指令越界。 |
| AMD HIP | AMD GPU AddressSanitizer | `api` | 重复释放属于GPU Runtime/API检查；不是设备指令越界。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.11](../../docs/llvm-gpu-sanitizer-research.md#case-s11)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.11 S11：同一分配实例被释放两次

**前提与错误代码。** 分配 64 B，保存两个别名；第一次 free 成功且已经退休。到第二次调用前不允许新的分配或 GPUVA 复用，排除 S09 的时间身份歧义：

~~~cpp
uint8_t *p = test_alloc(64);
uint8_t *alias = p;
test_free_and_wait(p);       // 第一次合法释放，状态变为 dead
test_free_and_wait(alias);   // 错误：再次释放同一 allocation instance
~~~

**哪里不对、怎样检。** 第二次调用试图再次终止已经结束的同一对象生存期。A 的 allocation 事件账本可发现 `dead -> free` 非法转移；B／E 的 allocator 拦截层可保留释放记录或 tombstone，在调用底层释放前检查。两条路线都依赖成功／失败结果与实例身份；只记录“调用过 free”会把失败的首次请求也错误计入，只有 shadow poison 而未观察第二次 API 则不会产生必要诊断。[L01][N02]

**组件观测。** compiler／deviceRT 为设备侧释放保留调用点并可靠发布状态，Host 释放则由 runtime 拦截；UMD 区分请求 ID、成功提交及退休完成，对嵌套回调去重；cmodel 独立检查状态序列。并发 free 还需在对象状态上执行一致的仲裁，不能让两个请求都基于旧 live 状态通过后才写 dead。

**合法反例、修复及报告。** 使用单一明确拥有者，清除所有需要失效的别名；只把 `p` 赋空并不能修复仍存活的 `alias`。第一次释放请求失败且对象仍 live 时，之后一次合法成功释放不应被报 double free。预期报告对象 ID、64 B 大小、allocation site、首次成功释放与第二次调用位置。若同 GPUVA 已创建新对象，应允许新指针正常释放；要阻止旧指针误释放新对象则需要 S09 的 D2 身份，不能仅凭地址历史断言 double free。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 同一生命周期被重复释放 |
| QEMU 已有 GPU ISA 执行模型 | M6 |
| KMD | 对象状态和释放记录 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S11.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
