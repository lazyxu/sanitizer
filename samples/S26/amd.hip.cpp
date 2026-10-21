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
案例 S26：未插桩设备函数与JIT覆盖缺口
GPU真实场景及错误原因：单TU提供最低限度例子；ROCm helper禁用address instrumentation，NVIDIA真正混合CU需额外多模块构建
本后端的GPU Sanitizer预期：unsupported。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

#if defined(GPU_BACKEND_ROCM)
__attribute__((no_sanitize("address")))
#endif
__device__ __attribute__((noinline)) void s26_legacy(unsigned* ptr,int idx) {
    ptr[idx]=17; // bad访问尾后，但ROCm helper可能被显式排除设备ASan插桩。
}
__global__ void s26_caller(unsigned* p,int bad) {
    if(threadIdx.x==0) s26_legacy(p,bad?16:15);
}

int main(int argc,char** argv) {
    unsigned* data=nullptr;
    GPU_CHECK(gpuMalloc((void**)&data,16*sizeof(unsigned)));
    GPU_CHECK(gpuMemset(data,0,16*sizeof(unsigned)));
    s26_caller<<<1,32>>>(data,is_bad(argc,argv));
    GPU_CHECK(gpuDeviceSynchronize()); GPU_CHECK(gpuFree(data)); return 0;
}
