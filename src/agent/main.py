import os
import click

from .config import Config
from .react_manager import ReActManager
from .tools import make_file_tools, make_terminal_tool, sqlite_Query, rag_Query


@click.command()
@click.argument(
    "work_directory",
    type=click.Path(exists=True, file_okay=False, dir_okay=True),
    required=False,
)
@click.option("--task", "-t", default=None, help="直接传入任务（否则交互输入）")
@click.option("--session", "-s", default="cli", help="会话 id（影响 rag 记忆隔离）")
def main(work_directory, task, session):
    """
    ReAct Agent：

    - 工作目录沙箱内 file / terminal
    - 表数据 → sqlite_Query
    - 知识库 → rag_Query（可带 session）
    - 用户丢文件进 work 或 data/raw 后，用自然语言指挥后续操作
    """
    if work_directory:
        work_path = os.path.abspath(work_directory)
    else:
        Config.DEFAULT_WORK_DIR.mkdir(parents=True, exist_ok=True)
        work_path = str(Config.DEFAULT_WORK_DIR.resolve())

    file_Read, file_Write = make_file_tools(work_path)
    run_Terminal_Command = make_terminal_tool(work_path)

    # rag_Query 第二参数 session 由模型可选传入；默认 agent 工具签名已带默认值
    tools = [
        file_Read,
        file_Write,
        run_Terminal_Command,
        sqlite_Query,
        rag_Query,
    ]

    agent = ReActManager(
        work_path,
        tools,
        model=Config.MODEL,
        session_id=session or "cli",
    )

    if not task:
        task = input("请输入任务:")

    final_answer = agent.run(task)
    print(f"final_answer={final_answer}")


if __name__ == "__main__":
    main()
