# S01：Global对象头尾越界

## 从厂商实际实现理解 S01（优先阅读）

**[S01 厂商优先的完整实施指南](../../docs/S01-vendor-implementation-guide.md)**：

| 实际参考路线 | CUDA/AMDGPU现有机制 | 自研组件重点 |
|---|---|---|
| NVIDIA默认运行时Memcheck | 对已编译GPU二进制的访存点运行时插桩、检查当前有效分配；某些邻接live对象场景可漏检 | UMD机器码patch和对象registry；Compiler提供MC指令语义；KMD提供正常映射/完成 |
| NVIDIA CUDA13.1+编译期Memcheck | Base-and-Bounds/fat pointer来源分析，运行时与GPU设备检查协同；未知来源降级 | Compiler SSA provenance与GPU检查库；UMD Kernel参数对象sidecar |
| AMD ROCm GPU ASan | LLVM访存插桩＋Shadow Memory＋Redzone/Poison＋ASan运行库 | Compiler LLVM Pass与设备checker；UMD shadow与对象生命周期；KMD按缺口改 |
| 其他GPU方案 | cmodel原生检查、区间表、GPU MMU Guard Page、Tag和硬件Bounds | 按厂商未提供的检测保证及项目GPU硬件能力选择 |

**已有QEMU全系统平台能执行目标GPU ISA**：验证不需要重做ISA执行器，只需按所选检测器增加实际GPU访存前观察、对象/Shadow、Guest UMD/KMD错误回传（已有接口可复用）。下文保留完整CUDA/HIP源码运行方式、原S01代码与数学判定，详细各组件代码和官方出处以独立指南为准。

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S01 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S01 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `detect` | 真实GPU heap边界访问；受支持工具应报告。 |
| AMD HIP | AMD GPU AddressSanitizer | `detect` | 真实GPU heap边界访问；受支持工具应报告。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.1](../../docs/llvm-gpu-sanitizer-research.md#case-s01)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.1 S01：Global 对象头部与尾部越界

**前提与错误代码。** 受控夹具给 `a` 分配 64 B，即 16 个 `uint32_t`，首地址为 `0x1000`；前后各 16 B 是保留的不可访问区，不包含其他 live 对象。启动一个 32 线程 block，分别传入 `tail=false` 和 `tail=true`：

~~~cpp
__global__ void s01(uint32_t *a, bool tail) {
    if (threadIdx.x == 0) {
        int i = tail ? 16 : -1;
        a[i] = 7;  // 错误：分别写 [0x1040,0x1044) 或 [0x0ffc,0x1000)
    }
}
// 独立运行：s01<<<1,32>>>(a, false); s01<<<1,32>>>(a, true);
~~~

**哪里不对、怎样检。** 合法字节区间只有 `[0x1000,0x1040)`；两次 4 B 写分别从尾后第一字节和头前 4 B 开始。正确条件是整个实际访问包含于该对象，并按第 6 章的减法条件避免地址加法溢出。B 可将两侧 redzone poison，检查被写入的全部字节；A0 可用 UMD 的有效区间和实际 store 范围检出本例。二者依赖这里确实存在的无效邻区；更远的访问若落入 live B，需转入 S04 的来源检查。D1 保留 `a` 的原对象 bounds 后也可检出，无须依赖邻区布局。[L02][L05]

**组件观测。** compiler 保留 store 的谓词、4 B 宽度和源位置；deviceRT 在执行写之前检查，不能让其他 31 个未执行线程产生报告；UMD 提供逻辑长度 64 B，而非把 allocator 的对齐补齐长度当对象长度；cmodel 对照实际地址与 byte enable，独立判断头、尾两侧。

**合法反例、修复及报告。** 改为 `a[0]`、`a[15]` 应通过；循环边界应使用 `0 <= i && i < 16`。预期为 global 越界写，包含 M1／M3 对应保证、宽度 4、对象范围及偏移 `-4`／`+64`。若只有页级映射检查，两个地址仍位于已映射页时可能漏检；这不是对象检查已经通过。

#### S01七种路线的溯源与组件落地：以既有QEMU GPU ISA执行器为验收入口

**S01 的主判断是实际生效的4字节Global访问是否处在64字节用户逻辑对象范围内。** 硬件GPUVA映射命中、应用对象检查通过、编译期原对象检查通过是不同等级。详细机制见[§4.8](#route-provenance-matrix)和各路线末尾的溯源说明。

| S01机制 | 厂商/上游借鉴或融合 | 三生产组件的最小改动 | **本项目已有QEMU模型**还需接什么 |
|---|---|---|---|
| A0模型访问前oracle | 通用bounds条件＋LLVM目标ISA语义[L05]，本项目工程化 | Compiler可不插桩；KMD复用Guest提交/完成；UMD传64B实际逻辑长度 | 现有Global Store实际访存前hook、对象表和错误回传；**不重写GPU ISA解释器** |
| B1编译插桩＋区间表 | LLVM访存插桩框架[L03]＋本项目live对象registry设计 | Compiler check/DeviceHelper；UMD不可变区间快照；KMD映射 | 执行真实插桩ISA、区间表可见性、报告 |
| B2编译插桩＋Shadow | Google/LLVM ASan[L02]＋AMD GPU ASan[A01] | Compiler Pass/device库；UMD shadow/redzone/poison；KMD缺失映射/错误契约才补 | 执行设备shadow checker、接入Guest metadata映射和命令先后 |
| C运行期patch | NVIDIA patch API[N03]的公开接口模式＋自研机器码重写 | **Compiler源码无需修改；UMD内独立Runtime Binary Rewriter**负责ISA decode/encode、patch/cache、PC map（可选复用现有MC库）；KMD正常代码加载 | 原始/重写ISA的合法结果差分及异常PC复核 |
| D1原对象bounds | NVIDIA编译期fat bounds[N04]＋cuCatch[P01] | Compiler原指针root/companion；UMD参数sidecar；KMD复用 | 执行原对象check，QEMU访存oracle核对来源；跨live B仍检出 |
| E编译＋patch | NVIDIA Hybrid[N02][N05]＋B/D/C融合 | Compiler manifest；UMD混合选择；KMD使用统一资源 | 检查跨模块模式切换、coverage与来源降级 |
| F0/F1/F2 | GPUVM页保护[K01]／LLVM HWASan Tag[L04]／GPUArmor[P04] | F0映射；F1/F2需新的tag/bounds协议或硬件项目 | F0验现有GPU MMU；F1/F2新增硬件指令时才扩展模型语义 |

**特别澄清：** NVIDIA-R纯运行时方案不要求修改生产编译器或重新编译用户Kernel；UMD中的Runtime Binary Rewriter可以独立提供解码、重新编码和检查注入。**用已有编译器编译工具的设备Patch模块，不属于修改编译器。** 见[S01厂商实施指南 §1.2.1](../../docs/S01-vendor-implementation-guide.md)。

**建议最先提交的工作包：** 在现有QEMU GPU ISA执行器的`GLOBAL_STORE_U32`语义入口加`before_gpu_memory_effect`，向模型输入`Object{base=0x1000,size=64}`和可控redzone；运行合法`a[0]`、`a[15]`及非法`a[-1]`、`a[16]`四组并从Guest UMD接收源PC、lane、byte-width、object报告。随后才实现LLVM B2设备库与实际QEMU插桩产物，不能把模型A0正确等同于真机B2已完成。

## 组件与模型平台检查点

| 层 | 需要的信息或修改 |
|---|---|
| 功能级 cmodel | 真实访问是否超出有效范围 |
| QEMU 已有 GPU ISA 执行模型 | M1/M2/M3 |
| KMD | 地址、宽度、边界 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S01.*.log`日志及`summary.json`。CPU ASan、单纯Host API错误或GPU MMU硬件fault不能冒充准确检测到本案例的对象级语义。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
