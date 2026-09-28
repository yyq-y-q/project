from pathlib import Path

# agent 包: src/agent/config.py → parents[2] = 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = PROJECT_ROOT / "data"


class Config:
    MODEL = "deepseek-chat"
    BASE_URL = "https://api.deepseek.com"
    API_KEY_ENV = "DEEP_SEEK_KEY"
    ENV_PATH = PROJECT_ROOT / ".env"

    MAX_TRY = 4
    MAX_TOKENS = 8192
    MAX_STEPS = 24

    # 默认 Agent 沙箱工作目录
    DEFAULT_WORK_DIR = DATA_DIR / "agent_sandbox"
