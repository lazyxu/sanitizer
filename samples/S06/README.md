# S06：Shared/LDS越界

## 可编译设备代码与一键运行

**这是一对独立的真实 GPU 源程序，不是CPU ASan演示。** [CUDA完整源码](cuda.cu)和[AMD HIP完整源码](amd.hip.cpp)分别具有设备kernel、Host入口和中文注释。运行时使用 `--good` 合法对照与 `--bad` 故障/边界场景；以GPU最终实际访存、设备sanitizer报告与原始日志作为验收证据。

~~~bash
# 已进入对应 Docker GPU 编译环境后，从仓库根目录执行：
python3 scripts/run_samples.py --backend cuda --case S06 --mode both --strict
python3 scripts/run_samples.py --backend rocm --case S06 --mode both --strict
~~~

| 后端 | 厂商工具 | 当前预期级别 | 判断依据 |
|---|---|---|---|
| CUDA | NVIDIA Compute Sanitizer `memcheck` | `conditional` | Shared/LDS对象与整体backing边界可能不同。 |
| AMD HIP | AMD GPU AddressSanitizer | `conditional` | Shared/LDS对象与整体backing边界可能不同。 |

**预期含义：** `detect` 是厂商官方能力下的期望，不是本项目已检出；`conditional` 需要GPU架构/ISA/执行时序支持；`unsupported` 表示当前选定工具不能保证；`api` 是GPU API错误但不构成GPU kernel内部sanitizer命中；`clean` 是用于排除误报的合法操作。


## 真实场景、错误原因、可检测算法、修复对照及实现要求

以下章节直接对应[主调研文档 §18.6](../../docs/llvm-gpu-sanitizer-research.md#case-s06)的正式原理与验收设计。**其中的CUDA风格伪代码和受控allocator不一定与本目录中可编译程序逐行相同**；两者须按相同违规语义而非源码文字对齐。实际运行前确认目标机器码保留相关访存，不可以假设未定义行为一定触发。

### 18.6 S06：静态／动态 shared 越界与 block 身份

**前提与错误代码。** 静态数组 `s` 只有 8 个 32 位元素；动态 shared 的应用请求长度是 24 B。两个 kernel 分别启动两个 block、每个 32 个线程，并只由各 block 的 thread 0 写，排除本例中的数据竞争干扰：

~~~cpp
__global__ void s06_static() {
    __shared__ uint32_t s[8];
    if (threadIdx.x == 0) s[8] = 1; // 错误：静态对象偏移 32，写 4 B
}
__global__ void s06_dynamic() {
    extern __shared__ uint32_t d[];
    if (threadIdx.x == 0) d[6] = 2; // 错误：动态请求仅 24 B，偏移 24
}
s06_static<<<2,32>>>();
s06_dynamic<<<2,32,24>>>();
~~~

**哪里不对、怎样检。** 检查必须分别知道静态对象大小和本次 launch 的动态长度。**真实GPU B/D插桩**需要Compiler输出与最终ISA一致的shared对象布局，并结合UMD动态shared请求，在设备端按`(launch,block,object)`检查；B可以为static layout增加Redzone、为动态区尾部Poison，但必须更新真实设备资源声明。只检查整个 shared backing，可能漏掉静态对象跨入邻变量；此时需原对象关联／D1 来源，只有地址布局表仍不够，也不能拿额外预留空间扩充应用合法范围。[L05][L06]

**组件观测。** compiler 输出最终布局而非优化前偏移；deviceRT 为不同 block 选择独立 shadow／对象上下文；UMD 传递请求的 24 B 及 sanitizer 额外占用。不能让 block 0 的状态误用到 block 1，也不能把不同 block 的 `s[0]` 当作共享同一对象。

**合法反例、修复及报告。** 分别改为 `s[7]`、`d[5]`，两个 block 均应通过。错误报告须区分 static／dynamic shared、block ID、4 B 宽度与偏移 32／24；可以合并重复报告，但保留观察到的 block／错误计数。若错误策略在首个 block 后终止 launch，未执行 block 不计漏检。仅完成 global 插桩的 B／C 不具有本例覆盖。

## 实体GPU生产组件职责

**下表只描述GPU设备Checker、UMD及KMD真实生产职责；不能把软件静态检查代替设备端错误证据。**

| 角色 | 正确职责 |
|---|---|
| **真实GPU生产Checker** | Compiler/UMD生成设备检查指令，或实际GPU硬件MMU/Tag/Bounds生效；GPU上的报告才是产品验收 |
| KMD（生产） | 调度资源配置；无法从外部DMA自动观察每个shared访问；接口充分时新增代码可为0 |

## 验收需要留下什么证据？

在对应真实GPU上记录：1）硬件型号、驱动和CUDA/ROCm版本；2）编译命令、目标GPU架构及最终ISA访存；3）`--good`正常且无误报；4）`--bad`出现对应的**设备级**GPU检查器报告，或者明确归类为`UNSUPPORTED/NOT_DETECTED/PREREQUISITE_NOT_MET`；5）`results/<backend>/S06.*.log`日志及`summary.json`。CPU ASan、Host API错误和模型独有hook不能冒充真实GPU检查器；GPU MMU fault仅证明符合其硬件页级规则，不等于对象级越界检测。

**当前状态：只有源码/文档及CPU静态检查证据，尚无实际GPU编译和sanitizer检出数据。** [GPU CI启用方法](../../docs/gpu-actions-ci.md)说明如何在可信master的NVIDIA/AMD GPU Runner中自动验证。
