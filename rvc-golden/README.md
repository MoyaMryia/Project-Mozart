# RVC Golden Reference

This directory is an isolated PyTorch reference for comparing the Mozart C++
RVC implementation against the original RVC inference path.

It intentionally does not use Mozart preprocessing, ONNX Runtime, TensorRT, or
the handwritten C++ mel/F0 implementation.

Run:

```bash
RVC_CUDA_GRAPH=0 /home/moyamryia/vc_backend_venv/bin/python \
  rvc-golden/run_reference.py
```

Inputs, outputs, model hashes, and intermediate tensors are kept under this
directory so each C++ stage can be compared numerically.
