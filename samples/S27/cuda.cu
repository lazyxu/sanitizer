// 本文件为可单独编译的GPU CUDA样例。
// 错误发生在GPU设备kernel/显存，绝不是CPU数组越界。通过 --bad / --good 选择。
// 错误路径仅供隔离调试；不同厂商Sanitizer的检测范围并不相同。
#define GPU_BACKEND_CUDA 1
#include <cstdio>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <cuda_runtime.h>
#define gpuError_t cudaError_t
#define gpuSuccess cudaSuccess
#define gpuGetErrorString cudaGetErrorString
#define gpuMalloc cudaMalloc
#define gpuFree cudaFree
#define gpuMemset cudaMemset
#define gpuMemcpy cudaMemcpy
#define gpuMemcpyDeviceToHost cudaMemcpyDeviceToHost
#define gpuMemcpyHostToDevice cudaMemcpyHostToDevice
#define gpuDeviceSynchronize cudaDeviceSynchronize
#define gpuDeviceReset cudaDeviceReset
#define gpuGetLastError cudaGetLastError
#define gpuStream_t cudaStream_t
#define gpuStreamCreate cudaStreamCreate
#define gpuStreamDestroy cudaStreamDestroy
#define gpuEvent_t cudaEvent_t
#define gpuEventCreate cudaEventCreate
#define gpuEventRecord cudaEventRecord
#define gpuEventDestroy cudaEventDestroy
#define gpuStreamWaitEvent cudaStreamWaitEvent
#define gpuMallocAsync cudaMallocAsync
#define gpuFreeAsync cudaFreeAsync
#define gpuHostAlloc cudaHostAlloc
#define gpuHostFree cudaFreeHost
#define gpuHostAllocMapped cudaHostAllocMapped
#define gpuHostGetDevicePointer cudaHostGetDevicePointer
#define gpuGraph_t cudaGraph_t
#define gpuGraphExec_t cudaGraphExec_t
#define gpuStreamBeginCapture cudaStreamBeginCapture
#define gpuStreamEndCapture cudaStreamEndCapture
#define gpuStreamCaptureModeGlobal cudaStreamCaptureModeGlobal
#define gpuGraphInstantiate cudaGraphInstantiate
#define gpuGraphLaunch cudaGraphLaunch
#define gpuGraphExecDestroy cudaGraphExecDestroy
#define gpuGraphDestroy cudaGraphDestroy
#define GPU_CHECK(expr) do { gpuError_t e=(expr); if(e!=gpuSuccess) { std::fprintf(stderr,"GPU_RUNTIME_ERROR %s:%d %s\n",__FILE__,__LINE__,gpuGetErrorString(e)); return 5; }}while(0)
static bool is_bad(int argc,char** argv) { return argc>1 && std::strcmp(argv[1],"--bad")==0; }

/*
案例 S27：Graph Replay持有已释放GPU对象
GPU真实场景及错误原因：图捕获的设备指针在replay前释放，可能被API拒绝或设备fault；不保证统一诊断
本后端的GPU Sanitizer预期：conditional。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
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
