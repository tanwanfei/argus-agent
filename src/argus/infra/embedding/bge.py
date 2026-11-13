import asyncio
from pathlib import Path

import numpy as np
import onnxruntime
from tokenizers import Tokenizer

from argus.memory.ports import Vector

_MAX_TOKENS = 512


class BgeEmbedder:
    def __init__(self, model_dir: Path, num_threads: int = 1) -> None:
        model_path = model_dir / "onnx" / "model.onnx"
        tokenizer_path = model_dir / "tokenizer.json"
        for required_file in (model_path, tokenizer_path):
            if not required_file.is_file():
                raise FileNotFoundError(f"找不到向量化模型文件：{required_file}")
        self._tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self._tokenizer.enable_truncation(max_length=_MAX_TOKENS)
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = num_threads
        options.inter_op_num_threads = 1
        self._session = onnxruntime.InferenceSession(
            str(model_path), options, providers=["CPUExecutionProvider"]
        )
        self._input_names = {model_input.name for model_input in self._session.get_inputs()}
        self.dimension = int(self._session.get_outputs()[0].shape[-1])

    async def embed(self, text: str) -> Vector:
        return await asyncio.to_thread(self._embed_blocking, text)

    def _embed_blocking(self, text: str) -> Vector:
        encoding = self._tokenizer.encode(text)
        model_inputs = {
            "input_ids": np.array([encoding.ids], dtype=np.int64),
            "attention_mask": np.array([encoding.attention_mask], dtype=np.int64),
            "token_type_ids": np.array([encoding.type_ids], dtype=np.int64),
        }
        model_inputs = {
            name: values for name, values in model_inputs.items() if name in self._input_names
        }
        token_vectors = self._session.run(None, model_inputs)[0]
        sentence_vector = token_vectors[0, 0]
        return tuple((sentence_vector / np.linalg.norm(sentence_vector)).tolist())
