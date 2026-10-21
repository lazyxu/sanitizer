# S25：Atomic Pointer/身份不一致

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S25 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S25 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `unsupported` | 两个独立原子槽不能形成整体value+identity事务，厂商ASan无指针身份协议。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | 两个独立原子槽不能形成整体value+identity事务，厂商ASan无指针身份协议。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.25](../../docs/llvm-gpu-sanitizer-research.md#case-s25)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.25 S25：atomic pointer 的值与身份不一致——合法程序也可能被错误插桩误报

**场景与例子。** 这是 D2 的正确性验收场景，关联 M5。A、B 的初始化在并发执行前对双方可见，此后对象持续存活且数据不再修改；slot 的原子作用域覆盖发布者和读取者。发布者只更换一个原子指针，读取者读取被选对象的不可变数据。

~~~cpp
atomic_pointer slot = &A;
// 发布者：
slot.store(&B, release);
// 读取者：
p = slot.load(acquire);
consume(p->immutable_value);  // A、B都live，读到任一合法值都不应被误报

// 错误插桩示意：两次独立取值可能跨过一次发布。
v = atomic_load(slot);       // 取到旧值 &A
id = atomic_load_pointer_shadow(slot); // 却取到新身份 B；writer也独立原子写此字段
check_temporal(v, id);       // 工具错误：把不属于同一次发布的二者组合
~~~

**哪里不对、应检测什么。** 上半段不是应用缺陷；下半段是检查器的元数据一致性缺陷。同类不一致在地址复用场景中，还可能把旧裸指针附上新 allocation 身份，从而漏掉 UAF。分别原子读取 value 和 metadata 不保证它们来自同一次写入，单纯“再读一次指针值”也不能排除 ABA。这里要求的是检查器维护发布身份的一致协议，不能靠一次地址表查找重建历史。[L05] 该结论由原子接口语义与身份模型推导，属于工程设计要求。

**实现方案与组件。** 可选一：在目标支持合适宽度且 ABI 受控时原子发布 `{value, identity}`。可选二：所有 writer/reader 都遵守经过证明的版本化 side metadata 协议，携带发布序号、正确的内存顺序和失败重试策略。Compiler 负责完整识别 store/exchange/CAS 与跨函数传播，device runtime 实现协议；UMD 只提供 allocation epoch，不能代替指针发布身份。使用真实GPU并发压力测试及值-身份发布一致性夹具核对协议；单纯地址有效性不具备完整D2身份保证。C/E 只有在全部相关 writer 可纳入协议时才能给同等保证；SIMT 内自旋锁和无界重试还需要单独证明前进性。

**修复、合法反例与预期报告。** 修复检查器，原合法程序应零误报。另设“保存旧指针→释放A→同地址新对象”的受控变体：旧身份应报 M5，新发布身份应通过。未知 writer 或无法证明成对一致时应报告 `provenance-unavailable`／覆盖不足，不能伪造当前 epoch 后宣布时间安全。

## 实体GPU生产组件职责

**实体GPU设备Checker或已有硬件机制必须实际产生检测与故障报告。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成可部署GPU ISA设备检查器或实体GPU硬件检查单元生效；真机记录对应错误 |
| KMD（生产） | 只提供分配生命周期和映射，不能替代原子发布身份；原有接口足够时零改动 |

## 验收需要留下什么证据？

在目标实体GPU运行错误与合法对照，保存硬件/固件/驱动/Compiler版本、实际ISA、模块build ID、device PC、lane/范围/对象/queue及原始日志。GPU内部真实检查成功记`GPU_DETECTED`；仅UMD/Host API识别的资源错误记`HOST_API_DETECTED`。未运行记`GPU_NOT_RUN`，不支持记`UNSUPPORTED`；未覆盖指令/模块、buffer丢失、tool OOM等记`PARTIAL_COVERAGE`或`TOOL_FAILURE`，不能算clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
