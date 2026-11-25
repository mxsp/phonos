import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import torch
import os

MODULE_PATH = Path(__file__).resolve().parents[1] / 'node5-esm2.py'
ROOT_PATH = MODULE_PATH.parent
if str(ROOT_PATH) not in sys.path:
    sys.path.insert(0, str(ROOT_PATH))
os.environ.setdefault('ENCRYPTION_KEY', 'A' * 44)
SPEC = importlib.util.spec_from_file_location('node5_esm2', MODULE_PATH)
node5_esm2 = importlib.util.module_from_spec(SPEC)
assert SPEC is not None and SPEC.loader is not None
SPEC.loader.exec_module(node5_esm2)

ModelBundle = node5_esm2.ModelBundle
compute_embeddings = node5_esm2.compute_embeddings


class DummyTensor:
    def __init__(self, length):
        self.shape = (1, length)

    def to(self, *_args, **_kwargs):  # pragma: no cover - behaviour trivial
        return self


class DummyTokenizer:
    def __call__(self, sequence, return_tensors=None, add_special_tokens=True, padding=False, truncation=True):
        length = len(sequence) + (2 if add_special_tokens else 0)
        token = DummyTensor(length)
        return {'input_ids': token, 'attention_mask': token}


class DummyModel:
    def __init__(self):
        self.device = 'cpu'

    def to(self, device):
        self.device = device
        return self

    def eval(self):  # pragma: no cover - interface parity
        return self

    def __call__(self, **kwargs):
        if self.device.startswith('cuda'):
            raise RuntimeError('CUDA error: no kernel image is available for execution on the device')
        seq_len = kwargs['input_ids'].shape[1]
        embedding_dim = 4
        data = torch.ones((1, seq_len, embedding_dim))
        return SimpleNamespace(last_hidden_state=data)


def test_compute_embeddings_falls_back_to_cpu(monkeypatch):
    bundle = ModelBundle(tokenizer=DummyTokenizer(), model=DummyModel(), device='cpu')
    bundle.to('cuda')

    residues, pooled, summary = compute_embeddings('ACDEFGHIKL', bundle)

    assert bundle.device == 'cpu'
    assert bundle.force_cpu is True
    assert summary['device_used'] == 'cpu'
    assert residues.shape[0] == len('ACDEFGHIKL')
    assert pooled.shape[0] == residues.shape[1]
