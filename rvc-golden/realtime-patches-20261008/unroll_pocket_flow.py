"""Combine five Pocket flow steps. Keep the original weights and time values."""
import argparse
import copy
import json
from pathlib import Path
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or args.source.resolve() == args.output.resolve():
        parser.error('Specify a new output file. Keep the source model unchanged.')
    import numpy as np
    import onnx
    from onnx import helper, numpy_helper
    import onnxruntime as ort

    model = onnx.load(args.source)
    assert [v.name for v in model.graph.input] == ['c', 's', 't', 'x']
    assert len(model.graph.output) == 1
    assert all(a.type not in (onnx.AttributeProto.GRAPH, onnx.AttributeProto.GRAPHS)
               for node in model.graph.node for a in node.attribute)
    assert all(v.domain in ('', 'ai.onnx') for v in model.opset_import)
    nodes = []
    initializers = list(copy.deepcopy(model.graph.initializer))
    weights = {v.name for v in initializers}
    dt = np.float32(1 / 5)
    initializers.append(numpy_helper.from_array(np.asarray(dt), 'mozart_dt'))
    current = 'x'
    for step in range(5):
        prefix = f'mozart_step_{step}/'
        s = np.asarray([[np.float32(step) / np.float32(5)]], dtype=np.float32)
        t = s + dt
        initializers.extend([numpy_helper.from_array(s, prefix+'s'),
                             numpy_helper.from_array(t, prefix+'t')])
        mapping = {'c': 'c', 's': prefix+'s', 't': prefix+'t', 'x': current}
        def renamed(name):
            if not name or name in weights:
                return name
            return mapping.get(name, prefix+name)
        for source in model.graph.node:
            node = copy.deepcopy(source)
            node.name = prefix+node.name
            node.input[:] = [renamed(n) for n in source.input]
            node.output[:] = [prefix+n if n else '' for n in source.output]
            nodes.append(node)
        delta = prefix+'delta'
        next_x = prefix+'next_x'
        nodes.extend([helper.make_node('Mul', [prefix+model.graph.output[0].name, 'mozart_dt'], [delta]),
                      helper.make_node('Add', [current, delta], [next_x])])
        current = next_x
    # The existing C++ integrator adds the output once when num_steps is one.
    nodes.append(helper.make_node('Sub', [current, 'x'], [model.graph.output[0].name]))
    merged = copy.deepcopy(model)
    merged.graph.CopyFrom(helper.make_graph(nodes, 'mozart_five_step_flow',
        copy.deepcopy(model.graph.input), copy.deepcopy(model.graph.output), initializers))
    entry = merged.metadata_props.add()
    entry.key, entry.value = 'mozart_flow_steps', '5; caller num_steps must be 1'
    onnx.checker.check_model(merged)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    onnx.save(merged, args.output)

    options = ort.SessionOptions()
    options.intra_op_num_threads = 2
    options.inter_op_num_threads = 2
    baseline = ort.InferenceSession(str(args.source), options, providers=['CPUExecutionProvider'])
    combined = ort.InferenceSession(str(args.output), options, providers=['CPUExecutionProvider'])
    rng = np.random.default_rng(42)
    checks = []
    for batch in (1, 2):
        for scale in (.01, .1, 1.):
            for repeat in range(2):
                c = (rng.standard_normal((batch, 1024))*scale).astype(np.float32)
                noise = (rng.standard_normal((batch, 32))*.7**.5).astype(np.float32)
                x = noise.copy()
                started = time.monotonic()
                for step in range(5):
                    s = np.full((batch, 1), np.float32(step)/np.float32(5), dtype=np.float32)
                    velocity = baseline.run(None, {'c': c, 's': s, 't': s+dt, 'x': x})[0]
                    x += velocity*dt
                baseline_seconds = time.monotonic()-started
                started = time.monotonic()
                actual = noise+combined.run(None, {'c': c, 's': np.zeros((batch, 1), np.float32),
                    't': np.ones((batch, 1), np.float32), 'x': noise})[0]
                combined_seconds = time.monotonic()-started
                error = np.abs(actual-x)
                good = bool(np.isfinite(actual).all() and np.allclose(actual, x, rtol=1e-4, atol=1e-5))
                checks.append({'batch': batch, 'scale': scale, 'repeat': repeat,
                    'max_absolute_error': float(error.max()), 'mean_absolute_error': float(error.mean()),
                    'passed': good, 'baseline_seconds': baseline_seconds, 'combined_seconds': combined_seconds})
    report = {'checks': checks, 'all_passed': all(r['passed'] for r in checks),
        'steps': 5, 'caller_steps': 1, 'weights_shared': True,
        'scope': 'Synthetic flow tensors on CPU. Full speech and CUDA need separate tests.'}
    args.report.write_text(json.dumps(report, indent=2)+'\n')
    print(json.dumps(report, indent=2))
    if not report['all_passed']:
        raise RuntimeError('The combined flow failed numerical comparison')


if __name__ == '__main__':
    main()
