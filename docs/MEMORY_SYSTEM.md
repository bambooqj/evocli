# Memory System

EvoCLI uses a tiered memory architecture that keeps relevant project context available across sessions without overwhelming the LLM's context window.

---

## Overview

```
                    User message
                         │
                         ▼
              ┌─────────────────────┐
              │   context_engine.py │ ← builds context for each request
              │                     │
              │  P1 (4,000 tokens)  │ ← critical constraints + current file
              │  P2 (2,000 tokens)  │ ← tool patterns + recent decisions
              │  P3 (1,500 tokens)  │ ← global preferences + cross-project
              └────────┬────────────┘
                       │ recall(query, top_k=20)
                       ▼
              ┌─────────────────────┐
              │  memory_client.py   │
              │                     │
              │  LanceDB (primary)  │ ← vector similarity search
              │  SQLite (fallback)  │ ← BM25 keyword search
              └─────────────────────┘
```

---

## Storage Backends

### LanceDB (Primary)

Vector database using Apache Arrow format.

- **Location**: `~/.evocli/vectors/`
- **Model**: `jinaai/jina-embeddings-v2-base-zh` (768 dimensions)
  - Bilingual: Chinese and English in the same embedding space
  - ~570 MB download, cached locally after first use
- **Index**: HNSW (Hierarchical Navigable Small World) for ANN search
- **Distance metric**: Cosine similarity

### SQLite FTS (Fallback)

Used when LanceDB is unavailable (first run before model download, or dependency issues).

- **Location**: `~/.evocli/memory.db`
- **Engine**: SQLite FTS5 with trigram tokenizer
- **Limitation**: keyword-only, no semantic understanding

### Hybrid Recall

The `ranked_context` tool combines both backends:

```python
# Pseudocode for hybrid search
vector_results = lancedb.search(embed(query), limit=20)
bm25_results   = sqlite.fts_search(query, limit=20)
combined       = reciprocal_rank_fusion(vector_results, bm25_results, k=60)
```

RRF (Reciprocal Rank Fusion) formula: `score(d) = Σ 1/(k + rank(d))`

---

## Memory Tiers

### L1 — Episodic (Session-level)

Memories created during active sessions.

- **What**: tool call results, user decisions, file changes observed
- **TTL**: persisted across sessions, decays by time and relevance
- **Priority**: P2 and P3 in context injection

### L2 — Semantic (Project-level)

Distilled knowledge extracted from L1 episodes.

- **What**: architectural patterns, recurring fixes, learned constraints
- **Created by**: `memory_distill.py` (runs on session pause)
- **Priority**: P1 in context injection (constraints)

### L3 — Global (Cross-project)

Long-term patterns that apply across projects.

- **What**: personal coding style, preferred patterns, anti-patterns
- **Created by**: manual `memory.write` calls or explicit learning commands

---

## Memory Operations

### Writing

```python
# Python Soul (agent or skill)
await bridge.call("memory.write", {
    "title": "Auth module uses JWT with RS256",
    "body": "The authentication module validates tokens using RS256 algorithm. "
            "Token expiry is checked in validate_token() at line 42.",
    "tags": ["auth", "jwt", "architecture"],
    "priority": "p1"  # "p1" | "p2" | "p3"
})
```

### Recall

```python
results = await bridge.call("memory.recall", {
    "query": "authentication design",
    "top_k": 10,
    "min_score": 0.7
})
# Returns list of { id, title, body, tags, score, created_at }
```

### Constraints

Constraints are P1 memories tagged with `"constraint"`. They always appear at the top of context.

```python
# Write a constraint
await bridge.call("memory.write", {
    "title": "No direct DB access from HTTP handlers",
    "body": "All database operations must go through the repository layer.",
    "tags": ["constraint", "architecture"],
    "priority": "p1"
})

# Get all active constraints
constraints = await bridge.call("memory.constraints", {})
```

---

## Memory Distillation

After each session (`evocli session pause`), the distillation engine extracts key knowledge:

```
session events
     │
     ▼
memory_distill.py
     ├─ success_chain: extract what worked (tool calls that succeeded)
     ├─ failure_chain: extract what failed (and why)
     └─ write to L2 memory with appropriate priority
```

The distiller uses the LLM to summarize long tool outputs into concise memory entries.

---

## Context Injection

`context_engine.py` assembles context before each LLM call:

```python
context = {
    "p1": [
        # Always included (up to 4,000 tokens)
        *constraint_memories,          # Active constraints
        *current_file_content,         # Open file
        *ranked_symbol_context,        # PageRank-weighted symbols
    ],
    "p2": [
        # Tool patterns (up to 2,000 tokens)
        *recent_tool_usage_patterns,
        *recalled_memories(query, top_k=5),
    ],
    "p3": [
        # Global preferences (up to 1,500 tokens)
        *global_preferences,
        *cross_project_patterns,
    ]
}
```

Total budget: `max_total` from config (default: 128,000 tokens).

---

## Memory Routing

`memory_router.py` decides where to store each new memory:

```
New information
     │
     ▼
MemRouter
  ├─ Jaccard deduplication (skip if >85% similar to existing)
  ├─ LLM classifier → category (constraint / fact / pattern / preference)
  └─ Write to appropriate L1/L2/L3 bucket
```

The router uses a local classifier (`local_classifier.py`) to avoid LLM calls for simple categorizations. This classifier is trained on labeled examples and runs in < 5ms.

---

## Embedding Model

**Model**: `jinaai/jina-embeddings-v2-base-zh`
- Dimensions: 768
- Languages: Chinese + English (bilingual, same vector space)
- Provider: Hugging Face (via fastembed)
- Cache: `~/.cache/huggingface/hub/` (auto-managed)

### First-run Download

The embedding model is ~570 MB and downloaded once. Use the mirror for faster download in China:

```bash
# Use HF mirror (recommended in China)
HF_ENDPOINT=https://hf-mirror.com python scripts/download_models.py

# Or run setup which handles this automatically
.\setup.ps1   # Windows
bash setup.sh # Linux/macOS
```

### Changing the Embedding Model

Edit `memory_client.py`:

```python
class EvoCLIMemory:
    EMBEDDING_MODEL = "jinaai/jina-embeddings-v2-base-zh"  # change here
    EMBEDDING_DIM   = 768                                    # update dimensions
```

Any supported fastembed model works. Changing the model requires re-indexing all existing memories.

---

## Memory Management

```bash
# View memory stats
evocli stats

# Search memory from CLI
evocli memory search "authentication design"

# Clear all memories (irreversible)
# Edit ~/.evocli/memories.jsonl and ~/.evocli/vectors/ directly
```

### Storage Locations

| Type | Location |
|---|---|
| Vector index | `~/.evocli/vectors/` |
| SQLite FTS | `~/.evocli/data/memories.jsonl` |
| Session events | `~/.evocli/events.db` |
| Memory DB | `~/.evocli/memory.db` |
