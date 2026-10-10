import re
import ast
from typing import List, Tuple, Dict


class ActionParser:
    """
    Agent Action 解析器

    负责：
    1. 解析 Action 名称
    2. 解析 Action 参数（支持位置参数与关键字参数，如 file_Write(file_path="a.txt", content="x")）
    3. 转换参数类型
    """

    def __init__(self):
        pass

    def parse_action(self, action: str) -> Tuple[str, List, Dict]:
        match = re.match(r"(\w+)\((.*?)\)", action, re.DOTALL)
        if not match:
            raise ValueError("Invalid function call syntax")

        act_name = match.group(1)
        args = match.group(2).strip()
        positional, kwargs = self._split_args_fixed(args)
        return act_name, positional, kwargs

    def _split_args_fixed(self, args: str):
        """拆分参数。返回 (位置参数列表, 关键字参数字典)。"""
        raw_items = []
        current_args = ""
        in_string = False
        string_char = None
        bracket_stack = []
        i = 0

        while i < len(args):
            char = args[i]
            if not in_string:
                if char in ["'", '"']:
                    in_string = True
                    string_char = char
                    current_args += char
                elif char in "({[":
                    bracket_stack.append(char)
                    current_args += char
                elif char in ")}]":
                    if bracket_stack:
                        bracket_stack.pop()
                    current_args += char
                elif char == "," and not bracket_stack:
                    raw_items.append(current_args.strip())
                    current_args = ""
                else:
                    current_args += char
            else:
                current_args += char
                if string_char == char and args[i - 1] != "\\":
                    in_string = False
                    string_char = None
            i += 1

        if current_args.strip():
            raw_items.append(current_args.strip())

        positional = []
        kwargs = {}
        for item in raw_items:
            if not item:
                continue
            m = re.match(
                r"^([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+)$",
                item,
                re.DOTALL,
            )
            if m:
                kwargs[m.group(1)] = self.single_py(m.group(2).strip())
            else:
                positional.append(self.single_py(item))
        return positional, kwargs

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
