"""Create an isolated sherpa package with a CUDA C API compatibility interface."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runtime_root', type=Path)
    args = parser.parse_args()
    root = args.runtime_root.resolve()
    library = root/'build/Release/libonnxruntime.so.1.29.0'
    if not library.is_file():
        parser.error('Build the CUDA shared library first')
    import sherpa_onnx
    source = Path(sherpa_onnx.__file__).parent
    binary = next((source/'lib').glob('_sherpa_onnx*.so'))
    symbols = subprocess.check_output(['readelf', '--dyn-syms', '--wide', str(binary)], text=True)
    imports = {line.split()[7].split('@')[0] for line in symbols.splitlines()
               if ' UND ' in line and len(line.split()) > 7 and line.split()[7].startswith('Ort')}
    if imports != {'OrtGetApiBase'} or 'OrtGetApiBase@VERS_1.27.1' not in symbols:
        parser.error('This procedure requires the sherpa C API 1.27.1 binding')
    package = root/'python/sherpa_onnx'
    if package.exists():
        parser.error('The isolated package directory already exists')
    package.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, package)
    if (source.parent/'sherpa_onnx.libs').exists():
        shutil.copytree(source.parent/'sherpa_onnx.libs', package.parent/'sherpa_onnx.libs', dirs_exist_ok=True)
    cpp = root/'ort-api-compat.cc'
    cpp.write_text('''#include <dlfcn.h>
#include <cstdio>
#include <cstdlib>
extern "C" const void* OrtGetApiBase() {
  using Function = const void* (*)();
  static Function function = []() {
    void* handle = dlopen(LIBRARY, RTLD_NOW | RTLD_LOCAL | RTLD_DEEPBIND);
    if (!handle) { std::fprintf(stderr, "%s\\n", dlerror()); std::abort(); }
    void* symbol = dlsym(handle, "OrtGetApiBase");
    if (!symbol) { std::fprintf(stderr, "%s\\n", dlerror()); std::abort(); }
    return reinterpret_cast<Function>(symbol);
  }();
  return function();
}
'''.replace('LIBRARY', json.dumps(str(library))))
    versions = root/'ort-api-compat.map'
    versions.write_text('VERS_1.27.1 { global: OrtGetApiBase; local: *; };\n')
    target = package/'lib/libonnxruntime.so'
    # Replace only the copied CPU library. Keep the source package unchanged.
    target.unlink()
    subprocess.run(['g++', '-std=c++17', '-shared', '-fPIC', '-O2', str(cpp), '-ldl',
                    '-Wl,--version-script='+str(versions), '-Wl,-soname,libonnxruntime.so',
                    '-o', str(target)], check=True)
    print(json.dumps({'pythonpath': str(package.parent), 'cuda_library': str(library),
                      'loader_library': str(target)}))


if __name__ == '__main__':
    main()
