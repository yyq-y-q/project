import logging
from sentence_transformers import SentenceTransformer, CrossEncoder
from .config import Config

logger = logging.getLogger(__name__)

class ModelManager:#这不是为了“功能”，而是为了“设计规范”
    """
    rag模型管理：
    1.嵌入模型加载
    2.重排模型加载
    """
    _embedding_model = None
    _rerank_model = None

    @classmethod
    def get_embedding_model(cls):
        if cls._embedding_model is None:
            #信息级别
            logger.info(f"加载嵌入模型: {Config.EMBEDDING_MODEL}")
            #建立模型实例
            cls._embedding_model = SentenceTransformer(
                Config.EMBEDDING_MODEL,
                device=Config.EMBEDDING_DEVICE
            )
        return cls._embedding_model

    @classmethod
    def get_rerank_model(cls):
        if cls._rerank_model is None:
            logger.info(f"加载重排模型: {Config.RERANK_MODEL}")
            cls._rerank_model = CrossEncoder(
                Config.RERANK_MODEL,
                device=Config.RERANK_DEVICE,
                max_length=512
            )
        return cls._rerank_model
