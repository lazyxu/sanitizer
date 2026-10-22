# 自研 GPU Sanitizer：真实硬件实施的逐组件关键代码

**版本：v2.0 · 2026-10-22 · 状态：设计与伪代码，尚未在目标 GPU 上实现或验收。**

本文件只讨论能够随应用代码在**实体 GPU 执行**的设备检查器，或由实体 GPU 已具备的 MMU/Tag/Bounds 机制执行的检查。软件责任以 Compiler、KMD、UMD 三组计费；Device Runtime 虽由工具链或 UMD 交付，其检查指令**实际在 GPU 执行**。设备固件、RTL/硬件设计、CLI/测试作为跨组件专项，不能混为编译器功能。

正式交付形式分为**B（LLVM设备地址检查：B2 Shadow或B1当前Live区间）、C（UMD运行时二进制重写）、D（D1原对象Bounds/D2指针时间身份）、E（B/D/C混合集成）、F（真实Tag/Bounds硬件具备时的条件性路线）**。**原I归B1；软件Tag归D2，F1/F2必须有真实芯片功能**。单独 Initcheck、Racecheck、Synccheck 不可等价为一次 AddressSanitizer 检查。

**这是B/C/D/E/F按组件分工的表，不是5种新的基础算法。** [核心设计对照](core-design-comparison.md)规定：①NV-R运行时Binary＋当前地址，②AMD-B2编译期Shadow，③NV-C编译期Original-Object Bounds；NV-C和本文C标签不是同一概念。D2是时间安全扩展；E组合B/D/C，不单独定义内存谓词；F需要真正Tag/Bounds硬件。Intel/MUSA的能力说明不构成第4种参考算法。

## 1. 检测执行与 Host/Device 信任链

~~~text
Host 应用 / SDK
    -> UMD / Host Runtime: 分配、逻辑对象、队列、事件、模块、覆盖清单
    -> KMD: BO/GPUVA/权限、提交与依赖、完成/fault、context/abort
    -> 实体 GPU: LLVM插入的GPU ISA检查指令 / UMD改写后的GPU ISA / 硬件检查
         -> GPU Device Runtime: shadow/bounds/identity/报告写入
    <- KMD: 硬件完成或真实设备写入的报告缓冲区
    <- UMD/CLI: 原始 PC、线程、对象、source map、覆盖/失败状态
~~~

只有设备端真实命中的访存/故障和可归因的运行日志才可升级为 GPU_DETECTED；无 GPU 硬件时应保持 GPU_NOT_RUN。任何缺少代码生成、资源映射、异步可见性、可靠终止的一环，都必须返回 UNSUPPORTED / PARTIAL_COVERAGE / TOOL_FAILURE，不能静默返回 clean。

## 2. Compiler：LLVM IR Pass + 后端最终访存审计（B/D/E；纯 C 可零修改）

**修改位置**：Clang/LLVM GPU target sanitizer 选项，IR/MIR pass 注册，目标地址空间/指针表示，instrumentation lowering，机器指令覆盖表，DebugLoc 与 module capability manifest。IR 阶段插入检查只是必要条件；最终 ISA 必须覆盖原子、宽向量、predication、masked/EVL、异步/特殊 intrinsic。

~~~cpp
// 伪代码：在具备地址空间/有效元素信息的阶段运行
bool instrument(MemoryOp op, GPUFunction fn) {
  Access a = classify(op);             // load/store/atomic/memcpy/image/async
  if (!a.supported) {
    fn.manifest.addUnsupported(op.opcode(), a.reason);
    return false;
  }
  Predicate active = laneActive() & a.elementMask & a.EVLMask;
  // masked-off 元素不能求值为真实访问，也不能被checker额外访问
  for (AccessPart p : lowerToActualByteFootprints(a)) {
    IRBuilder b(op);
    b.insertBefore(op, callGpuDeviceChecker(
        p.address, p.nbytes, a.addressSpace, p.requiredAlignment,
        active & p.predicate, provenanceToken(op), op.debugLoc()));
  }
  fn.manifest.recordInstrumented(op.id(), a.opcode(), a.addressSpace);
  return true;
}
// 后端验证：每条最终机器访存应对应检查点或显式不支持项
for (MachineMemOp m : finalizedISA(fn))
  require(checkCoverage(m) || manifest.hasUnsupported(m));
~~~

**D1/D2 特别注意**：原对象 token 必须在 GEP/phi/select/参数/函数调用和被支持的 spill/reload 中传播；不能在 dereference 时通过当前地址反查对象来冒充历史来源。D2 对包含指针的存储、复制和原子发布，需要约定指针值与身份元数据的一致性；不支持的 ABI 用 unknown 标记。

## 3. GPU Device Runtime：实际 GPU ISA 检查 + 稳定报告（B/C/D/E）

**修改位置**：专用设备检查库 ABI、设备端 shadow/descriptor 索引、报告环形缓冲、无递归元数据访存标记、SIMT 安全错误路径。Device runtime 调用必须成为真实 GPU 可执行代码而非 Host 回调。

~~~cpp
// 在GPU设备上执行的伪代码：逐有效lane/元素调用
CheckResult san_check(DevCtx& ctx, gpuva addr, uint64_t n,
                      Space space, Token provenance, bool active) {
  if (!active || n == 0) return OK;
  if (n > MAX_ACCESS || addr > GPUVA_MAX - (n - 1))
      return fail(ctx, RANGE_OVERFLOW, addr, n);
  if (!ctx.metadataReady || !ctx.reportBufferReady)
      return toolFailure(ctx, METADATA_UNAVAILABLE);
  if (space == GLOBAL && !shadowBytesAccessible(ctx, addr, n))
      return fail(ctx, OUT_OF_BOUNDS, addr, n);
  if (ctx.boundsEnabled && provenance.known &&
      !withinOriginalObject(provenance, addr, n))
      return fail(ctx, ORIGINAL_OBJECT_BOUNDS, addr, n);
  if (ctx.identityEnabled && provenance.known &&
      !sameAllocationGeneration(provenance, ctx.objects))
      return fail(ctx, USE_AFTER_REUSE, addr, n);
  if ((ctx.boundsEnabled || ctx.identityEnabled) && !provenance.known)
      ctx.coverage.markUnknownProvenance();
  return OK;
}

CheckResult fail(DevCtx& ctx, Error kind, gpuva addr, uint64_t n) {
  atomicOr(ctx.report.header.errorFlags, 1);
  uint64_t slot = atomicFetchAdd(ctx.report.header.nextSlot, 1);
  if (slot < ctx.report.capacity)
    ctx.report.items[slot] = {kind, addr, n, currentPC(),
                              blockId(), threadId(), laneId(), ctx.launchId};
  else
    atomicFetchAdd(ctx.report.header.droppedReports, 1);
  return MUST_ABORT_DISPATCH; // 设备端必须具备经过验证的终止契约
}
~~~

**错误时不能继续执行非法访存**。必须在实际 GPU 上证明 dispatch/context abort 可靠、其他 lane 不会悬挂在 barrier；否则该模式启动前应拒绝。不能仅让一条 lane return；不能把日志空间不足误报为程序 clean。Shadow 1:8 的可表示性限制、非连续有效字节以及 suballocation，须按对象描述符或更细粒度方案补充，不虚报保证。

## 4. UMD DebugAllocator / ShadowManager：对象真值与队列依赖（B/D/E）

**修改位置**：逻辑 allocation、pool suballocation、external import、GPUVA 映射关联、Shadow/metadata BO、poison/unpoison、对象版本与 retirement。KMD 提供 backing 的实际状态，但不自动知道 UMD 池内子分配。

~~~cpp
GpuAllocation sanMalloc(Context& c, uint64_t logicalBytes, Queue& q) {
  Backing bo = c.driver.allocBacking(roundUpForDeviceAllocation(logicalBytes));
  Allocation o = c.objects.create(
      /*id*/ nextId(), /*epoch*/ nextEpoch(), bo.gpuva,
      logicalBytes, bo.backingBytes, c.owner);
  MetadataOp init = c.shadow.buildPoisonPlan(
      bo.gpuva, logicalBytes, bo.backingBytes);
  Fence f = q.submitMetadata(init, /*after*/ bo.mapReadyFence);
  c.objects.publish(o, f);             // 未就绪前不可被checker消费
  return {o.gpuva, o.id, o.epoch, f};
}

Fence sanFreeAsync(Allocation o, Queue& q, DependencySet lastUses) {
  require(o.owner == currentOwner());
  require(o.state.compareExchange(LIVE, RETIRING));
  Fence barrier = q.waitFor(lastUses);  // 跨queue也需显式依赖
  Fence poisoned = q.submitMetadata(shadowPoison(o), barrier);
  Fence retired = q.submitRetire(o.backing, poisoned);
  c.objects.markDeadWhenComplete(o.id, o.epoch, retired);
  return retired;
}
~~~

**可见性与 UAF 保证**：free API 返回不等于 GPU 上失效立即生效；对象地址复用需保留此前所需 identity 历史直到所有 checker 消费结束。B的Poison只在已完成发布且设备可见的生命周期窗口生效，地址复用后旧裸指针与新裸指针同值时不能宣称具备 D2 保证。

## 5. UMD Runtime Binary Rewriter：独立于生产 Compiler（C/E）

**修改位置**：模块加载/JIT 路径、目标 ISA decoder/encoder、CFG/relo、branch target 与 PC 映射、寄存器/栈/scratch/resource 重新计算、代码缓存和失败覆盖。**C 纯运行时方案不要求修改生产 LLVM Compiler，也不要求应用重新编译。**

~~~cpp
PatchedModule patchOnLoad(Binary original, UmdDevice dev) {
  ISAProgram p = rewriter.decode(original);
  if (!p.valid || !allGpuMemoryInstructionsClassified(p))
    return rejectUnsupported(original, "UNKNOWN_OPCODE");
  for (Instruction& i : p.instructions)
    if (i.isGpuMemoryAccess())
      rewriter.insertBefore(i, makeGpuSanCheckStub(
          i.addressExpression, i.activeLanePredicate,
          i.elementMask, i.byteWidth, i.memorySpace, i.pc));
  rewriter.fixupCFGAndRelocations(p);
  rewriter.recalculateRegistersScratchBarriers(p);
  Binary patched = rewriter.encodeAndVerify(p);
  dev.validateExecutableResources(patched);
  return dev.loadSignedOrAuthorizedExecutable(
      patched, originalToPatchedPcMap(p));
}
~~~

译码可采用 UMD 内独立实现，或只读复用已经存在的 MC 库；“复用工具链库”不等于“修改生产 Compiler”。无法透明改写的指令与闭源模块应拒绝或报告明确覆盖缺口，不得将仅拦截 API 当成 GPU 访存已被拦截。必须对分支、barrier、atomic 和谓词进行真机合法结果对照。

## 6. UMD/Host Runtime：Dispatch、Graph、模块能力与报告（B/C/D/E/F）

**修改位置**：SDK 开关、module manifest、kernel 启动参数 sidecar、队列/事件/graph、copy/memset、JIT/插件、报告与符号化 ABI、跨模块回退协商。

~~~cpp
LaunchResult launchChecked(Kernel k, Args args, Queue q) {
  Mode m = chooseSupportedMode(k.manifest, q.deviceCaps);
  if (m == UNSUPPORTED) return failLaunch("COVERAGE_UNSUPPORTED");
  DeviceMetadata meta = publishMetadataWithDependencies(
      q, objectRegistrySnapshot(args), k.manifest);
  ReportBuffer log = allocateReportBuffer(q.context);
  if (!meta.ready || !log.ready) return failLaunch("TOOL_RESOURCE_FAILURE");
  Fence run = q.submit(k.binaryFor(m), withSidecar(args, meta, log),
                       deps = {meta.fence, args.lastWriterFences});
  Completion result = q.waitCompletion(run);
  return decodeReport(result, log, k.pcMap, k.manifest.coverage);
}
~~~

**不漏报要求**：UNSUPPORTED、partial、报告丢失、tool OOM/timeout 与干净运行分开；图捕获/replay 每次带执行 ID，不把创建时的对象身份自动沿用至未来执行。

## 7. KMD：GPUVA/BO、特权资源、提交、错误与终止（B/C/D/E/F）

**修改位置**：仅补现有 KMD 确实缺失的 GPUVA metadata 映射、提交依赖、可执行资源验证、context 关联 fault/report、真实 GPU job completion/abort/reset。已满足同一契约时对应 KMD **新增代码可以为零**。

~~~cpp
int ioctlSubmitSanitized(Context* ctx, UserSubmission in) {
  if (!ownerCanAccess(ctx, in.codeBo, in.dataBos, in.metadataBos))
    return -EACCES;
  if (!validateExecutable(ctx, in.codeBo, in.resourceDeclaration))
    return -EINVAL;
  if (!validateGpuMappingsAndPermissions(ctx, in.metadataBos, in.reportBo))
    return -EINVAL;
  Fence* f = gpuSubmit(ctx->queue, in.codeBo, in.dataBos,
                       in.metadataBos, in.reportBo, in.waitFences);
  if (!f) return -EIO;
  associateLaunchReport(ctx, f, in.launchId, in.moduleBuildId);
  return exportFence(f);
}

void onGpuCompletionOrFault(GpuEvent e) {
  Context* ctx = locateContext(e.contextId);
  publishFenceOutcome(ctx, e.launchId, e.errorCode);
  if (e.requiresAbort && hardwareSupportsContextAbort())
    requestContextAbort(ctx);       // 正在运行的dispatch与queued cancel不同
  else if (e.requiresAbort)
    markUnsupportedAndQuiesce(ctx); // 不声称已成功精确终止
}
~~~

KMD 不承担 LLVM 指针来源推导、不自动识别池内对象边界，也不能只根据 Kernel 提交顺序推出设备端内存 HB。驱动代码与固件/硬件特权机制必须有明确能力门槛。

## 8. Firmware：设备故障上报、上下文状态与恢复（F及依赖终止的路径）

**修改位置**：如果目标固件承载 context abort、fault ring、TLB/metadata invalidation 或安全退出，则按已实现硬件 ABI 增加对应消息与状态机。若该逻辑已在 KMD/RTL 完成，不必为了 Sanitizer 重写固件。

~~~c
// 设备固件伪代码，不代表现有芯片已提供以下动作
void handleSanitizerFault(HwFault f) {
  FaultRecord r = {f.context, f.dispatch, f.pc, f.address,
                   f.accessBytes, f.reason, f.laneMask};
  writeFaultRecordBeforeCompletion(r);
  if (capability(SAFE_CONTEXT_ABORT)) {
    stopIssuing(f.context);
    drainInFlightMemoryOps(f.context);
    flushReportWrites(f.context);
    signalAbortedCompletion(f.context, f.dispatch);
  } else {
    signalFatalUnsupported(f.context);
  }
}
~~~

必须验证报告写入和完成通知的设备可见性顺序。未实现的硬件/固件行为不能靠 Host 侧异常假装满足。

## 9. RTL/实体 GPU MMU/Tag/Bounds（仅真实硬件已有或另行实现 F）

**F1 Tag / F2 Bounds**：需要 ISA/RTL/metadata SRAM 或可达内存、同步失效、context 保存恢复、cache/TLB 一致性及可诊断 fault；在目标硬件没有相应机制前标为 FUTURE_HARDWARE / NOT_SUPPORTED，不能把软件 helper 命名成硬件检查。

~~~systemverilog
// 示意：位于实体GPU访存流水线、提交访存之前
always_comb begin
  san_fault = 1'b0;
  if (mem_req.valid && mem_req.enabled_byte_mask != '0) begin
    if (caps.bounds_supported && req_outside_object_bounds(
        mem_req.address, mem_req.active_bytes, meta.base, meta.limit))
      san_fault = 1'b1;
    if (caps.tags_supported && !tag_generation_match(
        mem_req.ptr_tag, meta.alloc_tag, meta.epoch))
      san_fault = 1'b1;
  end
end
// fault时阻止该指令真正写内存并按已定义GPU前进性契约终止/恢复
~~~

**硬件验收**：在硅片上核验检查开启/关闭、跨页与同页边界、alias/peer、context 切换、失效后重复地址、并发原子、异常记录和最坏路径开销；设计评审或 RTL 仿真成功不等于实体 GPU 上已经部署。

## 10. CLI/符号化、自动化与真实 GPU 验收

~~~python
# 伪代码：目标真实GPU设备执行，错误和工具失败是不同状态
def evaluate(target, case, checker):
    caps = query_physical_gpu_capabilities(target)
    if not caps.supports(checker):
        return "UNSUPPORTED"
    image = compile_or_patch_actual_gpu_binary(case, checker)
    assert image.manifest.has_required_instruction_coverage(case)
    result = execute_on_physical_gpu(target, image, case.input)
    if result.tool_oom or result.report_overflow or result.unknown_coverage:
        return "TOOL_FAILURE_OR_PARTIAL"
    if case.is_bad:
        return "GPU_DETECTED" if result.has_expected_device_error else "GPU_MISSED"
    return "GOOD_NO_FALSE_POSITIVE" if result.clean else "GPU_FALSE_POSITIVE"
~~~

每个 S01～S28 必须有坏例、修复合法例、必要的 unsupported 情况；保留设备型号、编译器版本、驱动/固件版本、目标 ISA、原始日志、模块和 PC 映射、覆盖 manifest，以及 baseline 对比。宿主静态 CI 可证实文档/脚本一致，不能宣称目标设备检查成功。

## 11. B～F 的选择与组件总结

| 工程组件 / 责任主体（非5种独立算法） | B2 Shadow／B1区间 | C（NV-R式Binary） | D1（NV-C原对象）／D2 | E 混合集成 | F1/F2条件性硬件 |
|---|---|---|---|---|---|
| Compiler/LLVM GPU | 访存 Pass、实际 ISA 覆盖 | **生产 Compiler 零改动可行** | provenance／指针身份传播 | 插桩、manifest、跨模式 ABI | 新指令/intrinsic（有硬件时） |
| GPU Device Runtime | Shadow check、report | patch stub 调用设备 checker | Bounds／身份 check、pointer metadata | 统一 ABI，防重复检查 | 必要的软件元数据与报告 |
| UMD DebugAllocator | 对象元数据/Poison | 资源/生命周期表 | 对象 id/epoch、sidecar | 共享对象服务 | Tag/bounds 初始化与失效 |
| UMD Binary Rewriter | 不要求 | **必要**：decode/patch/encode | 可选受控补充 | 未覆盖模块的 fallback | 通常不要求 |
| UMD/Host Runtime | launch/async/report | 模块加载与 patch cache | 参数、graph、外部 ABI | 按模块协商等级 | 模式开启和硬件报告 |
| KMD | metadata BO/GPUVA/complete | 可执行代码权限/映射 | metadata/context/retire | 前述契约并集 | context/fault/硬件元数据控制 |
| Firmware | 按实际 abort/fault 缺口 | 同左 | 同左 | 同左 | 新异常/失效/重置状态机 |
| 实体 GPU 硬件 | 执行插桩指令 | 执行改写指令 | 执行 Bounds/identity 代码 | 执行由B/D/C组合的检查 | **实际Tag/Bounds硬件** |
| CLI/CI | 诊断/覆盖 | 原 PC 映射和拒绝标记 | 来源未知、epoch边界 | 覆盖等级汇总 | 实际 hardware feature 检测 |

### 实施优先级与可交付证据

| 阶段 | 关键修改 | 退出证据（必须真实设备） |
|---|---|---|
| P0：基础契约 | Compiler manifest、Device Runtime 报告、UMD metadata、KMD 映射与完成契约 | 真机能读到检查器产生的报告，资源故障不假 clean |
| P1：B Global 首例 | Global load/store 检查、Shadow、poison/alloc、SIMT 失败路径 | S01 头尾越界报错、合法访问不误报，PC/lane/宽度正确 |
| P2：B 完整化 | atomic/vector/shared/private、async、覆盖矩阵 | 支持集合内 S01～S28 对应子集和性能基线 |
| P3：D1→D2 | 原对象 Bounds、epoch/身份、atomic pointer一致性 | S04（live B）、S09（复用旧指针）、S25（原子合法反例） |
| P4：E/C | UMD Binary Rewriter、跨模块协商、JIT fallback | 真机合法结果一致、原 PC 归因、未知 ISA 明确拒绝 |
| P5：F | 实际 ISA/RTL/KMD/固件及芯片支持 | 实卡 feature/fault/上下文/性能记录；硬件缺席时 UNSUPPORTED |

**状态声明**：以上均为需要实现的关键修改位置和工程伪代码，不是声称已经编辑目标 Compiler/UMD/KMD/RTL 源码、编译通过或得到真实 GPU 检测率。当前仓库中的 CUDA/HIP 示例与 CI 只提供测试输入和执行入口，实际硬件验收状态须以原始运行结果更新。
