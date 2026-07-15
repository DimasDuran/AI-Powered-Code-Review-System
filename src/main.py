import os
from fastapi import FastAPI, Request
from anthropic import Anthropic
import requests

app = FastAPI()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

@app.post("/webhook/github")
async def handle_pr(request: Request):

    payload = await request.json()

    if payload.get("action") !="opened":
        return {"message": "Webhook received"}

    pr_number = payload['pull_request']['number']
    repo = payload['repository']['full_name']

    # Get PR diff
    diff_url = payload['pull_request']['diff_url']
    diff_response = requests.get(diff_url)
    diff_content = diff_response.text

    # Simple LLM review
    review = await review_code(diff_content)

    # Post comment to PR
    await post_github_comment(repo, pr_number, review)

    return {"status": "reviewed"}


async def review_code(diff_content: str) -> str:
    response = client.messages.create(
        model="anthropic.claude-haiku-4-5-20251001-v1:0",
        messages=[
            {
                "role": "system",
                "content": "You are a helpful assistant that reviews code changes and provides feedback on the changes."
            },
            {
                "role": "user",
                "content": f"Review this pull request:\n\n```diff\n{diff_content}\n```"
            }
        ]
    )
    return response.content[0].text


async def post_github_comment(repo: str, pr_number: int, comment: str):
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
        "Content-Type": "application/json"
    }
    data = {"body": comment}
    response = requests.post(url, json=data, headers=headers)



