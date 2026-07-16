import os
import re
from typing import List, Dict, Optional
import requests

from multi_agent import MultiAgentReviewer


# Iteration 4: Inline Comments - comments are posted on specific lines of code
# The diff is split into chunks (file + hunk) and each chunk is reviewed separately


class InlineReviewer:
    """Generates inline comments on specific PR lines instead of just a general comment."""

    def __init__(self):
        """Initializes the MultiAgentReviewer to review each code chunk."""
        self.multi_agent = MultiAgentReviewer()

    async def review_pr_with_inline(self, pr_data: Dict) -> Dict:
        """Entry point: parses the diff into chunks, reviews each, posts inline comments and a summary."""
        diff = pr_data['diff']
        repo = pr_data['repository']
        pr_number = pr_data['number']

        chunks = self._parse_diff_chunks(diff)
        all_comments = []

        for chunk in chunks:
            comments = await self._review_chunk(chunk)
            all_comments.extend(comments)

        await self._post_inline_comments(repo, pr_number, all_comments)
        summary = self._generate_summary(all_comments)
        await self._post_pr_comment(repo, pr_number, summary)

        return {"inline_comments": len(all_comments), "summary": summary}

    def _parse_diff_chunks(self, diff: str) -> List[Dict]:
        """Splits the diff into chunks: each file and each hunk (@@...@@ section) becomes a reviewable chunk."""
        chunks = []
        file_diffs = re.split(r'diff --git', diff)[1:]

        for file_diff in file_diffs:
            match = re.search(r'a/(.*?) b/(.*?)\n', file_diff)
            if not match:
                continue
            file_path = match.group(1)

            hunks = re.findall(
                r'@@ -(\d+),?\d* \+(\d+),?\d* @@(.*?)\n(.*?)(?=@@|$)',
                file_diff, re.DOTALL
            )

            for old_line, new_line, context, hunk_content in hunks:
                added_lines = []
                current_line = int(new_line)

                for line in hunk_content.split('\n'):
                    if line.startswith('+') and not line.startswith('+++'):
                        added_lines.append({
                            "line_number": current_line,
                            "content": line[1:]
                        })
                        current_line += 1
                    elif not line.startswith('-'):
                        current_line += 1

                if added_lines:
                    chunks.append({
                        "file": file_path,
                        "start_line": int(new_line),
                        "hunk_context": context.strip(),
                        "added_lines": added_lines,
                        "full_hunk": hunk_content
                    })
        return chunks

    async def _review_chunk(self, chunk: Dict) -> List[Dict]:
        """Reviews a single chunk with the agents and maps findings to specific lines."""
        file_path = chunk['file']
        language = self._detect_language(file_path)
        code = '\n'.join([line['content'] for line in chunk['added_lines']])

        findings = await self.multi_agent.review_pr(code=code, language=language, test_files=[])
        comments = []

        for category, issues in findings['by_category'].items():
            for issue in issues:
                line_number = self._map_to_line_number(issue, chunk['added_lines'])
                if line_number:
                    comments.append({
                        "path": file_path,
                        "line": line_number,
                        "body": self._format_inline_comment(issue, category)
                    })
        return comments

    def _map_to_line_number(self, issue: Dict, added_lines: List[Dict]) -> Optional[int]:
        """Tries to map a finding to a specific line: first by explicit number, then by keyword matching."""
        if 'line' in issue and isinstance(issue['line'], int) and issue['line'] > 0:
            if issue['line'] <= len(added_lines):
                return added_lines[issue['line'] - 1]['line_number']

        keywords = re.findall(r'`([^`]+)`', issue.get('description', ''))
        for line_info in added_lines:
            if any(kw.lower() in line_info['content'].lower() for kw in keywords[:3]):
                return line_info['line_number']

        return added_lines[0]['line_number'] if added_lines else None

    def _format_inline_comment(self, issue: Dict, category: str) -> str:
        """Formats a finding as an inline comment with category, description, and suggestion."""
        severity = issue.get('severity', 'low')
        comment = f"**{category.title()}**: {issue['description']}\n\n"
        if 'suggestion' in issue:
            comment += f"Suggestion: {issue['suggestion']}\n\n"
        comment += "*Automated review suggestion.*"
        return comment

    async def _post_inline_comments(self, repo: str, pr_number: int, comments: List[Dict]):
        """Posts each inline comment using GitHub's Review Comments API (pulls/{number}/comments)."""
        url = f"https://api.github.com/repos/{repo}/pulls/{pr_number}/comments"
        headers = {
            "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github.v3+json"
        }
        for comment in comments:
            payload = {
                "body": comment['body'],
                "path": comment['path'],
                "line": comment['line'],
                "side": "RIGHT"
            }
            resp = requests.post(url, json=payload, headers=headers)
            if resp.status_code != 201:
                print(f"Failed inline comment: {resp.status_code} - {resp.text}")

    async def _post_pr_comment(self, repo: str, pr_number: int, summary: str):
        """Posts the general summary as a PR comment via the Issues API."""
        url = f"https://api.github.com/repos/{repo}/issues/{pr_number}/comments"
        headers = {
            "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}",
            "Accept": "application/vnd.github.v3+json"
        }
        requests.post(url, json={"body": summary}, headers=headers)

    def _generate_summary(self, comments: List[Dict]) -> str:
        """Counts comments by severity and generates a PR-level summary."""
        by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0}
        for c in comments:
            severity = "low"
            body = c['body']
            if "CRITICAL" in body:
                severity = "critical"
            elif "HIGH" in body.upper():
                severity = "high"
            elif "MEDIUM" in body.upper():
                severity = "medium"
            by_severity[severity] = by_severity.get(severity, 0) + 1

        summary = f"## AI Code Review Summary\n\n"
        summary += f"**{len(comments)} inline comments generated**\n\n"
        for sev, count in by_severity.items():
            if count > 0:
                summary += f"- **{sev.title()}**: {count}\n"
        return summary

    def _detect_language(self, file_path: str) -> str:
        """Detects the programming language based on the file extension."""
        ext_map = {
            '.py': 'python', '.js': 'javascript', '.ts': 'typescript',
            '.java': 'java', '.go': 'go', '.rs': 'rust', '.rb': 'ruby'
        }
        ext = os.path.splitext(file_path)[1]
        return ext_map.get(ext, 'unknown')
