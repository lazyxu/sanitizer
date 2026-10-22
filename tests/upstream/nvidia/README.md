# NVIDIA 官方 Compute Sanitizer 独立上游回归组

本目录包含 NVIDIA [compute-sanitizer-samples](https://github.com/NVIDIA/compute-sanitizer-samples) 的 **Memcheck / Racecheck / Initcheck / Synccheck** 原样源码及 Makefile，固定在 commit `1e1fab8e25148127f5dec10210ac70b799d1c36f`。文件及目录名称保持上游原样；`LICENSE` 是 NVIDIA 原始 BSD-3-Clause 许可。具体文件 SHA 由 [manifest.json](manifest.json) 固定，静态审查可发现源码或许可证被意外修改。更新上游时必须同步记录 commit 和各 Git Blob SHA、复查许可及工具行为。

与 [S01～S28](../../../samples/README.md) **完全独立**：本目录的 6 个 CUDA 源文件衍生出 7 个逻辑检查项（Memcheck 源码包含非法访存和泄漏两项），编号为 NV-MEM-01/02、NV-RACE-01/02、NV-INIT-01、NV-SYNC-01/02；不可重编号 Sxx，也不可混合计算通过率。

**原样上游示例只提供错误代码，没有 `--good` 合法对照**，本组只验证错误案例的检出/工具行为，不能证明误报率，也不能替代 S01～S28 的合法对照。NVIDIA 的非法固定地址测试与 64B 对象尾后 4B 访问不是同一个精度要求。任何 CUDA 检测结果都不自动代表自研 checker 实现完成。

## 使用

~~~bash
# CPU 上只校验许可、pin、文件完整性和 case 清单
python3 scripts/run_upstream_nvidia.py --validate-only -v

# 有 CUDA 编译器、没有 GPU 时只编译，绝不算检出
python3 scripts/run_upstream_nvidia.py --compile-only

# NVIDIA GPU 上运行安全案例；同步非法测试默认跳过
python3 scripts/run_upstream_nvidia.py --case all --strict

# 自研 checker 真正可执行时，用独立命令收集它的诊断，禁止自动回退 NVIDIA
python3 scripts/run_upstream_nvidia.py --case NV-MEM-01 \
  --custom-command 'my-gpu-sanitizer -- {binary}' \
  --custom-diagnostic-regex 'YOUR_ACTUAL_DEVICE_DIAGNOSTIC_PATTERN'
~~~

`-v/--verbose` 可显示编译器、厂商检测工具和环境查询命令；自研检测器额外参数会在命令追踪中脱敏，避免泄漏。`-v` 不代表 GPU 检测已通过。

自研 checker 尚未实现时，`custom_diagnostic.status` 固定为 `NOT_CONFIGURED`，**不会因为 NVIDIA 检出而自动记为自研检出**。提供自研命令必须同时提供设备侧诊断正则；若命令仅退出非零而无设备诊断，则记为工具错误，不能算检出。两者均保留实际命令、退出码与原始日志；日志可能包含环境路径，不应公开上传私有路径或秘密。

文件：`results/upstream/nvidia/summary.json`、`environment.json` 与按 case 分类的 build/vendor/custom 日志。诊断字段 `vendor_diagnostic`、`custom_diagnostic` 与 `gpu_evidence` **分别存放**。硬件可见与实际 kernel 的完成不是一回事：没有真实设备日志，状态只能是 `GPU_NOT_RUN` 或 `GPU_VISIBLE_LAUNCH_ATTEMPTED`。

NV-SYNC-01/02 标记为 potentially dangerous：常规 CI 仅静态验证或编译，只有隔离实验 GPU 并明确加 `--allow-dangerous` 才运行坏例，设置较短超时。即使成功运行，也需手工核对硬件版本与诊断类型；此回归不会构成生产验收。

更多架构依据：[核心设计比较](../../../docs/core-design-comparison.md)。
