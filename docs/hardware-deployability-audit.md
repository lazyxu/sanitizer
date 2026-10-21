# 全仓库真实GPU可部署性与组件职责审计

**当前版本：2026-10-21，范围：生产候选B/C/D/E/F、Compiler、Device Runtime、UMD、KMD、Firmware、真实GPU硬件以及真实GPU验收。**

## 1. 产品准入

产品检查必须真实执行在目标GPU上：B/D/E的LLVM设备检查ISA、C的UMD运行时Patch设备ISA，或F的实体GPU MMU／Tag／Bounds硬件逻辑。不能把Host API校验、普通CPU静态CI、纸面伪代码或元数据登记当作设备访存检出。检查器未知或资源不足须显式返回INCOMPLETE/UNSUPPORTED，不可返回CLEAN。

## 2. 方案与职责

| 候选 | 真机检查入口 | 必需的Compiler新增 | 必需的UMD能力 | KMD新增 | 硬件限制 |
|---|---|---|---|---|---|
| B | GPU ISA Shadow/Redzone | 访存前插桩与后端覆盖审核 | 逻辑对象、Shadow、报告 | 已有契约不足时 | 目标设备必须执行插桩ISA |
| C | GPU ISA Runtime Patch | **0（纯Runtime）** | Binary Rewriter／Patch Cache／Loader | 已有可执行映射或终止不足时 | 目标ISA可解码、可合法重写 |
| D | GPU ISA Bounds/Identity | D1来源Bounds、D2传播与ABI | Object/Epoch、sidecar、元数据生命周期 | 按已有接口缺口 | 来源未知时不得声称D1/D2 |
| E | B/D＋C的GPU设备检查 | 编译插桩部分必须 | 模块策略、降级、共享状态 | 接口并集去重 | fallback不恢复已丢失的来源 |
| F0 | 实体GPU MMU页权限 | 通常0 | VMM页布局和fault归因 | 需要时增强GPUVM/fault | 页级，不等于字节级bounds |
| F1/F2 | 实体GPU Tag/Bounds单元 | 依ISA及ABI而定 | Tag/Descriptor生命周期 | 硬件状态契约缺失时 | 若实体芯片无该功能，非当前可部署产品 |

## 3. 横向职责审计

Compiler负责真实GPU代码生成与最终访存覆盖审核；设备库负责Shadow／Bounds／Epoch检查及report ring。UMD负责真实allocation逻辑长度、async free顺序、模块装载、二进制重写和报告；KMD负责GPUVA映射、授权、completion、fault与可用的abort/reset，**是否新增源码取决于接口缺口**。Firmware负责确实存在的停止及故障支持；GPU硬件本身负责F路线的真实保护能力。Reporter/CLI记录设备ISA版本、原始PC、lane、mode、未覆盖路径以及真实设备运行证据。

C的ISA Decoder/Encoder、重定位、寄存器／scratch／resource重算归属于UMD的独立Runtime Binary Rewriter；只读复用编译器现成MC库可选，但不要求修改生产Compiler。任何涉及指针原对象或时间身份的保证，必须存在实际GPU可见的身份传播代码，不能由GPUVA当前命中的BO反推。

## 4. 预算与验证

只对真实产品三组件及条件Firmware／RTL计费，不增加任何不参与产品运行的专用平台投入。当前预算索引见[生产组件预算](estimates-v1.7.json)；[逐组件实施伪代码](real-gpu-component-implementation.md)为工程接口依据。

必须在真实GPU执行S01～S28的正反例、宽/掩码/原子访存、shared/private/async、对象复用、二进制Patch与报告队列溢出测试；日志包括设备/驱动版本、模块hash、检测指令证据、状态和性能。现有仓库的静态CI及56份样例源码不代表自研检查器已经实现或完成真机验收。

**当前状态：DOC_SPECIFIED；SELF_GPU_CHECKER_NOT_IMPLEMENTED；HARDWARE_NOT_VERIFIED。**
