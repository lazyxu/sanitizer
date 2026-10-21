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
案例 S01：Global头部与尾部越界
真正错误原因：GPU堆对象之外4B写入；官方memcheck及受支持ROCm GPU ASan覆盖
AMD 检测预期：detect；切勿将GPU Runtime错误或CPU ASan视为设备Sanitizer命中。
说明：--good 正常语义，--bad 故意触发；运行结果需通过GPU工具日志验证。
*/

__global__ void s01_store(unsigned* a, int bad, int head) {
    // 由运行时参数决定下标，避免常量表达式被优化器提前消除。
    if (threadIdx.x == 0) {
        int i = bad ? (head ? -1 : 16) : 15;
        a[i] = 7;  // 合法范围a[0..15]，越界发生在GPU global store。
    }
}

int main(int argc,char** argv) {
    bool bad=is_bad(argc,argv);
    bool head=(argc>2 && std::strcmp(argv[2],"--head")==0);
    unsigned* a=nullptr;
    GPU_CHECK(gpuMalloc((void**)&a,16*sizeof(unsigned)));
    GPU_CHECK(gpuMemset(a,0,16*sizeof(unsigned)));
    s01_store<<<1,32>>>(a,bad,head);
    GPU_CHECK(gpuDeviceSynchronize());
    GPU_CHECK(gpuFree(a));
    return 0;
}
