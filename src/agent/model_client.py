from openai import OpenAI
import os
from dotenv import load_dotenv

from .config import Config


class ModelClient:

    def __init__(self, model):
        self.model = model

        self.client = OpenAI(
            base_url=Config.BASE_URL,
            api_key=self.get_key()
        )

    @staticmethod
    def get_key():
        load_dotenv(getattr(Config, "ENV_PATH", None))

        key = os.getenv(Config.API_KEY_ENV)

        if not key:
            raise ValueError(
                f"未找到 {Config.API_KEY_ENV} 环境变量，请在 .env 文件中设置。"
            )

        return key

    def call_model(self, messages: list):

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            max_tokens=Config.MAX_TOKENS
        )

        content = response.choices[0].message.content

        messages.append({
            "role": "assistant",
            "content": content
        })

        return content