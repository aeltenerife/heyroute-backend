import asyncio
import httpx
import os

async def main():
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            headers = {'X-API-Key': "HXILqmQs6Jyv0unJqjKh3ZYFTH1ApHOWdurrrZqGPUHFOzIjWhMDP3M69SUhh7LWC4yG"}
            # Send a dummy post without a file just to see if it connects
            response = await client.post("http://altdsidccf.dlsu.edu.ph:33070/transcribe", headers=headers)
            print(f"Status Code: {response.status_code}")
            print(f"Response: {response.text}")
    except Exception as e:
        print(f"Error: {e}")

asyncio.run(main())
