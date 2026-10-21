# S27：Graph Replay悬垂引用

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S27 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S27 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | 捕获的graph引用GPU对象已释放，实际运行可能API拒绝或硬件fault。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | 捕获的graph引用GPU对象已释放，实际运行可能API拒绝或硬件fault。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。
**危险提示：** 该样例的错误分支可能造成GPU hang、context fault或reset；默认不自动执行bad，只有隔离硬件并明确传递`--allow-dangerous`才运行。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.27](../../docs/llvm-gpu-sanitizer-research.md#case-s27)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.27 S27：graph replay捕获已释放对象与执行ID／allocation epoch

**前提与错误。** `write_x`只写`p[0]`，避免引入初始化问题；p是graph外分配，capture只保存访问它的kernel节点，不含allocation节点。第一次replay完成后释放p，再重放原graph：节点仍持有旧指针。地址复用变体由测试allocator确保q取得同VA；普通malloc恰巧复用不能作为确定触发前提。

~~~c
cudaMalloc(&p, N);
cudaStreamBeginCapture(s, cudaStreamCaptureModeGlobal);
write_x<<<1, 1, 0, s>>>(p, 1);            // capture保存p的参数与原身份
cudaStreamEndCapture(s, &graph);
cudaGraphInstantiate(&exec, graph, 0);
cudaGraphLaunch(exec, s);                 // replay 1：p仍live，OK
cudaStreamSynchronize(s);
cudaFree(p);
cudaMalloc(&q, N);                       // 可选变体：专用夹具确保q与旧p同VA
cudaGraphLaunch(exec, s);                 // BAD：replay 2仍访问旧p
cudaStreamSynchronize(s);
~~~

**实现路线与观测点。** A记录capture节点、每次执行实例、对象创建/释放和节点实际访存；模型若只有VA表也会漏同VA复用，因此必须保留被捕获参数的原身份。E＋D2由runtime为节点参数建立identity sidecar，replay生成新的execution/launch ID，Compiler传播对象ID/epoch至使用点；不能在每次launch按当前VA表重新“认领”新对象。C可补充设备访问，但裸地址补丁无法自行恢复丢失来源。[N03]

**修复、报告与边界。** 让p存活至所有replay完成，或在受支持更新入口同时替换节点指针和身份。连续合法replay应具有不同执行ID、相同仍live对象epoch；graph内alloc/free节点每次建立新的分配生存期时，才生成相应新allocation epoch，不能机械地按replay全量换epoch。[N08] 报告graph/exec/replay/node ID、旧对象与free位置、若复用则给当前对象身份。B在地址仍poison时可报UAF，复用unpoison后可能漏；未跟踪节点参数更新、间接指针或device-side graph launch时须降级，不能假定每个设备发起replay都有Host回调。[N03]

## 真实GPU生产职责

| 角色 | 正确职责 |
|---|---|
| **真实GPU检查** | 只有实际GPU设备指令或芯片硬件检查产生产品检出 |
| **Compiler／Device Runtime／UMD** | 生成检查代码、维护状态和设备报告 |
| **KMD（生产）** | 提交和资源生存期；每轮launch不自动产生新allocation身份；原有接口足够时零改动 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S27.*.log`日志及`summary.json`。GPU硬件fault不代表具备完整逻辑对象边界信息。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
