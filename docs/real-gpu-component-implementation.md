# GPU Sanitizer：真实硬件实施规范与逐组件伪代码

**版本：v1.8（2026-10-21）｜范围：Compiler、Device Runtime、Host Runtime／UMD、Runtime Binary Rewriter、KMD、Firmware、GPU Hardware、Reporter／CLI、真实GPU测试及CI。**

## 1. 唯一准入条件

产品检测必须由**真实GPU执行的检查指令／设备检查库**，或**实体GPU实际具备的硬件保护能力**触发。当前候选仅 B（LLVM Shadow）、C（运行时二进制插桩）、D（原对象Bounds及时间身份）、E（混合）、F（硬件辅助）。纸面伪代码、CPU静态CI及无设备执行日志不能记作检出。没有对应硬件支持的 F1／F2 是芯片研发方向，不是当前产品功能。

B、D及E中编译插桩路径需修改生产Compiler。**纯C是UMD Runtime Binary Rewriter修改已编译GPU机器码，生产Compiler源代码新增量可以为0。** KMD只在现有的映射、提交、fault、abort接口不足时新增代码；接口必须核验，但不预设每条路线都修改KMD。

定义运行结果：HARDWARE_DETECTED（真机设备检查器检出）、HARDWARE_CLEAN（完整已支持检查执行且未检出）、UNSUPPORTED（能力缺失）、INCOMPLETE（资源不足／覆盖不足／检查失效）、DEVICE_FAULT（原生MMU/硬件异常）、NOT_RUN（未运行）。UNSUPPORTED、INCOMPLETE、NOT_RUN均不得折算为CLEAN。

## 2. 公共 ABI 与分层

~~~cpp
enum SanMode { SHADOW_B, BINARY_C, BOUNDS_D1, IDENTITY_D2, HYBRID_E, HW_F };
enum Coverage { FULL_OBJECT, ALLOCATION_ONLY, PAGE_ONLY, PARTIAL, UNKNOWN };
struct SanObject {
    uint64_t object_id, epoch;
    GpuVA base;
    uint64_t logical_bytes; // 用户可寻址的逻辑长度，不是BO的页对齐长度
    uint32_t owner_context, address_space, state;
};
struct SanContext {
    uint32_t abi_version, mode, coverage;
    DevicePtr<SanObject> objects;
    DevicePtr<uint8_t> shadow;
    DevicePtr<ReportRing> reports;
    uint64_t dispatch_id, alloc_generation;
};
struct SanReport {
    uint64_t dispatch_id, site_id, object_id, epoch;
    GpuVA address;
    uint32_t access_bytes, lane_id, block_id, access_kind, error_code;
    uint32_t report_version, overflow_count;
};
~~~

所有对象元数据更新都必须与相应stream/event、模块装载、launch、free完成状态建立确定的先后关系。使用GPU专用metadata映射；上下文隔离、防整数溢出、失败时显式INCOMPLETE、原始PC符号化和版本协商统一实现。

## 3. Compiler：LLVM IR／中端插桩（B、D、E）

~~~cpp
for (Instruction &I : kernel_ir.memory_operations()) {
    if (is_trusted_sanitizer_internal(I)) continue;
    MemoryEffect m = classify_effect(I); // load/store/atomic/vector/masked
    Provenance p = propagate_source_identity(I); // D1/D2需要早期生成
    Mask active = effective_lane_mask(I) & effective_element_mask(I);
    if (!supports(m.address_space, m.opcode, m.width)) {
        mark_coverage(I, PARTIAL); continue; // 不得假装已检查
    }
    IRBuilder before(I);
    before.call("__gpu_san_check", m.address, m.bytes,
                m.kind, active, p.object_id, p.epoch, site_id(I));
    mark_instrumented(I); // 保留原atomic的单次执行与memory ordering
}
~~~

关键要求：插桩必须实际到达GPU ISA；优化不能消除或错误合并需要的检查；对masked-off lane/element不访问shadow；用户atomic仍执行一次，不将CAS拆成check＋非原子读写。D1对象来源的phi/select、GEP、call/return、spill/reload以及子对象bounds必须保留；D2还须实现指针值与身份的成对传播，并列出不可支持的ABI入口。

## 4. Target Backend／ISA Final Audit（B、D、E）

~~~cpp
for (MachineInstr &mi : finalized_machine_function) {
    if (!mi.has_memory_effect()) continue;
    if (is_trusted_checker_memory(mi)) continue;
    if (has_verified_check_site(mi) && predicate_matches(mi)) continue;
    if (is_compiler_managed_spill(mi) && has_valid_backing_policy(mi)) continue;
    emit_coverage_gap(mi.opcode(), mi.pc(), "UNVERIFIED_MEMORY_EFFECT");
}
require(no_unreported_memory_effects_for_full_coverage);
~~~

检查后端最终拆分出的向量/原子/异步访存，确认访问宽度、address space、有效谓词与检查点对应。对缺乏设备检查路径的访存做拒绝或明确降级，而不是把IR层覆盖率当成机器码覆盖率。

## 5. Device Runtime：真实GPU端检查与报告（B、C、D、E）

~~~cpp
__device__ void __gpu_san_check(
    GpuVA addr, uint64_t n, AccessKind kind, Mask active,
    ObjectId origin, Epoch expected, SiteId site) {
    if (!lane_and_element_active(active)) return;
    if (n == 0) return;
    if (add_overflow(addr, n - 1)) return report_and_block(ADDRESS_OVERFLOW);
    if (!san_context_abi_valid()) return report_incomplete_and_abort();
    Verdict v = check_supported_space_and_range(addr, n, kind);
    if (mode_has_D1() && origin.valid())
        v &= check_original_object_bounds(origin, addr, n);
    if (mode_has_D2() && expected.valid())
        v &= check_object_epoch(origin, expected);
    if (v != VALID) {
        ring_publish_release(site, addr, n, lane_id(), v);
        apply_supported_failure_policy(); // trap/abort或已定义的安全屏蔽
    }
}
~~~

B的Shadow按被访问的每个granule和全部有效字节判断，不能只查首地址。Shadow/对象表不可访问、报告队列满、未支持opcode都必须记录INCOMPLETE或显式拒绝。错误后的continue/trap必须遵守目标SIMT barrier前进性；不能让单个lane直接退出而导致同一workgroup永久等待。

## 6. Host Runtime／UMD：对象账本与异步生命周期（B、D、E）

~~~cpp
GpuPtr san_alloc(size_t bytes, Stream s) {
    auto alloc = gpu_allocator_alloc_with_guard_or_shadow(bytes);
    ObjectId id = registry.create(alloc.gpuva, bytes, s, /*LIVE_PENDING*/);
    enqueue_metadata_unpoison(s, id, bytes); // 与用户launch在流中有序
    registry.publish_on_completion(id);
    return alloc.user_pointer;
}
void san_free(GpuPtr p, Stream s) {
    auto obj = registry.lookup_exact_base_and_owner(p);
    if (!obj || obj->already_retired()) return host_api_report(INVALID_FREE);
    enqueue_metadata_retire(s, obj->id);
    enqueue_real_gpu_free(s, p);
    registry.retire_after_completion(s, obj->id);
}
void san_launch(Module m, Kernel k, Args a, Stream s) {
    auto san = prepare_versioned_context(m, k, s);
    require(metadata_ready_before_dispatch(san, s));
    launch_with_san_context(m, k, inject_supported_arg_sidecar(a, san), s);
}
~~~

区分allocation逻辑长度与BO映射长度；实现池内子分配、外部import/alias与graph replay的显式能力等级。UMD负责对象ID和epoch分配、设备metadata上传、失效、队列依赖、运行时协商，不可让KMD凭一个GPUVA倒推语言对象或原始指针来源。

## 7. Runtime Binary Rewriter：纯运行时C

~~~cpp
PatchedModule patch_module(Binary original, TargetIsa target) {
    DecodedCFG cfg = decode_and_verify(original, target);
    for (DecodedMemOp &op : cfg.memory_effects()) {
        require(supported_opcode_width_predicate_space(op));
        Stub check = make_device_checker_stub(
            op.effective_address, op.bytes, op.kind,
            op.exec_mask, op.source_pc);
        cfg.insert_before(op, save_live_state(check));
        cfg.insert_after_check(op, restore_live_state());
    }
    relocate_all_control_flow_and_literals(cfg);
    recalculate_register_scratch_and_shared_usage(cfg);
    Binary patched = encode_and_sign_or_authorize(cfg);
    verify_all_memory_effects_accounted_for(patched);
    return cache_by_hash_isa_abi_policy(original, patched);
}
~~~

Patch发生在模块load/JIT finalize、提交GPU之前，不改写正在执行的kernel。必须精确保留branch target、call/return、register/predicate、barrier、原子与异常PC映射。机器码没有携带原对象来源时，只能提供所支持的地址级保证，不允许冒充D1/D2。

此组件归UMD独立工具库；可以复用现有只读MC接口，但**不能将Compiler源码修改当成纯C的前置条件**。

## 8. D1/D2：原对象Bounds和时间身份

~~~cpp
// D1：与当前地址所落入的对象无关，只使用指针最初携带的来源
bool check_d1(PtrMeta p, GpuVA address, uint64_t n) {
    return p.known && address >= p.base && n <= p.length
        && (address - p.base) <= (p.length - n);
}
// D2：epoch属于指针的历史身份，不能从当前GPUVA反查重新赋值
bool check_d2(PtrMeta p, SanObject current) {
    return p.known && p.object_id == current.object_id
        && p.epoch == current.epoch && current.state == LIVE;
}
struct PointerToken { GpuVA value; ObjectId object; Epoch epoch; };
PointerToken load_pointer_with_identity(Address slot);
void store_pointer_with_identity(Address slot, PointerToken token);
// 原子指针发布必须定义值+身份一致性的协议；未实现则明确UNSUPPORTED
~~~

D2的pointer-shadow、部分字节覆盖、memcpy、phi、跨调用边界、原子CAS/ABA和非插桩模块交互均需单独证明。不能把live allocation表generation当成完整指针时间身份，也不能声称仅有B的quarantine就能检测复用后的全部UAF。

## 9. E：Hybrid选择与覆盖清单

~~~cpp
Decision choose_mode(Module m, HardwareCaps caps) {
    if (m.has_verified_compiler_checks()) return {COMPILED, m.guarantees()};
    if (m.is_supported_binary() && rewriter_can_patch_all_ops(m))
        return {RUNTIME_PATCH, ALLOCATION_ONLY};
    return {REJECT_OR_EXPLICIT_UNKNOWN, UNKNOWN};
}
void load_checked(Module m) {
    Decision d = choose_mode(m, caps);
    require(!already_instrumented_twice(m));
    validate_device_runtime_abi_and_object_registry(m);
    record_manifest(m.hash(), d.mode, d.coverage, d.unchecked_opcodes);
}
~~~

共享Object Registry、Shadow、Report Ring和错误协议；禁止双重插桩；跨模块来源丢失时显式降级。E不是把B+C+D各预算直接相加，而是按共享公共底座、D增量、C受控fallback与整合成本去重。

## 10. KMD：真实GPUVA、队列与fault契约（按缺口修改）

~~~cpp
ioctl SAN_QUERY_CAPS -> {gpuva_mapping, sparse_pages, fault_pc, abort_scope};
ioctl SAN_MAP_METADATA(ctx, pages, permissions) {
    validate_owner_and_mapping_permissions();
    pin_or_map_into_real_gpu_page_tables();
}
submit_checked_dispatch(ctx, code_handle, metadata_handle, resource_usage) {
    verify_executable_mapping_and_resource_quotas();
    submit_to_actual_gpu_queue();
}
on_device_fault(irq, context, dispatch) {
    record_actual_hw_fault_and_completion_epoch();
    signal_UMD_without_fabricating_sanitizer_verdict();
}
~~~

必须核验已有接口是否足够：BO/GPUVA、metadata权限/映射、代码合法加载、completion/fault/abort/context isolation。足够则KMD新增量=0。KMD不识别任意tensor view或指针来源，不能把API错误响应冒充设备OOB检出。

## 11. Firmware／GPU Hardware（仅F和需硬件协作的终止）

~~~cpp
// F0：仅真实GPU MMU执行页权限检查，粒度为GPU页面
gpu_vm.reserve_guard_pages(before, after);
gpu_vm.map_user_pages(data, read_write);
on_real_gpu_page_fault(pc, gpuva, ctx) -> DEVICE_FAULT;

// F1/F2：只有实体硬件具备tag compare／descriptor bounds单元才可启用
if (!chip_caps.has_real_tag_check && !chip_caps.has_real_bounds_check)
    return UNSUPPORTED;
on_memory_issue(access, expected_meta) {
    if (!real_hardware_compare(access, expected_meta))
        hardware_raise_precise_fault(access.pc, access.context);
}
~~~

F0只保证跨入未映射/无权限GPU页时fault，不能检测同一合法页内的所有4B尾越界。F1/F2若硬件尚无支持，要单独开展ISA、RTL、微架构、DV、固件、驱动、silicon bring-up项目；不得以新增软件函数代替硬件实现。设备异常后的可靠abort需要KMD调用**已存在的**Firmware/硬件停止机制；不存在时不能承诺可安全恢复。

## 12. Reporter、CLI、覆盖与资源失败

~~~cpp
void consume_report(Dispatch d) {
    wait_for_actual_gpu_completion_or_failure(d);
    auto r = report_ring.read_acquire(d);
    merge_with_module_pc_map_and_debug_symbols(r);
    if (r.overflow || d.metadata_failure || d.unchecked_instructions)
        result.status = INCOMPLETE;
    else if (r.has_checker_error)
        result.status = HARDWARE_DETECTED;
    else if (d.has_actual_gpu_page_fault)
        result.status = DEVICE_FAULT;
    else if (d.checker_executed_on_real_gpu)
        result.status = HARDWARE_CLEAN;
    else
        result.status = NOT_RUN;
}
~~~

报告必须包含实际设备、driver/firmware/ISA版本、模块hash、模式及保证等级、site/原PC、访问宽度/地址空间、lane/workgroup、分配ID/epoch、日志溢出和资源状态。不能将UNKNOWN、PARTIAL、INCOMPLETE美化为“无错误”。

### 12.1 Initcheck：GPU字节定义性检查器

~~~cpp
__device__ void before_read(Address a, Bytes n, LaneMask active) {
    if (!active) return;
    if (!defined_bitmap.all_set_for_effective_bytes(a, n))
        device_report(UNINITIALIZED_READ, a, n);
}
__device__ void after_successful_store(Address a, ByteEnable en) {
    atomic_or_defined_bits(a, en); // 必须避免元数据位丢失
}
void after_async_copy_visible(CopyEvent e) {
    copy_defined_bits(e.dst, e.src, e.bytes); // 真实完成之后
}
~~~

仅写入真实成功的有效字节后才设置定义性；atomic read-modify-write先检查旧值再按成功路径更新。Host来源默认已定义的保守策略需明确说明，不能宣称完整CPU/GPU MSan。

### 12.2 Racecheck：设备访问事件、HB和scope/order

~~~cpp
__device__ void record_access(Access a) {
    ThreadClock here = clock_for(a.launch, a.block, a.thread);
    for (PriorAccess old : shadow_conflicts(a.object, a.byte_range)) {
        if ((a.write || old.write) &&
            !atomic_ordering_exempts(old, a) &&
            !happens_before(old.clock, here))
            device_report(DATA_RACE, old.site, a.site);
    }
    publish_access_state_without_synchronizing_app(a, here);
}
void on_valid_sync(SyncEvent e) {
    update_happens_before_edges(e); // scope和event/queue必须符合实际语义
}
~~~

不能用“谁先在物理设备执行”当成语言HB，也不能通过检查器自身的额外atomic给应用制造不存在的同步。Metadata OOM/clock溢出时报告INCOMPLETE，而不是判定race-free。

### 12.3 Synccheck：Barrier/Phase/Subgroup检查

~~~cpp
__device__ void check_barrier(Barrier b, Participation p) {
    auto expected = required_participants(b, current_live_lanes());
    if (!mask_satisfies_isa_rules(p.mask, expected))
        device_report(INVALID_BARRIER_MASK, b.site);
    if (!phase_consistent(b.id, p.phase, p.token))
        device_report(BARRIER_PHASE_MISMATCH, b.site);
    register_arrival_or_wait(b, p);
}
__device__ void check_async_wait(CopyGroup g, WaitToken t) {
    if (!references_committed_group_and_phase(g, t))
        device_report(INVALID_ASYNC_WAIT, g.site);
}
~~~

必须与目标ISA的合法退出、部分参与、warp/block/cluster作用域和async copy completion规则一致。超时只说明未完成；若无额外语义证据，不自动判定程序死锁。检测器本身不能在barrier前挂死。

## 13. 真机测试、性能及CI

~~~python
for sample in range(1, 29):
    build_cuda_or_hip_sample(sample)
    if not has_compatible_physical_gpu():
        record(sample, "NOT_RUN")
        continue
    good = run_on_physical_gpu(sample, variant="good")
    bad = run_on_physical_gpu(sample, variant="bad")
    assert good.exit_contract_ok and not good.unexpected_checker_errors
    assert bad.device_checker_executed or bad.explicitly_unsupported
    store_raw_logs_binary_hash_versions_and_coverage(good, bad)
~~~

静态检查包括IR/MIR/ISA覆盖、ABI契约、预算JSON和全文禁用方案回归。真实GPU验收必须覆盖S01-S28的正反例、masked/EVL、atomic、shared/private、async free、地址复用、JIT、report overflow和终止。按相同GPU/输入/编译器/驱动测量baseline与instrumented的运行耗时、显存、occupancy、寄存器及日志开销；不得用CPU CI状态推断真实GPU完成。

## 14. 每个组件汇总

| 组件 | B | C | D | E | F | 实施主责 |
|---|---|---|---|---|---|---|
| LLVM IR／Target Backend | 插桩＋最终ISA审计 | **生产Compiler源码0** | D1/D2传播和插桩 | 编译路径＋manifest | 仅已有硬件的ISA lowering | Compiler |
| GPU Device Runtime | Shadow/check/report | Patch stub设备检查 | descriptor/epoch | 统一helper | 必要时设备配套代码 | 设备库 |
| Host Runtime／UMD | alloc/free/Shadow | 装载和Patch生命周期 | object_id/epoch/sidecar | 模式选择和共享registry | tag/descriptor管理 | UMD |
| Runtime Binary Rewriter | 不需要 | **核心新增** | 不要求 | 受控C fallback | 不需要 | UMD独立模块 |
| KMD | 按契约缺口 | 按代码映射缺口 | 按映射/隔离缺口 | 上述并集去重 | 按真实硬件状态契约 | KMD |
| Firmware | 按实际abort缺口 | 同左 | 同左 | 同左 | 实际fault/状态支持 | 固件 |
| GPU RTL／MMU | 运行设备ISA | 运行Patch ISA | 运行身份检查ISA | 运行实际组合ISA | **必须具备对应真实机制** | GPU架构／RTL |
| Reporter／CLI | 设备Shadow诊断 | 原始PC还原 | 原对象/epoch报告 | 保证等级 | 硬件fault归因 | 工具 |
| 真机测试／CI | OOB和掩码 | ISA重写等价 | A→B/ABA/UAF | 混合兼容性 | 页fault/Tag/Bounds | QA／性能 |
| Initcheck | GPU位图定义性 | GPU位图定义性 | 对象初始态身份 | 覆盖降级 | 设备能力相关 | Device RT＋UMD |
| Racecheck | 真实GPU访存事件 | Binary访存采集 | 追踪对象身份 | 组合scope/HB | 硬件可选辅助 | Device RT＋UMD |
| Synccheck | barrier／async事件 | Binary同步采集 | 与对象边界独立 | 组合检查 | 有限硬件fault | Device RT＋UMD |

**实施顺序：** P0先交付B的真实GPU Global Store检查（含报告和S01正反例）；随后扩展B覆盖与D1，再根据真实需求分别实施D2、C/E、F。源码级和静态结构检查可以先执行，但**当前仓库并未因此完成自研GPU产品实现或真机验收**。