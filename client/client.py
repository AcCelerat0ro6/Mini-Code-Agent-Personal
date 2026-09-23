import os
from anthropic import Anthropic

# 初始化 Anthropic 客户端与指定模型
client = Anthropic(base_url=os.getenv("ANTHROPIC_BASE_URL"))