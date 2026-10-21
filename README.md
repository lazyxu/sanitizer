# LLVM GPU Sanitizer 技术调研

本仓库提供面向自研LLVM GPU后端的Sanitizer产品技术调研、逐组件实施规格、28组CUDA/HIP验证样例与硬件Runner。

**产品路线仅为B（LLVM Shadow）、C（UMD运行时二进制插桩）、D（原对象Bounds／时间身份）、E（Hybrid）和F（实体硬件MMU／Tag／Bounds）。**任何检测承诺都必须由真实GPU检查指令或实体硬件支撑。项目自研Compiler、UMD、KMD的生产Sanitizer仍处于实施规划阶段，没有对应真机完成证据。

## 当前阅读入口

- [真实GPU逐组件实施规范（含关键伪代码、完整汇总表）](docs/real-gpu-component-implementation.md)——**首选**，涵盖Compiler、Device Runtime、UMD、Runtime Binary Rewriter、KMD、Firmware、Hardware、Reporter、CI。
- [完整研究主稿](docs/llvm-gpu-sanitizer-research.md)——背景、厂商对标、B～F、28个细化场景、成本与验收。
- [S01厂商实现指南](docs/S01-vendor-implementation-guide.md)——NVIDIA运行时／编译期与AMD GPU ASan的对照。
- [真实GPU可部署性及组件边界审计](docs/hardware-deployability-audit.md)。
- [当前生产组件预算](docs/estimates-v1.7.json)——只保留真实GPU生产改动；数值是规划区间，非实测代码量。
- [28组CUDA/HIP案例](samples/README.md)——S01～S28各有源码及正反例。
- [真实GPU GitHub Actions](docs/gpu-actions-ci.md)；[研究进度](docs/research-progress.md)。

## 产品级工程边界

| 路线 | 实际执行检查位置 | Compiler源码新增 | UMD | KMD |
|---|---|---|---|---|
| B Shadow | 真实GPU设备ISA | 必须 | Shadow、生命周期、报告 | 仅按现有接口缺口 |
| C Binary Patch | 真实GPU运行patch后的ISA | **0（纯Runtime）** | 独立Binary Rewriter、加载与PC恢复 | 仅按代码映射／fault缺口 |
| D1/D2 Bounds／Temporal | 真实GPU设备ISA | 必须 | 对象ID/epoch、sidecar、身份元数据 | 仅按映射／隔离缺口 |
| E Hybrid | 真实GPU执行B/D及受控C | 编译路径必须 | mode/manifest/对象表和报告去重 | 契约并集，去重 |
| F Hardware | 实体GPU MMU／Tag／Bounds | 视硬件ISA而定 | 页面／Tag／描述符生命周期 | 真实硬件状态与fault接口 |

F0仅在实体GPU MMU页权限处检测页级非法访问，并不覆盖合法页内部任意对象越界。F1/F2需要真实硬件能力；没有对应芯片特性时必须另立ISA/RTL/DV/Firmware项目，不能列为已部署功能。

**推荐P0：**B的真实GPU Global Store检查，含Shadow/Redzone、UMD对象登记与可见性、真实KMD提交及设备报告，直接执行S01 good/bad样例。后续扩展B、D1，再视需求交付D2、C/E、F。Compiler IR/MIR/ISA结构验证和CPU静态CI不能代表GPU检出；未能运行设备测试应记录NOT_RUN。

本仓库当前提供的CUDA/HIP样例可以对照NVIDIA Compute Sanitizer／AMD GPU ASan的公开能力；它们不是已经实现的自研GPU检查器。详见[硬件验收规则](docs/real-gpu-component-implementation.md#13-真机测试性能及ci)。
