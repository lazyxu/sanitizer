# 自研 LLVM GPU sanitizer：架构与工程量工作笔记

> **2026-10-21产品准入修正：本项目仅保留B/C/D/E/F这些检查逻辑能够在实体GPU执行或由真实硬件实施的产品候选。旧“模型内直接检测”路线已撤销；cmodel/QEMU只能用来验证真实GPU检查指令或模拟真实硬件语义，不再单独报价为产品检测器。**
> **纯C运行时二进制插桩不要求修改生产Compiler；ISA Decoder/Encoder/重写工具由UMD独立Runtime Binary Rewriter维护，可选只读复用MC。归档中原C Compiler栏是历史交付包，不等于编译器强制修改。**

> **研究过程记录，非最终规范。** 本笔记保存早期调研内容，可能包含未更新的候选结论。正式判断以[主稿](../llvm-gpu-sanitizer-research.md)为准。

> 本文件是给主文档作者的中间研究笔记；所有架构取舍与 kLOC 均为工程判断，不能表述为 LLVM、AMD、NVIDIA 或其他厂商的现成实现规模。研究日期：2026-10-20。公开 LLVM 文档是滚动版，真正实施前必须冻结自研 fork 的 commit 并逐项确认。

## 1. 范围、前提与必须保留的结论

估算前提：已有能运行内核的 LLVM backend、assembler/disassembler、目标 ELF/代码对象装载、device call ABI、UMD 内存与队列管理、可修改的功能 cmodel、基本调试信息和自动化测试基础设施；sanitizer 本身从零开始。重点是 GPU 内存空间的空间与生命周期错误。数据竞争、未初始化读、同步合法性可共享事件，但其检测算法不是 ASan 的免费附带功能，须另立预算。

可用能力应分四层，不可把它们统称为“全内存安全”：

1. **映射/权限合法**：GPUVA 是否映射，页/segment 是否允许此次访问。
2. **地址可访问**：该字节此时是否属于任何 live allocation，是否落入 poisoned redzone 或 free 区间。
3. **对象 bounds**：这次访问是否仍在产生此指针的原对象允许范围内；跳到另一个 live object 也可被判错。
4. **temporal provenance**：指针所携带的 allocation identity/epoch 是否仍匹配其原对象；必须能识别同地址复用。

纯地址表、shadow 与 cmodel 的物理访问可见性都不会自动产生第 3/4 层。一个内存事务访问了合法页，并不证明它没有跨越 C/C++ 数组、slab 子分配或 stack object 的边界。子对象边界又比 allocation bounds 更严格，须有前端或显式 API 提供语义，不能从地址反推。

## 2. 真实GPU可部署的五类方案：组件修改面

| 路线 | 主要检测位置与新增工作 | 能力边界 | 适合用途 |
|---|---|---|---|
| B LLVM ASan 式 shadow | IR 内存访问插桩、target address-space/mask lowering、device shadow 查询、UMD poison/unpoison 与 redzone/quarantine | 基于 addressability；不保证任意跨对象跳跃或地址复用后的旧指针；共享/私有空间不能照搬 global 线性映射 | 有源码的日常测试主线、较低 ABI 改动 |
| C runtime binary patching | 模块装载时 decode、插入 check、寄存器/谓词保存、代码重定位与 PC 映射；独立 code cache；host patch policy | 可覆盖无源码二进制的已支持 ISA 访存，但没法恢复被丢失的 source object 与 pointer provenance；不能把“不认识的 opcode”视为已覆盖 | 第三方库、JIT 产物、无法重编译模块的补洞 |
| D object bounds + temporal provenance | 指针 identity/epoch 与 bounds 的编译器传播；pointer shadow 或变更指针表示；跨 call/memory/atomic/ABI 的一致性 | 支持链路内可以识别跨 live object 与复用后旧指针；整数伪造、外部 ABI、未知布局必须有严格语义或公开降级 | 精度需求高、可控制编译闭包的模块 |
| E hybrid | 本预算定义为 B 主线 + D 受控模块 + C 有限 fallback，公共生命周期、报告与 coverage manifest；cmodel 做对应语义验证 | 每条访问必须标明当前保证等级；C fallback 不获得 D 的身份能力；不允许模式切换时静默“重新赋予当前对象身份” | 可持续产品路线，逐步把高价值链路升级为 D |
| F hardware assisted | 若 ISA 已有 tag/bounds 支持：编译器 emit、allocator/tag lifecycle、UMD context、异常上报、cmodel 模型；若没有则另有硬件项目 | 随机小 tag 存在碰撞；capability/descriptor 精度由 ISA 决定；raw/untagged/DMA 路径仍可能绕过 | 下一代架构协同或已有硬件能力的使能 |

### C路线职责纠正（纯Runtime不要求修改Compiler）

旧预算表按“Compiler/Device RT/UMD”交付包分组，其中C的Compiler 0–2 kLOC和Device RT 6–11 kLOC只表述历史归属，不表示生产编译器必须修改。**纯运行时路径实际Compiler源码改动=0**，目标ISA decode/encode、分支重定位、opcode语义、Patch Stub生成和资源重算均可在UMD独立Runtime Binary Rewriter中实现；已有MC库只可选复用，不能作为强制前提。预算的重新归属见[正式§16.5a](../llvm-gpu-sanitizer-research.md)和[纯runtime责任表](../estimates-v1.6.1-runtime-C.json)。

**模型辅助测试边界：** cmodel已有GPU ISA执行能力可验证B/C/D/E设备检查指令，但模型独立的指令访存观察/安全判断不会运行在真机。有关模拟器事件tap的历史文字已撤下；调试人员如需检查模型正确性可按[正式§9](../llvm-gpu-sanitizer-research.md#hardware-deployment-gate)使用参考oracle，不能将它列为产品候选。

### B：ASan 式 shadow 的实际移植点

现有 LLVM ASan 可借鉴 IR 插桩、全局对象与栈布局处理、report call、属性控制与 ignorelist。官方源码确有 AMDGPU 特殊路径，但当前 `isUnsupportedAMDGPUAddrspace` 排除 AS3/AS5，且 `FunctionStackPoisoner` 对 AMDGPU 禁用其 `PoisonStack`。这直接说明“已有 GPU 分支”不是 shared/private/stack 开箱即用。来源 R3；主文档需核对自研 fork。

新增工作包括 target triple/feature/driver 选项、mapping 策略、generic 地址分类、device ABI、host 分配事件序列化、红区/隔离、stack/shared 的独立布局、后端新增内存操作审核、二进制与报告符号化。原 CPU `compiler-rt` 依赖进程初始化、host allocator 与 OS 的部分不能机械照搬到 device。

### C：runtime patching 的工程重点

**生产Compiler保持零改动**；UMD内Runtime Binary Rewriter在模块载入/JIT finalize时生成独立instrumented code object，在派发前完成切换；原始模块保留。不要在线改写正在执行的 kernel。缓存 key 包含原始 binary hash、ISA/stepping、插桩 ABI、coverage policy 和 patcher 版本。

patcher 要维护 opcode 分类表、访存宽度及地址计算、原条件码/执行 mask、scratch register 分配、call/return 与 branch relocation、跳转表、PC-relative 常量、indirect targets、代码 alignment/branch range、scoreboard/wait/dependency、spill 区安全性，以及原 PC↔新 PC 双向映射。错误保存/恢复 EXEC、SCC/VCC 或目标等价状态会引入难以察觉的语义变化。

atomic 原操作必须仍是一次原子的操作；先检查其目标地址，然后原样执行。不能拆成 load + store。带返回值的 atomics、失败 compare-exchange、部分 predicate、异步访存与 tensor descriptor 都要有独立支持声明。未知指令应在严格模式拒绝 instrumented load，兼容模式明确列为未覆盖，不能默默绕过后仍报告“全覆盖”。

### D：身份设计与 ABI 工作量

推荐抽象状态为 `PointerValue = {address, allocation_id, epoch, optional_bounds}`。bounds 可以从 `(allocation_id, epoch)` 的只读描述符获取，也可随指针缓存；核心是身份跟随指针，不能只跟随地址。每次有效的非零访问验证：

```text
descriptor = lookup(pointer.allocation_id)
require descriptor.live && descriptor.epoch == pointer.epoch
require address >= allowed_base
require size <= allowed_end - address     // 已先验证 address <= allowed_end
```

避免直接用 `address + size <= end` 而不检查无符号溢出。one-past pointer 的产生与比较可允许，非零解引用不允许；零长度 memcpy/memmove 的语义另按 LLVM 定义处理，不要因无实际字节访问而强制检查一个未访问地址。

GEP/phi/select/call/return 保存身份；new/suballoc 产生新 ID/epoch；free 使原身份失效。对象表的 epoch/state 更新本身要有一致快照，generation wrap、对象 ID 再利用、描述符回收必须设上限或采用隔离/宽 epoch。若要 subobject bounds，需前端约定何时收窄、如何处理 union、尾数组、container_of 等，不得把 allocation bounds 标签写成任意语言 subobject 精确。

指针写入内存后，metadata 必须随 load/store、结构拷贝、memcpy/memmove、overlap、零散字节拷贝保持一致。LLVM 普通调试 metadata 不是可执行指针状态的存储机制，优化可以丢失它；应使用显式 SSA 值/可见 intrinsic/指针外部状态等能表达语义的机制。新版 LangRef 对 non-integral pointers with external state 专门规定了相关 load/store 与内存拷贝限制，参见 R5，但把目标改成这类指针会扩大 backend、优化、ABI 与序列化兼容工作面。

外部 ABI 三种可 review 方案：

1. 在编译闭包内使用隐藏 metadata 参数与返回值；跨 kernel launch 的 host stub 传入每个参数的身份描述符。公开 kernel 参数数据布局保持兼容需要 descriptor/launch 协议配合，不能单靠 LLVM backend 私自追加参数。
2. 对已知外部函数、allocator、库接口提供 signature/layout adapter。输入/输出都定义 metadata 如何传输，不只在入口检查地址。
3. 对无法保证的边界返回 `unknown_provenance`，严格模式拒绝需要精确证明的访问；兼容模式保留 B 级地址检查并明确报告降级。按当前地址查询给旧裸指针重新授予新 epoch 会掩盖正要检测的 UAF，不是安全的自动修复。

只保留原 64 位数据布局的 pointer-shadow 方案有较好表面 ABI 兼容性，但外部无插桩的 pointer store/copy 不会更新 shadow，仍会丢状态。fat pointer 直接改变 struct/array/varargs/函数指针 ABI，成本更明显。两个方案都必须处理 inttoptr/ptrtoint、整数运算、序列化、device function pointer、回调、inline assembly 与动态链接。

### E：跨模式契约

每个模块携带 `coverage_manifest`：IR/ISA 插桩版本，覆盖的内存空间/opcode，是否有 identity，未知接口策略。loader 先判断 ABI/能力匹配，再选 B/D 或 C fallback。报告里保留实际 mode、unknown provenance 与未覆盖指令计数。

共享 allocation ID、统一生命周期、报告和 PC 归因是复用部分；D 的传播、C 的重定位、跨模式 adapter 是新复杂度。不可拿 B 的地址表在 D 边界假造身份，也不可将 C 的物理地址检查标成 D 的 temporal 证明。

### F：硬件是否存在是第一项门槛

不要因指针是 64 位而实际 GPUVA 是 48 位，就假定高 16 位可用来放 tag。需 ISA 明确保证 address tag 忽略/检查规则、地址转换、canonicality、指针比较与原子表示、descriptor encoding、对所有 load/store/atomic/DMA 的覆盖。没有这些规则，软件必须在每条访存前去 tag，这会重新引入全插桩与外部代码部署限制。

HWASan 设计文档讨论 tag granule、short granules 及随机碰撞。t 位独立均匀 tag 在一次比较中的同 tag 碰撞概率约为 `1/2^t`；同一坏指针和同一 tag 上反复访问是相关事件，不能将重复读次数直接相乘宣称漏检率指数下降。tag 在 granule 内未区分的子对象/尾部仍需 short-granule 或 bounds 扩展。来源 R2；自研硬件设计要另行定义其精度。

## 3. 公共编译器流水线：早期保语义、IR 检查、late 审核

| 层 | 必要改动 | 该层独有价值 | 不能靠该层补回的信息 |
|---|---|---|---|
| Frontend/早期 IR | sanitizer attribute/driver；目标对象与 lifetime 描述；D 的身份初始值、subobject 规则；kernel 参数签名 | 优化消除对象前记录源语义；保留 allocation/lifetime 来源 | 尚未知最终 spill、机器指令拆分和执行谓词 |
| 优化后的 LLVM IR | load/store/atomicrmw/cmpxchg、memintrinsic、masked/VP gather/scatter、target memory intrinsics 插桩；保持 debug site；检查数据流与 effect 属性 | 能看到类型、地址空间、对象关系；比较容易实现非零跨度与溢出检查 | inline asm 与后端新产生的访存不完整 |
| target lowering / MIR（寄存器分配前为主） | mask lowering、generic 分类、专用访存、调用约定、target check pseudo；记录来源/coverage | 可控寄存器分配并保留执行状态；识别最终地址空间 | 大部分源对象/指针来源已被压低 |
| late MIR / MC | 审核 legalization、spill/reload、frame access、libcall expansion 等新增访存；必要的最小补插桩；最终 site→PC map | 发现 IR 插桩之后的覆盖洞；检查最终 opcode 集 | 不能从一个机器地址重新推导 C++ 原对象身份 |

没有普适的“唯一正确 pass 时点”。建议内存检查靠近完成常规优化之后、关键 target lowering 之前；D 的身份传播则需要更早与优化交互。用 target late audit 判定每条最终 memory instruction 来自已验证源访问、sanitizer 自身可信访问、编译器管理 backing-storage，还是未覆盖。对新增 spill 的 backing range 检查有价值，但不等价于恢复所有 source stack object redzone。

内部 metadata/report/shadow 访问须标记并排除递归插桩；其边界也需用独立单元测试和 cmodel 验证。不能将任意带 `nosanitize` 标记的未知用户访问当成已检测。

加入检查会改变函数 memory effects、speculation 与 alias 假设，必须重算或失效相关分析属性。新加控制流不得破坏 convergence token/target reconvergence 约束。仅标 `convergent` 并不提供内存 fence 或 control barrier，LLVM 官方语义明确区分它们，见 R7。

## 4. lane mask、vector mask、poison 与原子的一致性

### 4.1 两级 mask 不可混淆

一个 GPU lane 对应一个逻辑线程；该线程的一条 LLVM `<N x T>` 操作还有 N 个向量元素。对 lane `l` 的元素 `e`，实际检查条件近似为：

```text
access_enabled(l,e) = logical_lane_active(l)
                    && instruction_predicate(l)
                    && vector_mask(l,e)
                    && (e < EVL)              // VP 指令才有
                    && byte_enable(l,e)       // 按目标指令定义
```

元素 mask 不能拿来当 wave ballot mask，wave mask 也不能替代 LLVM vector mask。masked-off 元素不应访问应用内存、shadow 或 pointer metadata；gather/scatter 逐地址处理。连续 load/store 可合并已证明连续有效的区间，但不能以最小/最大地址粗暴包含中间 masked-off 洞。

还要区分目标当前 EXEC 与逻辑 active lane：某些后端会暂时打开 whole/quad wave 执行。官方 AMDGPU 调试文档明确区分 conceptual active mask 与被临时修改的 EXEC（R8）。自研 target 须给出等价概念，否则插入报告 ballot 可能让原本不参与的线程进入检查。

LLVM LangRef 规定 masked-off lane 不访问内存；VP 用 mask 与 EVL 共同决定活动元素，其未激活结果可为 poison。插桩不能把 inactive 的 poison 指针直接用于分支或 shadow load。`freeze` 可阻断 poison 传播，但无差别 freeze 全部输入不等于修复 UB 或恢复原始诊断；首先应保证活动掩码和控制依赖正确。相关事实见 R4/R6。

### 4.2 atomic 两个问题要分别解决

**B/C 地址检测**：check target address，然后保持原 atomicrmw/cmpxchg/load/store 的单次原子操作、ordering、syncscope、volatile 和失败语义。报告/metadata 自身的同步不能擅自扩大/缩小用户操作的语义。与 free 并发时，检查后再执行存在生命周期 TOCTOU，sanitizer 不应宣称把非法并发 free 变成正确程序；需报告生命周期竞争或由 API 依赖约束排除。

**D 的原子指针值**：当地址位与 pointer identity 在分离表里，两个普通 atomic 不构成一个复合原子值。reader 可能拿到新 address + 旧 epoch，或旧 address + 新 epoch；即使 address 位值因 ABA 相等也不能靠重复读原指针判定配对正确。

可行方案必须明确选型：目标支持的加宽原子/宽 CAS 把 representation 和 identity 作为整体；或 atomic 发布一个不可变 descriptor 的 handle，descriptor 内含 address 与 identity，配合安全回收。用锁/版本协议也可建立配对，但所有参与者（包括 host、外部库和 device）必须遵守，且可能破坏 lock-free 性与 SIMT forward progress。普通 seqlock reader 加上未受控的 raw atomic writer 不能保证一致性。不能只写“加一个generation表即可支持atomic pointer”。LLVM Atomic Guide 与 LangRef 是 ordering/原子操作约束的依据，见 R9/R4；上述配对结论为设计推理。

## 5. 1:8 shadow、子分配与 GPUVA

经典 ASan 对齐的 8 字节单元用一个 shadow byte 表示：全可访问、全 poisoned，或前 k 字节可访问。这不是任意 8-bit byte-valid bitmap。官方算法来源 R1。

例：一个 granule 中 `[0,2]` 属于 A、`[3,5]` 为 poisoned、`[6,7]` 属于 B，经典表示无法同时表达这两个有效区间。如果 A/B 都是 live 且紧邻，则该 granule 可全 unpoison，但 A 越界读到 B 不报错；即使改为每字节 validity bitmap，也仍无法证明访问使用了正确对象身份。

因此支持 slab/custom pool 至少需要：子分配 callback；满足 granule 对齐与 redzone 的布局（增加浪费）；或额外精细 bitmap/interval bounds 表。三者只提升 addressability 精度，完整 object provenance 仍需 D。短尾部可以通过经典 prefix 表示；所有跨 granule、未对齐、长向量、跨页的跨度必须覆盖每个实际触达的 granule，不能只查起始字节。原 ASan 算法 wiki 展示了紧凑检查对未对齐部分越界的历史例子；不要据此断言每个当前目标都具有相同 bug，应把它作为自研检查器的验收反例。

**48 位 GPUVA 的算术**：若真正映射完整 `2^48` 字节应用地址空间，1:8 direct shadow 的索引域需 `2^45` 字节，即 **32 TiB 虚拟空间**。这不等于预分配 32 TiB 物理显存。实际 resident payload 大致是 live/poisoned 被触达地址覆盖对应的 shadow，另加页表、页对齐浪费、redzone、quarantine 和元数据。

但“稀疏所以一定可行”也不成立。需要 UMD/KMD 允许保留地址窗口、固定/可重定位 mapping、必要的 page-table 级数、shadow 初始化与驻留、地址互不冲突、所有设备的同一映射策略，以及正确处理不在应用 aperture 的坏地址。CPU ASan 大 VA 的事实不能直接推出 GPU MMU 能用同一方案。

候选 mapping：

- 固定 aperture 的线性 `(addr >> 3) + offset`：最快，但只在明确界定的 application VA 区间成立；先 range-check，防止坏 pointer 导致 shadow 自身 fault。
- 多段映射/页表式 shadow：按 VA 高位查 root，再查 leaf；降低保留窗口需求但多次读取、TLB/缓存依赖和并发更新更复杂。
- 稀疏 backing 的 direct shadow：仍需整个虚拟窗口布局成立。可将全 poisoned 或全合法页映到只读共享默认页，并在更新时按规则拆分；这只是可选实现，必须确认 GPU page mapping 能力。
- 受控 debug allocator aperture：限定 debug 分配到较小 VA 区域以降低首版成本；SVM、imported、peer、device malloc 等区域需显式扩展，不能默认覆盖。

shadow/metadata 对自身必须可访问。nonresident shadow 故障、初始化失败和 application OOB 要分开报告；无 GPU page-fault replay 时，launch 前必须保证所需 metadata 驻留。共享/私有空间采用工作组/线程 key 的独立 shadow，不直接把重用的局部 offset 当成全局唯一地址。

## 6. device runtime ABI 与报告格式

建议定义有版本的 `SanContext`，由 loader/runtime 通过已审计 hidden argument、隐式寄存器或 dispatch descriptor 提供：

```text
SanContext {
  abi_version, struct_size, feature_bits,
  address_space_policy, shadow_or_object_table_root,
  report_ring, report_capacity, overflow_counter,
  module_table, launch_id, context_id, device_id,
  metadata_epoch, failure_policy
}
```

检查入口按目标 calling convention 设计，抽象参数应含 `address, size, access_kind, address_space, site_id, pointer_identity?`；向量/多 lane 版本还包含对应有效 mask。固定波宽向量不宜直接成为跨版本公共 ABI。常见 scalar 小访问可 inline fast path；慢路径 device helper 负责复杂跨度、对象查表及报告。全部 helper 使用 sanitizer 内部保护域，禁止递归。

报告至少含 ABI/version、error class、original module build ID/hash、原始 PC/site ID、新 PC（若 patch）、source/inlining info key、launch/context/device/stream ID、workgroup/thread/lane、实际地址与大小、读写/atomic、地址空间、有效/失败 mask、对象 ID/epoch、base/end、allocation/free 来源与时序、coverage mode、丢报告计数。第一个失败 lane 不应抹掉同指令其他失败 lane 信息；可压缩共因记录并保留完整 fail mask。

report ring 用原子 reserve + 完成标志/release publication；host 需在规定 acquire/同步点读取。队列满时有有界的丢弃计数，不做无界 spin、device malloc、printf storm 或整个 grid barrier。sanitizer report 地址与对象表使用专用、常驻且不对用户 allocator 暴露的区域。

symbolizer 在 host 上做：原始 build ID 校验、original PC↔patched PC、DWARF line table 与 inline stack。source line 是最好努力，优化后多个访问合并或源变量消失不应伪造具体对象名。报告要能离线解析，因此保存模块 identity 与映射版本；通常无须 firmware/KMD 理解 C++ 类型或 DWARF。

## 7. UMD 的异步生命周期与 stream/event 语义

不能把 host API 返回时刻等同于 GPU allocation/free 生效时刻。推荐分离状态：

```text
reserved -> pending_live -> live -> pending_free -> retired -> reusable
```

allocation 建立元数据/清 shadow 操作必须 happens-before 首次合法使用；free 的 poison/epoch retire 应在 API 定义的 device 可见序列点生效；reuse 只有在允许重用的依赖与元数据更新完成后发生。pending_free 不自动意味着“此前已提交、应当先执行的 kernel”全都 UAF。也不能为了避免误报，把 free 延迟到所有队列 idle 后而掩盖真正的生命周期错误。

同 stream 通过既有 FIFO 顺序串入初始化/retire；跨 stream 依据实际 event record/wait、依赖图、graph execution 与 API 的同步语义建立关系。host 读对象表与 GPU 读 shadow 必须有发布/可见性协议。allocator 锁只保护 host 数据结构，不等于向正在执行的 GPU 传播了 poison。

需要单独覆盖：event 复用/generation、graph capture/instantiate/replay、异步内存池与子分配、device-side allocation/free、imported/IPC 句柄、peer access、unified/SVM 主机读写、迁移/remap、context teardown、module unload。imported allocation 的合法范围需要导出方提供可信大小/生命周期契约；仅有 GPUVA 时只可承诺映射级。

无顺序保证的 free/use 是生命周期竞争。工具可记录“未建立合法依赖”的候选问题；如需判定所有并发 race 的因果性，需要 HB 跟踪预算，不应让普通 ASan 查表假装完成通用 race detection。报告保留 command ID、stream ID、allocation/free event ID 有助于解释。

temporal 最小反例：

```text
old = alloc(64); id=17, epoch=4, address=X
free(old)
fresh = alloc(64); id=29, epoch=1, address=X
load(old)  // 地址表看到 X 属于fresh，无法知道操作数来自old
```

若检查输入只有 `X` 和“当前 X 的记录”，合法的 `load(fresh)` 与非法的 `load(old)` 完全不可区分。quarantine 只是延缓地址复用，不能宣称消除此反例；D 必须传入原 `id=17,epoch=4`。

## 8. 错误后的屏蔽、trap 与 barrier 死锁

检查要先于实际非法访存。无恢复模式可先有界写报告，再触发平台定义的 dispatch/context abort。不能用“单个 lane return/trap”代替整体失败：同 workgroup 其他 lane/wave 可能等待它参与 barrier，或在 cooperative grid synchronization 等待它，形成 hang。仅停止整个 wave 也不足以保证同组其余 wave 可进展。

继续模式若跳过非法 store，或给非法 load 返回固定值，必须保持原有线程仍能遵循同步/收敛协议；该执行已被诊断干预，后续结果与 cascade report 不应当作原程序的正确语义。返回 poison 后继续可能被优化推导为 UB 并消除后续行为，不能把 poison 当通用安全填充值。

首版推荐：有界 report + **平台已经保证可终止所有相关参与者的 abort**；若平台只能逐 wave trap，先解决 dispatch cancellation、barrier unwind、completion error 与报告回收协议。若这要求 firmware/KMD 改造，则明确列为使能 gate。软件不应伪造新 KMD 权限来“修复”hang；也不能因存在 watchdog 就声称报告必达。

对于 asynchronous copy，检查不能只在发射 instruction 时查 source/destination：期间 free、completion 与 barrier 关系必须由生命周期协议保证，否则仍有 TOCTOU。目标有 descriptor bounds/保护异常时要记录被硬件抑制的实际 fault，防止软件继续执行覆盖掉首个原因。

## 9. Firmware / KMD 边界

| 组件 | 默认责任 | 何时需要新增改动 | 不应下沉的工作 |
|---|---|---|---|
| UMD/host runtime | allocator hooks、VA aperture/metadata buffers、module patch、dispatch context、stream/event 生命周期、报告消费 | 六路线都需要相应子集 | 无须把全部算法塞进KMD |
| Device runtime | checks、pointer metadata、ring report、失败触发 | B/C/D/E/F；A 可由host模型替代 | 不直接解析host DWARF或调用host allocator |
| Firmware | 已有 trap/fault/context abort、队列错误 completion、保存最小故障上下文 | 现有异常无法安全中止dispatch/保留report；F新增tag/bounds fault | C++对象表、红区策略、符号化 |
| KMD | 隔离、映射与驻留、context资源、安全的fault/abort出口 | VA reserve/sparse mapping/metadata residency受限；新增fault ABI；F tag context或迁移状态 | 每条用户load/store的软件检查，source allocation归因 |
| RTL/微架构 | 既有 ISA/异常行为 | F若缺tag/bounds检验、metadata cache、异常精度、DMA覆盖，需要架构/RTL/DV项目 | 不能把完整hardware effort折算进软件kLOC |

B–E 的成本基线假定现有公开 UMD/KMD 接口已满足缓冲区与可靠失败协议，因此不把 KMD/FW 强制写成必改。F 的软件预算是假定所需硬件能力已经存在；缺失能力必须单独过 architecture/RTL/DV gate、另估人月和硅片时间。

## 10. 工程量：公共底座 P + 路线增量

单位为 **手写、去重 kLOC（千行）**。核心统计新增及实质修改代码，排除注释/空行/生成文件/第三方依赖搬运；测试统计手写 test/harness/fixture generator，不按生成测试用例展开。范围是可内测、具有明确支持矩阵的工程版本，不是极简 PoC，也不是覆盖多代 ISA/所有语言 ABI 的商业成熟产品。区间不是测量结果，项目首次完成 vertical slice 后应重估。核心与测试不互相混算。

### 10.1 公共底座 P（仅计一次）

统一 allocation/suballocation lifecycle schema、报告与 context ABI、UMD 事件发布骨架、基础 cmodel adapter、host symbolizer/coverage manifest、共享测试 harness。

| 组件 | 核心 kLOC | 手写测试 kLOC |
|---|---:|---:|
| Compiler | 1–2 | 1–2 |
| Device runtime | 1–2 | 1–2 |
| Host runtime + UMD | 4–7 | 3–5 |
| cmodel | 1–2 | 1–2 |
| Tooling/reporting | 2–4 | 2–3 |
| **P 总计** | **9–17** | **8–14** |

### 10.2 每路线相对于 P 的核心增量

| 路线 | Compiler | Device RT | Host RT + UMD | cmodel | Tooling | Δ核心合计 |
|---|---:|---:|---:|---:|---:|---:|
| B | 7–13 | 5–9 | 4–8 | 1–3 | 1–3 | **18–36** |
| C | 0–2 | 6–11 | 14–26 | 2–5 | 3–6 | **25–50** |
| D | 18–34 | 10–20 | 7–14 | 3–7 | 3–6 | **41–81** |
| E（已去重，见下一节） | 23–44 | 17–32 | 16–31 | 6–12 | 5–10 | **67–129** |
| F（已有所需硬件） | 5–10 | 4–8 | 4–8 | 6–12 | 2–5 | **21–43** |

### 10.3 每路线相对于 P 的手写测试增量

| 路线 | Compiler | Device RT | Host RT + UMD | cmodel | Tooling | Δ测试合计 |
|---|---:|---:|---:|---:|---:|---:|
| B | 6–11 | 4–7 | 3–6 | 1–3 | 1–3 | **15–30** |
| C | 0–2 | 5–9 | 12–23 | 2–5 | 3–5 | **22–44** |
| D | 15–29 | 8–16 | 6–12 | 3–7 | 3–5 | **35–69** |
| E（已去重） | 19–36 | 14–27 | 14–28 | 6–12 | 5–10 | **58–113** |
| F（已有所需硬件） | 5–10 | 4–8 | 4–8 | 6–12 | 2–4 | **21–42** |

### 10.4 从已有 GPU 栈、sanitizer 从零的总计

| 方案 | 计算式 | 核心 kLOC | 手写测试 kLOC |
|---|---|---:|---:|
| B | P + ΔB | **27–53** | **23–44** |
| C | P + ΔC | **34–67** | **30–58** |
| D | P + ΔD | **50–98** | **43–83** |
| E | P + 已去重的组合增量 | **76–146** | **66–127** |
| F | P + ΔF，软件使能部分 | **30–60** | **29–56** |

这些是替代/组合路线的预算，不可相加得到项目总量。P只算一次；E已经包含它需要的B、D与C工作，不得再加B/C/D总量。

### 10.5 E 的可算术复核拆分

E = P + 完整B增量 + B基础上D的独有部分 + C fallback子集 + 跨模式integration。D的共享地址检查、shadow lookup框架、allocator/reporting接口不重复计；C仅支持选定访存opcode及已知代码对象格式，不含完整C兼容面。D精准能力限于metadata传播闭合的模块与接口，C fallback仍是addressability级。

| E项 | Compiler核心/测试 | Device RT核心/测试 | Host+UMD核心/测试 | cmodel核心/测试 | Tooling核心/测试 | 核心合计 | 测试合计 |
|---|---|---|---|---|---|---:|---:|
| B完整增量 | 7–13 / 6–11 | 5–9 / 4–7 | 4–8 / 3–6 | 1–3 / 1–3 | 1–3 / 1–3 | 18–36 | 15–30 |
| D独有新增 | 14–26 / 11–20 | 8–15 / 6–12 | 4–8 / 4–8 | 2–4 / 2–4 | 1–2 / 1–2 | 29–55 | 24–46 |
| C fallback子集 | 0–1 / 0–1 | 2–4 / 2–4 | 6–11 / 5–10 | 1–2 / 1–2 | 1–2 / 1–2 | 10–20 | 9–19 |
| 跨模式integration | 2–4 / 2–4 | 2–4 / 2–4 | 2–4 / 2–4 | 2–3 / 2–3 | 2–3 / 2–3 | 10–18 | 10–18 |
| **E增量合计** | **23–44 / 19–36** | **17–32 / 14–27** | **16–31 / 14–28** | **6–12 / 6–12** | **5–10 / 5–10** | **67–129** | **58–113** |

### 10.6 估算敏感项与单列项目

- B 最敏感于 shadow VA/驻留、stack/private/shared、target memory intrinsics、pass后新增访问；受控 global-only PoC远小于此工程区间。
- C 最敏感于ISA可变编码、寄存器/谓词保存、重定位、间接目标、多种code object及JIT同步。
- D 最大项是端到端身份传播和atomic/外部ABI；若要求所有C/C++整数别名与外部二进制仍精准，这个有限支持矩阵估计不成立。
- E 需要额外接口契约与模式验证，复用不是零集成成本。
- F 要单列ISA规格、RTL、微架构性能、DV与异常精度、DMA覆盖、FW/KMD启用工作。不存在硬件时，不能使用“软件30–60kLOC”作为总项目报价。
- FW/KMD缺失可靠dispatch abort或稀疏metadata mapping时，对B–E也构成单列gate。本笔记不提供伪精确的软件LOC替代硬件/驱动项目量。

## 11. 必须进入验收套件的反例

| 反例 | 预期与验证意义 |
|---|---|
| 8字节granule内A有效、空洞poisoned、B有效 | 经典1:8不能表示；实现必须拒绝不支持布局、增加精细元数据或明确能力边界 |
| 从A计算的指针直接落入live B | B/C纯shadow可漏；D支持链路必须报对象越界 |
| free后同GPUVA复用，旧指针和新指针地址相同 | 无身份表无法区分；D旧指针必须报，fresh必须通过 |
| masked-off无效地址、active有效地址；EVL小于向量长度 | inactive元素无报告、无shadow fault；只有实际元素检查 |
| wave分歧 + 每lane多元素vector mask + whole-wave lowering | 不把线程mask与元素mask混同；报告lane和element准确 |
| unaligned访问跨两个granule/页，首字节合法尾字节非法 | 检查完整跨度，且地址加法溢出不会绕过 |
| atomic pointer反复地址复用/并发CAS | 读到的address与ID/epoch必须匹配；失败cmpxchg保持语义 |
| stream内alloc→kernel→free与跨streamevent依赖 | 按device顺序变化metadata，不能按host调用时刻误报 |
| 跨stream未同步free/use | 暴露生命周期竞争或明确能力限制，不能悄悄全局同步掩盖问题 |
| 一个lane OOB后同组barrier | 报告有界、全dispatch失败、没有永久barrier等待 |
| report ring满、metadata页缺失、符号文件不匹配 | 可靠错误分类和丢失计数，不把工具故障误标用户OOB |
| inline asm/未知opcode/外部ABI无metadata | coverage manifest必须显示未覆盖/降级，严格模式按策略拒绝 |
| binary relocation导致长跳、间接call、原PC归因 | patch后程序有效输入结果保持一致，报告指回原模块 |

测试层次：IR lit/FileCheck验证结构与属性但不能替代语义；MIR/MC验证最终指令与状态保存；host runtime生命周期状态机与故障注入；cmodel逐事件oracle；硬件正负例与性能/occupancy/显存变化。不同支持层级用同一失败类别和期望范围，允许优化后的site合并，不用机械比较所有trace顺序。

## 12. 原始来源

下列采用项目官方文档、LLVM源码或ASan作者维护的算法文档。事实采用概括方式，架构推理明确标为建议。

| ID | 来源与URL | 本笔记取用的事实 |
|---|---|---|
| R1 | [AddressSanitizerAlgorithm](https://github.com/google/sanitizers/wiki/AddressSanitizerAlgorithm) | 1:8前缀编码、redzone/quarantine、历史未对齐跨度反例 |
| R2 | [HWASan Design](https://clang.llvm.org/docs/HardwareAssistedAddressSanitizerDesign.html) | tag/short granule、tag碰撞与部署限制 |
| R3 | [LLVM AddressSanitizer.cpp](https://llvm.org/docs/doxygen/AddressSanitizer_8cpp_source.html) | AMDGPU支持路径、AS3/AS5排除、PoisonStack限制 |
| R4 | [LLVM LangRef](https://llvm.org/docs/LangRef.html) | masked load/store/gather/scatter，VP mask+EVL，atomic ordering与poison语义 |
| R5 | [LangRef: Pointers with external state](https://llvm.org/docs/LangRef.html#pointers-with-external-state) | 指针外部状态的load/store与memcpy/memmove传播、inttoptr限制 |
| R6 | [Clang AddressSanitizer](https://clang.llvm.org/docs/AddressSanitizer.html) | 编译器+runtime结构、custom allocator/container annotation、报告符号化和VA区别 |
| R7 | [LLVM Convergent Operation Semantics](https://llvm.org/docs/ConvergentOperations.html) | 收敛/控制流变换约束，convergent不隐含memory/control barrier |
| R8 | [AMDGPU Backend User Guide](https://llvm.org/docs/AMDGPUUsage.html) | 地址空间、同步scope、conceptual lane mask与EXEC差别、调试归因 |
| R9 | [LLVM Atomic Instructions and Concurrency Guide](https://llvm.org/docs/Atomics.html) | 原子RMW/CAS语义与target lowering约束 |

建议原文中可保留的非常短的依据句（其余是推理）：ASan用编译器插桩和runtime；经典shadow按8字节granule编码；LLVM masked-off元素不访问内存、VP叠加EVL；LLVM外部状态指针要求pointer-typed内存往返传播metadata；convergent不蕴含fence；tag检测可有碰撞。其它自研设计、代码量和路线推荐应显式归入工程判断。
