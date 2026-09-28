import re
import ast
from typing import List, Tuple


class ActionParser:
    """
    Agent Action 解析器

    负责：
    1. 解析 Action 名称
    2. 解析 Action 参数
    3. 转换参数类型
    """

    def __init__(self):
        pass


    def parse_action(self, action: str) -> Tuple[str, List]:

        match = re.match(
            r"(\w+)\((.*?)\)",
            action,
            re.DOTALL
        )

        if not match:
            raise ValueError(
                "Invalid function call syntax"
            )

        act_name = match.group(1)

        args = match.group(2).strip()

        arguments = self._split_args_fixed(args)

        return act_name, arguments



    def _split_args_fixed(self, args: str):

        # 保存最终参数
        arguments = []

        # 当前字符
        char = ""

        # 是否在字符串中
        in_string = False

        # 括号栈
        bracket_stack = []

        # 当前字符串使用的引号
        string_char = None

        # 当前参数
        current_args = ""

        i = 0


        while i < len(args):

            char = args[i]


            if not in_string:

                # 进入字符串
                if char in ["'", '"']:

                    in_string = True

                    string_char = char

                    current_args += char


                # 记录括号
                elif char in "({[":

                    bracket_stack.append(char)

                    current_args += char


                # 结束括号
                elif char in ")}]":

                    bracket_stack.pop()

                    current_args += char


                # 当前参数结束
                elif char == "," and not bracket_stack:

                    arguments.append(
                        self.single_py(
                            current_args.strip()
                        )
                    )

                    current_args = ""


                else:

                    current_args += char


            else:

                current_args += char


                # 判断字符串结束
                if (
                    string_char == char
                    and args[i - 1] != "\\"
                ):

                    in_string = False

                    string_char = None


            i += 1



        # 最后一个参数
        if current_args.strip():

            arguments.append(
                self.single_py(
                    current_args.strip()
                )
            )


        return arguments



    def single_py(self, args: str):

        """
        将参数转换成 Python 类型

        例如：

        "hello" -> hello

        123 -> 123

        [1,2] -> [1,2]

        {"a":1} -> {"a":1}
        """

        # 字符串处理
        if (
            (args.startswith("'") and args.endswith("'"))
            or
            (args.startswith('"') and args.endswith('"'))
        ):

            args = args[1:-1]

            args = args.replace("\\'", "'")
            args = args.replace('\\"', '"')

            args = args.replace("\\t", "\t")
            args = args.replace("\\n", "\n")

            args = args.replace("\\\\", "\\")

            args = args.replace("\\r", "\r")

            return args


        try:

            return ast.literal_eval(args)


        except (ValueError, SyntaxError):

            # 容错
            return args