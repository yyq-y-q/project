import os
import inspect
import platform

from string import Template


class PromptManager:
    """
    Prompt管理器

    负责：
    1. 生成工具描述
    2. 渲染系统Prompt
    """


    def __init__(self, tools: list, work_path: str):

        self.work_path = work_path

        self.tools = {
            func.__name__: func
            for func in tools
        }



    def get_tool_list(self) -> str:
        """
        生成工具说明列表

        提供给LLM知道有哪些工具可以调用
        """

        tools_descriptions = []


        for func in self.tools.values():

            name = func.__name__

            args = inspect.signature(func)

            doc = inspect.getdoc(func) or "无详细说明"


            tools_descriptions.append(
                f"- {name}{args}: {doc}"
            )


        return "\n".join(tools_descriptions)



    def get_system_prompt(self, temp_prompt: str):
        """
        将模板变量替换成真实信息
        """


        system_prompt = Template(
            temp_prompt
        ).substitute(

            tool_list=self.get_tool_list(),

            operating_system=self.get_operation_system_name(),


            file_list=",".join(

                os.path.abspath(
                    os.path.join(
                        self.work_path,
                        f
                    )
                )

                for f in os.listdir(
                    self.work_path
                )

            )

        )


        return system_prompt



    def get_operation_system_name(self):

        sys = {

            "Windows": "windows",

            "Linux": "Linux",

            "Darwin": "macOS"

        }


        return sys.get(
            platform.system(),
            "UNKNOWN"
        )