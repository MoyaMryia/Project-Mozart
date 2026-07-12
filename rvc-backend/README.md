# Mozart RVC 后端

C++17 RVC 变声后端。详细文档见 [`docs/RVC_BACKEND.md`](../docs/RVC_BACKEND.md)。

```bash
mkdir build && cd build
cmake .. -DCMAKE_BUILD_TYPE=Release && make -j$(nproc)
./rvc_backend
```
