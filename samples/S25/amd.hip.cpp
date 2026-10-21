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
案例 S25：Atomic指针值与独立身份元数据失配
GPU真实场景及错误原因：两个独立原子槽的值与ID非同一次发布；厂商GPU ASan不具有任意指针来源一致性判定
本后端的GPU Sanitizer预期：unsupported。若未检测到必须显示NOT_DETECTED/UNSUPPORTED。
--good使用合法对照；--bad触发错误或检查器边界。设备越界绝非CPU数组错误。
*/

__global__ void s25_atomic_pair(unsigned long long* metadata,int bad) {
    if(threadIdx.x==0) {
        // 将值与逻辑对象ID放在两个原子slot；它们之间没有原子整体性。
        atomicExch(&metadata[0], 0x1000ull);
        atomicExch(&metadata[1], 41ull);
    }
    if(bad && threadIdx.x==1) {
        // 另一个线程原子修改ID；地址和值没有同步变成同一个发布版本。
        atomicExch(&metadata[1], 92ull);
    }
}

int main(int argc,char** argv) {
    unsigned long long* metadata=nullptr;
    GPU_CHECK(gpuMalloc((void**)&metadata,2*sizeof(unsigned long long)));
    GPU_CHECK(gpuMemset(metadata,0,2*sizeof(unsigned long long)));
    s25_atomic_pair<<<1,32>>>(metadata,is_bad(argc,argv));
    GPU_CHECK(gpuDeviceSynchronize()); GPU_CHECK(gpuFree(metadata)); return 0;
}
