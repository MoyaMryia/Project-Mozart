#!/usr/bin/env python3
"""保留 Golden 特征与音高，用后端相同随机数算法生成 PyTorch 对照。"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

import numpy as np
import soundfile as sf
import torch


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--checkpoint', type=Path, required=True)
    parser.add_argument('--rvc-source', type=Path, required=True)
    parser.add_argument('--tensor-dir', type=Path, required=True)
    parser.add_argument('--backend', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(exist_ok=False)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'tools'))
    from export_generator_onnx import load_generator
    from compare_streaming_quality import compare
    torch.set_num_threads(2)
    wrapper, rate = load_generator(args.checkpoint, args.rvc_source)
    if rate != 48000 or wrapper.generator.inter_channels != 192 or wrapper.generator.dec.m_source.l_sin_gen.dim != 1:
        raise ValueError('This diagnostic requires the verified 48 kHz, 192-channel, single-source contract')
    def captured(name):
        return np.load(args.tensor_dir/f'generator_00_{name}.npy')
    frames = captured('feats').shape[1]
    source = args.output/'native-noise.cpp'
    source.write_text('''#include <fstream>
#include <random>
#include <string>
#include <vector>
int main(int argc,char**argv){
 const int t=std::stoi(argv[1]);std::mt19937 r(114514);
 std::normal_distribution<float> n(0,1);std::uniform_real_distribution<float> u(0,1);
 std::vector<float> a(192*t),b(1),c(480*t);
 for(auto&x:a)x=n(r);for(auto&x:b)x=u(r);b.front()=0;for(auto&x:c)x=n(r);
 std::ofstream f(argv[2],std::ios::binary);
 for(auto*v:{&a,&b,&c})f.write(reinterpret_cast<char*>(v->data()),v->size()*sizeof(float));
}''')
    binary = args.output/'native-noise'
    subprocess.run(['g++','-O3','-std=c++17',str(source),'-o',str(binary)],check=True)
    values = args.output/'native-noise.bin'
    subprocess.run([str(binary),str(frames),str(values)],check=True)
    noise = np.fromfile(values,dtype=np.float32)
    end = 192*frames
    inputs = [captured('feats'),np.array([frames],np.float32),captured('pitch').astype(np.float32),
        captured('pitchf'),captured('sid'),noise[:end].reshape(1,192,frames),
        noise[end:end+1].reshape(1,1,1),noise[end+1:].reshape(1,480*frames,1)]
    with torch.inference_mode():
        expected = wrapper(*(torch.from_numpy(value) for value in inputs)).numpy().ravel()[rate:-rate]
    output = args.output/'qiqi-espeak-native-noise-python-reference.wav'
    sf.write(output,expected,rate,subtype='FLOAT')
    actual, actual_rate = sf.read(args.backend,dtype='float32')
    if actual_rate != rate or actual.shape != expected.shape:
        raise ValueError('Backend output shape differs')
    result = compare(output,args.backend,len(expected)*16000//rate+16000)
    result['waveform_relative_rmse'] = float(np.linalg.norm(actual-expected)/np.linalg.norm(expected))
    result['notes'] = ['保留原 Golden 特征、音高和 speaker；仅替换显式噪声。',
        '噪声来自相同 libstdc++、mt19937 种子 114514 及相同分布调用顺序。',
        '原始 Golden 输出保留；该补充对照用于区分随机噪声与上游数值差异。']
    (args.output/'comparison.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')


if __name__ == '__main__':
    main()
