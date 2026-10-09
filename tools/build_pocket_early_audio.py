#!/usr/bin/env python3
"""Build an independent CPU runtime with early Pocket audio decoding."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile

SOURCE_SHA256 = '78f5d10f957d2de1867a1e08395e9ec2ec388911c853dd141887396667f3ff34'
ORT_SHA256 = '7c0dc460a78745792ee3c339f2369549a1396d60e79cef0b6616e3948205bda6'


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def obtain(path, url, expected):
    if not path.exists():
        temporary = path.with_suffix(path.suffix+'.download')
        urllib.request.urlretrieve(url, temporary)
        temporary.replace(path)
    if digest(path) != expected:
        raise RuntimeError(f'Archive hash mismatch: {path}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('root', type=Path)
    parser.add_argument('--jobs', type=int, default=2)
    args = parser.parse_args()
    if args.jobs < 1:
        parser.error('--jobs must be positive')
    root = args.root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    source_archive = root/'sherpa-onnx-v1.13.6.tar.gz'
    obtain(source_archive, 'https://codeload.github.com/k2-fsa/sherpa-onnx/tar.gz/refs/tags/v1.13.6', SOURCE_SHA256)
    ort_archive = root/'onnxruntime-linux-aarch64-glibc2_17-Release-1.27.1.zip'
    obtain(ort_archive, 'https://github.com/csukuangfj/onnxruntime-libs/releases/download/v1.27.1/'+ort_archive.name, ORT_SHA256)
    source = root/'sherpa-onnx-1.13.6'
    if not (source/'.mozart-source-ready').exists():
        with tarfile.open(source_archive) as archive:
            archive.extractall(root, members=(m for m in archive if m.isfile() or m.isdir()), filter='data')
        (source/'.mozart-source-ready').touch()
    ort = root/'ort'
    if not ort.exists():
        with zipfile.ZipFile(ort_archive) as archive:
            archive.extractall(ort)
    ort = next(ort.glob('*/include')).parent
    patch = Path(__file__).parent/'native/pocket-early-decode.patch'
    header = source/'sherpa-onnx/csrc/offline-tts-pocket-impl.h'
    if 'const bool early_decode =' not in header.read_text():
        subprocess.run(['patch', '-p1', '-i', str(patch.resolve())], cwd=source, check=True)
    # Use GitHub directly. Preserve upstream dependency archive hashes.
    for path in [source/'CMakeLists.txt', *source.glob('cmake/*.cmake')]:
        text = path.read_text()
        text = re.sub(r'https://github.com/([^/]+/[^/]+)/archive/(.+?)(\.tar\.gz|\.zip)',
            lambda m: 'https://codeload.github.com/'+m[1]+('/tar.gz/' if m[3] == '.tar.gz' else '/zip/')+m[2], text)
        text = re.sub(r'set\((\w+_URL2)\s+"https://[^\"]*"\)', r'set(\1 "")', text)
        path.write_text(text)
    environment = os.environ.copy()
    environment.update(SHERPA_ONNXRUNTIME_INCLUDE_DIR=str(ort/'include'),
                       SHERPA_ONNXRUNTIME_LIB_DIR=str(ort/'lib'))
    build = root/'build'
    subprocess.run(['cmake', '-S', str(source), '-B', str(build), '-G', 'Ninja',
        '-DCMAKE_BUILD_TYPE=Release', '-DBUILD_SHARED_LIBS=OFF', '-DSHERPA_ONNX_ENABLE_PYTHON=ON',
        '-DSHERPA_ONNX_ENABLE_BINARY=OFF', '-DSHERPA_ONNX_ENABLE_PORTAUDIO=OFF',
        '-DSHERPA_ONNX_ENABLE_WEBSOCKET=OFF', '-DSHERPA_ONNX_ENABLE_TESTS=OFF',
        '-DSHERPA_ONNX_BUILD_C_API_EXAMPLES=OFF', '-DSHERPA_ONNX_ENABLE_C_API=OFF',
        '-DSHERPA_ONNX_ENABLE_SPEAKER_DIARIZATION=OFF', '-DPython_EXECUTABLE='+sys.executable],
        env=environment, check=True)
    subprocess.run(['cmake', '--build', str(build), '--target', '_sherpa_onnx', '-j', str(args.jobs)], check=True)
    import sherpa_onnx
    package = root/'python/sherpa_onnx'
    shutil.copytree(Path(sherpa_onnx.__file__).parent, package, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('lib', '__pycache__'))
    library = package/'lib'
    library.mkdir(exist_ok=True)
    extension = next(build.rglob('_sherpa_onnx*.so'))
    shutil.copy2(extension, library/extension.name)
    shutil.copy2(ort/'lib/libonnxruntime.so', library/'libonnxruntime.so.1.27.1')
    for name in ('libonnxruntime.so', 'libonnxruntime.so.1'):
        link = library/name
        link.unlink(missing_ok=True)
        link.symlink_to('libonnxruntime.so.1.27.1')
    manifest = {'protocol': 1, 'sherpa_version': '1.13.6', 'source_sha256': SOURCE_SHA256,
        'patch_sha256': digest(patch), 'header_sha256': digest(header),
        'extension_sha256': digest(extension), 'onnxruntime_sha256': digest(library/'libonnxruntime.so'),
        'pythonpath': str(package.parent)}
    (package/'mozart_early_decode.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest))


if __name__ == '__main__':
    main()
