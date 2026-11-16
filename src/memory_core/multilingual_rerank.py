from __future__ import annotations

import platform
import threading

MODEL = 'cross-encoder/mmarco-mMiniLMv2-L12-H384-v1'
FLOAT_MODEL_FILE = 'onnx/model.onnx'
ARM_MODEL_FILE = 'onnx/model_qint8_arm64.onnx'
_registration_lock = threading.Lock()


def register_model() -> None:
    from fastembed.common.model_description import ModelSource
    from fastembed.rerank.cross_encoder import TextCrossEncoder

    with _registration_lock:
        if any(item['model'] == MODEL for item in TextCrossEncoder.list_supported_models()):
            return
        model_file = ARM_MODEL_FILE if platform.machine().lower() in ('arm64', 'aarch64') else FLOAT_MODEL_FILE
        TextCrossEncoder.add_custom_model(
            MODEL, sources=ModelSource(hf=MODEL), model_file=model_file,
            description='Multilingual mMARCO MiniLM cross-encoder', license='apache-2.0',
        )
