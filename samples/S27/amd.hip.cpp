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
案例 S27：Graph Replay持有已释放GPU对象
GPU真实场景及错误原因：图捕获的设备指针在replay前释放，可能被API拒绝或设备fault；不保证统一诊断
本后端的GPU Sanitizer预期：unsupported。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

__global__ void s27_replay_access(unsigned* old) {
    if(threadIdx.x==0) old[0]=0xA7u;
}

int main(int argc,char** argv) {
    bool bad=is_bad(argc,argv); unsigned* data=nullptr;
    gpuStream_t stream{}; gpuGraph_t graph{}; gpuGraphExec_t executable{};
    GPU_CHECK(gpuMalloc((void**)&data,64));
    GPU_CHECK(gpuMemset(data,0,64));
    GPU_CHECK(gpuStreamCreate(&stream));
    GPU_CHECK(gpuStreamBeginCapture(stream,gpuStreamCaptureModeGlobal));
    s27_replay_access<<<1,32,0,stream>>>(data);
    GPU_CHECK(gpuStreamEndCapture(stream,&graph));
    GPU_CHECK(gpuGraphInstantiate(&executable,graph,nullptr,nullptr,0));
    if(bad) GPU_CHECK(gpuFree(data)); // graph保存了现在悬垂的指针。
    GPU_CHECK(gpuGraphLaunch(executable,stream));
    GPU_CHECK(gpuDeviceSynchronize());
    if(!bad) GPU_CHECK(gpuFree(data));
    GPU_CHECK(gpuGraphExecDestroy(executable));
    GPU_CHECK(gpuGraphDestroy(graph)); GPU_CHECK(gpuStreamDestroy(stream)); return 0;
}
