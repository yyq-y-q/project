import logging
import os

from dotenv import load_dotenv
from openai import OpenAI

from .config import Config

logger = logging.getLogger(__name__)


class LLMGenerator:

    def __init__(self):

        load_dotenv(Config.ENV_PATH)

        api_key = os.getenv(Config.LLM_API_KEY_ENV)

        if not api_key:
            raise ValueError(
                f"未找到 {Config.LLM_API_KEY_ENV}，请检查 .env"
            )

        self.client: OpenAI = OpenAI(
            api_key=api_key.strip(),
            base_url=Config.LLM_BASE_URL,
            timeout=30
        )

    # 用来做 RAG 问答调用大模型的函数
    def generate(
        self,
        query: str,
        contexts: list[dict]
    ) -> str:

        if not contexts:
            return "未找到相关资料。"

        context_text = "\n\n".join(
            [
                f"""
片段ID:{item['chunk_id']}

内容:
{item['text']}

来源:
{item['metadata']}
"""
                for item in contexts
            ]
        )

        # 历史会拼接到 query 中
        prompt = f"""你是一位知识助手，请根据用户的问题和下列片段生成准确的回答。

用户问题：{query}

相关片段：

{context_text}

请基于上述问题作答，不要编造。"""

        try:

            response = self.client.chat.completions.create(
                model=Config.LLM_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": prompt
                    }
                ],
                stream=False,
                temperature=0.3,
                max_tokens=2048
            )

            content = response.choices[0].message.content

            if content is None:
                return "模型未返回有效内容。"

            return content

        except Exception as e:

            logger.error(
                f"LLM 生成失败: {e}"
            )

            # LLM 错误不要暴露给用户
            return "服务暂时不可用，请稍后再试"