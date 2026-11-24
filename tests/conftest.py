"""测试一律用确定性假模型:不读开发者 .env 里的真实 provider 配置。"""

import os

os.environ["DID_AI_PLANNER"] = "fake"
os.environ["DID_AI_MODEL"] = ""
