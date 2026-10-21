// GPU Guard Page：用HIP GPU VMM API预留两个GPUVA页，仅映射第一页。
 // 运行：hipcc -x hip -std=c++17 -g --offload-arch=gfx90a:xnack+ -fsanitize=address -shared-libsan gpu_guard_page.hip.cpp -o gpu_guard_page
 // 注意：GPUVA fault是页保护，不是CPU mprotect；能否产生GPU ASan特定报告取决于平台。
#include <hip/hip_runtime.h>
#include <cstdint>
#include <cstdio>
#include <cstring>
#define HIP_OK(call) do { hipError_t e=(call); if(e!=hipSuccess) {std::fprintf(stderr,"HIP_RUNTIME_ERROR: %s\n",hipGetErrorString(e));return 5;} }while(0)
__global__ void gpu_guard_write(volatile std::uint32_t* last,int bad) {
    if(threadIdx.x==0) last[bad?1:0]=0x12345678u; // 1指向未映射GPUVA页。
}
int main(int argc,char** argv) {
    bool bad=argc>1 && !std::strcmp(argv[1],"--bad");
    if(bad && (argc<3 || std::strcmp(argv[2],"--allow-fault"))) {
        std::fprintf(stderr,"bad可能导致设备fault，请加--allow-fault\n");
        return 2;
    }
    int vmm=0;
    HIP_OK(hipDeviceGetAttribute(&vmm,hipDeviceAttributeVirtualMemoryManagementSupported,0));
    if(!vmm) {std::puts("SKIP: HIP GPU VMM unsupported");return 77;}
    hipMemAllocationProp prop{};
    prop.type=hipMemAllocationTypePinned;
    prop.location.type=hipMemLocationTypeDevice;
    prop.location.id=0;
    size_t gran=0;
    HIP_OK(hipMemGetAllocationGranularity(&gran,&prop,hipMemAllocationGranularityMinimum));
    if(gran==0 || gran>SIZE_MAX/2) return 6;
    void* base=nullptr; hipMemGenericAllocationHandle_t handle{};
    HIP_OK(hipMemAddressReserve(&base,2*gran,gran,nullptr,0));
    HIP_OK(hipMemCreate(&handle,gran,&prop,0));
    HIP_OK(hipMemMap(base,gran,0,handle,0));
    hipMemAccessDesc access{};
    access.location=prop.location;
    access.flags=hipMemAccessFlagsProtReadWrite;
    HIP_OK(hipMemSetAccess(base,gran,&access,1));
    auto* last=reinterpret_cast<volatile std::uint32_t*>(
         reinterpret_cast<std::uintptr_t>(base)+gran-sizeof(std::uint32_t));
    gpu_guard_write<<<1,1>>>(last,bad?1:0);
    hipError_t status=hipDeviceSynchronize();
    if(status!=hipSuccess) {
        std::fprintf(stderr,"HIP_GPU_RUNTIME_ERROR: %s（GPUVA fault，不应算成CPU ASan命中）\n",hipGetErrorString(status));
        return 5;
    }
    HIP_OK(hipMemUnmap(base,gran));
    HIP_OK(hipMemRelease(handle));
    HIP_OK(hipMemAddressFree(base,2*gran));
    std::puts("GPU_VMM_GUARD_GOOD");
    return 0;
}
