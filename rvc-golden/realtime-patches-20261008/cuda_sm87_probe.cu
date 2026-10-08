#include <cuda_runtime.h>
#include <cublas_v2.h>
#include <cstdio>
__global__ void fill(float* p) { p[threadIdx.x] = 1.0f; }
int main() {
    float *a, *b, *c;
    if (cudaMalloc(&a, 64*sizeof(float)) != cudaSuccess ||
        cudaMalloc(&b, 64*sizeof(float)) != cudaSuccess ||
        cudaMalloc(&c, 64*sizeof(float)) != cudaSuccess) return 1;
    fill<<<1,64>>>(a); fill<<<1,64>>>(b);
    auto kernel = cudaDeviceSynchronize();
    printf("sm87 kernel: %s\n", cudaGetErrorString(kernel));
    cublasHandle_t h;
    auto init = cublasCreate(&h);
    printf("cublas init: %d\n", int(init));
    float one=1, zero=0;
    auto status = init == CUBLAS_STATUS_SUCCESS
        ? cublasSgemm(h,CUBLAS_OP_N,CUBLAS_OP_N,8,8,8,&one,a,8,b,8,&zero,c,8) : init;
    auto sync = cudaDeviceSynchronize();
    printf("cublas sgemm: %d; sync: %s\n", int(status), cudaGetErrorString(sync));
    float result=0;
    cudaMemcpy(&result,c,sizeof(float),cudaMemcpyDeviceToHost);
    printf("result: %f; expected: 8\n",result);
    if (init == CUBLAS_STATUS_SUCCESS) cublasDestroy(h);
    cudaFree(a); cudaFree(b); cudaFree(c);
    return kernel != cudaSuccess || status != CUBLAS_STATUS_SUCCESS || sync != cudaSuccess || result != 8;
}
