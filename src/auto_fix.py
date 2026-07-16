import os
import json
import re
import uuid
from typing import Dict, List, Optional
from datetime import datetime

import requests
from anthropic import Anthropic

from multi_agent import MultiAgentReviewer
from learning import AdaptiveReviewer, embedding_model, vector_db


# Iteration 10: Auto-Fix - the system not only detects issues, it generates fix code
# Includes: fix generation, suggestion application, and additional skills (tests, docstrings, refactor)


client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))


# Fix generator: asks the LLM to generate corrected code for an issue
class FixGenerator:
    """Uses the LLM (Sonnet model, most capable) to generate fix code given an issue and the original code."""

    def __init__(self):
        """Uses Claude Sonnet (most powerful model) for precise fix generation."""
        self.model = "anthropic.claude-sonnet-4-20250514-v1:0"

    def generate_fix(self, code: str, issue: Dict, language: str) -> Optional[str]:
        """Sends the code with the issue to the LLM and asks for only the corrected code."""
        response = client.messages.create(
            model=self.model,
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": f"""Generate a fix for this code issue.

Issue: {issue['description']}
Severity: {issue.get('severity', 'medium')}
Language: {language}

Code with issue:
```{language}
{code}
```

Return ONLY the fixed code, no explanation. If you cannot generate a safe fix, return "NO_SAFE_FIX"."""
            }]
        )
        fix = response.content[0].text.strip()
        if fix == "NO_SAFE_FIX" or not fix:
            return None
        return fix

    def generate_multi_step_refactor(self, code: str, issues: List[Dict], language: str) -> List[Dict]:
        """Generates a sequence of refactoring steps, each with its fix, to address multiple issues."""
        steps = []
        for i, issue in enumerate(issues[:5]):
            fix = self.generate_fix(code, issue, language)
            if fix:
                steps.append({
                    "step": i + 1,
                    "issue": issue['description'],
                    "fix": fix,
                    "applied": False
                })
        return steps


# Suggestion applier: records applied fixes and searches for similar ones in history
class SuggestionApplier:
    """Stores fixes that developers applied and searches for similar fixes to reuse them."""

    def __init__(self):
        """Creates the 'applied_fixes' collection in ChromaDB for storing historically accepted fixes."""
        self._applied_fixes_db = vector_db.get_or_create_collection("applied_fixes")

    def apply_fix(self, file_path: str, original_code: str, fix_code: str) -> Optional[str]:
        """Applies the fix if it differs from the original code."""
        if not fix_code or fix_code == original_code:
            return None
        return fix_code

    def record_applied_fix(self, pr_number: int, file_path: str, issue_type: str,
                           original: str, fixed: str, accepted: bool):
        """Stores an applied fix (accepted or rejected) in the vector database for future learning."""
        emb = embedding_model.encode(original)
        self._applied_fixes_db.add(
            embeddings=[emb.tolist()],
            documents=[original],
            metadatas=[{
                "pr_number": pr_number,
                "file_path": file_path,
                "issue_type": issue_type,
                "fixed_code": fixed,
                "accepted": accepted,
                "timestamp": datetime.now().isoformat()
            }],
            ids=[f"fix_{pr_number}_{file_path}_{uuid.uuid4().hex[:8]}"]
        )

    def find_similar_fix(self, code: str, issue_type: str) -> Optional[Dict]:
        """Searches history for an accepted fix for similar code and the same issue type."""
        emb = embedding_model.encode(code)
        results = self._applied_fixes_db.query(
            query_embeddings=[emb.tolist()],
            n_results=1,
            where={"issue_type": issue_type, "accepted": True}
        )
        if results['documents'] and len(results['documents'][0]) > 0:
            return {
                "original": results['documents'][0],
                "metadata": results['metadatas'][0][0]
            }
        return None


# Reviewer with auto-fix: extends AdaptiveReviewer and adds fix generation
class AutoFixReviewer(AdaptiveReviewer):
    """Reviewer version that not only detects issues, but generates fix code using the LLM or historical fixes."""

    def __init__(self):
        """Initializes fix generator and suggestion applier."""
        super().__init__()
        self.fix_generator = FixGenerator()
        self.suggestion_applier = SuggestionApplier()

    async def review_and_fix(self, pr_data: Dict) -> Dict:
        """Runs the review and for each issue tries to generate a fix (first checks history, then generates new)."""
        findings = await self.review_with_learning(pr_data)
        code = pr_data.get('code', '')
        language = pr_data.get('language', 'python')
        file_path = pr_data.get('file_path', 'unknown')
        pr_number = pr_data.get('pr_number', 0)

        for category, issues in findings['by_category'].items():
            for issue in issues:
                similar = self.suggestion_applier.find_similar_fix(
                    issue.get('code_snippet', code),
                    issue.get('type', 'unknown')
                )
                if similar:
                    issue['auto_fix'] = similar['metadata']['fixed_code']
                    issue['fix_source'] = 'historical'
                else:
                    fix = self.fix_generator.generate_fix(code, issue, language)
                    if fix:
                        issue['auto_fix'] = fix
                        issue['fix_source'] = 'generated'

        return findings

    def format_fix_comment(self, findings: Dict) -> str:
        """Formats the review comment including code blocks with the suggested fixes."""
        comment = "## AI Code Review with Auto-Fix Suggestions\n\n"
        total_with_fix = 0

        for category, issues in findings['by_category'].items():
            if not issues:
                continue
            comment += f"### {category.title()}\n\n"
            for issue in issues:
                comment += f"- **{issue.get('severity', 'low').upper()}**: {issue['description']}\n"
                if 'auto_fix' in issue:
                    total_with_fix += 1
                    comment += f"  ```\n  {issue['auto_fix'][:500]}\n  ```\n"
                    comment += f"  _Fix source: {issue.get('fix_source', 'generated')}_\n\n"
                if 'suggestion' in issue:
                    comment += f"  Suggestion: {issue['suggestion']}\n\n"

        comment += f"---\n**{total_with_fix} issues have auto-fix suggestions available**\n"
        return comment


# Additional code generation skills: tests, docstrings, refactoring
class CodeGenerationSkill:
    """Auxiliary skills: generates tests, docstrings, and readability refactors using the LLM."""

    def __init__(self):
        """Initializes the fix generator (reuses its model configuration)."""
        self.fix_generator = FixGenerator()

    def generate_test(self, code: str, language: str) -> str:
        """Asks the LLM to generate unit tests for the given code."""
        response = client.messages.create(
            model="anthropic.claude-sonnet-4-20250514-v1:0",
            max_tokens=2000,
            messages=[{
                "role": "user",
                "content": f"Generate unit tests for this code in {language}:\n\n```{language}\n{code}\n```\nReturn only the test code."
            }]
        )
        return response.content[0].text.strip()

    def generate_docstring(self, code: str, language: str) -> str:
        """Asks the LLM to add docstrings/comments to the code (uses Haiku since it's simpler)."""
        response = client.messages.create(
            model="anthropic.claude-haiku-4-5-20251001-v1:0",
            max_tokens=1000,
            messages=[{
                "role": "user",
                "content": f"Add docstrings/comments to this {language} code:\n\n```{language}\n{code}\n```\nReturn only the documented code."
            }]
        )
        return response.content[0].text.strip()

    def refactor_for_readability(self, code: str, language: str) -> str:
        """Asks the LLM to refactor the code for better readability (extract functions, better names, clear structure)."""
        response = client.messages.create(
            model="anthropic.claude-sonnet-4-20250514-v1:0",
            max_tokens=3000,
            messages=[{
                "role": "user",
                "content": f"Refactor this {language} code for better readability (extract functions, better names, clear structure):\n\n```{language}\n{code}\n```\nReturn only the refactored code."
            }]
        )
        return response.content[0].text.strip()
