"""Read provider information from the shared library that sherpa loaded on Linux."""
import ctypes
from pathlib import Path


def sherpa_runtime_info(module):
    library = (Path(module.__file__).parent/'lib/libonnxruntime.so').resolve(strict=True)
    mapped = {line.split(maxsplit=5)[-1].strip() for line in Path('/proc/self/maps').read_text().splitlines()}
    if str(library) not in mapped:
        raise RuntimeError('Sherpa did not load the expected ONNX Runtime library')
    native = ctypes.CDLL(str(library))
    native.OrtGetApiBase.restype = ctypes.POINTER(ctypes.c_void_p)
    base = native.OrtGetApiBase()
    class DynamicSymbol(ctypes.Structure):
        _fields_ = [('filename', ctypes.c_char_p), ('base', ctypes.c_void_p),
                    ('name', ctypes.c_char_p), ('address', ctypes.c_void_p)]
    symbol = DynamicSymbol()
    system = ctypes.CDLL(None)
    system.dladdr.argtypes = [ctypes.c_void_p, ctypes.POINTER(DynamicSymbol)]
    system.dladdr.restype = ctypes.c_int
    if not system.dladdr(base[0], ctypes.byref(symbol)) or not symbol.filename:
        raise RuntimeError('Cannot identify the ONNX Runtime C API implementation')
    implementation = Path(symbol.filename.decode()).resolve(strict=True)
    if str(implementation) not in mapped:
        raise RuntimeError('The ONNX Runtime C API library is not mapped')
    version = ctypes.CFUNCTYPE(ctypes.c_char_p)(base[1])().decode()
    api = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_uint32)(base[0])(8)
    if not api:
        raise RuntimeError('ONNX Runtime does not provide C API 8')
    functions = ctypes.cast(api, ctypes.POINTER(ctypes.c_void_p))
    # C API 8 defines these positions. Later versions preserve the table layout.
    # The build procedure verifies the positions against the official header.
    error_message = ctypes.CFUNCTYPE(ctypes.c_char_p, ctypes.c_void_p)(functions[2])
    release_status = ctypes.CFUNCTYPE(None, ctypes.c_void_p)(functions[93])
    def require_success(status):
        if status:
            message = error_message(status).decode()
            release_status(status)
            raise RuntimeError(message)
    names = ctypes.POINTER(ctypes.c_char_p)()
    count = ctypes.c_int()
    get_providers = ctypes.CFUNCTYPE(ctypes.c_void_p,
        ctypes.POINTER(ctypes.POINTER(ctypes.c_char_p)), ctypes.POINTER(ctypes.c_int))(functions[125])
    release_providers = ctypes.CFUNCTYPE(ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_char_p), ctypes.c_int)(functions[126])
    require_success(get_providers(ctypes.byref(names), ctypes.byref(count)))
    try:
        providers = [names[index].decode() for index in range(count.value)]
    finally:
        require_success(release_providers(names, count))
    return {'onnxruntime_library': str(implementation), 'onnxruntime_loader_library': str(library),
            'onnxruntime_version': version, 'available_providers': providers}


def require_cuda(runtime):
    if 'CUDAExecutionProvider' not in runtime.get('available_providers', []):
        raise RuntimeError('CUDA provider is unavailable in the loaded sherpa runtime; CPU fallback is disabled')
