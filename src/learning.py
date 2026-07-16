import os
import uuid
from datetime import datetime, timedelta
from typing import Dict, List, Optional

import chromadb
from sentence_transformers import SentenceTransformer

from multi_agent import MultiAgentReviewer


# Iteration 5: Feedback Loop - the system learns from developer acceptances/rejections
# Accepted patterns increase confidence; rejected ones are marked negative to avoid re-suggesting


embedding_model = SentenceTransformer('all-MiniLM-L6-v2')
vector_db = chromadb.Client()


class FeedbackTracker:
    """Records each AI suggestion and developer feedback (accepted/rejected/modified) in a DB."""

    def __init__(self):
        """Initializes without a DB (configured externally)."""
        self.db = None

    async def record_suggestion(
        self, suggestion_id: str, pr_number: int, category: str,
        issue_type: str, code_snippet: str, suggestion_text: str
    ):
        """Stores an AI suggestion in the DB with 'pending' status until the developer responds."""
        if self.db:
            await self.db.execute("""
                INSERT INTO ai_suggestions (
                    id, pr_number, category, issue_type,
                    code_snippet, suggestion, created_at, status
                ) VALUES ($1, $2, $3, $4, $5, $6, $7, 'pending')
            """, suggestion_id, pr_number, category, issue_type,
                 code_snippet, suggestion_text, datetime.now())

    async def record_feedback(
        self, suggestion_id: str, action: str,
        modified_code: str = None, comment: str = None
    ):
        """Updates a suggestion's status when the developer gives feedback (accepted/rejected/modified)."""
        if self.db:
            await self.db.execute("""
                UPDATE ai_suggestions
                SET status = $1, modified_code = $2,
                    developer_comment = $3, feedback_at = $4
                WHERE id = $5
            """, action, modified_code, comment, datetime.now(), suggestion_id)

    async def get_pattern_accuracy(self, issue_type: str, days: int = 30) -> float:
        """Calculates the acceptance rate for a given issue type over the last N days."""
        if not self.db:
            return 0.0
        result = await self.db.fetch_one("""
            SELECT COUNT(*) as total,
                   SUM(CASE WHEN status = 'accepted' THEN 1 ELSE 0 END) as accepted
            FROM ai_suggestions
            WHERE issue_type = $1 AND created_at > $2 AND status != 'pending'
        """, issue_type, datetime.now() - timedelta(days=days))
        if not result or result['total'] == 0:
            return 0.0
        return result['accepted'] / result['total']


class TeamPatternLearner:
    """Learns team patterns: stores what the team accepts, rejects, or modifies in a vector DB."""

    def __init__(self):
        """Initializes the feedback tracker and the 'team_patterns' collection in ChromaDB."""
        self.feedback_tracker = FeedbackTracker()
        self.pattern_db = vector_db.get_or_create_collection("team_patterns")

    async def learn_from_feedback(self):
        """Processes recent feedback (last hour) and updates patterns: positive, negative, or modified."""
        feedback = await self.feedback_tracker.db.fetch_all("""
            SELECT * FROM ai_suggestions
            WHERE feedback_at > $1 AND status != 'pending'
        """, datetime.now() - timedelta(hours=1)) if self.feedback_tracker.db else []

        for item in feedback:
            if item['status'] == 'accepted':
                await self._add_positive_pattern(item)
            elif item['status'] == 'rejected':
                await self._add_negative_pattern(item)
            elif item['status'] == 'modified':
                await self._learn_modification_pattern(item)

    async def _add_positive_pattern(self, item: Dict):
        """Stores an accepted pattern: if a similar one exists, increments its confidence."""
        pattern = {
            "category": item['category'], "issue_type": item['issue_type'],
            "code_before": item['code_snippet'], "suggestion": item['suggestion'],
            "confidence": 1.0, "occurrences": 1
        }
        existing = await self._find_similar_pattern(item['code_snippet'], item['category'])
        if existing:
            meta = existing['metadata']
            meta["confidence"] = min(meta.get("confidence", 0.5) + 0.1, 1.0)
            meta["occurrences"] = meta.get("occurrences", 0) + 1
            self.pattern_db.update(existing['id'], metadata=meta)
        else:
            emb = embedding_model.encode(item['code_snippet'])
            self.pattern_db.add(
                embeddings=[emb.tolist()], documents=[item['code_snippet']],
                metadatas=[pattern], ids=[f"pattern_{item['id']}"]
            )

    async def _add_negative_pattern(self, item: Dict):
        """Stores a rejected pattern with negative confidence so it won't be suggested again."""
        pattern = {
            "category": item['category'], "issue_type": item['issue_type'],
            "code": item['code_snippet'], "confidence": -1.0,
            "rejection_reason": item.get('developer_comment', '')
        }
        emb = embedding_model.encode(item['code_snippet'])
        self.pattern_db.add(
            embeddings=[emb.tolist()], documents=[item['code_snippet']],
            metadatas=[pattern], ids=[f"negative_{item['id']}"]
        )

    async def _learn_modification_pattern(self, item: Dict):
        """Stores a team-preferred modification to use as a suggestion in similar cases."""
        pattern = {
            "category": item['category'], "issue_type": item['issue_type'],
            "original_suggestion": item['suggestion'],
            "developer_modification": item['modified_code'], "confidence": 0.8
        }
        emb = embedding_model.encode(item['code_snippet'])
        self.pattern_db.add(
            embeddings=[emb.tolist()], documents=[item['code_snippet']],
            metadatas=[pattern], ids=[f"modified_{item['id']}"]
        )

    async def _find_similar_pattern(self, code: str, category: str) -> Optional[Dict]:
        """Checks the vector DB for a very similar pattern (distance < 0.1) in the same category."""
        emb = embedding_model.encode(code)
        results = self.pattern_db.query(
            query_embeddings=[emb.tolist()], n_results=1,
            where={"category": category}
        )
        if results['documents'] and len(results['documents'][0]) > 0:
            if results['distances'][0][0] < 0.1:
                return {"id": results['ids'][0][0], "metadata": results['metadatas'][0][0]}
        return None

    async def should_suggest(self, code: str, issue_type: str) -> bool:
        """Decides whether to suggest an issue: checks accuracy >30% and no similar negative pattern."""
        accuracy = await self.feedback_tracker.get_pattern_accuracy(issue_type)
        if accuracy < 0.3:
            return False

        emb = embedding_model.encode(code)
        similar = self.pattern_db.query(query_embeddings=[emb.tolist()], n_results=3)
        for meta in similar['metadatas'][0]:
            if meta.get('confidence', 0) < 0:
                return False
        return True


class AdaptiveReviewer:
    """Reviewer that filters suggestions based on learned team patterns and adds personalized suggestions."""

    def __init__(self):
        """Initializes the multi-agent reviewer and the pattern learner."""
        self.multi_agent = MultiAgentReviewer()
        self.pattern_learner = TeamPatternLearner()
        self.feedback_tracker = FeedbackTracker()

    async def review_with_learning(self, pr_data: Dict) -> Dict:
        """Runs multi-agent review, filters with team patterns, enriches with preferences, and records suggestions."""
        findings = await self.multi_agent.review_pr(
            code=pr_data['code'], language=pr_data['language'],
            test_files=pr_data.get('test_files', [])
        )

        filtered_findings = await self._filter_with_patterns(findings, pr_data['code'])
        enhanced_findings = await self._enhance_with_team_patterns(filtered_findings, pr_data['code'])

        for category, issues in enhanced_findings['by_category'].items():
            for issue in issues:
                sid = str(uuid.uuid4())
                issue['suggestion_id'] = sid
                await self.feedback_tracker.record_suggestion(
                    suggestion_id=sid, pr_number=pr_data.get('pr_number', 0),
                    category=category, issue_type=issue.get('type', 'unknown'),
                    code_snippet=issue.get('code_snippet', ''),
                    suggestion_text=issue['description']
                )
        return enhanced_findings

    async def _filter_with_patterns(self, findings: Dict, code: str) -> Dict:
        """Filters findings: only shows those the team typically accepts (based on learned patterns)."""
        filtered = {"by_category": {}, "summary": findings['summary'].copy()}
        for category, issues in findings['by_category'].items():
            filtered_issues = []
            for issue in issues:
                if await self.pattern_learner.should_suggest(code, issue.get('type', 'unknown')):
                    filtered_issues.append(issue)
            filtered['by_category'][category] = filtered_issues
        total = sum(len(v) for v in filtered['by_category'].values())
        filtered['summary']['total_issues'] = total
        return filtered

    async def _enhance_with_team_patterns(self, findings: Dict, code: str) -> Dict:
        """Adds suggestions based on modifications the team previously preferred."""
        emb = embedding_model.encode(code)
        team_patterns = self.pattern_learner.pattern_db.query(
            query_embeddings=[emb.tolist()], n_results=5,
            where={"confidence": {"$gt": 0.5}}
        )
        for doc, meta in zip(team_patterns['documents'][0], team_patterns['metadatas'][0]):
            if meta.get('developer_modification'):
                findings['by_category'].setdefault('team_style', []).append({
                    "type": "team_preference", "severity": "low",
                    "description": f"Team prefers: {meta['developer_modification']}",
                    "confidence": meta['confidence']
                })
        return findings
