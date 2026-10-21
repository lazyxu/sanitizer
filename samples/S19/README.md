# S19：GPU指令级异步Copy缺少Wait

**真实GPU错误/边界**：CUDA SM80+ cp.async真实GPU指令；HIP版本为功能对照，不能冒充等价cp.async检测。

- [CUDA独立源码](cuda.cu)，预期`conditional`，推荐工具`synccheck`。
- [AMD HIP独立源码](amd.hip.cpp)，GPU ASan预期`unsupported`。
- `--good` 为合法对照，`--bad` 为故障/特殊合法边界。
- Docker内部运行：`python3 scripts/run_samples.py --backend cuda --case S19 --mode both` 或 `--backend rocm`。



**结果严格分级**：`detect`仅表示依据官方覆盖预计有GPU报告，未在本项目GPU实测；`conditional`表示GPU/版本/执行调度决定是否触发；`unsupported`不把无报告算通过；`clean`用于没有真实访存的mask场景；`api`只是GPU Runtime API错误。AMD的Host AddressSanitizer报告一律不算GPU Sanitizer命中。

参考：[CUDA Compute Sanitizer手册](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)、[ROCm GPU ASan文档](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)。
