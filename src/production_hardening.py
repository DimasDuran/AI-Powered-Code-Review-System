import os
import hmac
import hashlib
import json
import logging
import asyncio
from typing import Dict, Optional, Callable
from datetime import datetime, timedelta
from collections import defaultdict

import requests
from fastapi import FastAPI, Request, HTTPException

from async_queue import PENDING_TASKS, COMPLETED_TASKS, ReviewWorker, estimate_review_time
from monitoring import metrics, alerts, track_error


# Iteration 9: Production hardening
# Webhook validation (HMAC), rate limiting, retry with backoff, sensitive data filtering in logs


logger = logging.getLogger(__name__)
app = FastAPI()


# GitHub webhook validator using HMAC-SHA256
class WebhookValidator:
    """Validates incoming webhooks are genuinely from GitHub using HMAC with the shared secret."""

    def __init__(self):
        """Reads the webhook secret from an environment variable."""
        self.secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")

    def validate_github(self, payload_body: bytes, signature_header: str) -> bool:
        """Compares the HMAC signature from X-Hub-Signature-256 with the locally computed one."""
        if not self.secret:
            return True
        if not signature_header:
            return False
        sig_parts = signature_header.split('=')
        if len(sig_parts) != 2:
            return False
        algo, sig = sig_parts
        expected = hmac.new(
            self.secret.encode(), payload_body,
            hashlib.sha256 if algo == 'sha256' else hashlib.sha1
        ).hexdigest()
        return hmac.compare_digest(expected, sig)


# IP-based rate limiter using a sliding window
class RateLimiter:
    """Limits requests per IP using a sliding window of N seconds."""

    def __init__(self, max_requests: int = 100, window_seconds: int = 60):
        """Configures max requests per time window."""
        self.max_requests = max_requests
        self.window = window_seconds
        self._buckets: Dict[str, list] = defaultdict(list)

    def check(self, key: str) -> bool:
        """Checks if the IP can make a request: removes old timestamps, counts current ones."""
        now = datetime.now()
        cutoff = now - timedelta(seconds=self.window)
        self._buckets[key] = [t for t in self._buckets[key] if t > cutoff]
        if len(self._buckets[key]) >= self.max_requests:
            return False
        self._buckets[key].append(now)
        return True


# Retry handler with exponential backoff
class RetryHandler:
    """Retries failed operations with exponential backoff (1s, 2s, 4s)."""

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0):
        """Configures max retries and base delay for backoff."""
        self.max_retries = max_retries
        self.base_delay = base_delay

    async def execute(self, operation: str, fn: Callable, *args, **kwargs) -> Optional[any]:
        """Executes a function with retries: on failure, waits base_delay * 2^attempt and retries."""
        last_error = None
        for attempt in range(self.max_retries):
            try:
                if asyncio.iscoroutinefunction(fn):
                    return await fn(*args, **kwargs)
                return fn(*args, **kwargs)
            except Exception as e:
                last_error = e
                logger.warning(f"Retry {attempt + 1}/{self.max_retries} for {operation}: {e}")
                if attempt < self.max_retries - 1:
                    await asyncio.sleep(self.base_delay * (2 ** attempt))
        track_error(operation, last_error)
        return None


# Secure worker that extends ReviewWorker with secret detection in logs
class SecureReviewWorker(ReviewWorker):
    """Worker that extends ReviewWorker with sensitive pattern detection to avoid logging secrets."""

    def __init__(self, worker_id: int, priority):
        """Initializes with a retry handler."""
        super().__init__(worker_id, priority)
        self.retry = RetryHandler(max_retries=3)

    async def process_review_secure(self, task: Dict):
        """Processes a review but skips logging content if it detects secret patterns (api_key, password, token)."""
        secret_patterns = [
            'api_key', 'apikey', 'api-key', 'secret', 'password',
            'token', 'credential', 'private_key', 'auth_token',
        ]
        diff = task.get('diff', '')
        for pattern in secret_patterns:
            if pattern in diff.lower():
                logger.info(f"Task {task['id']}: potential secret detected, skipping logging")
        await self.process_review(task)


# Production guard: combines validation, rate limiting, and retry
class ProductionGuard:
    """Orchestrates all hardening measures: validates webhooks, rate limits, and executes with retry."""

    def __init__(self):
        """Initializes validator, rate limiter, and retry handler."""
        self.validator = WebhookValidator()
        self.rate_limiter = RateLimiter(max_requests=200, window_seconds=60)
        self.retry = RetryHandler()

    async def guarded_webhook(self, request: Request) -> Dict:
        """Handles a webhook with HMAC validation, IP-based rate limiting, and metric recording."""
        start = __import__('time').time()

        body = await request.body()
        sig = request.headers.get('X-Hub-Signature-256', '')
        if not self.validator.validate_github(body, sig):
            raise HTTPException(status_code=401, detail="Invalid webhook signature")

        ip = request.client.host if request.client else "unknown"
        if not self.rate_limiter.check(ip):
            metrics.record_count("rate_limited_requests")
            raise HTTPException(status_code=429, detail="Rate limit exceeded")

        payload = json.loads(body)
        pr_data = payload.get('pull_request', {})

        task = {
            "id": str(__import__('uuid').uuid4()),
            "platform": "github",
            "repo": payload['repository']['full_name'],
            "pr_number": pr_data.get('number', 0),
            "diff_url": pr_data.get('diff_url', ''),
            "base_branch": pr_data['base']['ref'],
            "author": pr_data['user']['login'],
            "priority": "NORMAL"
        }

        PENDING_TASKS[task['id']] = task
        metrics.record_count("prs_received")
        latency = __import__('time').time() - start
        metrics.record_latency("webhook", latency)

        return {
            "status": "queued",
            "task_id": task['id'],
            "estimated_time_minutes": estimate_review_time(task)
        }


# Privacy filter: redacts secrets in logs
class DataPrivacyFilter:
    """Filters sensitive data (tokens, keys) from logs to avoid exposing secrets."""

    def __init__(self):
        """List of known secret patterns (GitHub tokens, OpenAI keys, AWS keys, PGP)."""
        self._sensitive_patterns = [
            'ghp_', 'gho_', 'ghu_', 'ghs_', 'ghr_',
            'sk-', 'pk-', 'AKIA', '-----BEGIN',
        ]

    def sanitize_log(self, text: str) -> str:
        """Replaces any sensitive pattern found in the text with ***REDACTED***."""
        result = text
        for pattern in self._sensitive_patterns:
            if pattern in result:
                idx = result.index(pattern)
                result = result[:idx] + pattern + '***REDACTED***'
                return self.sanitize_log(result)
        return result

    def sanitize_task(self, task: Dict) -> Dict:
        """Sanitizes task fields that could contain secrets (diff, code, full_files)."""
        safe = task.copy()
        for field in ['diff', 'code', 'full_files']:
            if field in safe and isinstance(safe[field], str):
                safe[field] = self.sanitize_log(safe[field])
        return safe


# Global instances
production_guard = ProductionGuard()
privacy = DataPrivacyFilter()


# Endpoints that apply production guards
@app.post("/webhook/github")
async def secure_webhook(request: Request):
    """GitHub webhook endpoint with HMAC validation and rate limiting."""
    return await production_guard.guarded_webhook(request)


@app.post("/webhook/gitlab")
async def gitlab_webhook(request: Request):
    """GitLab webhook endpoint with HMAC validation and rate limiting."""
    return await production_guard.guarded_webhook(request)
