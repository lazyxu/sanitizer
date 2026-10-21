# GPU Guard Page：CUDA Driver VMM 和 AMD HIP VMM

**这是GPU Guard Page，不是Linux mprotect()、CPU ASan或Host堆内存越界实验。**

两份可单独编译的样例使用 GPU 虚拟内存：预留2个GPUVA granule，仅映射第一个，并且只给第一个授予设备读写权限。线程写入映射页最后4B合法；越过该页4B访问第二个未映射GPUVA granule时，可能产生GPU MMU fault。

**不是任意大小对象的字节级边界保护。** 对象可以只有64B，但GPU页最小granularity通常远大于64B；除非特殊安排位置，越界4B仍可能落在已映射页内。硬件fault报告也不等于ASan精确object report。VMM不受支持或设备缺必要权限时应返回`SKIP`，不是“Sanitizer无错误”。

- [CUDA完整源码](gpu_guard_page.cu)：参考[NVIDIA CUDA Driver API Virtual Memory Management](https://docs.nvidia.com/cuda/archive/13.4.2/cuda-driver-api/cuda_driver_api/group__CUDA__VA.html)；编译示例在源文件开头。
- [AMD HIP完整源码](gpu_guard_page.hip.cpp)：参考[AMD HIP Virtual Memory Management](https://rocm.docs.amd.com/projects/HIP/en/latest/how-to/hip_runtime_api/memory_management/virtual_memory.html)。

**运行限制：** bad只能在隔离的GPU环境中显式用`--bad --allow-fault`执行，有可能造成整个GPU context故障/复位。此实验不属于默认S01～S28自动测试，因为它测试的是GPU MMU映射、fault和驱动回传能力，而非厂商Sanitizer对64B对象的完整字节级检查。

**本环境未实际编译或运行。** CUDA/HIP VMM API的可用性随实际GPU、驱动与所选toolkit版本变化，真实验证时记录设备属性、granularity、工具版本和驱动错误。
