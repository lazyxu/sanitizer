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
案例 S22：被Mask关闭的非法地址不得误报
GPU真实场景及错误原因：bad模式故意构造inactive lane的非法地址，但未执行访存；正确Sanitizer不应报错
本后端的GPU Sanitizer预期：clean。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

__global__ void s22_mask(const unsigned* src,unsigned* dst,int bad) {
    unsigned t=threadIdx.x;
    // 只有前16个lane执行访存；后16个lane可构造不合法指针但不得解引用。
    unsigned idx=(bad && t>=16)?(1024+t):t;
    if(t<16) dst[idx]=src[idx];
}

int main(int argc,char** argv) {
    unsigned *src=nullptr,*dst=nullptr;
    GPU_CHECK(gpuMalloc((void**)&src,16*sizeof(unsigned)));
    GPU_CHECK(gpuMalloc((void**)&dst,16*sizeof(unsigned)));
    GPU_CHECK(gpuMemset(src,0x42,16*sizeof(unsigned)));
    s22_mask<<<1,32>>>(src,dst,is_bad(argc,argv));
    GPU_CHECK(gpuDeviceSynchronize());
    GPU_CHECK(gpuFree(dst)); GPU_CHECK(gpuFree(src)); return 0;
}
