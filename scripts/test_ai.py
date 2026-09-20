import os
import requests
from dotenv import load_dotenv

load_dotenv()

api_key = os.getenv("AI_API_KEY")
base_url = os.getenv("AI_BASE_URL")
model = os.getenv("AI_MODEL")

if not api_key:
    raise RuntimeError("没有找到 AI_API_KEY")

if not model:
    raise RuntimeError("没有找到 AI_MODEL")

response = requests.post(
    f"{base_url}/chat/completions",
    headers={
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    },
    json={
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "请只返回JSON，不要输出其他文字。",
            },
            {
                "role": "user",
                "content": "返回 {\"answer\":\"hello\"}",
            },
        ],
        "response_format": {
            "type": "json_object"
        },
        "max_tokens": 100,
    },
    timeout=60,
)

print("HTTP状态码:", response.status_code)
print(response.text)