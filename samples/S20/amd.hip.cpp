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
案例 S20：异步Free与跨Queue访问缺少依赖
GPU真实场景及错误原因：运行顺序取决于stream调度；NVIDIA有stream ordered allocation race检测但并非所有配置都保证触发
本后端的GPU Sanitizer预期：unsupported。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

__global__ void s20_access(unsigned* data) {
    if(threadIdx.x==0) data[0]=88;
}

int main(int argc,char** argv) {
    bool bad=is_bad(argc,argv); unsigned* p=nullptr;
    gpuStream_t owner{},worker{}; gpuEvent_t done{};
    GPU_CHECK(gpuMalloc((void**)&p,64));
    GPU_CHECK(gpuStreamCreate(&owner)); GPU_CHECK(gpuStreamCreate(&worker));
    GPU_CHECK(gpuEventCreate(&done));
    // worker需要仍存活的p，但bad在owner队列提交free，没有等待worker。
    s20_access<<<1,32,0,worker>>>(p);
    if(!bad) {
        GPU_CHECK(gpuEventRecord(done,worker));
        GPU_CHECK(gpuStreamWaitEvent(owner,done,0));
    }
    gpuError_t free_result=gpuFreeAsync(p,owner);
    if(free_result!=gpuSuccess) {
        std::fprintf(stderr,"SKIP: async free unavailable: %s\n",gpuGetErrorString(free_result));
        return 77;
    }
    GPU_CHECK(gpuDeviceSynchronize());
    GPU_CHECK(gpuEventDestroy(done));
    GPU_CHECK(gpuStreamDestroy(worker));
    GPU_CHECK(gpuStreamDestroy(owner)); return 0;
}
