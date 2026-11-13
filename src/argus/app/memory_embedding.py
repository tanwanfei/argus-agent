import logging
from pathlib import Path

from argus.memory.ports import Embedder

logger = logging.getLogger(__name__)

EMBEDDING_MODEL_NAME = "bge-small-zh-v1.5"


def load_embedder(models_dir: Path) -> Embedder | None:
    model_dir = models_dir / "embedding" / EMBEDDING_MODEL_NAME
    try:
        from argus.infra.embedding.bge import BgeEmbedder

        embedder = BgeEmbedder(model_dir)
    except (ImportError, FileNotFoundError) as error:
        logger.warning("记忆向量化模型没加载上，情景记忆暂时不能用，语义记忆照常：%s", error)
        return None
    logger.info("记忆向量化模型已加载：%s（%d 维）", EMBEDDING_MODEL_NAME, embedder.dimension)
    return embedder
