import os
import time
import json
import logging
from typing import Dict, Optional
from datetime import datetime, timedelta
from collections import defaultdict


# Iteration 8: Monitoring & observability
# MetricsCollector: latencies, counts, errors, costs, accuracy
# HealthChecker: periodically verifies components
# AlertManager: alerts when metrics exceed thresholds


logger = logging.getLogger(__name__)


# In-memory metrics collector (in production: Prometheus)
class MetricsCollector:
    """Records performance metrics: latencies, counts, errors, costs, and suggestion accuracy."""

    def __init__(self):
        """Initializes defaultdicts to store time series for each metric."""
        self._latencies: Dict[str, list] = defaultdict(list)
        self._counts: Dict[str, int] = defaultdict(int)
        self._errors: Dict[str, int] = defaultdict(int)
        self._costs: Dict[str, list] = defaultdict(list)
        self._accuracy: Dict[str, list] = defaultdict(list)

    def record_latency(self, operation: str, seconds: float):
        """Records the latency of an operation (e.g., full_review, webhook). Caps at 5000 samples."""
        self._latencies[operation].append(seconds)
        if len(self._latencies[operation]) > 10000:
            self._latencies[operation] = self._latencies[operation][-5000:]

    def record_count(self, metric: str, value: int = 1):
        """Increments a counter (e.g., prs_reviewed, rate_limited_requests)."""
        self._counts[metric] += value

    def record_error(self, operation: str):
        """Increments the error counter for an operation."""
        self._errors[operation] += 1

    def record_cost(self, pr_id: str, cost: float):
        """Records the review cost for a PR."""
        self._costs['all'].append(cost)
        self._costs[pr_id] = [cost]

    def record_feedback(self, suggestion_id: str, accepted: bool):
        """Records whether a suggestion was accepted (1.0) or rejected (0.0) for accuracy calculation."""
        self._accuracy['suggestions'].append(1.0 if accepted else 0.0)

    def get_latency_stats(self, operation: str) -> Dict:
        """Calculates latency statistics: avg, p50, p95, p99."""
        vals = self._latencies.get(operation, [])
        if not vals:
            return {"avg": 0, "p50": 0, "p95": 0, "p99": 0, "count": 0}
        sorted_vals = sorted(vals)
        n = len(sorted_vals)
        return {
            "avg": round(sum(sorted_vals) / n, 3),
            "p50": round(sorted_vals[n // 2], 3),
            "p95": round(sorted_vals[int(n * 0.95)], 3),
            "p99": round(sorted_vals[int(n * 0.99)], 3),
            "count": n
        }

    def get_snapshot(self) -> Dict:
        """Takes a full snapshot of all current metric states."""
        return {
            "timestamp": datetime.now().isoformat(),
            "counts": dict(self._counts),
            "errors": dict(self._errors),
            "latency": {
                op: self.get_latency_stats(op)
                for op in self._latencies
            },
            "cost": {
                "total": round(sum(self._costs.get('all', [])), 2),
                "avg_per_pr": round(
                    sum(self._costs.get('all', [])) / max(len(self._costs.get('all', [])), 1), 4
                ),
                "total_prs": len(self._costs.get('all', []))
            },
            "accuracy": {
                "suggestion_acceptance": round(
                    sum(self._accuracy.get('suggestions', [])) / max(len(self._accuracy.get('suggestions', [])), 1), 3
                ) if self._accuracy.get('suggestions') else 0
            }
        }


# Health checker: runs check functions on components and records their status
class HealthChecker:
    """Runs periodic checks on components (API, DB, LLM) and reports their health status."""

    def __init__(self):
        """Initializes dictionaries for last check time and healthy status."""
        self._last_check: Dict[str, datetime] = {}
        self._healthy: Dict[str, bool] = {}

    def check_component(self, name: str, check_fn) -> bool:
        """Runs a check function and records whether the component is healthy."""
        try:
            result = check_fn()
            self._healthy[name] = result
            self._last_check[name] = datetime.now()
            return result
        except Exception as e:
            logger.warning(f"Health check {name} failed: {e}")
            self._healthy[name] = False
            self._last_check[name] = datetime.now()
            return False

    def is_healthy(self, name: str) -> bool:
        """Returns the last known health status (assumes healthy if >60s since last check)."""
        last = self._last_check.get(name)
        if not last or datetime.now() - last > timedelta(seconds=60):
            return True
        return self._healthy.get(name, True)

    def all_healthy(self) -> bool:
        """Returns True only if all checked components are healthy."""
        return all(self._healthy.values()) if self._healthy else True


# Alert manager: checks metrics against thresholds and sends notifications
class AlertManager:
    """Monitors metrics against thresholds (warn/crit) and sends alerts via webhook."""

    def __init__(self, webhook_url: Optional[str] = None):
        """Configures the webhook URL for sending alerts (Slack, PagerDuty, etc)."""
        self.webhook_url = webhook_url
        self._alerts: list = []

    def check_threshold(self, name: str, value: float, warn_at: float, crit_at: float) -> Optional[str]:
        """Compares a value against thresholds: if above crit_at, sends alert; if above warn_at, logs warning."""
        if value >= crit_at:
            self._alerts.append({
                "type": "critical", "metric": name,
                "value": value, "threshold": crit_at,
                "timestamp": datetime.now().isoformat()
            })
            self._send_alert(f"CRITICAL: {name}={value} (threshold={crit_at})")
            return "critical"
        elif value >= warn_at:
            self._alerts.append({
                "type": "warning", "metric": name,
                "value": value, "threshold": warn_at,
                "timestamp": datetime.now().isoformat()
            })
            return "warning"
        return None

    def _send_alert(self, message: str):
        """Sends an alert via webhook HTTP and also logs it."""
        if self.webhook_url:
            try:
                import requests
                requests.post(self.webhook_url, json={"text": message}, timeout=5)
            except Exception as e:
                logger.error(f"Failed to send alert: {e}")
        logger.warning(f"ALERT: {message}")

    def get_recent_alerts(self, minutes: int = 60) -> list:
        """Returns alerts generated in the last N minutes."""
        cutoff = datetime.now() - timedelta(minutes=minutes)
        return [a for a in self._alerts if datetime.fromisoformat(a['timestamp']) > cutoff]


# Singleton instances
metrics = MetricsCollector()
health = HealthChecker()
alerts = AlertManager(os.getenv("ALERT_WEBHOOK_URL"))


# Helper: records metrics for a complete review and checks alert thresholds
def track_review_metrics(start_time: float, pr_id: str, num_issues: int, cost: float):
    """Measures total latency, records reviewed PR, cost, and checks alert thresholds."""
    latency = time.time() - start_time
    metrics.record_latency("full_review", latency)
    metrics.record_count("prs_reviewed")
    metrics.record_count(f"issues_{num_issues}")
    metrics.record_cost(pr_id, cost)

    alerts.check_threshold("review_latency", latency, warn_at=180, crit_at=300)
    alerts.check_threshold("cost_per_pr", cost, warn_at=1.50, crit_at=2.00)


# Helper: records an error in an operation
def track_error(operation: str, error: Exception):
    """Records an error in metrics and in the log."""
    metrics.record_error(operation)
    logger.error(f"Error in {operation}: {error}")


# Helper: generates a full system health report
async def report_health_status() -> Dict:
    """Compiles a report with metrics, component health, and recent alerts."""
    snapshot = metrics.get_snapshot()
    snapshot["health"] = {
        "all_healthy": health.all_healthy(),
        "components": dict(health._healthy)
    }
    snapshot["alerts"] = {
        "recent_count": len(alerts.get_recent_alerts(60)),
        "critical": len([a for a in alerts._alerts if a['type'] == 'critical'])
    }
    return snapshot
