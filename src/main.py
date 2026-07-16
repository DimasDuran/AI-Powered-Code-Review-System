import os
from fastapi import FastAPI, Request
from anthropic import Anthropic
import requests

app = FastAPI()
client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# Iteration 1: Bare minimum - single LLM reviews the diff and posts a general comment
# Problem: no codebase context, hallucinations, no specialized checks


# Endpoint that receives GitHub webhook when a PR is opened
@app.post("/webhook/github")
async def handle_pr(request: Request):
    """Receives the GitHub webhook, extracts the PR diff, sends it to the LLM, and posts the result as a comment."""

    payload = await request.json()

    if payload.get("action") != "opened":
        return {"message": "Webhook received"}

    pr_number = payload['pull_request']['number']
    repo = payload['repository']['full_name']

    # Fetch the PR diff from GitHub's diff_url
    diff_url = payload['pull_request']['diff_url']
    diff_response = requests.get(diff_url)
    diff_content = diff_response.text

    # Send diff to LLM for review
    review = await review_code(diff_content)

    # Post the review result as a comment on the PR
    await post_github_comment(repo, pr_number, review)

    return {"status": "reviewed"}


# Sends the diff to Claude and returns the review as text
async def review_code(diff_content: str) -> str:
    """Sends the full diff to Anthropic Claude with a generic code review prompt and returns the response."""
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


# Posts a comment on the GitHub PR using the Issues API
async def post_github_comment(repo: str, pr_number: int, comment: str):
    """Posts the review text as a comment on the PR using GitHub's REST API (issues/comments)."""
    url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
    headers = {
        "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
        "Content-Type": "application/json"
    }
    data = {"body": comment}
    response = requests.post(url, json=data, headers=headers)
