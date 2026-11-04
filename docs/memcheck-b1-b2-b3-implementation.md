# GPU Memcheck 实施方案：B1 / B2 / B3 与条件注入 C

**版本：1.0｜日期：2026-11-04｜用途：内部技术评审与归档**  
**实现状态：自研设计与工程估算；目标 GPU 尚未实施、未实机验收（GPU_NOT_RUN）。**

## 0. 摘要与结论速览

本文将友商公开参照限定为 NVIDIA 的 NV-R（运行时 binary patching）与 NV-C（编译期 base-and-bounds，官方称 compile-time instrumentation），以及 AMD GPU AddressSanitizer 的公开形态；自研部分是 B 包的三个配置：B1 单对象 Live 区间、B2 字节级 Shadow（参照 AMD 方案）、B3 原对象 Base/Size（参照 NV-C）和可选注入方式 C。B1 是仓库自研设计，不参考 NV-C；B3 沿用仓库已有对象来源设计并在本文改用新代号。

| 约束组合 | 更匹配配置 | 理由 |
|---|---|---|
| 释放后访问与对象内局部 Poison 是主要缺陷 | B2 | 字节级状态可表达 Redzone、子分配与 Poison；依赖安全 Shadow 驻留 |
| 跨紧邻对象的一次宽访问是主要缺陷 | B1 | 整次访问必须落在同一个 Live 对象内，能拒绝跨界宽访问 |
| 来源对象 A 的派生地址完整落入另一个 Live B 是主要缺陷 | B3 | 检查使用指针携带的原对象 Base/Size，而不是重新按地址查当前对象 |
| 闭源模块确实无法重编译 | C + B1/B2/B3 之一 | C 改变注入位置，不独立提供检查谓词；只覆盖经验证可重写的机器码 |

B2 的交付必须分账：[A] Sanitizer 自身的编译插桩、Shadow/Poison、Allocator 与设备报告；[B] 另立项的 KMD 可恢复缺页与 retry 能力。当前目标 KMD 不具备为 Sanitizer 引入的可恢复缺页能力，[B] 不计入 [A]。自研所有性能判断均为 GPU_NOT_RUN，必须在目标 GPU 上测量后才能形成性能结论。[自研设计，GPU_NOT_RUN]

**范围：** 只讨论 NVIDIA、AMD 两家友商的公开 Memcheck 参照，以及自研 GPU 的空间访问、分配释放状态、泄漏与硬件异常归因。

## 1. 目标与验收口径：Memcheck 到底要抓什么

Memcheck 的交付目标是让每个受支持的设备访存，在原访存真正执行前得到可归因的判定；并让 UMD 对分配、释放、重复释放和泄漏形成一致的对象账本。仅有编译成功、插入了检查调用或 Host API 拦截，不代表设备访存已经受到保护。[自研设计，GPU_NOT_RUN]

| 缺陷类 | 裸跑为何抓不到 | 期望报错形态 |
|---|---|---|
| 空间越界、跨 allocation | 设备地址可能仍落在有效映射页内，硬件不会理解逻辑对象边界 | 越界类型、读写类型、地址、访问宽度、对象边界、lane、PC |
| 一次宽访问跨紧邻对象 | 首地址和末地址可能都在已映射区域，单看地址不能证明整次访问属于一个对象 | 完整访问区间、跨越的对象边界、来源对象（若配置具备） |
| 地址完整落入另一个 Live 对象 | 当前地址仍然可访问，裸地址无法说明它原来由哪个对象派生 | 原对象 Base/Size、实际地址与宽度、来源传播状态 |
| 未对齐访问 | 部分指令或硬件可能容忍未对齐，或只在特定访问宽度/地址空间触发异常 | 要求对齐值、实际地址、访问宽度、指令 PC |
| 释放后访问 | backing 可能仍映射且物理页未回收，普通执行不一定立即故障 | UAF、已释放对象范围、释放发布点、访问 PC/lane |
| double-free | 第二次释放可能落入分配器内部状态而不触发设备访存故障 | API 调用位置、对象句柄、重复释放状态 |
| 泄漏 | 进程退出时仍有 GPU allocation 不代表内核硬件会报错 | 未释放对象数、字节数、分配调用点及所属 Context |
| GPU 硬件异常 | 裸跑可能只返回异步设备错误，缺少触发访存的上下文 | Fault 地址、访问类型、队列/Context、设备 PC、可用的 lane 与模块映射 |

反例准入声明：只保留至少一个配置能抓、至少一个配置会漏的反例；所有配置都无保证的场景不写、不提。

### 1.1 反例：跨对象宽访问

下面的最小设备侧示意假定测试分配器将两个独立逻辑 allocation 紧邻布置：A 为前 32 字节，B 为后 32 字节。测试布局刻意不在两者之间插入 Poison 字节，使 Shadow 只看到两个相邻的可访问区域。该布局用于隔离检查谓词的表达能力，不代表所有生产分配器都会如此布局。

~~~cpp
#include <cstdint>
struct alignas(8) Wide16 { uint64_t lo; uint64_t hi; };

// 测试夹具：arena[0,32) 是 allocation A；arena[32,64) 是 allocation B。
alignas(16) unsigned char arena[64];

void probe_adjacent_allocations()
{
    unsigned char* A = arena;
    unsigned char* B = arena + 32;

    // A 派生地址 [A+24, A+40)，16 字节宽访问跨过 A/B 边界。
    volatile Wide16* cross = reinterpret_cast<volatile Wide16*>(A + 24);
    *cross = Wide16{0x11, 0x22};
    // B1：报。整次访问不属于同一个 Live allocation。
    // B2：漏。两侧 Shadow granule 均可访问时，Shadow 不表达 A/B 对象边界。
    // B3：报。来源为 A，访问末端超出 A 的 Base/Size。
    // NV-R：漏。按当前地址可访问性检查时，映射地址仍有效。
    // AMD 方案：漏。当前 granule 合法时不区分 A/B 对象边界。

    // 变体：仍从 A 派生，但实际访问完整落入另一个 Live allocation B。
    unsigned char* derived_from_A = A;
    derived_from_A += 32; // A 的 one-past 地址，随后访问落在 B。
    *reinterpret_cast<volatile uint32_t*>(derived_from_A) = 7;
    // B1：漏。按地址查到当前 Live B，完整访问包含在 B 内。
    // B2：漏。目标字节处于可访问 Shadow 区域。
    // B3：报。来源仍为 A，地址不属于 A 的原始范围。
    // NV-R：漏。当前地址可访问。
    // AMD 方案：漏。Shadow 只表示目标字节是否可访问。
}
~~~

这里 B2 与 AMD 方案的漏报是 Shadow 表达能力的固有边界：当 A、B 之间没有 Poison 间隔，且目标 granule 被标为可访问时，Shadow 无法仅凭字节可访问状态恢复两个独立对象的边界。这不是 AMD 方案实现缺陷，也不意味着常见 Redzone 布局下的所有跨界访问都会漏报。该反例只用于区分 B1 的“同一对象完整包含”与 B3 的“原对象来源边界”。[厂商公开] [自研设计，GPU_NOT_RUN]

### 1.2 反例：释放后访问

下面假定 free 已经按 API 队列依赖生效，且对象仍处于 Poison/退役状态、尚未还给复用池。该条件隔离的是释放状态判定本身。

~~~cpp
#include <cstdlib>
#include <cstdint>

void probe_use_after_free()
{
    volatile uint32_t* p =
        static_cast<volatile uint32_t*>(std::malloc(64));
    if (!p) return;

    p[0] = 1;
    std::free(const_cast<uint32_t*>(p)); // 释放已发布，块仍 Poison/退役且未复用。
    p[0] = 2;

    // B1：报。对象索引中的该区间已退役，不再是 Live。
    // B2：报。释放后对应 Shadow 已 Poison。
    // B3：单独不足以报。Base/Size 只说明来源范围，须组合 B1 或 B2 的释放状态。
    // NV-R：依赖对象撤销是否已被运行时检查元数据观察到。
    // AMD 方案：报。GPU ASan 的 Poison/运行库生命周期路径可报告该 UAF。
}
~~~

释放后访问反例只证明 Poison/退役发布完成且尚未复用时的判定能力；Poison/退役发布顺序归 UMD 负责。它不证明地址重新分配后还能仅凭裸地址恢复对象状态。[厂商公开] [自研设计，GPU_NOT_RUN]

### 1.3 验收维度与退出条件

| 验收维度 | 必须留存的证据 |
|---|---|
| 谓词层级 | 当前地址可访问性；地址是否落在原来源对象内。两层不得合并成一个“越界检查”勾选项 |
| 元数据载体 | B1：设备对象索引与已发布版本快照；B2：Shadow/Poison 字节状态；B3：随指针传播的原对象 Base/Size |
| 归因深度 | lane + PC + 越界字节数 + allocation site；不能提供的字段标 Unknown，不伪造 |
| 退出条件 | 两个反例均在目标 GPU 实测；分别记录报错配置、漏报配置和漏报的固有表达边界；合法对照不误报；工具故障不记为通过 |

首轮交付必须同时提供：两个反例的源码与构建记录、合法对照、目标 GPU 型号与驱动/工具链版本、最终设备指令覆盖清单、设备端原始报告、UMD 对象事件日志、GPU_NOT_RUN 或 GPU_DETECTED 状态。静态检查通过只证明文档/源码结构，不证明设备端判定。[自研设计，GPU_NOT_RUN]
