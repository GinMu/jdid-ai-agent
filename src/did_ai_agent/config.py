"""运行配置。全部经 env 读入,默认值保证**离线可跑**(fake planner)。"""

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DID_AI_", env_file=".env", extra="ignore")

    planner: str = "fake"  # fake | langchain
    # model 用 "<provider>:<model-id>":deepseek 走 langchain-deepseek
    model: str = "deepseek:deepseek-flash"
    base_url: str = ""  # 留空 = 用 langchain-deepseek 的默认端点;本地桩测试才填
    api_key: str = ""  # 也从 DEEPSEEK_API_KEY 读(见下方 resolve_api_key)
    temperature: float = 0.0

    max_turns: int = 6
    rate_limit_per_min: int = 30
    audit_size: int = 200


@lru_cache
def get_settings() -> Settings:
    return Settings()


def resolve_api_key(settings: "Settings") -> str:
    """显式配置优先,其次读常见的 provider key 环境变量。"""
    import os

    return (
        settings.api_key
        or os.environ.get("DEEPSEEK_API_KEY", "")
        or os.environ.get("OPENAI_API_KEY", "")
    )
