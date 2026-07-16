import os
import json
import asyncio
import tempfile
import subprocess
from typing import List, Dict, Optional
from enum import Enum
from anthropic import Anthropic


# Iteration 3: Multi-Agent - split the review into 5 specialized agents
# Each agent focuses on one area (security, performance, style, bugs, tests)
# They run in parallel and results are aggregated


client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# Enum defining the available review categories
class ReviewCategory(Enum):
    SECURITY = "security"
    PERFORMANCE = "performance"
    STYLE = "style"
    BUGS = "bugs"
    TESTS = "tests"


class ReviewAgent:
    """Base class for review agents. All share the _call_llm method to invoke Claude."""

    def __init__(self, model: str = "anthropic.claude-haiku-4-5-20251001-v1:0"):
        """Stores the model this agent will use (can vary per agent to optimize cost)."""
        self.model = model

    def _call_llm(self, system_prompt: str, user_prompt: str) -> str:
        """Shared method: calls Claude API with a system prompt and user prompt, returns text."""
        response = client.messages.create(
            model=self.model,
            max_tokens=2000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )
        return response.content[0].text


class SecurityAgent(ReviewAgent):
    """Specialized security agent. Combines Bandit (SAST tool) + LLM to detect vulnerabilities."""

    async def review(self, code: str, language: str) -> Dict:
        """Runs Bandit if Python, then asks the LLM to identify vulnerabilities. Merges both results."""
        tool_results = await self._run_bandit(code) if language == "python" else []

        llm_findings = json.loads(self._call_llm(
            "You are a security expert. Return JSON only.",
            f"""Review this code for security vulnerabilities:
- SQL injection
- XSS
- Command injection
- Hardcoded secrets
- Insecure crypto
- Path traversal
- SSRF

Code:
```{language}
{code}
```

Return JSON: {{"vulnerabilities": [{{"type": "...", "severity": "high|medium|low", "line": <int>, "description": "..."}}]}}"""
        ))

        return {
            "category": ReviewCategory.SECURITY.value,
            "findings": tool_results + llm_findings.get("vulnerabilities", [])
        }

    async def _run_bandit(self, code: str) -> List[Dict]:
        """Writes code to a temp file, runs Bandit in JSON format, parses the results."""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(code)
            temp_path = f.name
        try:
            result = subprocess.run(
                ['bandit', '-f', 'json', temp_path],
                capture_output=True, text=True, timeout=30
            )
            findings = json.loads(result.stdout).get('results', [])
            return [{
                "type": f['test_id'],
                "severity": f['issue_severity'].lower(),
                "line": f['line_number'],
                "description": f['issue_text']
            } for f in findings]
        except Exception:
            return []
        finally:
            os.unlink(temp_path)


class PerformanceAgent(ReviewAgent):
    """Specialized performance agent: detects N+1 queries, inefficient algorithms, blocking I/O, etc."""

    async def review(self, code: str, language: str) -> Dict:
        """Asks the LLM to identify performance issues and returns the findings."""
        findings = json.loads(self._call_llm(
            "You are a performance expert. Return JSON only.",
            f"""Review this code for performance issues:
- N+1 queries
- Missing indexes
- Inefficient algorithms
- Memory leaks
- Blocking I/O in async code
- String concatenation in loops

Code:
```{language}
{code}
```

Return JSON: {{"issues": [{{"type": "...", "severity": "high|medium|low", "line": <int>, "description": "...", "suggestion": "..."}}]}}"""
        ))
        return {
            "category": ReviewCategory.PERFORMANCE.value,
            "findings": findings.get("issues", [])
        }


class StyleAgent(ReviewAgent):
    """Specialized style and code quality agent: naming, function length, complexity, SOLID."""

    def __init__(self):
        """Uses a cheaper model (Haiku) because style review is less critical."""
        super().__init__(model="anthropic.claude-haiku-4-5-20251001-v1:0")

    async def review(self, code: str, language: str) -> Dict:
        """Asks the LLM to review style and SOLID principles, returns the findings."""
        findings = json.loads(self._call_llm(
            "You are a code style expert. Return JSON only.",
            f"""Review this code style:
- Naming conventions
- Function length
- Code duplication
- Complexity
- SOLID principles

Code:
```{language}
{code}
```

Return JSON: {{"issues": [{{"type": "...", "severity": "low|medium", "line": <int>, "description": "..."}}]}}"""
        ))
        return {
            "category": ReviewCategory.STYLE.value,
            "findings": findings.get("issues", [])
        }


class BugDetectionAgent(ReviewAgent):
    """Specialized bug detection agent: null pointers, race conditions, resource leaks, logic errors."""

    async def review(self, code: str, language: str) -> Dict:
        """Asks the LLM to identify potential bugs with fix suggestions."""
        findings = json.loads(self._call_llm(
            "You are a bug detection expert. Return JSON only.",
            f"""Find potential bugs in this code:
- Null pointer dereference
- Off-by-one errors
- Race conditions
- Resource leaks
- Type mismatches
- Logic errors

Code:
```{language}
{code}
```

Return JSON: {{"bugs": [{{"type": "...", "severity": "high|medium|low", "line": <int>, "description": "...", "suggestion": "..."}}]}}"""
        ))
        return {
            "category": ReviewCategory.BUGS.value,
            "findings": findings.get("bugs", [])
        }


class TestCoverageAgent(ReviewAgent):
    """Specialized test coverage agent: checks if the modified code has adequate tests."""

    def __init__(self):
        """Uses a cheaper model (Haiku) because this is a less critical review."""
        super().__init__(model="anthropic.claude-haiku-4-5-20251001-v1:0")

    async def review(self, code: str, test_files: List[str]) -> Dict:
        """Compares the modified code against existing tests and identifies coverage gaps."""
        test_content = test_files[0] if test_files else "No tests found"
        findings = json.loads(self._call_llm(
            "You are a testing expert. Return JSON only.",
            f"""Review test coverage:

Changed code:
```python
{code}
```

Existing tests:
```python
{test_content}
```

Check: code path coverage, edge cases, meaningful assertions, missing integration tests.

Return JSON: {{"gaps": [{{"type": "...", "severity": "medium|low", "description": "..."}}]}}"""
        ))
        return {
            "category": ReviewCategory.TESTS.value,
            "findings": findings.get("gaps", [])
        }


class MultiAgentReviewer:
    """Orchestrator that runs all 5 agents in parallel and aggregates results into a single report."""

    def __init__(self):
        """Registers one instance of each specialized agent."""
        self.agents = {
            ReviewCategory.SECURITY: SecurityAgent(),
            ReviewCategory.PERFORMANCE: PerformanceAgent(),
            ReviewCategory.STYLE: StyleAgent(),
            ReviewCategory.BUGS: BugDetectionAgent(),
            ReviewCategory.TESTS: TestCoverageAgent()
        }

    async def review_pr(self, code: str, language: str, test_files: List[str]) -> Dict:
        """Runs all agents concurrently with asyncio.gather and aggregates results."""
        tasks = [
            self.agents[ReviewCategory.SECURITY].review(code, language),
            self.agents[ReviewCategory.PERFORMANCE].review(code, language),
            self.agents[ReviewCategory.STYLE].review(code, language),
            self.agents[ReviewCategory.BUGS].review(code, language),
            self.agents[ReviewCategory.TESTS].review(code, test_files)
        ]
        results = await asyncio.gather(*tasks)
        return self._aggregate_findings(results)

    def _aggregate_findings(self, results: List[Dict]) -> Dict:
        """Combines findings from all agents into a dict with summary (total by severity) and by_category."""
        aggregated = {
            "summary": {"total_issues": 0, "critical": 0, "high": 0, "medium": 0, "low": 0},
            "by_category": {}
        }
        for result in results:
            category = result['category']
            findings = result['findings']
            aggregated['by_category'][category] = findings
            aggregated['summary']['total_issues'] += len(findings)
            for finding in findings:
                severity = finding.get('severity', 'low')
                if severity in aggregated['summary']:
                    aggregated['summary'][severity] += 1
        return aggregated

    def format_review_comment(self, findings: Dict) -> str:
        """Formats findings as a readable GitHub comment with sections per category."""
        s = findings['summary']
        comment = f"## AI Code Review\n**Summary**: {s['total_issues']} issues found\n\n"
        if s['critical'] > 0:
            comment += f"CRITICAL: {s['critical']}\n"
        if s['high'] > 0:
            comment += f"HIGH: {s['high']}\n"

        icons = {'security': 'LOCK', 'performance': 'ZAP', 'style': 'SPARKLE', 'bugs': 'BUG', 'tests': 'FLASK'}

        for category, issues in findings['by_category'].items():
            if not issues:
                continue
            comment += f"\n### {icons.get(category, 'NOTE')} {category.title()}\n\n"
            for issue in issues[:5]:
                comment += f"- **Line {issue.get('line', 'N/A')}**: {issue['description']}\n"
                if 'suggestion' in issue:
                    comment += f"  Suggestion: {issue['suggestion']}\n"
            if len(issues) > 5:
                comment += f"*...and {len(issues) - 5} more*"

        return comment
