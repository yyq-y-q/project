import re
from collections.abc import Callable

from .config import Config
from .action_parser import ActionParser
from .model_client import ModelClient
from .prompt_manager import PromptManager
from .prompt_template import react_system_prompt_template


class ReActManager:
    def __init__(
        self,
        work_Path: str,
        tools: list[Callable],
        model: str,
        session_id: str = "default",
    ):
        self.work_Path = work_Path
        self.session_id = session_id or "default"
        self.tools = {func.__name__: func for func in tools}
        self.model = model
        self.model_client = ModelClient(model)
        self.action_parser = ActionParser()
        self.prompt_manager = PromptManager(tools, work_Path)

    def run(self, task: str) -> str:
        max_try = Config.MAX_TRY
        max_steps = getattr(Config, "MAX_STEPS", 24)
        try_time = 0
        try_timeaction = 0
        steps = 0

        messages = [
            {
                "role": "system",
                "content": self.prompt_manager.get_system_prompt(
                    react_system_prompt_template
                ),
            },
            {
                "role": "user",
                "content": (
                    f"<session_id>{self.session_id}</session_id>\n"
                    f"<question>{task}</question>"
                ),
            },
        ]

        while True:
            steps += 1
            if steps > max_steps:
                raise RuntimeError(f"超过最大步数 {max_steps}，已中止")

            content = self.model_client.call_model(messages)

            thought_match = re.search(
                r"<thought>(.*?)</thought>", content, re.DOTALL
            )
            if thought_match:
                print(f"\n\n💡thought:{thought_match.group(1)}")

            if "<final_answer>" in content:
                final_answer_match = re.search(
                    r"<final_answer>(.*?)</final_answer>", content, re.DOTALL
                )
                if final_answer_match:
                    return final_answer_match.group(1)
                if try_time <= max_try:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "<final_answer>格式不正确，"
                                "请确保内容在两个标签内</final_answer>"
                            ),
                        }
                    )
                    try_time += 1
                    continue
                raise RuntimeError("<final_answer>加载超时")

            action_match = re.search(
                r"<action>(.*?)</action>", content, re.DOTALL
            )
            if not action_match:
                if try_timeaction <= max_try:
                    try_timeaction += 1
                    messages.append(
                        {
                            "role": "user",
                            "content": "缺少有效action，请重新输出",
                        }
                    )
                    continue
                raise RuntimeError("<action>加载超时")

            try_timeaction = 0
            action = action_match.group(1)

            try:
                act_name, args = self.action_parser.parse_action(action)
            except (ValueError, SyntaxError) as e:
                try_timeaction += 1
                if try_timeaction <= max_try:
                    messages.append(
                        {
                            "role": "user",
                            "content": f"action解析失败:{e}",
                        }
                    )
                    continue
                raise RuntimeError(f"解析失败:{e}")

            print(f"\n\n🛠Action:{act_name}({','.join(map(str, args))})")

            if act_name not in self.tools:
                observation = f"未知工具: {act_name}"
            else:
                try:
                    # rag_Query 若只传 question，session 用工具默认；也可模型显式传
                    observation = self.tools[act_name](*args)
                except Exception as e:
                    observation = f"工具执行错误:{e}"

            # 截断过长 observation，避免上下文爆炸
            obs_str = str(observation)
            if len(obs_str) > 12000:
                obs_str = obs_str[:12000] + "\n...[truncated]"

            print(f"\n\n🔍observation:{obs_str[:2000]}")

            messages.append(
                {
                    "role": "user",
                    "content": f"<observation>{obs_str}</observation>",
                }
            )
