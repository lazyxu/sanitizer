# S20：Async Free跨Queue缺依赖

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S20 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S20 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | CUDA stream-ordered free生命周期竞态依赖执行顺序，不能承诺必报。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | CUDA stream-ordered free生命周期竞态依赖执行顺序，不能承诺必报。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。
**危险提示：** 该样例的错误分支可能造成GPU hang、context fault或reset；默认不自动执行bad，只有隔离硬件并明确传递`--allow-dangerous`才运行。

## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.20](../../docs/llvm-gpu-sanitizer-research.md#case-s20)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.20 S20：async free跨queue缺依赖与释放生效边界

**前提与错误。** 两个独立queue；`read_only`只读取有效、已初始化的分配，从而隔离生命周期竞争。allocation发布已通过`ready`正确排序，唯一缺失的是q1最后一次使用到q0释放的依赖。`fixed`是Host端测试选择，两种版本分开运行。

~~~c
cudaMallocAsync(&p, N, q0);
cudaMemsetAsync(p, 0, N, q0);
cudaEventRecord(ready, q0);
cudaStreamWaitEvent(q1, ready, 0);        // allocation和初始化均已发布
read_only<<<1, 32, 0, q1>>>(p, out);
if (fixed) {
    cudaEventRecord(done, q1);
    cudaStreamWaitEvent(q0, done, 0);     // 修复：最后使用先于free
}
cudaFreeAsync(p, q0);                    // BAD：fixed=false时无使用->free依赖
cudaDeviceSynchronize();
~~~

**实现路线与观测点。** A同时记录对象epoch、命令DAG、free请求、生效与完成；访问和free无序时报生命周期竞争，free生效后执行的访问报UAF。E由runtime/UMD将free命令及依赖下发，B按设备顺序poison；D2再传播旧指针身份，以覆盖随后同VA复用。free提交不能立即全局poison，同queue排在free之前的合法访问应放行。[N02][N03]

**修复、报告与边界。** `fixed=true`建立合法链；同queue中“使用→free”也是合法反例。CUDA规定free操作开始后不能继续使用；`retire complete`是确认退休、回收描述符及后续复用的证据，不应把非法访问边界拖到完成，更不能用Host API返回代替设备时刻。[N08] 报告对象/epoch、最后使用queue与launch、free提交点及缺失依赖；有完成证据时补报实际退休节点。仅监控API调用顺序会误报或漏报；B遇同VA新分配重新unpoison可能漏旧指针，D2仍需完整身份传播；隐藏free、外部queue或不可见依赖必须输出unknown。

内存池若插入真实设备依赖，须纳入命令图；工具为诊断插入的同步不得伪造应用原本有序。

## 实体GPU生产组件职责

**产品检测必须由实体GPU执行对应设备Checker或真实硬件机制，正式验收需要实卡设备报告。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | 编译/运行时GPU检查ISA、GPU设备运行库、或受支持硬件MMU/Tag/Bounds；必须真机执行及报告 |
| KMD（生产） | 提交完成、回收与错误证据；不擅自将free提交等同于全部访问失效；原接口满足契约时不新增代码 |

## 验收需要留下什么证据？


**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
