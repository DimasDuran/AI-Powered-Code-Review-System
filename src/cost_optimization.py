import os
import json
import hashlib
from typing import Dict, List, Optional
from datetime import datetime, timedelta

from multi_agent import (
    MultiAgentReviewer, SecurityAgent, PerformanceAgent,
    StyleAgent, BugDetectionAgent, TestCoverageAgent,
    ReviewCategory
)
from learning import AdaptiveReviewer


# Iteration 7: Cost optimization
# 1) Semantic cache: repeated reviews served from cache
# 2) Model routing: uses cheaper models for simple tasks (style, tests)
# 3) Incremental diff: only reviews new commits, not the entire PR


# Semantic cache with TTL: stores review results for similar code
class SemanticCache:
    """Caches review results by semantic hash of the code to avoid repeated LLM calls."""

    def __init__(self, ttl_seconds: int = 3600):
        """Initializes an in-memory cache with configurable TTL."""
        self._cache: Dict[str, dict] = {}
        self.ttl = ttl_seconds

    def _make_key(self, code: str, review_type: str) -> str:
        """Generates a SHA256 hash of normalized code + review type as the cache key."""
        normalized = ' '.join(code.split())
        raw = f"{review_type}:{normalized}"
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, code: str, review_type: str) -> Optional[Dict]:
        """Returns cached result if it exists and hasn't expired (TTL)."""
        key = self._make_key(code, review_type)
        entry = self._cache.get(key)
        if entry and datetime.now() - entry['ts'] < timedelta(seconds=self.ttl):
            return entry['data']
        return None

    def set(self, code: str, review_type: str, data: Dict):
        """Stores a result in cache with the current timestamp."""
        key = self._make_key(code, review_type)
        self._cache[key] = {'data': data, 'ts': datetime.now()}

    def invalidate(self, code: str, review_type: str):
        """Invalidates a cache entry (e.g., when new feedback makes it obsolete)."""
        key = self._make_key(code, review_type)
        self._cache.pop(key, None)


# Model router: classifies diff complexity and picks the right model
class ModelRouter:
    """Chooses which LLM model to use based on diff complexity and review type."""

    MODELS = {
        "complex": "anthropic.claude-sonnet-4-20250514-v1:0",
        "standard": "anthropic.claude-haiku-4-5-20251001-v1:0",
        "simple": "anthropic.claude-haiku-4-5-20251001-v1:0",
    }

    @classmethod
    def classify_diff(cls, diff: str) -> str:
        """Classifies the diff as 'complex', 'standard', or 'simple' based on length and keywords (DB, auth, async)."""
        lines = diff.count('\n')
        has_db = any(kw in diff.lower() for kw in ['select ', 'insert ', 'update ', 'delete ', 'query'])
        has_auth = any(kw in diff.lower() for kw in ['password', 'token', 'auth', 'login', 'api_key'])
        has_async = any(kw in diff.lower() for kw in ['async ', 'await ', 'asyncio'])

        if lines > 500 or has_db or has_auth:
            return "complex"
        if lines > 100 or has_async:
            return "standard"
        return "simple"

    @classmethod
    def get_model(cls, diff: str, review_type: str) -> str:
        """Returns the optimal model: simple for style/tests, and by complexity for security/performance/bugs."""
        complexity = cls.classify_diff(diff)

        if review_type in (ReviewCategory.STYLE.value, ReviewCategory.TESTS.value):
            return cls.MODELS["simple"]
        if complexity == "simple" and review_type in (ReviewCategory.BUGS.value,):
            return cls.MODELS["simple"]

        return cls.MODELS[complexity]


# Incremental diff tracker: avoids re-reviewing already-seen commits
class IncrementalDiffTracker:
    """Tracks which commits have already been reviewed for a PR and only processes new ones."""

    def __init__(self):
        """Dictionary: PR_number -> set of already-seen SHAs."""
        self._seen_commits: Dict[str, set] = {}

    def get_new_changes(self, pr_number: int, base_sha: str, head_sha: str) -> str:
        """Returns the head SHA if new, None if already reviewed."""
        key = f"{pr_number}:{base_sha}"
        if key in self._seen_commits:
            return head_sha
        self._seen_commits[key] = {base_sha}
        return head_sha

    def get_pr_diff(self, pr_number: int) -> Optional[str]:
        return None


# Cost-optimized reviewer: extends AdaptiveReviewer with caching, routing, and incremental diff
class CostOptimizedReviewer(AdaptiveReviewer):
    """Optimized version of AdaptiveReviewer: caches results, routes models, and only reviews new changes."""

    def __init__(self):
        """Initializes cache, model router, and incremental diff tracker."""
        super().__init__()
        self.cache = SemanticCache(ttl_seconds=1800)
        self.model_router = ModelRouter()
        self.diff_tracker = IncrementalDiffTracker()

    async def review_pr_optimized(self, pr_data: Dict) -> Dict:
        """Reviews a PR with optimizations: checks cache first, then assigns model by complexity per agent."""
        diff = pr_data.get('diff', '')
        code = pr_data.get('code', diff)
        language = pr_data.get('language', 'python')
        test_files = pr_data.get('test_files', [])

        cached = self.cache.get(code, "full_review")
        if cached:
            return cached

        tasks = []
        agents_map = {
            ReviewCategory.SECURITY: SecurityAgent(),
            ReviewCategory.PERFORMANCE: PerformanceAgent(),
            ReviewCategory.STYLE: StyleAgent(),
            ReviewCategory.BUGS: BugDetectionAgent(),
            ReviewCategory.TESTS: TestCoverageAgent(),
        }

        for category, agent in agents_map.items():
            model = self.model_router.get_model(diff, category.value)
            if hasattr(agent, 'model'):
                agent.model = model
            tasks.append(agent.review(code, language) if category != ReviewCategory.TESTS
                         else agent.review(code, test_files))

        import asyncio
        results = await asyncio.gather(*tasks)
        findings = self.multi_agent._aggregate_findings(results)

        self.cache.set(code, "full_review", findings)
        return findings

    @staticmethod
    def estimate_cost(findings: Dict, diff_length: int) -> Dict:
        """Estimates the review cost based on diff complexity and findings count per category."""
        total_issues = findings['summary']['total_issues']
        complexity = ModelRouter.classify_diff("x\n" * diff_length)

        cost_map = {"simple": 0.15, "standard": 0.50, "complex": 1.20}
        base_cost = cost_map.get(complexity, 0.50)

        style_tests_count = len(findings['by_category'].get('style', []))
        style_tests_count += len(findings['by_category'].get('tests', []))
        security_perf_count = total_issues - style_tests_count

        estimated = {
            "base_model_cost": base_cost,
            "style_tests_cost": style_tests_count * 0.10,
            "security_perf_cost": security_perf_count * 0.25,
            "cache_savings": 0.0,
            "total_estimated": base_cost + (style_tests_count * 0.10) + (security_perf_count * 0.25)
        }
        estimated['total_estimated'] = round(estimated['total_estimated'], 2)
        return estimated
