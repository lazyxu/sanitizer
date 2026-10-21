// GPU Guard Page：使用CUDA Driver API的GPU虚拟地址预留和按页映射，绝非mprotect CPU页。
 // 运行：nvcc -std=c++17 -lineinfo gpu_guard_page.cu -lcuda -o gpu_guard_page
 //       compute-sanitizer --tool memcheck ./gpu_guard_page --good
 //       compute-sanitizer --tool memcheck ./gpu_guard_page --bad --allow-fault
 // bad可能让GPU上下文fault/reset；须在隔离的GPU测试设备上运行。
#include <cuda.h>
#include <cuda_runtime.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#define CUDA_OK(call) do { auto e=(call); if(e!=cudaSuccess) { std::fprintf(stderr,"CUDA_RUNTIME_ERROR: %s\n",cudaGetErrorString(e));return 5;} } while(0)
#define CU_OK(call) do { CUresult e=(call); if(e!=CUDA_SUCCESS) { const char* n="unknown"; cuGetErrorName(e,&n); std::fprintf(stderr,"CUDA_DRIVER_ERROR: %s\n",n);return 5;} } while(0)
__global__ void gpu_guard_write(volatile std::uint32_t* last_word,int bad) {
    if(threadIdx.x==0) {
        // last_word[0]处于已映射页的最后4B；last_word[1]落入未映射的GPUVA页。
        last_word[bad?1:0]=0x12345678u;
    }
}
int main(int argc,char** argv) {
    bool bad=argc>1 && !std::strcmp(argv[1],"--bad");
    if(bad && (argc<3 || std::strcmp(argv[2],"--allow-fault"))) {
        std::fprintf(stderr,"bad可能造成GPU fault，请加--allow-fault显式开启\n");
        return 2;
    }
    CUDA_OK(cudaSetDevice(0));
    CUDA_OK(cudaFree(nullptr)); // 初始化runtime primary context
    int supported=0;
    CUDA_OK(cudaDeviceGetAttribute(&supported,cudaDevAttrVirtualAddressManagementSupported,0));
    if(!supported) {std::puts("SKIP: CUDA GPU VMM unsupported");return 77;}
    CUmemAllocationProp prop{};
    prop.type=CU_MEM_ALLOCATION_TYPE_PINNED;
    prop.location.type=CU_MEM_LOCATION_TYPE_DEVICE;
    prop.location.id=0;
    size_t gran=0; CU_OK(cuMemGetAllocationGranularity(&gran,&prop,CU_MEM_ALLOC_GRANULARITY_MINIMUM));
    if(gran==0 || gran>SIZE_MAX/2) return 6;
    // 预留2页GPUVA，但仅映射第一页。第二页保留不映射，作为GPU Guard Page。
    CUdeviceptr va=0;
    CU_OK(cuMemAddressReserve(&va,2*gran,gran,0,0));
    CUmemGenericAllocationHandle handle{};
    CU_OK(cuMemCreate(&handle,gran,&prop,0));
    CU_OK(cuMemMap(va,gran,0,handle,0));
    CUmemAccessDesc access{};
    access.location=prop.location;
    access.flags=CU_MEM_ACCESS_FLAGS_PROT_READWRITE;
    CU_OK(cuMemSetAccess(va,gran,&access,1));
    auto* last=reinterpret_cast<volatile std::uint32_t*>(va+gran-sizeof(std::uint32_t));
    gpu_guard_write<<<1,1>>>(last,bad?1:0);
    cudaError_t status=cudaDeviceSynchronize();
    if(status!=cudaSuccess) {
        std::fprintf(stderr,"GPU_RUNTIME_ERROR: %s (可能为真实GPUVA fault，不代表精确对象OOB)\n",cudaGetErrorString(status));
        return 5; // fault后context不可靠，停止继续调用VMM。
    }
    CU_OK(cuMemUnmap(va,gran));
    CU_OK(cuMemRelease(handle));
    CU_OK(cuMemAddressFree(va,2*gran));
    std::puts("GPU_VMM_GUARD_GOOD");
    return 0;
}
