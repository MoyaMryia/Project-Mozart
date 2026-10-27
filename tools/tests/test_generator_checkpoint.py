import unittest

try:
    import torch
except ImportError:
    torch = None


_TorchModule = torch.nn.Module if torch is not None else object


@unittest.skipUnless(torch is not None, "PyTorch is not installed")
class GeneratorCheckpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tools.export_generator_onnx import load_inference_weights

        cls.load_inference_weights = staticmethod(load_inference_weights)

    class TinyGenerator(_TorchModule):
        def __init__(self):
            super().__init__()
            self.conv = torch.nn.utils.weight_norm(torch.nn.Conv1d(2, 3, 3))
            self.linear = torch.nn.Linear(3, 2)
            self.enc_q = torch.nn.Linear(2, 1)

        def forward(self, value):
            value = self.conv(value).mean(dim=-1)
            return self.linear(value)

    def make_models(self):
        torch.manual_seed(17)
        normalized = self.TinyGenerator().eval()
        plain = self.TinyGenerator().eval()
        plain.load_state_dict(normalized.state_dict())
        torch.nn.utils.remove_weight_norm(plain.conv)
        return normalized, plain

    def test_plain_and_normalized_checkpoints_load_completely(self):
        normalized, plain = self.make_models()
        self.assert_models_match(normalized.state_dict())
        self.assert_models_match(plain.state_dict())

    def assert_models_match(self, source_weights):
        target = self.TinyGenerator().eval()
        expected = self.TinyGenerator().eval()
        if "conv.weight" in source_weights:
            torch.nn.utils.remove_weight_norm(expected.conv)
        expected.load_state_dict(source_weights)
        if hasattr(expected.conv, "weight_g"):
            torch.nn.utils.remove_weight_norm(expected.conv)
        self.load_inference_weights(target, source_weights)
        self.assertFalse(hasattr(target.conv, "weight_g"))
        self.assertFalse(hasattr(target.conv, "weight_v"))
        for expected_value, actual_value in zip(
            expected.parameters(), target.parameters()
        ):
            torch.testing.assert_close(actual_value, expected_value)
        sample = torch.randn(2, 2, 9)
        torch.testing.assert_close(target(sample), expected(sample))

    def test_missing_inference_weight_fails(self):
        _, plain = self.make_models()
        weights = dict(plain.state_dict())
        del weights["linear.bias"]
        with self.assertRaisesRegex(RuntimeError, "missing=.*linear.bias"):
            self.load_inference_weights(self.TinyGenerator(), weights)

    def test_unexpected_weight_fails(self):
        _, plain = self.make_models()
        weights = dict(plain.state_dict())
        weights["unexpected.weight"] = torch.ones(1)
        with self.assertRaisesRegex(RuntimeError, "unexpected=.*unexpected.weight"):
            self.load_inference_weights(self.TinyGenerator(), weights)

    def test_mixed_plain_and_normalized_weight_fails(self):
        normalized, plain = self.make_models()
        weights = dict(plain.state_dict())
        weights.update({
            name: value
            for name, value in normalized.state_dict().items()
            if name.startswith("conv.weight_")
        })
        with self.assertRaisesRegex(RuntimeError, "conflicting plain and normalized"):
            self.load_inference_weights(self.TinyGenerator(), weights)

    def test_missing_enc_q_weight_is_allowed(self):
        _, plain = self.make_models()
        weights = dict(plain.state_dict())
        del weights["enc_q.weight"]
        self.load_inference_weights(self.TinyGenerator(), weights)


if __name__ == "__main__":
    unittest.main()
