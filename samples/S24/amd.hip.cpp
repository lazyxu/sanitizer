// 本文件为可单独编译的GPU AMD HIP样例。
// 错误发生在GPU设备kernel/显存，绝不是CPU数组越界。通过 --bad / --good 选择。
// 错误路径仅供隔离调试；不同厂商Sanitizer的检测范围并不相同。
#define GPU_BACKEND_ROCM 1
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <hip/hip_runtime.h>
#define gpuError_t hipError_t
#define gpuSuccess hipSuccess
#define gpuGetErrorString hipGetErrorString
#define gpuMalloc hipMalloc
#define gpuFree hipFree
#define gpuMemset hipMemset
#define gpuMemcpy hipMemcpy
#define gpuMemcpyDeviceToHost hipMemcpyDeviceToHost
#define gpuMemcpyHostToDevice hipMemcpyHostToDevice
#define gpuDeviceSynchronize hipDeviceSynchronize
#define gpuDeviceReset hipDeviceReset
#define gpuGetLastError hipGetLastError
#define gpuStream_t hipStream_t
#define gpuStreamCreate hipStreamCreate
#define gpuStreamDestroy hipStreamDestroy
#define gpuEvent_t hipEvent_t
#define gpuEventCreate hipEventCreate
#define gpuEventRecord hipEventRecord
#define gpuEventDestroy hipEventDestroy
#define gpuStreamWaitEvent hipStreamWaitEvent
#define gpuMallocAsync hipMallocAsync
#define gpuFreeAsync hipFreeAsync
#define gpuHostAlloc hipHostMalloc
#define gpuHostFree hipHostFree
#define gpuHostAllocMapped hipHostMallocMapped
#define gpuHostGetDevicePointer hipHostGetDevicePointer
#define gpuGraph_t hipGraph_t
#define gpuGraphExec_t hipGraphExec_t
#define gpuStreamBeginCapture hipStreamBeginCapture
#define gpuStreamEndCapture hipStreamEndCapture
#define gpuStreamCaptureModeGlobal hipStreamCaptureModeGlobal
#define gpuGraphInstantiate hipGraphInstantiate
#define gpuGraphLaunch hipGraphLaunch
#define gpuGraphExecDestroy hipGraphExecDestroy
#define gpuGraphDestroy hipGraphDestroy
#define GPU_CHECK(expr) do { gpuError_t e=(expr); if(e!=gpuSuccess) { std::fprintf(stderr,"GPU_RUNTIME_ERROR %s:%d %s\n",__FILE__,__LINE__,gpuGetErrorString(e)); return 5; }}while(0)
static bool is_bad(int argc,char** argv) { return argc>1 && std::strcmp(argv[1],"--bad")==0; }

/*
案例 S24：CPU/GPU共享映射与可见性
GPU真实场景及错误原因：这是CPU与GPU共享映射的竞态演示；无独立跨CPU/GPU HB工具不能保证报告
本后端的GPU Sanitizer预期：unsupported。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

__global__ void s24_shared_mapping(volatile unsigned* mapped) {
    if(threadIdx.x==0) {
        for(unsigned i=0;i<2048;i++) mapped[0]=i;
    }
}

int main(int argc,char** argv) {
    bool bad=is_bad(argc,argv);
    void* host_memory=nullptr;
    GPU_CHECK(gpuHostAlloc(&host_memory,sizeof(unsigned),gpuHostAllocMapped));
    unsigned* device_alias=nullptr;
    GPU_CHECK(gpuHostGetDevicePointer((void**)&device_alias,host_memory,0));
    *reinterpret_cast<unsigned*>(host_memory)=0;
    s24_shared_mapping<<<1,1>>>(device_alias);
    // bad: Host写和设备kernel可能并发，无event/HB，工具并不自动检测。
    if(bad) *reinterpret_cast<volatile unsigned*>(host_memory)=99;
    GPU_CHECK(gpuDeviceSynchronize());
    if(!bad) *reinterpret_cast<volatile unsigned*>(host_memory)=99;
    GPU_CHECK(gpuHostFree(host_memory));
    return 0;
}
