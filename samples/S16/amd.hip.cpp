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
案例 S16：不同GPU队列跨Kernel的并发写
GPU真实场景及错误原因：跨stream Global访问缺少event HB；两家选定工具均无全局跨Kernel确定性竞态覆盖
本后端的GPU Sanitizer预期：unsupported。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

__global__ void s16_writer(volatile unsigned* p,unsigned seed) {
    if(threadIdx.x==0) {
        // volatile避免访存完全被编译器折叠成一次写。
        for(unsigned i=0;i<1024;i++) p[0]=seed+i;
    }
}

int main(int argc,char** argv) {
    bool bad=is_bad(argc,argv);
    unsigned* p=nullptr; gpuStream_t a{},b{}; gpuEvent_t ready{};
    GPU_CHECK(gpuMalloc((void**)&p,sizeof(unsigned)));
    GPU_CHECK(gpuMemset(p,0,sizeof(unsigned)));
    GPU_CHECK(gpuStreamCreate(&a)); GPU_CHECK(gpuStreamCreate(&b));
    GPU_CHECK(gpuEventCreate(&ready));
    s16_writer<<<1,1,0,a>>>(p,100);
    if(!bad) {
        GPU_CHECK(gpuEventRecord(ready,a));
        GPU_CHECK(gpuStreamWaitEvent(b,ready,0));
    }
    // bad: 两个stream之间没有HB依赖；运行调度未必真正重叠。
    s16_writer<<<1,1,0,b>>>(p,200);
    GPU_CHECK(gpuDeviceSynchronize());
    GPU_CHECK(gpuEventDestroy(ready)); GPU_CHECK(gpuStreamDestroy(b));
    GPU_CHECK(gpuStreamDestroy(a)); GPU_CHECK(gpuFree(p)); return 0;
}
