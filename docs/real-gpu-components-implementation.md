# 自研 GPU Sanitizer：真实硬件实施的逐组件关键代码

**版本：v2.8 · 技术复核：2026-11-02 · 状态：原创设计与工程伪代码；目标 GPU 未实施、未验收（GPU_NOT_RUN）。**

本文是通用设备检查、元数据 ABI、UMD/KMD 顺序和失败处理的唯一实现设计入口。软件按 Compiler、KMD、UMD 三组归属；Device Runtime 无论随工具链还是 UMD 交付，其检查指令均须在**实体 GPU**执行。下文 helper 表示必须实现和验证的契约，不是现成接口或已经可编译的目标源码。

分类统一见[核心设计对照](core-design-comparison.md)：**2 家主要厂商、3 种基础参考配置、2 层空间保证**。NV-R 是运行时 Binary 插桩加当前地址检查；AMD-B2 是 LLVM Shadow/Redzone；NV-C 是原对象 Bounds。工程包 B 含 B2 Shadow/B1 当前 Live 区间，C 是运行时重写入口，D1 是来源 Bounds、D2 增加时间身份，E 是集成，F 只保留条件性真实芯片接口和原预算边界。NV-C 不等于工程包 C；Initcheck、Racecheck、Synccheck 另有算法和预算。

## 1. 检测执行与 Host/Device 信任链

| 阶段 | 生产者与执行位置 | 必须交给下一阶段的事实 |
|---|---|---|
| 资源与对象登记 | UMD/Host Runtime | 逻辑对象身份、映射关联、实际队列依赖、模块覆盖 |
| 资源授权与提交 | KMD | 有效且保持驻留的 BO/GPUVA、权限、完成和终止契约 |
| 访问前检查 | 实体 GPU 上的 Compiler 插桩或 UMD 改写代码 | 实际活动 lane/元素的访问范围、选定谓词的判定 |
| 故障与报告 | Device Runtime；必要时硬件/固件/KMD | 具体诊断、覆盖缺口、报告丢失及真实终态 |
| 归因与验收 | UMD/CLI | 版本匹配的原 PC/源码映射、设备证据和结果分类 |

**严格模式的公共前提**：检查结果实际控制原访存；元数据查找本身安全；错误后能阻止该非法操作并可靠结束执行。只插入日志 helper、只拦截 Host API 或仅看到 IR 检查均不满足这三个条件。Host invalid free/leak 是有效的 API 诊断，但不能登记为 GPU 内部 Load/Store 已检出。

### 1.1 Sanitizer 共用元数据与报告 ABI

下列结构是唯一的**字段语义示意**，不是冻结的二进制布局。实际 ABI 需确定端序、对齐、字段宽度、版本、大小和兼容策略。每个 `SanAccessEvent` 表示一个实际活动 lane 中一个元素的连续有效字节段；不连续 byte enable 拆为若干段，共用指令位置，不能用固定 32-bit mask 假设所有访问宽度。

~~~cpp
struct SanAccessEvent {
    uint64_t context_id, launch_id, module_id, gpu_pc, address;
    uint64_t block_id, thread_id, scope_id;
    uint32_t lane_id, element_id, bytes, kind, space, required_alignment;
    uint32_t site_id, mask_record; // 可选扩展记录索引；不用时为约定哨兵值
};
struct SanObject {
    uint64_t allocation_id, base_gpuva, requested_size, generation;
    uint64_t owner_context;
    uint32_t state, address_space, permissions;
};
struct SanReport {
    uint32_t abi_version, record_bytes, checker_id, error_kind;
    uint32_t verdict, coverage_status;
    SanAccessEvent access;
    uint64_t expected_object_id, expected_generation;
    uint64_t observed_object_id, observed_generation, object_base, object_size;
};
~~~

`address` 在 Global 中为 GPUVA；Shared/Private 中必须结合 launch/block/thread/frame 域解释，`scope_id` 区分 block/frame 等实际实例，不能跨域比较裸数值。`module_id` 关联不可变 Build ID、原 PC/Patch PC 和 source map；模块卸载后也保留至相关报告消费结束。完整 lane mask、元素 mask、EVL 和 byte enable 放入版本化扩展，未记录的字段必须标未知，不能默认全活动。

**对象扩展**按支持范围增加 `parent_object_id`、`backing_bo_id`、`mapping_id`、allocation site 及可选 `view_shape/stride`。同一 GPUVA、BO handle 或可复用表槽都不等于不可复用的分配身份；alias/import 保留共同对象身份和独立映射身份。Global 基址以外的空间采用各自描述符。主稿 §17 的 32 B 热对象/64 B 完整对象仅为预算示例，不是上述结构的 `sizeof`。

**报告缓冲头**独立保存 ABI/launch、capacity、已提交记录数、sticky error、sticky tool failure、饱和 dropped 计数和 coverage 缺口。详细记录写完后发布 commit 标志；读取端须在真实设备完成及所需 cache 同步后读取。零条报告只有在覆盖满足、执行正常完成、工具无故障时才表示本次未发现错误；程序错误和报告丢失可以同时成立。

## 2. Compiler：LLVM IR Pass + 后端最终访存审计（B/D/E；纯 C 可零修改）

**修改位置**：target 选项、早期来源分析、IR/MIR pass、设备库调用 lowering、机器访存覆盖表、DebugLoc 和 manifest。原对象来源需要在被优化抹去前保留；检查插入后仍须核对最终 ISA，包括优化产生的宽访存、spill、inline asm、atomic 和 async 指令。

~~~cpp
// 编译器构造 IR 的伪代码；emitUnderPredicate 表示真正的条件执行。
bool instrument(MemoryOp op, GPUFunction& fn) {
  AccessShape a = classifyExactMemoryEffects(op);
  if (!a.supported || !canGuardWithoutChangingSemantics(op))
    return fn.manifest.rejectOrMarkGap(op, "UNSUPPORTED_MEMORY_EFFECT");

  CheckSet checks;
  for (AccessPart p : actualByteFootprints(a)) {
    Predicate enabled = logicalLaneActive(op) && instructionPredicate(op)
        && elementMask(op, p.element) && elementBelowEVL(op, p.element)
        && p.hasEnabledBytes;
    checks.add(emitUnderPredicate(enabled, [&] {
      // 地址、来源和元数据只在该元素活动时求值，不先读再用select丢弃。
      return emitDeviceCheck(p.address(), p.bytes(), p.space(),
                             p.requiredAlignment(), provenanceOf(p), siteOf(op));
    }));
  }
  splitAndGuardMemoryOp(op, checks.allExecutedChecksAreValid(),
                        targetVerifiedFailureExit());
  fn.manifest.recordInstrumented(op, checks);
  return true;
}
// 检查器自己的访存也要证明安全，但不能递归插桩。
for (MachineMemOp m : finalizedISA(fn))
  require(checkedApplicationAccess(m) || provedSafeInternalAccess(m)
          || manifest.hasExplicitGap(m));
~~~

`checks` 的汇合必须符合目标 SIMT 和 LLVM 收敛规则，不能在只有部分 lane 到达的路径插入全组 barrier。所有需要的有效字节段须先检查，再按原谓词发出原操作。错误路径不产生可供应用继续使用的 Load/Atomic 返回值；不以填零、忽略 Store 或单 lane return 冒充成功。未支持的指令进入显式覆盖清单；严格模式加载时拒绝，而不是绕过检查。

**D1 原对象 Bounds**随指针派生保留 A 的 base/size，不因地址进入 Live B 就改换对象。检查先处理 UNKNOWN，再做防溢出的数学判定：

~~~cpp
struct PointerMeta {
    uint64_t object_base, object_size, object_id, generation, scope_id;
    uint32_t address_space;
    bool provenance_known, identity_known;
};
bool withinOriginalObject(const PointerMeta& p, uint64_t addr, uint64_t width) {
    // 前置条件：来源已知，地址空间/作用域匹配，width是实际有效字节数。
    if (width == 0 || addr < p.object_base || width > p.object_size)
        return false;
    return addr - p.object_base <= p.object_size - width;
}
~~~

GEP/phi/select/call/return 要传播所选指针自己的元数据；Pointer Value 与 `PointerMeta` 是配对状态。合法的 one-past 指针可以形成，但不能执行非零宽度解引用。D1 只回答空间边界，固定 base/size 本身不证明对象仍存活；D2 才检查指针保留的历史身份。

### 2.1 目标 LLVM 状态传播与 Mask/Atomic 约束

**Mask 不是一个位图。** 逻辑活动 lane、机器 predicate、vector element mask、VP EVL 和实际 byte enable 共同定义访问集合。Whole/Quad Wave lowering 的临时 EXEC 不能直接替代应用活动集合。Masked-Off 元素不得因检查而加载指针 metadata/shadow，也不能把原本未使用的 poison 地址变成无条件控制依赖。依据 [LLVM masked/VP 语义](https://llvm.org/docs/LangRef.html#masked-vector-load-and-store-intrinsics)；[`convergent` 不隐含 barrier/fence](https://llvm.org/docs/ConvergentOperations.html#memory-model-non-interaction)。

| 指针传播边界 | D1/D2 必须实现的契约 | 不能证明时的处理 |
|---|---|---|
| Pointer load/store、spill/reload | 指针值与来源/identity 配对，匹配对象和地址空间 | 来源或身份 UNKNOWN |
| 完整 `memcpy/memmove`、结构体复制 | 复制 pointer metadata；重叠 move 的元数据语义与值一致 | 不把只复制字节视为身份已保留 |
| 部分字节覆盖、非指针写入 | 使被破坏指针槽的 side metadata 失效；不能遗留旧身份 | UNKNOWN，或在严格 ABI 中拒绝 |
| `ptrtoint/inttoptr`、外部 ABI | 只有明确的 target/ABI 协议才恢复外部状态 | 不能按当前地址重新发一个身份 |
| Atomic pointer | 原子读取/发布同一份 Value＋Identity，所有参与方遵守协议 | 不承诺 D2 全覆盖 |

[LLVM pointers with external state](https://llvm.org/docs/LangRef.html#pointers-with-external-state) 明确 typed pointer load/store 及 `llvm.memcpy/memmove` 的外部状态传播规则，整数转换不自动重建外部状态。这些规则只在选择并实现相应 target 指针表示时适用；普通 Debug Metadata 或额外 side table 不会自动获得这些保证。

用户 atomic 仍是一次具有原 scope/order 的原子操作，不能改成普通 Load＋Store。Atomic Pointer 可考虑目标支持的复合 Wide CAS，或原子发布 **Immutable Descriptor Handle**；后者还要解决句柄复用/ABA、描述符回收和 ABI 改变。两个独立 atomic store 不构成配对发布；带锁/版本协议必须覆盖所有读写方并证明 SIMT 前进性。[LLVM Atomic 指南](https://llvm.org/docs/Atomics.html)提供原子语义依据，具体实现是本项目设计工作。

## 3. GPU Device Runtime：实际 GPU ISA 检查 + 稳定报告（B/C/D/E）

**修改位置**：设备检查库、无缺页查询、对象/身份访问、报告发布、失败出口。检查函数只返回判定，Compiler/rewriter 按 §2 的契约控制原访存。以下是严格模式；`UNKNOWN` 与 `TOOL_FAILURE` 均不能转换成 `VALID`。

~~~cpp
CheckResult san_check(DevCtx& ctx, const SanAccessEvent& a, PointerMeta p) {
  // 只对已活动且非零字节段调用；零长度bulk操作在生成访问集合时排除。
  if (!ctx.manifest.supports(a.space, a.kind, a.bytes))
    return {UNKNOWN, UNSUPPORTED_ACCESS};
  if (!ctx.resourcesPublishedAndPinned()) return {TOOL_FAILURE, METADATA_UNAVAILABLE};
  if (!validAddressArithmetic(a.address, a.bytes, addressLimit(a.space)))
    return {INVALID, RANGE_OVERFLOW};
  if (!satisfiesRequiredAlignment(a)) return {INVALID, MISALIGNED_ACCESS};
  CheckResult permission = ctx.permissionChecker.checkNoFault(a);
  if (permission.verdict != VALID) return permission;

  if (ctx.plan.requiresAddressability()) {
    CheckResult r = ctx.addressChecker[a.space](a); // B1/B2或该空间专用检查器
    if (r.verdict != VALID) return r;
  }
  if (ctx.plan.requiresOriginalBounds()) {
    if (!p.provenance_known) return {UNKNOWN, UNKNOWN_PROVENANCE};
    if (!sameAddressDomain(p, a) || !withinOriginalObject(p, a.address, a.bytes))
      return {INVALID, ORIGINAL_OBJECT_BOUNDS};
  }
  if (ctx.plan.requiresLifetimeIdentity()) {
    if (!p.identity_known) return {UNKNOWN, UNKNOWN_IDENTITY};
    IdentityResult live = ctx.objects.lookupIdentityNoFault(p.object_id, p.generation);
    if (!live.metadataValid) return {TOOL_FAILURE, IDENTITY_METADATA_FAILURE};
    if (!live.sameGeneration || !live.isLive) return {INVALID, USE_AFTER_FREE_OR_REUSE};
  }
  return {VALID, NO_ERROR};
}
~~~

`validAddressArithmetic` 先验证非零宽度及 `addr <= addressLimit`，再验证 `bytes - 1 <= addressLimit - addr`；不能先计算可能回绕的 `addr + bytes`。所有空间专用检查器须显式登记，缺失 Shared/Private/image 支持不能走默认成功分支。检查与原访存之间的并发 free 不是靠上述两次读取即可原子解决，支持边界见 §4。

**B1 Global 查询**使用已发布、可安全读取的当前 Live 区间：先在正确 context/映射域内查找覆盖首字节的记录，再以 `offset <= size - width` 验证整个访问属于该记录并满足权限。缺失记录仅在登记范围完整时判 INVALID；可能合法的外部空间返回 UNKNOWN。查询结构在访问期间必须保持一致、有效，不能读取正在回收的节点。此处找到的“当前对象”只用于地址有效性，不能反向写进 D1/D2 token；A 指针完全落入 Live B 仍可能通过。

**报告与失败出口**：先设置常驻 header 的 sticky 状态，再以不会回绕复用槽位的有界原子预留写详细记录，写完发布 commit；满载时增加饱和 dropped 计数，错误仍然成立。若 header 本身不可写，走独立的设备 fault/完成错误通道，不能继续原访存。失败出口必须阻止该操作并使整次 launch 失败，可靠终止范围在启动前协商；既不承诺回滚先前已完成的写，也不允许一个 lane 提前退出而让其他 lane 永久等 barrier。

### 3.1 GPU Global Shadow 的安全查找契约（适用于无可恢复缺页目标）

本节是 **B2 Checker 的唯一设备查询实现入口**。映射取舍与 XNACK/HMM 解释由 [AMD 专题 §2](amd-gpu-asan-hardware-fallback.md)维护。`ctx.addressChecker[GLOBAL]` 在选择 B2 时绑定下面的实现；设备不得先盲读 Shadow，再指望未知映射的 fault 自动恢复。

~~~cpp
ShadowResult safeShadowLookupNoFault(DevCtx& ctx, gpuva addr, uint64_t bytes) {
  if (!validAddressArithmetic(addr, bytes, ctx.globalAddressLimit))
    return {INVALID, RANGE_OVERFLOW};
  if (!ctx.rootPublishedAndPinned()) return {TOOL_FAILURE, METADATA_UNAVAILABLE};
  for (Granule g : touchedShadowGranules(addr, bytes)) {
    // 根和遍历到的目录自身常驻；叶子地址/offset也必须验证后才能读取。
    ShadowPageState s = ctx.alwaysMappedRoot.lookupAcquire(g);
    if (s.kind == UNKNOWN_ADDRESS_SPACE) return {UNKNOWN, UNSUPPORTED_ADDRESS};
    if (s.kind == UNREGISTERED_IN_COMPLETE_DOMAIN) return {INVALID, OUT_OF_BOUNDS};
    if (s.kind == REGISTERED_BUT_NOT_MAPPED) return {TOOL_FAILURE, SHADOW_NOT_READY};
    if (s.kind != MAPPED_READY || !s.ready || !s.physicallyMapped || !s.leafReferencePinned
        || !s.validLeafOffset(g)) return {TOOL_FAILURE, BAD_SHADOW_REFERENCE};
    byte value = gpuLoadShadow(s.leaf, g);
    if (!allRequestedBytesAddressable(value, g, addr, bytes))
      return {INVALID, OUT_OF_BOUNDS};
  }
  return {VALID, NO_ERROR};
}
~~~

Present 位并不能抵抗随后 unmap 的竞态：叶子映射和目录版本要保持到所有可能的设备读者结束，更新必须遵守发布/回收顺序。Poison 覆盖逻辑无效区；跨 granule、跨 Shadow 页都要检查，首字节合法不足以放行宽访存。Shadow 页存在也不等于所对应应用字节有效。

**1:8 编码限制**：单字节有效前缀无法表示同一 granule 内两个不连续有效岛；子分配必须采用可表达布局或明确支持的更细元数据。即使字节可访问性完全精确，也不能自动识别 A 来源进入 Live B；这属于 D1。Shadow/目录失败是工具错误，不能伪装为应用 OOB。

## 4. UMD DebugAllocator / ShadowManager：对象真值与队列依赖（B/D/E）

**修改位置**：逻辑分配/子分配、导入与 alias、Shadow/interval/identity 资源、异步发布和退休。UMD 负责对象语义，KMD 负责实际 backing/映射；对象表、Shadow 和身份状态必须对应同一生命周期。下面的 metadata transaction 要在目标 API 中实现有序可见性，不是假设一个天然跨表原子指令。

~~~cpp
GpuAllocation sanMalloc(Context& c, uint64_t size, Queue& q) {
  Layout l = checkedDebugLayout(size); // 逻辑对象/backing分开；按所选模式保留redzone
  Backing b = c.driver.allocateAndMap(l.backingBytes);
  if (!b.accepted) return allocationFailureWithoutPublication(b);
  Allocation o = c.objects.createFreshIdentity(b, l.logicalBase(b), size);
  Fence ready = q.submitMetadataTransaction(
      prepareDescriptorAndAddressability(o, l),
      deps = {b.mapReadyFence, c.metadata.conflictingUpdateFences(o)});
  if (!ready.accepted) return retainOrReleaseAfterFailedPublication(o, ready);
  c.objects.recordPendingPublication(o, ready); // Host记录不使设备提前LIVE
  return {o.gpuva, o.id, o.generation, ready};
}

FreeTicket sanFreeAsync(Context& c, AllocationHandle handle, Queue& q) {
  Allocation& o = c.objects.requireExactOwnedNotFreedAllocation(handle);
  HostReleaseTicket ticket = c.objects.reserveFreeRequest(o); // 防重复请求
  // 仅使用API实际提供的queue/event依赖；不自动等所有应用读写来修复错误。
  Fence dead = q.submitMetadataTransaction(
      invalidateIdentityAndAddressability(o), deps = q.applicationDependencies());
  if (!dead.accepted) return ticket.failWithoutReclaim(dead);
  ticket.commit(dead);
  // dead是逻辑失效点；backing/历史记录回收另等设备读者真正结束。
  return c.retirement.deferBackingAndMetadataReclaim(o, dead);
}
~~~

| 时点 | 对 GPU 检查器的含义 | 禁止的混淆 |
|---|---|---|
| 分配发布生效 | descriptor、B1 interval/B2 Shadow、可选 D2 identity 可被消费 | Host API 返回不自动等于发布完成 |
| free 请求排队 | Host 阻止重复请求；设备按原依赖运行 | 不立即把同队列先前合法访问判 UAF |
| free 按 API 契约生效 | 在设备可见的失效点标 DEAD、撤销 interval/poison Shadow | 不等到 backing 释放完才开始判非法 |
| retire/reuse | 等所有相关读者/报告结束再回收元数据；复用分配建立新身份 | 未回收物理内存不等于旧对象仍 LIVE |

提交失败只有在已确认命令从未发出时才回滚 Host 请求；无法确认设备是否执行时保留资源并报告失败，不把失败 fence 当作安全退休证据。同一 Shadow granule 的更新必须避免覆盖其他对象有效字节；transaction 内部依赖与应用 happens-before 分开登记，不能用工具新加的 fence 证明应用没有竞争。Registry 不能只含顶层 kernel 参数：设备内的指针数组、嵌套结构和受支持的动态分配也会带来访问目标。

**D2 回收边界**：活跃 checker 退出后可回收其临时节点，但应用内仍可能保存旧指针供未来 kernel 加载；必须继续保留能拒绝旧 token 的证据，例如不会回绕的槽位代次或等效身份记录。不能只等当前 launch 完成就抹去历史身份；容量/代次耗尽或证明丢失必须显式失败，旧 ID 不得重新匹配。

**并发边界**：B1/B2 只能在当前地址状态生效窗口发现 UAF，复用后同值裸指针不能被区别。无序 free 与正在运行的访问可能竞争，检查后、访存前也可能失效；本设计不承诺检测所有调度下的这一竞态，更不能通过暗加应用依赖让错误消失。此类支持需要真实时序测试及独立 race/lifetime 协议。[主稿 §8.4](llvm-gpu-sanitizer-research.md)

## 5. UMD Runtime Binary Rewriter：独立于生产 Compiler（C/E）

**修改位置**：loader/JIT、ISA decoder/encoder、控制流与 relocation、资源重算、patch cache、原 PC 映射。**纯 C 的生产 Compiler 新增源码可以为零，支持集合内无需重编译应用。** 工具自己的 patch stub 仍需构建为目标设备指令，运行时仍必须选择 B1/B2 等具体检查谓词。

~~~cpp
PatchedModule patchOnLoad(Binary original, UmdDevice dev, CheckPlan plan) {
  ISAProgram p = rewriter.decodeForExactTarget(original, dev.isaRevision);
  if (!p.valid || !allMemoryEffectsAndControlFlowClassified(p))
    return rejectUnsupported(original, "UNKNOWN_OPCODE_OR_CONTROL_FLOW");
  for (Instruction i : p.originalInstructions()) {
    if (i.isApplicationMemoryAccess())
      rewriter.replaceWithGuardedOriginal(i, plan,
          preservePredicateMasksScopeOrderAndOriginalPC(i));
  }
  rewriter.fixupCFGAndRelocations(p);
  rewriter.recalculateRegistersScratchBarriers(p);
  Binary patched = rewriter.encodeAndVerify(p);
  if (!dev.validateResourcesAndFinalCoverage(patched, plan))
    return rejectUnsupported(original, "PATCH_VALIDATION_FAILED");
  return dev.loadNewAuthorizedExecutable(patched, originalToPatchedPcMap(p));
}
~~~

`replaceWithGuardedOriginal` 必须遵守 §2/§3：check 的返回值控制原访存，不只是调用日志函数；atomic、volatile 和 async 副作用不复制、不拆错。重写器引入的 spill/stub/metadata 访存须自身安全，不能把原应用访存标为“工具内部”来规避覆盖。

缓存键至少包含原 Binary hash、ISA 修订、checker ABI、检查配置和重写器版本；实时对象/launch 信息在提交时绑定。使用新代码副本，保持原版本到在途任务结束，不能热修改正在执行的模块。可独立实现 decoder，也可只读复用 MC 库；两者都不等于修改生产 LLVM。工程大项是 opcode/控制流闭合、资源与前进性验证；验收包括合法结果一致、原 PC 归因、特殊指令拒绝和首次/缓存命中的 patch 成本。

## 6. UMD/Host Runtime：Dispatch、Graph、模块能力与报告（B/C/D/E/F）

**修改位置**：检查请求、manifest、参数 sidecar、队列/事件/graph、copy/memset、模块/JIT 生命周期、完成和符号化。产品 API 保持异步；提交和结果收集分开，不能为读取报告把每次 launch 变成全设备同步。

~~~cpp
LaunchTicket launchChecked(Kernel k, Args args, Queue& q, Requirements required) {
  CheckPlan plan = selectPrimaryGpuChecker(q.device, q.registryCaps, k.manifest, required);
  if (!plan.supported) return rejectLaunch("COVERAGE_UNSUPPORTED");
  MetadataBundle meta = prepareAndPinRegistryForDeclaredScope(q.context, plan);
  ArgumentSidecar sidecar = preserveProducerProvenance(args); // 未知则保留UNKNOWN
  ReportBuffer log = prepareAndPinInitializedReport(q.context);
  if (!meta.prepared || !log.prepared) return rejectLaunch("TOOL_RESOURCE_FAILURE");
  return q.submitRegisteredLaunch(k.binaryFor(plan), args, sidecar, meta, log,
      deps = unionOf(q.applicationDependencies(), meta.readyFences, log.readyFences));
}
RunResult collectCheckedResult(LaunchTicket t) {
  Completion done = waitForTrueTerminalOutcome(t);
  makeCompletedReportVisibleToHost(t, done);
  return decodeCommittedReportsAndCompletion(t, done);
}
~~~

不得从当前 raw address 反查并重建“原对象”sidecar；来源要从可信的分配根、生产者传播或明确 ABI 获取。未知来源必须遵守协商后的降级或拒绝策略。全局 Registry 的准备范围也不能只按顶层 args 估计；若做可达子集优化，必须证明支持集合完整。

`meta.readyFences` 只保障检查器安全，不构成应用 HB：异步 alloc/use 原本缺少所需依赖时，也不能因内部等待而判合法；UMD 要核验原始 API 依赖，不能确认的范围明确标不覆盖。资源准备采用事务式清理，提交前失败释放未使用资源；可能已提交的失败按 KMD 真实终态处理，不提前回收。

Graph 每次 replay 使用新执行 ID，仍 LIVE 的 allocation 保持原身份；失效的捕获指针不能靠反查新 allocation 自动修复。copy/memset 的字节状态和 pointer metadata 传播分别按所选检查器处理。模块卸载、context 销毁及失败任务均需要保留资源直至真实终态。

### 6.1 自研 GPU 的统一检查模式能力协商

**请求的保证先于表示策略。** B1/B2 都是当前地址检查；D1/D2 是额外语义要求，不能为了可运行而静默降级。Manifest 必须同时说明插桩入口、opcode/空间、来源和身份闭合范围、ABI、失败出口与允许的回退；它可由 Compiler 产生，纯 C 则由 Loader/Rewriter 根据已验证的 Binary 覆盖生成，不强制修改生产 Compiler。

~~~cpp
CheckPlan selectPrimaryGpuChecker(TargetGPU gpu, RegistryCaps reg,
                                 Manifest m, Requirements required) {
  // Manifest展开已支持的D1/D2增强与插桩入口；下列只是基础表示选择。
  for (CheckPlan p : enumerateDeclaredPlans(m,
       {B2_SHADOW, B1_CURRENT_LIVE_INTERVAL, D1_ORIGINAL_OBJECT_BOUNDS})) {
    if (!p.providesEvery(required) || !gpu.canExecute(p)) continue;
    if (!reg.canPrepareResidentMetadata(p) || !gpu.hasVerifiedFailureExit(p)) continue;
    return p; // 实际资源分配/发布失败仍在launch阶段拒绝
  }
  return UNSUPPORTED;
}
~~~

E 中原来源未知时只可采用事先声明允许的较弱保证，并报告相应覆盖缺口；C fallback 不会恢复 D1/D2 历史信息。运行期出现 UNKNOWN 不得按严格模式的成功结果放行。性能报告必须标出所用计划、覆盖和失败策略，不能拿较弱检查的耗时冒充同等保证的加速。

## 7. KMD：GPUVA/BO、特权资源、提交、错误与终止（B/C/D/E/F）

**修改位置**：只补已有 KMD 缺失的权限/映射、资源驻留、提交依赖、完成/fault、运行中 abort/reset。已有契约充分时新增代码可以为零，仍需核验真机行为。KMD 不负责推导 LLVM 来源，也不自动知道 UMD 池内边界。

~~~cpp
SubmitResult ioctlSubmitSanitized(Context& ctx, UserSubmission in) {
  PinnedSubmission s = validateAndPinOwnedCodeDataMetadataAndReport(ctx, in);
  if (!s.valid || !validateExecutableResourcesAndDependencies(s)) return REJECTED;
  // 先关联上下文/launch/报告，再允许硬件执行，避免快速完成先于登记。
  Job j = createRegisteredJob(ctx, s, in.launchId, in.moduleBuildId);
  return enqueueOrRollbackJob(j);
}
void onGpuFault(Job& j, Fault f) {
  j.recordFaultAndBlockNewDependentWork(f);
  j.requestVerifiedAbortOrReset(); // 只是请求，不发布可回收的完成fence
}
void onGpuTerminalProof(Job& j, TerminalEvidence e) {
  if (!e.provesNoFurtherAccessByJob) return j.keepResourcesPinnedAndEscalate();
  publishTerminalFence(j, e.status, e.reportVisibility);
  deferReleaseUntilReportConsumersFinish(j);
}
~~~

queued cancel、正在运行的 dispatch/context abort 和 device reset 是不同能力。发出 abort 请求或看到 fault 不能立即释放 BO/metadata；只有确认后续 GPU 访问已停止才进入回收。没有严格失败出口的平台须在启动前拒绝对应模式；运行中异常导致无法确认终止时要报告设备/工具失败并保留资源，不能用一个名为 quiesce 的空动作宣称成功。

终态 fence 与报告 cache 可见性也需分别满足；Host 需进行目标要求的同步后读取。Linux [dma-buf/fence](https://docs.kernel.org/driver-api/dma-buf.html)和[DRM reset](https://docs.kernel.org/gpu/drm-uapi.html#device-reset)是职责及顺序的参考，不证明自研驱动已有同等 abort 精度。Racecheck 只能使用应用真实同步事件建立 HB，不能把所有 KMD 提交时间顺序当成 HB。

## 8. Firmware：设备故障上报、上下文状态与恢复（F及依赖终止的路径）

**条件性修改边界**：仅当固件实际负责 fault ring、context abort、TLB/metadata 失效或上下文保存时修改；已有 KMD/RTL 完成的功能不重复开发。固件须按真实硬件状态区分“收到请求”“停止发射”“在途访问已结束或取消”“终态已确认”，并将报告可见性或报告丢失状态传给 KMD。

停止发射不等于已停止所有访存；遇到不可排空事务不能无限等候后假报完成，须走平台已有的恢复/设备失败契约。reset 影响其他任务时应将实际影响范围和失败状态逐一关联。所有行为都需要真实设备验证；Host 合成故障只能检查软件处理分支。

## 9. RTL/实体 GPU MMU/Tag/Bounds（仅真实硬件已有或另行实现 F）

**本节仅保留 F1 Tag/F2 Bounds 的既有接口与预算边界。** 实际芯片未提供相应功能时为 UNSUPPORTED；软件 helper、GPUVA 位宽空余或 SDK feature 名称都不能证明硬件检查存在。F 软件预算不包含另立项的 RTL/DV/硅验证，正式数值仅见[预算 JSON](estimates-v1.8.json)。

若未来具备真实机制，需逐项提供 ISA 检查点及阻止非法访存的语义、metadata/tag 生效与失效、atomic/DMA 覆盖、上下文保存、fault 字段和终止范围。Tag 比较不自动等于无碰撞的 Allocation ID/Epoch；只按当前地址查 Bounds 也不自动得到 D1 来源。本文不据此增加候选算法、实测性能或当前芯片能力。

## 10. CLI/符号化、自动化与真实 GPU 验收

下列是**证据判定逻辑**，不替代现有运行器的字段名称；CUDA/HIP 实际状态映射和命令见[GPU CI 指南](gpu-actions-ci.md)及[samples manifest](../samples/manifest.json)。

~~~python
def evaluate(target, case, checker):
    if not physical_gpu_available(target):
        return "GPU_NOT_RUN"
    if not query_physical_gpu_capabilities(target).supports(checker):
        return "UNSUPPORTED"
    image = compile_or_patch_actual_gpu_binary(case, checker)
    if not image.ok or not image.has_required_coverage:
        return "BUILD_OR_INSTRUMENTATION_FAILURE"
    result = execute_on_physical_gpu(target, image, case.input)
    if result.timeout or result.device_lost or result.tool_failure:
        return "TOOL_FAILURE"  # 保留可能同时存在的具体设备诊断
    if result.application_failed_without_checker_diagnostic:
        return "APPLICATION_FAILED"
    if result.report_loss or result.coverage_gap or not result.target_check_site_reached:
        return "PARTIAL_COVERAGE"
    if case.is_bad:
        return "GPU_DETECTED" if result.matches_expected_device_error else "GPU_MISSED"
    return "GOOD_NO_FALSE_POSITIVE" if result.completed_cleanly else "GPU_FALSE_POSITIVE"
~~~

错误匹配必须核对设备、checker、类型、PC/访问/对象及实际触发条件，不能只看非零退出码或摘要行。报告截断时可保留已有命中证据，但整个支持集不算完整通过。每例记录设备/驱动/工具链/固件、Build ID、选定计划、输入、原始日志和合法对照；静态 CI 只证明文档/脚本结构。

性能按[主稿 §17](llvm-gpu-sanitizer-research.md)记录首次编译/patch、缓存命中、kernel/端到端时间、metadata/报告峰值、register/scratch/occupancy 和 coverage。当前无目标 GPU 测量，不给预计加速比或“低开销”结论。

## 11. B～F 的选择与组件总结

本表用于分配工作，不重复计数算法；正式 kLOC、测试预算和 KMD 条件增量仅维护于[主稿 §16](llvm-gpu-sanitizer-research.md)及[v1.8 JSON](estimates-v1.8.json)。同一公共对象、报告或资源服务只计一次。

| 责任主体 | B1/B2 | C（NV-R式入口） | D1/D2 | E | 条件性 F |
|---|---|---|---|---|---|
| Compiler | 访存 Pass、目标 ISA 覆盖 | **生产 Compiler 可零改动** | 来源/身份传播、指针 ABI | manifest、跨模式边界 | 仅真实 ISA 所需适配 |
| GPU Device Runtime | interval/shadow、空间检查 | 供 patch 调用的同类谓词 | 原对象/历史身份检查 | 统一 ABI 和错误出口 | 既有硬件接口所需软件 |
| UMD | 对象/metadata 发布、队列、报告 | 加 decoder/rewriter/cache/PC map | 加 sidecar、identity 生命周期 | 共用服务、覆盖协商 | tag/bounds 生命周期接口 |
| KMD | 映射/驻留/完成/失败 | 加合法可执行资源契约 | 同一资源/终态契约 | 只补契约缺口 | 真实硬件上下文/fault接口 |
| Firmware/RTL | 仅实际终止/恢复缺口 | 同左 | 同左 | 同左 | 另立项，不混入软件预算 |

### 实施优先级与可交付证据

| 顺序 | 最大的不确定工作 | 阶段退出证据 |
|---|---|---|
| P＋B Global | checker ISA、metadata 发布、报告与可靠失败链 | 实卡 S01 错例/合法对照，PC/lane/宽度及资源失败分类正确 |
| B 指令/空间扩展 | vector/mask、atomic、Shared/Private、async 语义 | 声明支持的 S01～S28 子集、最终 ISA 清单、真实性能基线 |
| D1 后 D2 | SSA/ABI/copy 闭合、Value＋Identity 原子一致性 | S04、S09、S25/S26 的正反例和明确 UNKNOWN 边界 |
| 按需求 C/E | 全指令译码、重定位、资源重算、混合覆盖 | 合法结果一致、原 PC 可追溯、未知 opcode 拒绝，首次及稳态成本 |
| 条件性 F | 实际芯片能力与外部工程范围 | 先提供硬件准入证据，否则不进入可交付支持集合 |

**完成边界**：以上为目标源码的实现约束和评审入口，未声称编辑或编译了自研 Compiler/UMD/KMD 产品代码。现有 CUDA/HIP 示例与 CI 提供测试输入及执行入口；目标 GPU 检测状态仍为 **GPU_NOT_RUN**，升级状态必须附真实设备原始证据。
