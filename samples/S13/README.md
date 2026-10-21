# S13：GPU global未初始化读取

**GPU场景**：NVIDIA initcheck检查GPU未初始化读取；AMD GPU ASan检查地址有效性而非值定义性

- CUDA源码：[cuda.cu](cuda.cu)，NVIDIA预期：`detect`，工具：`initcheck`。
- AMD HIP源码：[amd.hip.cpp](amd.hip.cpp)，GPU ASan预期：`unsupported`。
- 正常用例：`--good`；错误用例：`--bad`。宿主代码只分配/提交和读取报告；GPU错误发生在`__global__`/设备访存，S10～S12为资源/API类独立分类。
- 进入Docker后：`python3 scripts/run_samples.py --backend cuda --case S13 --mode both` 或将cuda换为rocm。

**结果解释**：`detect`表示根据官方工具能力期待GPU报告，但未经用户GPU实测；`conditional`受GPU/编译器/内存模型影响；`unsupported`不保证有GPU报告；`api`仅GPU Runtime API错误，不计作GPU Kernel Sanitizer命中。未检测到时必须如实输出`NOT_DETECTED`。修复对照`--good`不应有GPU报告。

官方依据：[NVIDIA Compute Sanitizer](https://docs.nvidia.com/compute-sanitizer/ComputeSanitizer/index.html)；[AMD GPU ASan](https://rocm.docs.amd.com/projects/llvm-project/en/docs-7.2.4/conceptual/using-gpu-sanitizer.html)。
