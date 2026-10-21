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
案例 S28：多Lane越界报告与有界日志
GPU真实场景及错误原因：多个lane触发GPU越界，测试工具报告上限/汇聚；无法靠应用证明sanitizer内部OOM机制
本后端的GPU Sanitizer预期：conditional。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

__global__ void s28_many(unsigned* data,int bad) {
    unsigned tid=blockIdx.x*blockDim.x+threadIdx.x;
    if(bad) {
        // 每个线程写出仅64字节的对象，用于报告限额与诊断聚合。
        data[16+tid]=tid;
    } else if(tid<16) data[tid]=tid;
}

int main(int argc,char** argv) {
    unsigned* data=nullptr; GPU_CHECK(gpuMalloc((void**)&data,16*sizeof(unsigned)));
    GPU_CHECK(gpuMemset(data,0,16*sizeof(unsigned)));
    s28_many<<<4,64>>>(data,is_bad(argc,argv));
    GPU_CHECK(gpuDeviceSynchronize()); GPU_CHECK(gpuFree(data)); return 0;
}
