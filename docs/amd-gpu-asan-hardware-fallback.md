# AMD GPU AddressSanitizer 机制复刻：逐组件修改、硬件依赖与无硬件特性替代路径

**版本：2026-10-21 · 目标：在自研实体GPU上实现真实可执行的Address Sanitizer；不是移植仅限于某一厂商的GPU配置，也不是已有产线实现。**

本指南区分两件事：(1) AMD ROCm 7.2.4 当前官方 GPU ASan 的已支持环境；(2) 自研LLVM GPU后端可以采取的等效软件检查架构。两者不能互相推导硬件支持。产品检查的执行点始终是真实GPU上的load/store/atomic之前的设备指令或真实MMU硬件，绝非仅有Host API拦截。

## 1. AMD当前实现的可核实事实

以 https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html 为版本固定依据（不同版本不能合并声称同一支持矩阵）：

- AMD通过LLVM ASan插桩和compiler-rt Host/设备报告、带Redzone的allocator及GPU可读Shadow配合完成访存检查。GPU Load/Store/Atomic之前查询Shadow；经典1:8编码有尾部前缀表达边界，约12.5% Shadow字节额外空间；Redzone、设备库、映射和错误日志还增加额外开销。
- 官方GPU ASan要求适配的xnack+设备目标；带xnack-的示例会警告GPU端插桩被忽略，应用可以继续运行但没有设备检查能力。HSA_XNACK=1只是启用真正具备该能力的设备配置，不会给不支持的GPU制造XNACK。Linux HMM、受支持GPU和ASan版HIP/HSA运行库也是该官方运行路径条件。
- 使用带插桩的HIP/HSA组件，核验运行期依赖例如libclang_rt.asan-x86_64.so以及/asan/libamdhip64.so，不能只看编译成功。官方GPU错误报告带 on amdgpu device、GPU PC、workgroup/lane和访问大小；普通CPU ASan或GPU API错误不属于设备访存检出。
- 目前文档列出private/stack访问未插桩的缺口；HIP Shared/LDS可以建模到Global内存中、并可能以Global类报告，因此不是完整精度的原生LDS对象诊断。宽访问、子对象、来源边界、redzone以外的跳越仍要按实际检查算法判断。
- 上游LLVM AddressSanitizer.cpp对AMDGPU有地址空间及generic指针动态分类路径；不能把通用CPU Pass启用视为自研Target所有global/LDS/private/texture/async opcode都已覆盖。
- AMD的性能、报告和兼容性结论仅代表官方已测试目标及版本；自研GPU不沿用这些数值作为实测结果。

参考：ROCm GPU ASan 7.2.4（上方URL）；https://llvm.org/doxygen/AddressSanitizer_8cpp_source.html ；https://rocm.docs.amd.com/projects/HIP/en/latest/how-to/hip_runtime_api/memory_management/virtual_memory.html 。

## 2. 硬件依赖总表：缺失后的真实GPU替代方案

| 条件 | AMD官方GPU ASan | 自研GPU必需吗 | 缺失时可部署的实现 | 代价和不能补回的保证 |
|---|---|---|---|---|
| XNACK / retryable page fault | 文档要求受支持的xnack+目标和运行配置 | **不是Shadow算法本身必需** | GPU本地或设备可见显式Shadow，预分配或逐分配显式映射，dispatch前全部驻留 | 不做按需换页；不能直接运行官方ROCm xnack+构建或声称AMD工具通过 |
| Linux HMM / 共享虚拟内存 | 官方环境需HMM及兼容内核 | **不是自研离散GPU的普遍必需项** | Host维护对象账本，拷贝GPU Shadow/descriptor快照，按queue fence生效；设备端直接读固定GPUVA | 失去透明统一内存；须实现上传/下载与同步 |
| GPU能Load有效元数据 | GPU Device Runtime必需的基本能力 | **是** | 用global/constant/设备本地内存中映射的表查询，或可信硬件metadata通路 | 若GPU完全不能从合法设备内存读到元数据，软件Shadow无法部署；UNSUPPORTED |
| 完整GPUVA预留 / Sparse VMM | 有助于大地址空间Shadow布局 | 不是必需 | 按allocation建立Shadow段、Radix/区间descriptor表、两级索引 | 查询比平坦Shadow慢；未知/失配必须拒绝或PARTIAL |
| 可保护的GPU页与MMU故障 | F0 Guard Page依赖实际PTE权限 | 不是B/D软件检查必需 | 在设备ISA内做逻辑对象Bounds或Shadow字节检查 | 不能声称有硬件Guard Page、不能阻断未插桩代码的越页访问 |
| GPU硬件Tag比较指令 | 普通AMD Shadow方案不以专用Tag为前提 | 不是B/D必需 | D1传原对象base/limit；D2显式(pointer identity,epoch)及元数据查询 | RAW指针未知来源只能降级；不能宣称有硬件Tag隔离 |
| GPU硬件Bounds单元 | 普通AMD Shadow不要求 | 不是B/D必需 | 每条实际访存前插入设备Bounds/Shadow helper | 额外指令/寄存器/带宽，不能保证未插桩代码安全 |
| Device atomics和内存发布语义 | report ring/并发Shadow元数据需要同步原语 | 至少需要**一种可证明的并发协议** | 每lane或每block的独立固定报告槽、Host在完成后合并；shadow写更新使用有序命令 | 少量报告/高内存开销；缺少可证明发布语义时无法可靠报告 |
| SIMT级可靠trap/dispatch abort | 影响错误后停止 | 若模式要求阻止非法访问，则必须有可靠策略 | 条件谓词屏蔽无效访存+sticky fatal标记+统一dispatch完成策略，或受支持的硬件trap/context abort | 一lane提前return可能使barrier死锁；不能证明安全时拒绝该运行模式 |
| Private/Stack/LDS对象大小和生命周期 | 当前AMD覆盖不足或归类粗糙 | 要实现各空间对象级检测则必需 | Compiler记录frame/LDS布局；设备端使用(block,thread,frame,object)局部Bounds；可使用Global metadata backing | 未获得真实布局就只能声明未覆盖，不能把完整backing视为所有子对象有效 |
| 精确硬件fault PC | 用于硬件fault归因 | B软件报告不要求硬件精确PC | 插桩时放site_id、module hash、设备报告里写site和lane | 可定位受插桩访问，无法归因未插桩硬件Fault到每个lane |
| 足够设备显存 | Shadow、Redzone和报表都要资源 | 必须有可满足最小方案的存储 | 对象区间检查、紧凑descriptor、分配级Shadow、限量报告；限制支持集合 | 若资源不足则TOOL_FAILURE，不得静默回退成CLEAN |

**需要强调：**“没有XNACK”与“GPU没有访存能力”不是一回事。对自研目标，正常GPU global load能读取预映射设备Shadow就可以实现B软件检测；若既无法建立合法设备地址映射，也无设备load或可调用helper，则无法靠Host模拟替代实体GPU检查。

## 3. Clang/Driver与构建系统

自研编译开关不要直接照搬AMDGPU的xnack+强制检查。先检测目标GPU真实能力，并按业务契约选择算法；同时把未支持空间/指令写入manifest。若是实际AMD ROCm官方工具，仍必须遵守它的xnack+及运行库要求。

~~~cpp
SanMode chooseSanitizer(const TargetCaps& caps) {
  if (!caps.gpu_loads_device_metadata) return Unsupported;
  if (caps.flat_gpuva_window && caps.shadow_backing_ready) return B_ShadowFlat;
  if (caps.device_descriptor_access && caps.alloc_registry_abi) return B_Interval;
  return Unsupported;
}
if (using_official_rocm_gpu_asan && !actual_xnack_plus_supported)
  fail_build("GPU ASan device instrumentation unavailable"); // 不悄悄生成CPU-only成功
link_device_library(mode, san_abi_version);
emit_module_manifest(mode, supported_opcodes, addr_spaces, unchecked_sites);
~~~

**无XNACK替代：**自研Pass不依赖XNACK编译目标；使用设备global metadata load读取预驻留的B2 Shadow，或使用B1 GPU端当前Live区间查询；如果来源可证明，D1可直接比较寄存器/参数base/limit。**B1/B2都不能仅凭地址判断恢复原对象来源**。**无HMM替代：**Host内存中的指针不能被当成GPU指针传给设备，UMD必须提供实际GPUVA并在launch前完成descriptor上传与fence。此替代是新实现，不是“让现有ROCm官方ASan在不支持架构上可用”的命令行技巧。

## 4. LLVM IR Pass：访存前检查（Compiler，方案B）

分早期对象语义、优化后IR检查与最终ISA审计三阶段：D1来源身份需要在优化可能丢弃前传播；B地址检查放在保持真实访问、masked条件与地址空间语义的阶段；late audit确保后端新增的访存仍有检查点。

~~~cpp
for (auto& insn : device_fn.instructions()) {
  if (is_sanitizer_internal(insn)) continue;
  for (auto m : classify_memory_effects(insn)) {
    if (!known_bytes_and_active_mask(m)) {
      manifest.uncovered(m.site, "UNKNOWN_FOOTPRINT"); continue;
    }
    Mask effective = active_lanes(m) & active_elements(m) & evl_mask(m);
    if (!is_supported_addrspace(m.space)) {
      manifest.uncovered(m.site, "UNSUPPORTED_ADDRSPACE"); continue;
    }
    emit_before(insn, call_device_check(
      m.address, m.bytes, m.kind, m.space, effective, m.site,
      original_object_token_if_available(m)));
  }
}
~~~

必须包含Load/Store/Atomic、masked/VP、memcpy、device malloc、image/surface、async-copy语义，且逐个声明支持范围。原子操作保持一次原子，不允许改写成非原子的load/store。被mask关闭元素绝不访问Shadow或报告误报。AddressSpace(3)/(5)在LLVM当前AMDGPU路径存在特定排除/处理，不能直接以AMD当前Pass成功推断已覆盖LDS/private。

## 5. Target Backend / MC：最终机器访存覆盖

~~~cpp
for (MachineMemOp op : final_target_isa(kernel)) {
  if (op.is_trusted_device_checker) continue;
  if (!op.has_dominating_check_with_matching_mask_width_space()) {
    manifest.record_uncovered(op.pc, op.opcode, op.space);
  }
}
if (strict_mode && manifest.has_uncovered_memory_effects()) reject_load();
~~~

**没有专用硬件指令的替代：**用现有目标GPU integer/branch/global-load生成检查Stub，不要求新增bounds硬件指令。**若Target不暴露特殊async/image真实足迹：**该类opcode标PARTIAL/UNSUPPORTED，不能让后端audit假通过。目标资源压力也应测量register/scratch/occupancy及编译时间。

## 6. GPU Device Runtime：Shadow和对象区间两种可部署实现

经典Shadow查询1:8时必须包含所有跨granule有效字节，不能只检查起始地址。地址端点计算先检查整数溢出，区分Generic/Global/Shared/Private。Shadow读本身需要合法映射，所有元数据helper标记可信避免递归插桩。

~~~cpp
__device__ SanVerdict check_access(GpuVA addr, uint64_t bytes,
                                    Space space, ActiveMask mask,
                                    SiteId site) {
  if (!active(mask) || bytes == 0) return VALID;
  if (bytes - 1 > MAX_GPUVA - addr) return fail_and_block(RANGE_OVERFLOW, site);
  if (!metadata_ready()) return fail_tool_and_block(METADATA_UNAVAILABLE);
  if (space == Global && ctx.mode == Shadow) {
    for (auto g : all_granules_intersecting(addr, bytes))
      if (!shadow_encodes_all_accessed_bytes_valid(g, addr, bytes))
        return fail_and_block(OUT_OF_BOUNDS, site);
  } else {
    auto d = device_descriptor_lookup(addr, space, block_id(), thread_id());
    if (!d.found || !range_within_original_object(d, addr, bytes))
      return fail_and_block(OBJECT_BOUNDS, site);
  }
  return VALID;
}
~~~

**无大片连续Shadow GPUVA：**UMD先为每个allocation建立device-local Shadow segment，设备端两级表由allocation区域映射到段起点；或使用GPU存储的排序区间表（B1，原方案I）进行有界查找。只有在全查找链路可在真实GPU执行并在dispatch开始前可见时才能算设备检查。基于当前地址查live allocation的区间表是B1地址级检查，无法自动发现A指针访问live B；要D1原对象token才能发现。

**无自动页换入：**metadata必须全驻留且保证合法映射，缺页不做透明Host补救；分配/上传失败拒绝launch而不是访问空Shadow。若报告资源不足，设置sticky TOOL_FAILURE且阻止相应GPU访存。

## 7. UMD：DebugAllocator、ShadowManager和异步生命周期

UMD是对象语义的权威来源：物理BO长度不等于逻辑对象长度；pool内suballocation、tensor view、external import以及graph replay要有不同的对象/映射身份。映射和命令完成由KMD提供底层证据。

~~~cpp
Allocation san_alloc(Queue& q, size_t n) {
  auto bo = driver.allocate_backing(round_up(n + redzones));
  auto obj = registry.create(bo.gpuva, n, bo.backing_length, next_id());
  auto shadow = device_shadow_segment_for(obj);
  Fence ready = q.enqueue(shadow_poison_redzones_and_unpoison_body(obj, shadow));
  return {obj.gpuva, obj.id, ready}; // 所有kernel必须在ready之后
}
Fence san_free(Queue& q, Allocation obj, FenceSet final_uses) {
  auto barrier = q.wait_for(final_uses);
  auto poison = q.enqueue_after(barrier, poison_and_retire_metadata(obj));
  return q.enqueue_after(poison, actual_free_or_quarantine(obj));
}
~~~

**无HMM替代：**Host为Registry生成连续descriptor快照，使用真实DMA/copy engine或UMD认可的上传路径拷贝到GPU元数据BO；Gpu launch依赖上传fence；设备写的报告在完成fence后读回。**无可靠稀疏映射：**按allocation扩展Shadow，显存超限时切换事先声明的区间检查或拒绝该场景。**若无前进性或可靠abort：**默认strict模式拒绝风险配置。

Shadow Poison/Unpoison必须保证合法stream-before关系，不按Host API返回时间直接宣称GPU free已生效。Quarantine延长检测窗口，但不等于能检测地址复用后的旧指针。

## 8. KMD：真实GPU映射、权限和完成，不强制每条路线都改

KMD实际责任为BO/GPUVA、合法元数据页权限、代码对象装载、queue提交、fence与fault传递；不从裸GPUVA推导tensor view或原始指针身份。已有接口足够时新增KMD代码可为零。

~~~cpp
SubmitResult submit_checked(Context ctx, Binary checked, BO metadata,
                            BO report, Fence ready) {
  require(can_map_gpu_metadata(ctx, metadata));
  require(validate_gpu_code_and_usage(ctx, checked));
  require(metadata.page_permissions == GPU_READ);
  require(report.page_permissions.has(GPU_WRITE));
  return submit_gpu_job(ctx, checked, {metadata, report}, wait=ready);
}
~~~

**无XNACK：**KMD不启用page-fault retry，改为在launch前全部映射必要metadata/Shadow；缺页视为配置或运行失败。**没有GPU VMM权限功能：**采用本来就能让GPU合法访问的预留设备BO，硬件隔离能力仍缺失；对于需要进程隔离的保证，不能用仅UMD登记对象来冒充KMD权限。**KMD未暴露真实GPU完成fence：**必须补对应契约，否则无法证明Shadow更新、报告回读和异步释放时序。

## 9. Firmware / Trap / 错误终止

这是最容易被低估的**真正硬件依赖**。发现一个非法store后不能只在一个lane return，让别的lane停在barrier。推荐区分：(a)真正的设备硬件trap/context abort；(b)有明确可证明的指令级谓词屏蔽+统一错误完成；(c)二者都没有则不支持严格安全停止保证。

~~~cpp
DeviceResult on_san_error(Context& ctx, Access a) {
  report_sticky_error_before_completion(ctx, a.site, a.address);
  if (caps.safe_dispatch_abort) return request_real_abort(ctx.dispatch);
  if (caps.can_predicate_suppress_invalid_memory_effect &&
      caps.has_defined_failure_completion)
    return suppress_memory_effect_and_mark_dispatch_failed();
  return MODE_UNSUPPORTED; // 必须在launch前预测并拒绝无法安全处理的模式
}
~~~

无硬件trap的谓词屏蔽只能防止当次非法访存生效；被屏蔽load的后续值没有正常应用语义，整个dispatch结果必须标为失败，不能宣称应用继续计算正确。对atomic/copy/async、barrier参与集合和并发错误，需要单独证明屏蔽不会破坏硬件前进性。Firmware若已有正确停止API，仅按KMD/UMD协议补接线；不存在时不应虚构。

## 10. RTL/实体硬件：F0、F1、F2不是AMD软件ASan的必要条件

- **F0 MMU Guard Page（原G；辅助能力，非独立完整Sanitizer）：**只有实体GPU GPUVM真正支持未映射页/权限fault、且分配按页边界布局时可用。页内4B越界不会因为Guard Page存在而自动故障；无MMU时使用真实GPU设备指令的Shadow/Bounds检查。
- **F1 硬件Tag：**实体芯片需比较指针tag与memory tag并在正确ISA异常点处理，不允许把CPU ASan的软件Tag函数称为硬件支持。无硬件时使用D1的原对象Bounds，或在D2受控ABI内显式携带object_id/epoch并执行GPU软件比较。
- **F2 硬件Bounds：**没有descriptor硬件比较单元时，Compiler以原本可执行的GPU integer/branch/load来比较base、limit、访问宽度。只有保留来源token才能提供真正的原对象检查，不能靠当前GPUVA反查。

**硬件逻辑缺失的准确代价：**软件增大指令量、寄存器、访存和metadata占用；无法覆盖未插桩kernel；专用硬件fault isolation、周期精度及透明硬件检测也不会因软件替代自动获得。是否投资新RTL须在同一真实GPU应用上测量B/D软件瓶颈后决定。

## 11. Reporter、CLI与失败分类

~~~cpp
Result aggregate_gpu_result(Completion done, ReportBuffer report,
                            CoverageManifest coverage) {
  if (!done.real_gpu_executed) return GPU_NOT_RUN;
  if (report.overflow || report.metadata_oom || done.report_failed)
    return TOOL_FAILURE;
  if (!coverage.all_required_gpu_ops_checked) return PARTIAL_COVERAGE;
  if (report.has_matching_device_oob) return GPU_DETECTED;
  if (done.hw_fault) return DEVICE_FAULT; // 不等于精确对象越界
  if (!done.completed_normally) return TOOL_FAILURE;
  return CLEAN_FOR_DECLARED_COVERAGE;
}
~~~

至少记录真实GPU型号/ISA、编译/运行库版本、模块hash和PC映射、访问宽度、space、lane、block、分配/epoch、Checker版本、未覆盖指令与拒绝原因。Host ASan、普通API错误和GPU设备错误分别记录，不混合统计。

## 12. CI、S01样例、无XNACK实验及验收次序

### AMD官方环境对照（只适用于实际受支持的AMD设备）

~~~bash
# GPU真实支持xnack+、内核HMM以及对应/asan运行库时，才使用这一路径
export HSA_XNACK=1
export ROCM_ARCH=gfx90a:xnack+ # 示例，必须替换成真实目标架构
hipcc -x hip -O1 -g -fsanitize=address -shared-libsan \
  --offload-arch="$ROCM_ARCH" samples/S01/amd.hip.cpp -o /tmp/s01_gpu_asan
ldd /tmp/s01_gpu_asan   # 检查ASan Host runtime和/asan/下的HIP库
/tmp/s01_gpu_asan --good
/tmp/s01_gpu_asan --bad
~~~

如果设备不能提供该官方配置，记录UNSUPPORTED_OFFICIAL_ROCM_DEVICE_ASAN，不要简单设置环境变量绕过工具预检，也不能将CPU ASan作为设备检出。

### 自研GPU（无需XNACK的真正实现路径）

~~~text
1. 读取目标ISA/设备元数据Load能力/队列与KMD映射能力
2. UMD创建设备可访问的Object Registry和Shadow segment，全部映射并完成初始化
3. LLVM为S01 Global Store编译设备check + report指令，反汇编核对最终实际ISA
4. 设置Strict覆盖/报告/资源合约，启动前检查metadata_ready和abort/suppress能力
5. 在实体GPU执行S01 good: a[0], a[15]；bad: a[-1], a[16]
6. 同时验证shadow初始化未完成、metadata映射失败、report溢出、非法lane关闭等反例
7. 收集设备PC/lane/访问宽度/对象范围/模块hash/原始日志和真实性能开销
~~~

### 缺硬件功能的逐项验收矩阵

| 测试 | 硬件条件 | 预期结果 |
|---|---|---|
| B Global + 显式Shadow | 无XNACK但GPU可读固定Shadow | 真实设备报告；确认GPU ISA真的执行check |
| B Global + HMM关闭 | 设备BO与fence上传可用 | 正反例结果一致，不能偷偷读Host地址 |
| 无连续Shadow GPUVA | Device segmented Shadow/区间表可读 | 报告能力等级和查询开销；不可将跨对象精度擅自提高 |
| VMM/Guard缺失 | 软件GPU指令仍可执行 | B/D正常，F0标UNSUPPORTED |
| 无Tag/Bounds硬件 | 软件D1/D2实现完整来源/epoch | D1/D2受控范围可用，F1/F2标UNSUPPORTED |
| 无可靠context abort | 有安全谓词屏蔽与错误完成协议 | 失败dispatch被拒绝或整体失败，不出现barrier挂死 |
| Shared/Private无精确布局 | 未实现新target插桩和资源协议 | 对应space标PARTIAL/UNSUPPORTED |
| Shadow OOM/报告满 | GPU真实执行失败注入 | TOOL_FAILURE且sticky/dropped状态可见 |
| 真实GPU性能 | 固定同一ISA/输入/调度/驱动条件 | 报告原始Kernel时间、显存、occupancy、覆盖及前后差异 |

推荐先做B的Global Load/Store和实际GPU报告，再扩展masked/vector/atomic/async、LDS/private的受控软件Bounds；D1解决原对象跨活对象问题，D2解决同VA复用后旧指针，C/E处理无法重新编译的模块。软件B/D在真实GPU可靠运行前，不优先立项F1/F2硬件扩展。

**实现状态：本文全部为自研GPU实施设计/伪代码。此仓库尚无目标自研GPU生产Compiler/UMD/KMD源码与真实设备检测报告；静态CI成功不能代替GPU执行。**
