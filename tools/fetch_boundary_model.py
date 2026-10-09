"""Download the fixed Chinese boundary model from its official GitHub repository."""
import argparse
import hashlib
from pathlib import Path
import tempfile
import urllib.request

from boundary_classifier import MODEL_SHA256


REVISION = '7193727df7e497b0993ad96688f34c3a6add0618'
MODEL_URL = f'https://raw.githubusercontent.com/segment-any-text/wtpsplit/{REVISION}/models/zh/model.onnx'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path.home()/'models/mozart-boundaries/nnsplit-zh.onnx')
    args = parser.parse_args()
    if args.output.exists():
        if hashlib.sha256(args.output.read_bytes()).hexdigest() != MODEL_SHA256:
            raise FileExistsError('Existing boundary model has a different SHA-256')
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(MODEL_URL, timeout=30) as source:
            data = source.read(4062820)
        if hashlib.sha256(data).hexdigest() != MODEL_SHA256:
            raise ValueError('Downloaded boundary model has a different SHA-256')
        with tempfile.NamedTemporaryFile(dir=args.output.parent, delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
        try:
            temporary.replace(args.output)
        finally:
            temporary.unlink(missing_ok=True)
    print(args.output)


if __name__ == '__main__':
    main()
