import os
import json
import uuid
import time
import asyncio
import logging
import traceback
from enum import Enum
from typing import Dict, Optional

from fastapi import FastAPI, Request
import requests

from learning import AdaptiveReviewer


# Iteration 6: Scalability - async processing with priority queue
# PRs are queued and workers process them in the background according to priority


logger = logging.getLogger(__name__)
app = FastAPI()

redis_client = None


# Priorities: CRITICAL (main branch + >100 files) > HIGH > NORMAL > LOW (drafts/bots)
class PRPriority(Enum):
    CRITICAL = 1
    HIGH = 2
    NORMAL = 3
    LOW = 4


# In-memory dicts for pending and completed tasks (in production: Redis/Kafka)
PENDING_TASKS: Dict[str, dict] = {}
COMPLETED_TASKS: Dict[str, dict] = {}


# Endpoint that receives GitHub webhook, determines priority, and queues the task
@app.post("/webhook/github")
async def github_webhook(request: Request):
    """Receives the webhook, validates the event, determines priority, queues the task, and responds immediately."""
    payload = await request.json()
    event_type = request.headers.get('X-GitHub-Event')

    if event_type not in ['pull_request', 'pull_request_review']:
        return {"status": "ignored"}

    action = payload.get('action')
    if action not in ['opened', 'synchronize', 'reopened']:
        return {"status": "ignored"}

    pr_data = payload['pull_request']
    priority = determine_priority(pr_data)

    task = {
        "id": str(uuid.uuid4()),
        "platform": "github",
        "repo": payload['repository']['full_name'],
        "pr_number": pr_data['number'],
        "pr_url": pr_data['html_url'],
        "diff_url": pr_data['diff_url'],
        "base_branch": pr_data['base']['ref'],
        "files_changed": pr_data.get('changed_files', 0),
        "additions": pr_data.get('additions', 0),
        "deletions": pr_data.get('deletions', 0),
        "is_draft": pr_data.get('draft', False),
        "author": pr_data['user']['login'],
        "created_at": pr_data['created_at'],
        "priority": priority.name
    }

    PENDING_TASKS[task['id']] = task

    return {
        "status": "queued",
        "task_id": task['id'],
        "estimated_time_minutes": estimate_review_time(task),
    }


# Endpoint to check a task's status by ID
@app.get("/status/{task_id}")
async def get_status(task_id: str):
    """Public endpoint for users to check if their PR has been reviewed."""
    if task_id in COMPLETED_TASKS:
        return COMPLETED_TASKS[task_id]
    if task_id in PENDING_TASKS:
        return {"status": "queued", "task": PENDING_TASKS[task_id]}
    return {"status": "not_found"}


# Determines priority based on: base branch, file count, draft status
def determine_priority(pr_data: Dict) -> PRPriority:
    """Priority logic: main/master + many files = CRITICAL; drafts = LOW; rest = NORMAL."""
    base_branch = pr_data['base']['ref']
    files_changed = pr_data.get('changed_files', 0)
    is_draft = pr_data.get('draft', False)

    if base_branch in ['main', 'master', 'production']:
        return PRPriority.CRITICAL if files_changed > 100 else PRPriority.HIGH
    if is_draft:
        return PRPriority.LOW
    return PRPriority.NORMAL


# Estimates review time based on changed lines and priority
def estimate_review_time(task: Dict) -> int:
    """Heuristic formula: 2min base + 1min per 1000 lines, adjusted by priority (CRITICAL is faster)."""
    base = 2
    lines = task.get('additions', 0) + task.get('deletions', 0)
    estimated = base + (lines / 1000)
    multiplier = {'CRITICAL': 0.5, 'HIGH': 0.7, 'NORMAL': 1.0, 'LOW': 2.0}
    estimated *= multiplier.get(task['priority'], 1.0)
    return int(estimated)


# Individual worker that processes a PR: fetches diff, runs AdaptiveReviewer, saves results
class ReviewWorker:
    """Worker that processes a PR from the queue: fetches the diff, runs the review, and stores the result."""

    def __init__(self, worker_id: int, priority: PRPriority):
        """Assigns ID, priority, and creates the adaptive reviewer."""
        self.worker_id = worker_id
        self.priority = priority
        self.reviewer = AdaptiveReviewer()

    async def process_review(self, task: Dict):
        """Processes a PR: downloads the diff, builds pr_data, runs review_with_learning, saves the result."""
        start = time.time()
        logger.info(f"Worker {self.worker_id} processing task {task['id']}")

        try:
            diff_resp = requests.get(task['diff_url'])
            diff = diff_resp.text

            pr_data = {
                "pr_number": task['pr_number'],
                "diff": diff,
                "code": diff,
                "language": "python",
                "test_files": [],
                "repository": task['repo']
            }

            findings = await self.reviewer.review_with_learning(pr_data)
            latency = time.time() - start

            result = {
                "status": "completed",
                "task_id": task['id'],
                "latency_seconds": round(latency, 2),
                "issues_found": findings['summary']['total_issues'],
                "findings": findings
            }
            COMPLETED_TASKS[task['id']] = result
            PENDING_TASKS.pop(task['id'], None)

            logger.info(f"Task {task['id']} done in {latency:.1f}s")

        except Exception as e:
            logger.error(f"Task {task['id']} failed: {e}")
            COMPLETED_TASKS[task['id']] = {
                "status": "failed", "task_id": task['id'],
                "error": str(e)
            }
            PENDING_TASKS.pop(task['id'], None)


# Worker pool: distributes workers by priority and processes the queue
class WorkerPool:
    """Manages the worker pool: distributes load by priority (20% CRITICAL, 30% HIGH, 40% NORMAL, 10% LOW)."""

    def __init__(self):
        """Initializes an empty worker list."""
        self.workers = []
        self.running = False

    def start(self, num_workers: int = 10):
        """Creates N workers distributed proportionally by priority."""
        self.running = True
        dist = {
            PRPriority.CRITICAL: max(1, int(num_workers * 0.2)),
            PRPriority.HIGH: max(1, int(num_workers * 0.3)),
            PRPriority.NORMAL: max(1, int(num_workers * 0.4)),
            PRPriority.LOW: max(1, int(num_workers * 0.1))
        }
        wid = 0
        for priority, count in dist.items():
            for _ in range(count):
                self.workers.append(ReviewWorker(wid, priority))
                wid += 1

    async def process_queue(self):
        """Main loop: takes pending tasks sorted by priority and assigns them to available workers."""
        while self.running:
            if not PENDING_TASKS:
                await asyncio.sleep(1)
                continue

            sorted_tasks = sorted(
                PENDING_TASKS.items(),
                key=lambda t: PRPriority[t[1]['priority']].value
            )

            for task_id, task in sorted_tasks[:3]:
                worker = min(self.workers, key=lambda w: w.worker_id)
                await worker.process_review(task)

            if not sorted_tasks:
                await asyncio.sleep(0.5)
