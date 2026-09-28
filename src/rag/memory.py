import json
import logging
import re
import threading
from collections import deque

from .config import Config
from .llm_generator import LLMGenerator

logger = logging.getLogger(__name__)

_SESSION_ID_UNSAFE = re.compile(r"[^A-Za-z0-9._\-]+")


def normalize_session_id(session_id: str | None) -> str:
    """把任意 session id 规范化为文件系统安全的字符串。

    RAG 记忆侧用 sha256 摘要落盘，不依赖本函数；本函数用于
    chat 上传工作区等需要可读目录名的场景。冒号、斜杠等一律
    替换为下划线，长度限制 120，空值回退 "default"。
    """
    text = (session_id or "").strip()
    if not text:
        text = "default"
    text = _SESSION_ID_UNSAFE.sub("_", text).strip("._")
    return (text[:120] or "default")


class MemoryManager:
    def __init__(self, llm: LLMGenerator):

        # 最大保留多少轮
        self.max_rounds = Config.MAX_HISTORY_ROUNDS

        # 保存路径
        self.persist_path = Config.HISTORY_PERSIST_PATH

        # 是否开启摘要
        self.enable_summary = Config.ENABLE_HISTORY_SUMMARY

        # LLM实例
        self.llm = llm


        # 最近对话
        self.history = deque()
    
        # 历史摘要
        self.summary = ""


        # 多线程保护
        self.lock = threading.Lock()


        # 加载历史
        self._load()

    def add_turn(
        self,
        user_msg,
        assistant_msg
    ):

        self.history.append(
            {
                "role":"user",
                "content":user_msg
            }
        )


        self.history.append(
            {
                "role":"assistant",
                "content":assistant_msg
            }
        )


        if (len(self.history)>self.max_rounds*2 
        and Config.ENABLE_HISTORY_SUMMARY):

            self.summarize()


        self._persist()
#一旦历史对话超过设定的最大轮数，最旧的那一轮就会被自动“挤出去”，永远只保留最近的 N 轮。
    def get_recent_messages(self, n_rounds: int|None = None) -> list[dict[str, str]]:
        if n_rounds is None:
            return list(self.history).copy()
        total = len(self.history)
        #最近n轮对话的起始索引
        #如果计算值小于 0，就取 0；如果计算值大于 0，就取计算值本身
        start = max(0, total - n_rounds * 2)
        #双头队列转列表
        return list(self.history)[start:]
#格式化
    def format_history(self, n_rounds: int |None= None) -> str:
        msgs = self.get_recent_messages(n_rounds)
        if not msgs:
            return ""
        lines = [f"{'用户' if m['role']=='user' else '助手'}: {m['content']}" for m in msgs]
        return "\n".join(lines)

    def clear(self):
        self.history.clear()
        self._persist()

    def summarize(self):
        
        old=list(self.history)[
            :-self.max_rounds*2
        ]


        if not old:
            return


        text="\n".join(
            [
                f"{x['role']}:{x['content']}"
                for x in old
            ]
        )


        prompt=f"""

    总结以下历史。
    保留用户需求和关键事实。

    {text}

    """


        summary=self._call_llm_for_summary(prompt)


        if summary:

            self.summary=summary


            self.history=deque(
                list(self.history)[
                    -self.max_rounds*2:
                ]
            )
#用来调用模型做摘要
    def _call_llm_for_summary(self, prompt: str) -> str:
        # 由于 LLMGenerator.generate 需要 contexts，这里简化直接调用 API
        try:
            response = self.llm.client.chat.completions.create(
                model=Config.LLM_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=200
            )
            content = response.choices[0].message.content

            if content is None:
                return "模型未返回有效内容。"

            return content
        except Exception as e:
            #.error()：日志的级别严重错误
            logger.error(f"摘要调用失败: {e}")
            return ""
#本地化历史  跨会话记忆 防止意外中断丢失数据 便于调试和人工审计（开发友好）
    def _persist(self):

        if not self.persist_path:
            return


        try:

            self.persist_path.parent.mkdir(
                parents=True,
                exist_ok=True
            )


            with open(
                self.persist_path,
                "w",
                encoding="utf-8"
            ) as f:

                json.dump(
                    list(self.history),
                    f,
                    ensure_ascii=False,
                    indent=2
                )


        except Exception as e:

            logger.error(
                f"历史保存失败:{e}"
            )
    def _load(self):

        if not self.persist_path.exists():

            return


        try:

            with open(
                self.persist_path,
                "r",
                encoding="utf-8"
            ) as f:

                data=json.load(f)


            self.history=deque(
                data,
                maxlen=self.max_rounds*2
            )


            logger.info(
                f"历史加载成功:{len(self.history)}"
            )


        except Exception as e:

            logger.warning(
                f"历史加载失败:{e}"
            )

