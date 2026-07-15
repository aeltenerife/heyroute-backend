import httpx
import json
import re

import os

QWEN_API_URL = os.getenv("QWEN_API_URL", "http://172.16.3.213:80/v1/chat/completions")
QWEN_MODEL_NAME = os.getenv("QWEN_MODEL_NAME", "Qwen/Qwen2.5-7B-Instruct-AWQ")

# extracts the JSON string from the LLM response
def extract_json_string(text: str) -> str:
    match = re.search(r'\{.*\}', text.strip(), re.DOTALL)
    if match:
        return match.group(0)
    return text

# send conversation history/prompt to the LLM server and get the parsed JSON response
async def process_with_llm(messages:list) -> dict:
    payload = {
        "model": QWEN_MODEL_NAME,
        "messages": messages,
        "temperature": 0.0,
        "max_tokens": 1024
    }

    headers = {"Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout = 30.0) as client:
        response = await client.post(QWEN_API_URL, headers=headers, json=payload)

        if response.status_code != 200:
            raise Exception(f"LLM Server Error: {response.text}")
        
        reply_text = response.json()['choices'][0]['message']['content']
        clean_json_string = extract_json_string(reply_text)
        return json.loads(clean_json_string)
