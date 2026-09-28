import logging
import json
import jieba
import numpy as np

from rank_bm25 import BM25Okapi
from .config import Config

logger = logging.getLogger(__name__)
#关键词召回
class BM25Index:
    """重建知识库时扔掉旧货，避免已删文件还留着"""
    def __init__(self):
        #切片原文
        self.corpus = []
        #分词原文
        self.tokenized_corpus = []
        #检索概率模型实例
        self.bm25 = None

    def build(self, chunks: list[dict]):
        logger.info("构建 BM25 索引...")
        #放置切片
        self.corpus = chunks
        #中文分词
        # 只对text进行分词
        self.tokenized_corpus = [
            jieba.lcut(
                item["text"]
            )
            for item in chunks
        ]
        #初始化对象 传入分词文档计算参数   频次的影响越高影响越大  长文档惩罚越高惩罚越重
        self.bm25 = BM25Okapi(
            self.tokenized_corpus,
                k1=1.5,
                b=0.75
            )
        data={
        "corpus":self.corpus,
        "tokens":self.tokenized_corpus
    }
        # 路径已是绝对 Path；父目录 data/ 可能尚未创建
        Config.BM25_PATH.parent.mkdir(parents=True, exist_ok=True)
        #保存chunk数据
        with open(Config.BM25_PATH, "w", encoding="utf-8") as f:
            # indent=2：给人看；ensure_ascii=False：中文不转 \uXXXX
            # 机器读 json.load 不在乎换不换行，这只影响可读性
            json.dump(data, f, ensure_ascii=False, indent=2)
            #保存对象实例
        # with open(
        #     Config.BM25_INDEX_PATH, 
        #     'wb') as f:
        #         pickle.dump(
        #             self.bm25,
        #             f
        #         )
        logger.info(f"BM25 索引已保存，文档数: {len(self.corpus)}")

    def clear(self) -> None:
        """内存 + 磁盘 BM25 一并清空（全量重建前调用）。"""
        self.corpus = []
        self.tokenized_corpus = []
        self.bm25 = None
        try:
            if Config.BM25_PATH.exists():
                Config.BM25_PATH.unlink()
                logger.info("已删除 BM25 文件: %s", Config.BM25_PATH)
        except Exception as e:
            logger.warning("删除 BM25 文件失败: %s", e)

    def load(self) -> bool:
        try:
            with open(Config.BM25_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
                self.corpus = data["corpus"]
                self.tokenized_corpus = data["tokens"]
                self.bm25 = BM25Okapi(self.tokenized_corpus)
            return True
        except FileNotFoundError:
            return False
        #     with open(Config.BM25_INDEX_PATH, 'rb') as f:
        #         #取出对象
        #         self.bm25 = pickle.load(f)
        #     logger.info(f"BM25 索引加载成功，文档数: {len(self.corpus)}")
        #     return True
        # except FileNotFoundError:
        #     #警告，出现了意外情况，但程序还能处理
        #     logger.warning("BM25 索引文件不存在，请先 build")
        #     return False

    def retrieve(
        self,
        query: str,
        top_k: int = Config.RETRIEVAL_TOP_K,
        principal=None,
    ):
        if self.bm25 is None:
            raise RuntimeError("BM25 索引未加载")
        #问题分词
        query_tokens = jieba.lcut(query)
        #问题算分 那几个参数再用公式计算
        scores = self.bm25.get_scores(query_tokens)
 #np.argsort()把分数从小到大排序，然后返回“排好序的索引位置”  -1逆序 topk前几个
 #输出的是索引
        sorted_indices = np.argsort(scores)[::-1][:max(top_k, top_k * 10)]
        docs = []
        scores_top = []
        for i in sorted_indices:
            # 拷贝，避免 retrieve 给文档挂分时污染 corpus 原件
            item = dict(self.corpus[i])
            item["metadata"] = dict(item.get("metadata") or {})
            item["bm25_score"] = float(scores[i])
            docs.append(item)
            scores_top.append(float(scores[i]))
        if principal is not None:
            from app.acl import filter_chunks_by_acl

            docs = filter_chunks_by_acl(docs, principal)
            scores_top = [float(item.get("bm25_score", 0.0)) for item in docs]
        return docs[:top_k], scores_top[:top_k]
