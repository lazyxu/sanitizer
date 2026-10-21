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
案例 S01：Global头部与尾部越界
真正错误原因：GPU堆对象之外4B写入；官方memcheck及受支持ROCm GPU ASan覆盖
NVIDIA 检测预期：detect；切勿将GPU Runtime错误或CPU ASan视为设备Sanitizer命中。
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
