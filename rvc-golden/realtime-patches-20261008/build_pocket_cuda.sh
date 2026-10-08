#!/usr/bin/env bash
set -euo pipefail
runtime_root=${1:?Specify the isolated runtime directory}
python_bin=${2:?Specify the Python executable}
int8_model=${3:?Specify the int8 model directory}
test_lock=${4:?Specify the shared test lock}
expected_commit=2e2543fbe9fae542f921d47a72d21d5a4ef0b710
test "$(git -C "$runtime_root/onnxruntime" rev-parse HEAD)" = "$expected_commit"
exec 9>"$test_lock"
flock -w 5 9
"$python_bin" - "$runtime_root" "$int8_model" <<'PY'
from pathlib import Path
import sys
import onnx
root = Path(sys.argv[1])
models = Path(sys.argv[2])
deps = root/'onnxruntime/cmake/deps.txt'
lines = []
for line in deps.read_text().splitlines():
    fields = line.split(';')
    if len(fields) == 3 and fields[1].startswith('https://github.com/') and '/archive/' in fields[1]:
        project, revision = fields[1][len('https://github.com/'):].split('/archive/', 1)
        kind = 'tar.gz' if revision.endswith('.tar.gz') else 'zip'
        revision = revision[:-(len(kind)+1)]
        if '/' in revision and not revision.startswith('refs/'):
            revision = revision.split('/')[0]
        fields[1] = f'https://codeload.github.com/{project}/{kind}/{revision}'
        line = ';'.join(fields)
    lines.append(line)
updated = '\n'.join(lines)+'\n'
if updated != deps.read_text():
    deps.write_text(updated)
ops = set()
for path in list(models.glob('*.onnx'))+list((root/'sherpa-onnx-pocket-tts-2026-01-26').glob('*.onnx')):
    model = onnx.load(path)
    assert all(o.domain == '' and o.version == 14 for o in model.opset_import), path
    ops.update(node.op_type for node in model.graph.node)
contrib = ['BiasGelu', 'DynamicQuantizeMatMul', 'FastGelu', 'FusedConv', 'FusedMatMul',
           'Gelu', 'LayerNormalization', 'MatMulIntegerToFloat', 'SimplifiedLayerNormalization',
           'SkipLayerNormalization', 'SkipSimplifiedLayerNormalization']
(root/'pocket-ops.config').write_text('ai.onnx;14;'+','.join(sorted(ops))+'\ncom.microsoft;1;'+','.join(contrib)+'\n')
assert not {'Attention', 'RotaryEmbedding'} & ops
cuda_cmake = root/'onnxruntime/cmake/onnxruntime_providers_cuda.cmake'
source = cuda_cmake.read_text()
if 'MOZART_POCKET_BUILD' not in source:
    for variable in ['onnxruntime_providers_cuda_cc_srcs', 'onnxruntime_providers_cuda_cu_srcs']:
        anchor = f'  list(FILTER {variable} EXCLUDE REGEX "core/providers/cuda/plugin/.*")'
        assert source.count(anchor) == 1
        source = source.replace(anchor, anchor+f'\n  if(MOZART_POCKET_BUILD)\n    list(FILTER {variable} EXCLUDE REGEX "core/providers/cuda/llm/.*")\n  endif()')
    anchor = 'elseif(onnxruntime_DISABLE_CONTRIB_OPS AND NOT onnxruntime_CUDA_MINIMAL)'
    assert source.count(anchor) == 1
    source = source.replace(anchor, 'elseif(onnxruntime_DISABLE_CONTRIB_OPS AND NOT onnxruntime_CUDA_MINIMAL AND NOT MOZART_POCKET_BUILD)')
source = source.replace('if(onnxruntime_cuda_flash_attention_srcs)',
                        'if(onnxruntime_cuda_flash_attention_srcs AND NOT MOZART_POCKET_BUILD)')
if 'mozart_pocket_cuda=2' not in source:
    source += '''
if(MOZART_POCKET_BUILD)
  set_property(GLOBAL APPEND PROPERTY JOB_POOLS mozart_pocket_cuda=2)
  set_property(TARGET onnxruntime_providers_cuda PROPERTY JOB_POOL_COMPILE mozart_pocket_cuda)
  set_property(TARGET onnxruntime_providers_cuda PROPERTY CUDA_COMPILER_LAUNCHER "")
endif()
'''
if source != cuda_cmake.read_text():
    cuda_cmake.write_text(source)
PY
exec "$python_bin" "$runtime_root/onnxruntime/tools/ci_build/build.py" \
  --build_dir "$runtime_root/build" --config Release --update --build \
  --parallel 4 --nvcc_threads 1 --cmake_generator Ninja --skip_tests --skip_submodule_sync \
  --build_shared_lib --use_cuda --cuda_home /usr/local/cuda --cudnn_home /usr \
  --disable_cuda_nhwc_ops --disable_contrib_ops --disable_ml_ops --include_ops_by_config "$runtime_root/pocket-ops.config" \
  --cmake_extra_defines CMAKE_CUDA_ARCHITECTURES=87 \
  MOZART_POCKET_BUILD=ON \
  onnxruntime_USE_FLASH_ATTENTION=OFF onnxruntime_USE_MEMORY_EFFICIENT_ATTENTION=OFF \
  onnxruntime_USE_TRT_FUSED_ATTENTION=OFF \
  onnxruntime_BUILD_UNIT_TESTS=OFF onnxruntime_USE_TELEMETRY=OFF
