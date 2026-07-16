import os
from pathlib import Path
from sentence_transformers import SentenceTransformer
import chromadb
import ast
import requests
import base64
import re


# Iteration 2: Add codebase context using RAG (Retrieval-Augmented Generation)
# The LLM now sees the full files and similar code from the repository


# Embedding model to convert code into semantic vectors
embedding_model = SentenceTransformer('all-MiniLM-L6-v2')

# Vector database for storing and searching code embeddings
vector_db = chromadb.Client()


class CodeIndexer:
    """Indexes the repository code into a vector database for semantic search."""

    def __init__(self, repo_name: str):
        """Creates a ChromaDB collection dedicated to this repository."""
        self.collection = vector_db.get_or_create_collection(f"repo_{repo_name}")

    async def index_repository(self, repo_path: str):
        """Walks all .py files, extracts functions, and stores them as vectors in ChromaDB."""
        for file_path in Path(repo_path).rglob("*.py"):
            with open(file_path) as f:
                code_content = f.read()

            functions = self._extract_functions(code_content)

            for func_name, func_code in functions:
                embedding = embedding_model.encode(func_code)

                self.collection.add(
                    embeddings=[embedding.tolist()],
                    documents=[func_code],
                    metadatas=[{
                        "file": str(file_path),
                        "function": func_name,
                        "type": "function"
                    }],
                    ids=[f"{file_path}::{func_name}"]
                )

    def _extract_functions(self, code: str) -> list:
        """Uses Python's AST module to extract function definitions and their source code."""
        functions = []
        try:
            tree = ast.parse(code)
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    func_code = ast.get_source_segment(code, node)
                    functions.append((node.name, func_code))
        except:
            pass
        return functions

    async def find_related_code(self, changed_code: str, top_k: int = 5) -> list:
        """Converts the changed code to an embedding and searches for similar code in the vector database."""
        query_embedding = embedding_model.encode(changed_code)

        results = self.collection.query(
            query_embeddings=[query_embedding.tolist()],
            n_results=top_k
        )

        return [
            {
                "code": doc,
                "file": meta["file"],
                "function": meta["function"]
            }
            for doc, meta in zip(results['documents'][0], results['metadatas'][0])
        ]


class ContextualReviewer:
    """Reviewer that augments the LLM prompt with full file context and similar patterns from the repo."""

    def __init__(self, repo_name: str):
        """Initializes the code indexer for the given repository."""
        self.indexer = CodeIndexer(repo_name)

    async def review_pr(self, diff: str, repo_name: str) -> str:
        """Reviews the PR by building a prompt with: diff, full files, and related code from the codebase."""
        changed_files = self._parse_diff(diff)
        full_context = await self._get_full_files(changed_files, repo_name)
        related_code = await self._find_related_patterns(changed_files)
        prompt = self._build_review_prompt(diff, full_context, related_code)

        from anthropic import Anthropic
        client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

        response = client.messages.create(
            model="anthropic.claude-haiku-4-5-20251001-v1:0",
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}]
        )

        return response.content[0].text

    def _parse_diff(self, diff: str) -> list:
        """Parses the git diff to extract the list of modified files and their added code."""
        files = []
        for match in re.finditer(r'diff --git a/(.*?) b/(.*?)\n', diff):
            file_path = match.group(1)
            file_diff = diff[match.end():].split('diff --git')[0]
            added_lines = [
                line[1:] for line in file_diff.split('\n')
                if line.startswith('+') and not line.startswith('+++')
            ]
            files.append({
                "path": file_path,
                "added_code": '\n'.join(added_lines)
            })
        return files

    async def _get_full_files(self, changed_files: list, repo_name: str) -> dict:
        """Fetches the full content of each modified file from GitHub's API (base64 decoded)."""
        full_files = {}
        for file_info in changed_files:
            url = f"https://api.github.com/repos/{repo_name}/contents/{file_info['path']}"
            response = requests.get(url, headers={
                "Authorization": f"Bearer {os.getenv('GITHUB_TOKEN')}"
            })
            if response.status_code == 200:
                content = base64.b64decode(response.json()['content']).decode()
                full_files[file_info['path']] = content
        return full_files

    async def _find_related_patterns(self, changed_files: list) -> list:
        """For each modified file, searches for similar patterns in the codebase using the vector database."""
        related = []
        for file_info in changed_files:
            similar_code = await self.indexer.find_related_code(
                file_info['added_code'], top_k=3
            )
            related.extend(similar_code)
        return related

    def _build_review_prompt(self, diff: str, full_files: dict, related_code: list) -> str:
        """Builds an enriched prompt that includes the diff, full files, and similar examples from the repo."""
        prompt = "Review this pull request:\n\n## Changed Files (Full Context):\n"
        for file_path, content in full_files.items():
            prompt += f"\n### {file_path}\n```python\n{content[:1000]}...\n```\n"

        prompt += f"\n## Diff:\n```diff\n{diff[:2000]}\n```\n\n## Similar Patterns in Codebase:\n"
        for item in related_code[:3]:
            prompt += f"\n{item['file']} - {item['function']}:\n```python\n{item['code'][:300]}\n```\n"

        prompt += """
Provide a code review covering:
1. Bugs or logical errors
2. Style consistency with existing code
3. Performance issues
4. Security concerns
"""
        return prompt
