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
案例 S02：16字节宽访问尾部越界
真正错误原因：真实16字节GPU读取跨越30字节对象末端；可能被lowering拆分
AMD 检测预期：detect；切勿将GPU Runtime错误或CPU ASan视为设备Sanitizer命中。
说明：--good 正常语义，--bad 故意触发；运行结果需通过GPU工具日志验证。
*/

struct alignas(16) Vec16 { unsigned a,b,c,d; };
__global__ void s02_wide(const unsigned char* src,unsigned* out,int bad) {
    if(threadIdx.x==0) {
        // src首地址按16B对齐。bad时读取偏移16..31，其中30,31无效。
        // 使用动态分支让目标机器码保留设备侧访存。
        const unsigned char* start=src+(bad?16:0);
        Vec16 v=*reinterpret_cast<const Vec16*>(start);
        out[0]=v.a+v.d; 
    }
}

int main(int argc,char** argv) {
    bool bad=is_bad(argc,argv);
    unsigned char* src=nullptr; unsigned* out=nullptr;
    GPU_CHECK(gpuMalloc((void**)&src,30));
    GPU_CHECK(gpuMalloc((void**)&out,sizeof(unsigned)));
    GPU_CHECK(gpuMemset(src,0x41,30));
    s02_wide<<<1,32>>>(src,out,bad);
    GPU_CHECK(gpuDeviceSynchronize());
    GPU_CHECK(gpuFree(out)); GPU_CHECK(gpuFree(src));
    return 0;
}
