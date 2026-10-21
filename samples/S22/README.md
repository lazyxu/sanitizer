# S22：被Mask关闭的非法地址不得误报

**真实GPU错误/边界**：bad模式故意构造inactive lane的非法地址，但未执行访存；正确Sanitizer不应报错。

- [CUDA独立源码](cuda.cu)，预期`clean`，推荐工具`memcheck`。
- [AMD HIP独立源码](amd.hip.cpp)，GPU ASan预期`clean`。
- `--good` 为合法对照，`--bad` 为故障/特殊合法边界。
- Docker内部运行：`python3 scripts/run_samples.py --backend cuda --case S22 --mode both` 或 `--backend rocm`。



**结果严格分级**：`detect`仅表示依据官方覆盖预计有GPU报告，未在本项目GPU实测；`conditional`表示GPU/版本/执行调度决定是否触发；`unsupported`不把无报告算通过；`clean`用于没有真实访存的mask场景；`api`只是GPU Runtime API错误。AMD的Host AddressSanitizer报告一律不算GPU Sanitizer命中。

参考：[CUDA Compute Sanitizer手册](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)、[ROCm GPU ASan文档](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)。
