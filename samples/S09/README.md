# S09：复用GPUVA旧指针

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S09 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S09 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `unsupported` | 普通分配器不保证重用相同GPUVA；需要确定性的epoch身份检测。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | 普通分配器不保证重用相同GPUVA；需要确定性的epoch身份检测。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

**本 README 是 S09 的唯一详细技术与验收规范。** [主研究 §18.9](../../docs/llvm-gpu-sanitizer-research.md#case-s09) 仅保留场景摘要与稳定入口。本页伪代码与可编译 CUDA/HIP 程序按**相同违规语义**对齐，不保证逐行一致；真机验收必须确认目标访存确实执行，不能把被优化消除的操作当成已检查。

### 18.9 S09：同一 GPUVA 被新对象复用后的旧指针

**前提与错误代码。** `slot` 和 `out` 预先分配且不参与复用。先把 A 的指针存入设备 pointer slot，再退休 A，由受控 allocator 尝试让 B 复用同一个 64 B 槽位；必须核对实际 GPUVA 相等和 A dead／B live 两个生命周期事件。未观察到复用应标记夹具条件未满足，不得计为通过：

~~~cpp
__global__ void save_ptr(uint32_t **slot, uint32_t *p) {
    if (threadIdx.x == 0) *slot = p; // D2 还须保存 A 的旁路时间身份
}
__global__ void use_saved(uint32_t **slot, uint32_t *out) {
    if (threadIdx.x == 0) out[0] = (*slot)[0]; // 错误：解引用旧 A 指针
}
uint32_t *a = test_alloc(64);
GPUVA old_va = gpuva(a);
AllocationID old_id = test_identity_at_allocation(a); // 分配时保存，不能事后反查
save_ptr<<<1,32>>>(slot, a);
wait_all();
test_free_and_wait(a);
uint32_t *b = test_alloc_reusing(old_va, 64);
AllocationID new_id = test_identity_at_allocation(b);
require(gpuva(b) == old_va && new_id != old_id);
require(test_is_dead(old_id) && test_is_live(new_id));
fill_and_wait(b, 64);  // B 全部已初始化；仍保留 slot 中的旧 A 指针
use_saved<<<1,32>>>(slot, out);
~~~

**哪里不对、怎样检。** 访问范围仍是新 B 的合法前 4 B，但指针预期 A 的生存期已经结束。D2 要让 pointer store／reload 传递 `(A_ID,A_epoch)`，与当前对象身份比较；**D2的真实GPU检查指令与metadata传播**必须覆盖寄存器、内存槽及allocator事件的预期身份。普通分配表查询只得到 B 当前 generation，缺少指针预期值；B的Unpoison、D1的同尺寸bounds及仅依赖当前地址有效性的C因而不能保证识别旧指针。[P01][L02]

**组件观测。** compiler 保留 slot 的 pointer metadata 配对；deviceRT 避免载入时把旧指针重新标记为 B；UMD 发布新身份，并保持检查所需的旧描述符。普通 `malloc` 是否复用不可假定；仅凭当前有效Allocation记录无法恢复旧指针身份。

**合法反例、修复及报告。** 独立运行通过正确携带 B 身份的 `b[0]` 应通过；修复是清除旧引用并分发新指针及其身份。报告应同时给出相同 GPUVA、预期 A 和当前 B 的 ID／epoch、两次分配与 A 的释放位置。未知 writer、身份丢失、tag 碰撞或 epoch 回绕必须作为保证边界，不能静默退回地址查询后仍声称完整 M5。

## 实体GPU生产组件职责

**只能由实体GPU实际执行设备检查器或已有硬件机制提供产品检测，实卡证据是验收依据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成并使GPU执行设备检查ISA；或实体GPU硬件检查单元/MMU触发，附真实设备报告 |
| KMD（生产） | 当前BO句柄或GPUVA不能重建旧指针身份；已充分支持的接口不需改源码 |

## 验收需要留下什么证据？

在实体GPU运行`--good`/`--bad`并保存硬件/驱动/编译器/模块build ID、真实设备ISA、PC/lane/宽度/对象/queue及原始报告。Host API资源/释放错误只能标记`HOST_API_DETECTED`，不能冒充设备内`GPU_DETECTED`。未运行记`GPU_NOT_RUN`，硬件能力不支持记`UNSUPPORTED`；部分覆盖、日志丢失或工具OOM必须标为`PARTIAL_COVERAGE`/`TOOL_FAILURE`而不是clean。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
