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
案例 S19：GPU指令级异步Copy缺少Wait
GPU真实场景及错误原因：CUDA SM80+ cp.async真实GPU指令；HIP版本为功能对照，不能冒充等价cp.async检测
本后端的GPU Sanitizer预期：conditional。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

__global__ void s19_async(const unsigned* src,unsigned* dst,int bad) {
    __shared__ unsigned shared_data[32];
    unsigned tid=threadIdx.x;
    if(tid<32) {
#if defined(GPU_BACKEND_CUDA) && defined(__CUDA_ARCH__) && __CUDA_ARCH__ >= 800
        // cp.async将Global复制到Shared，commit后需要等到本批完成。
        unsigned shared_addr=(unsigned)__cvta_generic_to_shared(&shared_data[tid]);
        const unsigned* global_addr=src+tid;
        asm volatile("cp.async.ca.shared.global [%0], [%1], 4;" :: "r"(shared_addr),"l"(global_addr) : "memory");
        asm volatile("cp.async.commit_group;" ::: "memory");
        if(!bad) asm volatile("cp.async.wait_group 0;" ::: "memory");
#else
        // HIP和CUDA<SM80：同步复制仅提供合法功能对照，不测试GPU异步指令。
        shared_data[tid]=src[tid];
#endif
    }
    __syncthreads();
    if(tid<32) dst[tid]=shared_data[tid];
}

int main(int argc,char** argv) {
    bool bad=is_bad(argc,argv);
#if defined(GPU_BACKEND_CUDA)
    cudaDeviceProp prop{}; GPU_CHECK(cudaGetDeviceProperties(&prop,0));
    if(prop.major<8) { std::puts("SKIP: CUDA cp.async requires SM80+"); return 77; }
#else
    if(bad) { std::puts("SKIP: HIP sample does not implement native async LDS copy"); return 77; }
#endif
    unsigned *src=nullptr,*dst=nullptr;
    GPU_CHECK(gpuMalloc((void**)&src,32*sizeof(unsigned)));
    GPU_CHECK(gpuMalloc((void**)&dst,32*sizeof(unsigned)));
    GPU_CHECK(gpuMemset(src,0x32,32*sizeof(unsigned)));
    s19_async<<<1,32>>>(src,dst,bad);
    GPU_CHECK(gpuDeviceSynchronize());
    GPU_CHECK(gpuFree(dst)); GPU_CHECK(gpuFree(src)); return 0;
}
