# S24：CPU/GPU共享映射

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S24 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S24 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `unsupported` | 共享host-mapped显存中的CPU-GPU冲突，不能交给CPU ASan冒充GPU竞态。 |
| AMD HIP | AMD GPU AddressSanitizer | `unsupported` | 共享host-mapped显存中的CPU-GPU冲突，不能交给CPU ASan冒充GPU竞态。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.24](../../docs/llvm-gpu-sanitizer-research.md#case-s24)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.24 S24：CPU/GPU共享、peer／IPC别名的身份与同步

**前提与错误。** CPU/GPU分支只在已确认允许该内存类型被两端并发访问、且目标定义相应一致性的系统运行；不能因调用`cudaMallocManaged`就默认满足。Host-mapped内存须另核平台规则。peer/IPC分支使用另一块已合法导入、授予读写权限的device分配，不能把managed分配直接当作传统CUDA IPC可导出对象。[N08]

~~~c
cudaMallocManaged(&p, sizeof(int));
*p = 0;                                 // launch前真实初始化
write_x<<<1, 1, 0, s>>>(p, 1);
*p = 2;                                 // BAD①：CPU写与GPU写无HB
cudaStreamSynchronize(s);                // 将此等待移到CPU写之前可修复
// 独立用例②：a为可导出的device对象；导入/映射/授权过程均成功
handle = export_device_allocation(a);    // 伪API：导出共享对象
b = import_and_map_rw(handle, peer);     // a、b可有不同VA，规范对象相同
write_on_device(owner, a, q0, 3);
write_on_device(peer,  b, q1, 4);         // BAD②：同对象写写，无跨device依赖
~~~

**实现路线与观测点。** A须连同Host访问、peer设备、映射别名及命令同步建模；只模拟GPU访存不能检出CPU对端。E将CPU checker的访问/HB事件、GPU Compiler/C访存事件与UMD的map/import/alias/权限事件归并，以规范对象ID＋偏移匹配；导入产生映射身份，不凭空创建独立所有权。D2能保存身份但仍须另加HB引擎。[N03]

**修复、报告与边界。** CPU写前等待GPU完成；peer/IPC使用目标支持的跨设备／进程事件或完成协议，并遵守别名一致性规则。system-scope release/acquire只在该内存、地址宽度、对齐及互连支持相互原子性时成为合法反例，device-scope不能同步CPU；驻留、prefetch或映射成功都不建立HB。[N08] 报告CPU/两GPU端PC、device/context、两VA到同对象的关系及缺失同步。若peer未授权，应报权限/不可访问错误，不能以此代称数据race。不可见CPU访问、IPC另一进程、别名漏登记或概率观测窗口未命中均限制覆盖。

## 生产方案与两类模型辅助验证职责

**只有可在真实GPU执行的设备检查/实体硬件检查才能作为本场景Sanitizer；cmodel及QEMU仅验证。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成可部署GPU ISA设备检查器或实体GPU硬件检查单元生效；真机记录对应错误 |
| 独立功能级cmodel（验证） | 规范对象、CPU事件和系统同步模型；模型提供测试证据，不提供新增硬件检测能力 |
| **现有QEMU GPU ISA解释器（验证）** | 关联Guest CPU、GPU与外部映射；KVM下另有CPU观测方案；不把模型自有检查hook计入产品 |
| KMD（生产） | 共享BO、导入映射、权限及外部同步；不能单凭同址推断所有权；原有接口足够时零改动 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S24.*.log`日志及`summary.json`。CPU ASan、Host API错误或QEMU模型独有检测都不能冒充真实GPU检查器；GPU硬件fault不代表具备完整逻辑对象边界信息。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
