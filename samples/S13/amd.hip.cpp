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
案例 S13：GPU global未初始化读取
真正错误原因：NVIDIA initcheck检查GPU未初始化读取；AMD GPU ASan检查地址有效性而非值定义性
AMD 检测预期：unsupported；切勿将GPU Runtime错误或CPU ASan视为设备Sanitizer命中。
说明：--good 正常语义，--bad 故意触发；运行结果需通过GPU工具日志验证。
*/

__global__ void s13_uninit(const unsigned* source,unsigned* out) {
    if(threadIdx.x==0) {
        // source[3]合法映射但bad从未写入过；不是越界。
        out[0]=source[3];
    }
}

int main(int argc,char** argv) {
    bool bad=is_bad(argc,argv);
    unsigned *source=nullptr,*out=nullptr;
    GPU_CHECK(gpuMalloc((void**)&source,16*sizeof(unsigned)));
    GPU_CHECK(gpuMalloc((void**)&out,sizeof(unsigned)));
    if(!bad) GPU_CHECK(gpuMemset(source,0,16*sizeof(unsigned)));
    s13_uninit<<<1,32>>>(source,out);
    GPU_CHECK(gpuDeviceSynchronize());
    GPU_CHECK(gpuFree(out)); GPU_CHECK(gpuFree(source)); return 0;
}
