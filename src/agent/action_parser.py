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

    def _find_call_end(self, action: str) -> int:
        """找到真正闭合 Action 调用的右括号下标（支持嵌套括号与字符串内括号）。

        旧实现用非贪婪正则 \\(.*?\\) 匹配参数，参数内部一旦出现右括号
        （如 run_Terminal_Command(echo 'print("hi")')）就会提前截断，
        导致命令被腰斩、引号不闭合、shell 语法错误。这里改为扫描括号深度。
        """
        m = re.match(r"(\w+)\s*\(", action)
        if not m:
            return -1
        start = m.end() - 1  # 指向 '('
        depth = 0
        in_string = False
        string_char = None
        i = start
        while i < len(action):
            ch = action[i]
            if in_string:
                if ch == string_char and action[i - 1] != "\\":
                    in_string = False
                    string_char = None
                i += 1
                continue
            if ch in ("'", '"'):
                in_string = True
                string_char = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return -1

    def parse_action(self, action: str) -> Tuple[str, List, Dict]:
        end = self._find_call_end(action)
        if end < 0:
            raise ValueError("Invalid function call syntax")

        name_match = re.match(r"(\w+)\s*\(", action)
        act_name = name_match.group(1)
        args = action[name_match.end() : end].strip()
        positional, kwargs = self._split_args_fixed(args)
        # 兼容 JSON 风格调用：file_Write({"file_path": "...", "content": "..."})
        if not kwargs and len(positional) == 1 and isinstance(positional[0], dict):
            kwargs = positional[0]
            positional = []
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
