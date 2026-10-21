# GPU Sanitizer 研究进度

## 当前设计版本 v1.8（2026-10-21）

### 已完成的文档和样例工作
- 确定仅有B/C/D/E/F五条面向真实GPU的产品候选；明确Compiler、Device Runtime、UMD、KMD、Firmware与GPU硬件边界。
- 补充[真实GPU逐组件实施规范](real-gpu-component-implementation.md)，每个组件都有关键伪代码、验收和交叉总结。
- 保留[研究主稿](llvm-gpu-sanitizer-research.md)、[S01厂商指南](S01-vendor-implementation-guide.md)、[硬件可部署性审计](hardware-deployability-audit.md)。
- 28组S01～S28的CUDA/HIP样例、GPU硬件Runner与静态结构检查可用于厂商现有工具对照。
- C纯Runtime机器码Patch归UMD独立重写器，**生产Compiler源码新增可以为零**；KMD只按现有接口缺口追加。

### 尚未完成的自研产品实现
- 自研目标GPU的LLVM B检查器、Shadow/Redzone、Device Runtime与UMD/KMD闭环。
- D1原对象Bounds、D2完整时间身份传播，C的二进制重写、E的覆盖保证分层。
- 所需的实体F0 GPU MMU页保护验收、可能的F1/F2 ISA/RTL/DV及固件实现。
- 自研GPU上S01～S28的真实设备报告、覆盖证据、误报漏报、性能与资源开销。

### 下一步
P0先做B的真实GPU Global Store检查、逻辑64B对象与Redzone、GPU端错误报告及S01 good/bad；然后扩大地址空间、lane mask/atomic覆盖和D1。后续按不可重编译模块需求启动C/E，硬件支持存在时再落地F。

**证据：**源码与CPU静态测试不等于真机验收；任何尚未取得真实GPU执行报告的产品能力都记为NOT_RUN或HARDWARE_NOT_VERIFIED。
