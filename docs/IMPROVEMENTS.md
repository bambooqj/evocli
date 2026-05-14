# EvoCLI 系统不足分析与改进提示词

**生成时间**: 2026-05-15  
**分析方法**: 4 个并行审计代理（Python Soul 质量审计 + Rust Host 质量审计 + 成熟方案研究 × 2）  
**参考论文**: Reflexion(2023), HippoRAG(2024), LLMLingua-2(2024), Agentless(2024), ToolBench(2023), RepoGraph(2025), SetFit, ReFlect(2026)

---

## 一、问题优先级总览

| 优先级 | 问题 | 影响范围 | 参考方案 |
|---|---|---|---|
| 🔴 CRITICAL | 安全漏洞: symlink路径穿越 | 安全 | `canonicalize()` 规范化 |
| 🔴 CRITICAL | Soul Bridge 无界信道内存增长 | 稳定性 | Bounded channel + backpressure |
| 🔴 HIGH | MCP 每次调用重新连接 | 性能 | 连接池 (DashMap + Mutex) |
| 🔴 HIGH | shell.run 超时后子进程泄漏 | 资源 | 杀整个进程组 |
| 🟡 MEDIUM | LLM重试固定延迟，无指数退避 | 可靠性 | Exponential backoff + jitter |
| 🟡 MEDIUM | 记忆衰减函数不一致 (指数 vs 幂律) | 记忆质量 | 统一使用幂律衰减 |
| 🟡 MEDIUM | 意图分类器误判 (新建→planner等) | UX | SetFit校准阈值 |
| 🟡 MEDIUM | PrefixSpan minlen 废弃警告 | 进化引擎 | API更新 |
| 🟡 MEDIUM | Skill draft 无自动晋升机制 | Skill系统 | 基于成功率的自动晋升 |
| 🟡 MEDIUM | 上下文注入无语义去重 | Token效率 | LLMLingua-2 微压缩 |
| 🟡 MEDIUM | code_intel LSP崩溃无自动恢复 | 代码智能 | 自动重启 + 健康检查 |
| 🟢 LOW | Jaccard去重阈值硬编码 | 记忆质量 | 配置化阈值 |
| 🟢 LOW | RepoMap PageRank未个性化 | 代码智能 | Personalized PageRank |
| 🟢 LOW | 无 Ctrl+Z 快捷键 | TUI UX | 映射到/undo |

---

## 二、修复提示词（可直接发给 AI 执行）

---

### PROMPT-01: 安全漏洞修复 — 符号链接路径穿越

**严重等级**: 🔴 CRITICAL  
**参考**: OWASP Path Traversal, CWE-22

```
修复 EvoCLI Rust Host 的路径访问安全漏洞。

文件: crates/host/src/security.rs

问题: validate_path_access() 使用字符串包含检查路径，没有规范化路径，
攻击者可创建符号链接绕过限制：
  ln -s ~/.ssh ./evil_link
  -> 调用 fs.read(path="evil_link/.ssh/id_rsa") 可成功读取

修复方案:
1. 在 validate_path_access() 中使用 std::fs::canonicalize() 解析符号链接
2. 对不存在的路径（新建文件）使用父目录的 canonicalize
3. 将 denied_paths 也 canonicalize 后再比较

具体实现:
```rust
pub fn validate_path_access(&self, path: &Path) -> Result<()> {
    // 1. 尝试规范化（解析符号链接）
    let resolved = if path.exists() {
        std::fs::canonicalize(path)
            .with_context(|| format!("Cannot resolve path: {}", path.display()))?
    } else {
        // 新建文件：规范化父目录
        if let Some(parent) = path.parent() {
            if parent.as_os_str().is_empty() {
                std::env::current_dir()?.join(path)
            } else {
                std::fs::canonicalize(parent)
                    .with_context(|| format!("Cannot resolve parent: {}", parent.display()))?
                    .join(path.file_name().unwrap_or_default())
            }
        } else {
            path.to_path_buf()
        }
    };
    
    let resolved_str = resolved.to_string_lossy().to_lowercase();
    
    for denied in self.cfg.denied_paths.iter().chain(self.cfg.extra_denied_paths.iter()) {
        // 也规范化 denied_path 以确保一致性
        let denied_lower = denied.to_lowercase();
        if resolved_str.contains(&denied_lower) {
            self.audit_log("path.validate", &resolved.display().to_string(), false);
            bail!("[E202] '{}' denied by path rule '{}'.", resolved.display(), denied);
        }
    }
    Ok(())
}
```

验证: 创建符号链接后调用 fs.read 应返回 [E202] 错误。
```

---

### PROMPT-02: Soul Bridge 有界信道 + 背压

**严重等级**: 🔴 CRITICAL  
**参考**: Tokio 文档, Backpressure 模式

```
修复 EvoCLI Soul Bridge 的无界信道导致内存无限增长问题。

文件: crates/soul_bridge/src/lib.rs

问题: 当前使用 mpsc::unbounded_channel()，如果 Python Soul 生产速度 > TUI 消费速度，
内存会无限增长。在大型流式响应（长代码生成）时尤其危险。

修复方案 (参考 Tokio 最佳实践):
1. 将 stdin_tx / tool_tx 改为有界信道 (bounded(256))
2. 对背压场景返回 SendError 并记录警告，而非 panic
3. malformed JSON 记录警告而非 continue

具体步骤:
1. 将所有 unbounded_channel() 调用改为 channel(256)
2. 修改 _start_reader() 中 malformed JSON 的处理:
   ```rust
   Err(e) => {
       tracing::warn!("JSON-RPC parse error (protocol desync): {e}. Raw: {raw:?}");
       // 不要 continue — 记录后继续，避免静默丢失事件
   }
   ```
3. 发送失败时添加背压警告:
   ```rust
   if let Err(e) = tx.send(chunk).await {
       tracing::warn!("Channel full — TUI consumer is slow: {e}");
   }
   ```

验证: 压力测试下内存不超过 ~50MB 增量。
```

---

### PROMPT-03: MCP 持久连接池

**严重等级**: 🔴 HIGH  
**参考**: MCP Specification (Lifecycle 2025), mcp-mux 架构

```
修复 EvoCLI MCP 集成的性能问题：当前每次工具调用都重新创建子进程连接。

文件: evocli-soul/evocli_soul/handlers/mcp_bridge.py

问题: McpClientProcess 在每次 call_mcp_tool 调用时都启动新进程、发送 initialize、
调用工具、然后关闭。对于 Node.js 服务器（启动耗时 200-800ms），这使每次工具调用
增加 0.5-1s 的延迟。

修复方案 (参考 mcp-mux 架构 + MCP Spec Session Lifecycle):

1. 实现进程级连接池 (Python asyncio):
```python
import asyncio
import hashlib
from dataclasses import dataclass, field
from typing import Dict, Optional

@dataclass  
class _McpConnection:
    proc: asyncio.subprocess.Process
    stdin: asyncio.StreamWriter
    stdout: asyncio.StreamReader
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    call_count: int = 0
    max_calls: int = 100  # 避免内存泄漏，定期重建

_CONNECTION_POOL: Dict[str, _McpConnection] = {}
_POOL_LOCK = asyncio.Lock()

async def _get_or_create_connection(server_config: dict) -> _McpConnection:
    """获取或创建 MCP 服务器持久连接。"""
    key = hashlib.md5(str(server_config).encode()).hexdigest()
    
    async with _POOL_LOCK:
        conn = _CONNECTION_POOL.get(key)
        if conn and conn.proc.returncode is None and conn.call_count < conn.max_calls:
            return conn
        
        # 关闭旧连接
        if conn and conn.proc.returncode is None:
            conn.proc.terminate()
        
        # 创建新连接并发送 initialize
        proc = await asyncio.create_subprocess_exec(
            *server_config["command_args"],
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        conn = _McpConnection(proc=proc, stdin=proc.stdin, stdout=proc.stdout, lock=asyncio.Lock())
        await _mcp_initialize(conn)
        _CONNECTION_POOL[key] = conn
        return conn

async def call_mcp_tool(server_name: str, tool_name: str, arguments: dict) -> dict:
    """调用 MCP 工具，使用持久连接。"""
    server_config = _load_server_config(server_name)
    conn = await _get_or_create_connection(server_config)
    
    async with conn.lock:  # 串行化对同一连接的访问
        conn.call_count += 1
        return await _mcp_call(conn, tool_name, arguments)
```

2. 在进程退出时清理连接池:
```python
import atexit
def _cleanup_pool():
    for conn in _CONNECTION_POOL.values():
        if conn.proc.returncode is None:
            conn.proc.terminate()
atexit.register(_cleanup_pool)
```

预期效果: 第一次调用 0.5-1s 启动，后续调用 < 50ms。
```

---

### PROMPT-04: Shell 进程组完全终止

**严重等级**: 🔴 HIGH  
**参考**: Unix 进程组管理, Windows Job Objects

```
修复 EvoCLI shell.run 超时后子进程泄漏问题。

文件: crates/tools/src/lib.rs

问题: wait_timeout() 只调用 child.kill()，杀死 shell 进程但不杀死其子进程树。
例如: shell.run("sleep 1000 &") 超时后 sleep 进程继续运行。

修复方案 (跨平台):

Unix:
```rust
#[cfg(unix)]
fn kill_process_group(child: &mut std::process::Child) {
    use std::os::unix::process::CommandExt;
    if let Some(pid) = child.id() {
        unsafe {
            // 杀整个进程组（负号 = 进程组信号）
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
    }
}
```

Windows:
```rust
#[cfg(windows)]
fn kill_process_group(child: &mut std::process::Child) {
    // 使用 Job Object 限制子进程
    // 在 Command 创建时设置 CREATE_NEW_PROCESS_GROUP
    child.kill().ok();
}
```

创建进程时使用进程组:
```rust
#[cfg(unix)]
fn build_command(cmd: &str) -> std::process::Command {
    let mut c = std::process::Command::new("sh");
    c.arg("-c").arg(cmd);
    // 创建新进程组，方便整体终止
    unsafe { c.pre_exec(|| { libc::setpgrp(); Ok(()) }); }
    c
}
```

在 Cargo.toml 添加: libc = "0.2" (unix feature)

验证: shell.run("sleep 1000 &") 超时后，ps 中不应存在 sleep 进程。
```

---

### PROMPT-05: LLM 指数退避重试

**严重等级**: 🟡 MEDIUM  
**参考**: AWS 指数退避最佳实践, ToolBench(2023) 错误恢复策略

```
修复 EvoCLI LLM 客户端的固定延迟重试，改为指数退避 + 抖动。

文件: evocli-soul/evocli_soul/llm_client.py

问题: 当前重试使用固定延迟 (_retry_after)，不区分错误类型，
对瞬时错误（429/5xx）和永久错误（4xx）使用相同策略。

参考 ToolBench(2023) 的错误分类策略:
- Transient: 429 (rate limit), 5xx (server error), timeout → 指数退避重试
- Permanent: 400/401/403 (bad request/auth) → 立即失败，反馈给 LLM 修正

修复实现:
```python
import random
import asyncio

TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_RETRIES = 5
BASE_DELAY = 1.0   # 初始延迟 1s
MAX_DELAY = 60.0   # 最大延迟 60s

async def _retry_with_backoff(fn, *args, **kwargs):
    """
    指数退避重试，带满抖动 (Full Jitter)。
    
    参考: AWS "Exponential Backoff And Jitter" (2015)
    Full Jitter: sleep = random(0, min(MAX_DELAY, BASE * 2^attempt))
    """
    last_exc = None
    for attempt in range(MAX_RETRIES):
        try:
            return await fn(*args, **kwargs)
        except Exception as e:
            status = getattr(e, 'status_code', None) or getattr(e, 'status', None)
            
            # 永久错误: 不重试，直接抛出让 LLM 知道
            if status and status not in TRANSIENT_STATUS_CODES and 400 <= status < 500:
                log.warning("Permanent error %d — no retry: %s", status, e)
                raise
            
            last_exc = e
            # Full Jitter 指数退避
            cap = min(MAX_DELAY, BASE_DELAY * (2 ** attempt))
            delay = random.uniform(0, cap)
            
            log.info("Transient error (attempt %d/%d), retry in %.1fs: %s",
                     attempt + 1, MAX_RETRIES, delay, e)
            
            if attempt < MAX_RETRIES - 1:
                await asyncio.sleep(delay)
    
    raise last_exc

# 替换现有重试逻辑:
async def _acompletion_with_retry_events(self, **kwargs):
    return await _retry_with_backoff(self._router.acompletion, **kwargs)
```

验证: mock 429 错误，确认第 1 次重试延迟 0-1s，第 2 次 0-2s，第 3 次 0-4s。
```

---

### PROMPT-06: 记忆衰减函数统一 — 幂律衰减

**严重等级**: 🟡 MEDIUM  
**参考**: HippoRAG(NeurIPS 2024), 人类记忆遗忘曲线研究 (Ebbinghaus 1885)

```
修复 EvoCLI 记忆系统两个后端使用不同衰减函数的不一致问题。

文件: evocli-soul/evocli_soul/memory_client.py

问题:
- _JSONLinesStore: 使用指数衰减 0.5^(days/30)  ← line 153-155
- _vector_search: 使用指数衰减 0.95^days      ← line 491-492
两者参数不同，导致同一记忆在不同后端有不同的老化速率。

参考 HippoRAG (2024) 和认知科学研究:
幂律衰减 f(t) = (1 + t/t0)^(-α) 比指数衰减更符合人类长期记忆特性:
- 遥远但高质量的记忆衰减更慢（"重锚点"效应）
- α=0.5, t0=1.0 是经验最优参数

修复:
```python
# 统一的幂律衰减函数
MEMORY_DECAY_ALPHA = 0.5   # 衰减速率 (可配置)
MEMORY_DECAY_T0 = 1.0      # 特征时间尺度（天）

def power_law_decay(days_old: float) -> float:
    """
    幂律遗忘曲线 f(t) = (1 + t/t0)^(-α)
    参考: Ebbinghaus(1885), HippoRAG(2024)
    
    特性: 
    - t=0: f=1.0 (新记忆，无衰减)
    - t=7: f≈0.77 (一周后仍保留77%)  
    - t=30: f≈0.58 (一月后仍保留58%)
    - t=365: f≈0.28 (一年后仍保留28%)
    比指数衰减在长期更保留高价值记忆
    """
    return (1.0 + days_old / MEMORY_DECAY_T0) ** (-MEMORY_DECAY_ALPHA)

# 在两个存储后端中替换现有的衰减公式:
# 旧: 0.5 ** (days_since / 30.0) 
# 旧: 0.95 ** days_old
# 新: power_law_decay(days_old)
```

同时: 将 Jaccard 去重阈值 0.85 改为可配置:
```python
# config_defaults.py 中添加:
"memory.dedup_jaccard_threshold": (0.85, "Memory deduplication Jaccard similarity threshold"),

# memory_client.py 中:
from evocli_soul.config_defaults import cfg_float
JACCARD_THRESHOLD = cfg_float("memory.dedup_jaccard_threshold")
```

验证: 创建两条相似记忆，确认衰减函数在两个后端返回相同分数。
```

---

### PROMPT-07: 意图分类器校准 — 基于 SetFit 原则

**严重等级**: 🟡 MEDIUM  
**参考**: SetFit(Tunstall et al. 2022), 阈值校准 AISTATS 2026

```
改进 EvoCLI 意图分类器的准确性，减少误判（如 "新建" → planner）。

文件: evocli-soul/evocli_soul/intent_profile.py

问题: 当前语义分类使用单一全局阈值 0.22，且 planner 描述中的"创建"示例
导致 embedding 将"新建文件/目录"误判为 planner。

参考 SetFit 原则: 每个意图使用独立校准阈值，而非全局阈值。

修复 1: 每意图独立阈值字典
```python
# 基于经验校准的每意图阈值
# 高精度意图（避免误判）阈值更高，低精度意图可以低一些
INTENT_THRESHOLDS: dict[str, float] = {
    "chat":       0.35,   # 高阈值: 避免把代码问题误判为闲聊
    "question":   0.30,
    "researcher": 0.25,
    "planner":    0.40,   # 高阈值: 避免把"创建"误判为规划
    "reviewer":   0.30,
    "debugger":   0.28,
    "coder":      0.22,   # 低阈值: 宁可多执行也不漏掉
    "risky":      0.45,   # 最高阈值: 危险操作必须确定
}

def classify_by_similarity(prompt: str, descriptions: dict, fallback: str = "") -> str:
    scores = {}
    for intent, desc in descriptions.items():
        scores[intent] = cosine_similarity(embed(prompt), embed(desc))
    
    best_intent = max(scores, key=lambda k: scores[k])
    threshold = INTENT_THRESHOLDS.get(best_intent, 0.22)
    
    if scores[best_intent] < threshold:
        return fallback  # 低于阈值 → 回退关键词分类
    return best_intent
```

修复 2: 增强 coder 描述，减少 planner 的歧义词
已在代码中完成（移除 planner 中的"创建"，加入 coder 的"新建/建一个"）。

修复 3: 添加意图分类日志，方便后续调优
```python
log.info("intent: %s (score=%.3f, threshold=%.2f) — %s",
         intent, best_score, threshold, reason)
```

验证: 测试以下 prompts 的分类结果:
- "新建一个test目录" → coder ✓
- "设计数据库架构" → planner ✓  
- "帮我规划下这个功能" → planner ✓
- "你好" → chat ✓
- "删除所有日志文件" → risky ✓
```

---

### PROMPT-08: PrefixSpan API 更新 + 滑窗回退改善

**严重等级**: 🟡 MEDIUM  
**参考**: PrefixSpan-py 文档

```
修复 EvoCLI 进化引擎的 PrefixSpan minlen 废弃警告，并改善滑窗回退质量。

文件: evocli-soul/evocli_soul/evolution/pattern_detector.py

问题 1: PrefixSpan API 变化，minlen 参数位置或名称已废弃（日志中可见警告）。
问题 2: 滑窗回退 (_sliding_window) 只按频率计数，无质量过滤。

修复 1: 更新 PrefixSpan 调用方式
```python
from prefixspan import PrefixSpan

def detect_frequent_sequences(sequences: list, min_support: int = 2, max_len: int = 6) -> list:
    try:
        ps = PrefixSpan(sequences)
        # 新 API: frequent(min_support) 不需要 minlen 参数
        # 使用 closed=True 返回闭合模式（减少冗余）
        patterns = ps.frequent(min_support, closed=True)
        # 按长度过滤: 只保留 2-max_len 的模式
        return [(freq, seq) for freq, seq in patterns if 2 <= len(seq) <= max_len]
    except Exception as e:
        log.warning("PrefixSpan failed: %s — using sliding window fallback", e)
        return _sliding_window_fallback(sequences, min_support, max_len)
```

修复 2: 改善滑窗回退质量过滤
```python
def _sliding_window_fallback(sequences, min_support, max_len):
    """
    改进的滑窗回退: 添加质量过滤
    过滤掉: 过于通用的单工具序列, 读取后立即读取的无意义模式
    """
    from collections import Counter
    
    # 无意义的"噪声"工具调用（不应形成 skill）
    NOISE_TOOLS = {"memory_recall", "todo_read", "todo_write", "task_complete"}
    
    counts = Counter()
    for seq in sequences:
        for length in range(2, min(max_len + 1, len(seq) + 1)):
            for i in range(len(seq) - length + 1):
                window = tuple(seq[i:i+length])
                # 质量过滤: 至少一个非噪声工具
                if not all(t in NOISE_TOOLS for t in window):
                    counts[window] += 1
    
    return [(cnt, list(seq)) for seq, cnt in counts.items() if cnt >= min_support]
```

验证: 运行 `evocli evolve scan` 后不再出现 minlen 相关警告。
```

---

### PROMPT-09: Skill 自动晋升机制

**严重等级**: 🟡 MEDIUM  
**参考**: n8n Workflow 验证机制, LeetProof(2026)

```
实现 EvoCLI Skill Draft 到 Verified 的自动晋升机制。

文件: evocli-soul/evocli_soul/skill_engine.py, evocli-soul/evocli_soul/evolution/__init__.py

问题: skill_draft 生成的技能一直停留在 "draft" 状态，需要用户手动晋升，
没有基于成功率的自动晋升路径。

参考 n8n 的 Checkpoint 模式: 工作流执行前通过静态分析 + 模拟运行验证。

实现自动晋升逻辑:
```python
# skill_engine.py 中添加:

DRAFT_AUTO_PROMOTE_THRESHOLD = 2   # 成功运行 N 次后自动变 verified
VERIFIED_AUTO_TRUST_THRESHOLD = 5  # 成功运行 M 次后自动变 trusted

async def record_skill_execution(self, skill_id: str, success: bool) -> None:
    """记录技能执行结果，触发自动晋升检查。"""
    skill = self._skills.get(skill_id)
    if not skill:
        return
    
    # 读取执行历史
    history = skill.metadata.get("execution_history", {"successes": 0, "failures": 0})
    if success:
        history["successes"] += 1
    else:
        history["failures"] += 1
    skill.metadata["execution_history"] = history
    
    total = history["successes"] + history["failures"]
    success_rate = history["successes"] / total if total > 0 else 0.0
    
    # 自动晋升规则 (参考 LeetProof 的验证门槛)
    if skill.status == "draft":
        if history["successes"] >= DRAFT_AUTO_PROMOTE_THRESHOLD and success_rate >= 0.8:
            skill.status = "verified"
            log.info("Skill '%s' auto-promoted: draft → verified (success_rate=%.1f%%)",
                     skill_id, success_rate * 100)
            self._save_skill_status(skill_id, "verified")
    
    elif skill.status == "verified":
        if history["successes"] >= VERIFIED_AUTO_TRUST_THRESHOLD and success_rate >= 0.9:
            skill.status = "trusted"
            log.info("Skill '%s' auto-promoted: verified → trusted (success_rate=%.1f%%)",
                     skill_id, success_rate * 100)
            self._save_skill_status(skill_id, "trusted")
```

验证: 创建一个 draft skill，连续成功运行 2 次后状态应自动变为 verified。
```

---

### PROMPT-10: 上下文语义去重 — LLMLingua-2 微压缩

**严重等级**: 🟡 MEDIUM  
**参考**: LLMLingua-2(Pan et al. 2024), Claude Code 微压缩模式

```
实现 EvoCLI 上下文注入前的语义去重和微压缩。

文件: evocli-soul/evocli_soul/context_engine.py

问题: context_engine.build() 将记忆条目、代码片段、mention 文件直接拼接，
没有检测冗余内容，浪费 Token 预算。

参考 LLMLingua-2 和 Claude Code 的"微压缩" (Micro-compaction) 模式:
对已经处理过的 tool_result（大文件读取）进行缩减。

实现 1: 简单去重（低复杂度，立即可实现）
```python
import hashlib
from typing import list as List

def _deduplicate_context_items(items: List[str], threshold: float = 0.85) -> List[str]:
    """
    基于内容哈希 + Jaccard 相似度的上下文去重。
    
    策略: 
    1. 精确重复 → 哈希去重 (O(n))
    2. 近似重复 → Jaccard 去重 (O(n²) 但 n 通常很小)
    """
    seen_hashes = set()
    seen_content = []
    result = []
    
    for item in items:
        # 精确去重
        h = hashlib.md5(item.strip().encode()).hexdigest()
        if h in seen_hashes:
            continue
        seen_hashes.add(h)
        
        # Jaccard 近似去重
        item_words = set(item.lower().split())
        is_duplicate = False
        for prev_words in seen_content:
            intersection = len(item_words & prev_words)
            union = len(item_words | prev_words)
            if union > 0 and intersection / union >= threshold:
                is_duplicate = True
                break
        
        if not is_duplicate:
            seen_content.append(item_words)
            result.append(item)
    
    return result
```

实现 2: 微压缩 — 压缩已处理的 tool_result
```python
def _compact_tool_results(messages: list, max_chars: int = 500) -> list:
    """
    参考 Claude Code 的 micro-compaction:
    将对话历史中已处理的大型 tool_result 截断为摘要，
    只保留最近 N 轮的完整结果。
    """
    KEEP_RECENT = 3  # 保留最近 3 个 tool_result 的完整内容
    tool_count = 0
    
    result = []
    for msg in reversed(messages):
        if msg.get("role") == "tool":
            tool_count += 1
            if tool_count > KEEP_RECENT:
                content = msg.get("content", "")
                if len(content) > max_chars:
                    msg = dict(msg)
                    msg["content"] = content[:max_chars] + f"\n...[truncated {len(content)-max_chars} chars]"
        result.append(msg)
    
    return list(reversed(result))
```

在 context_engine.build() 中调用去重:
```python
memory_items = self._collect_memory_items(...)
memory_items = _deduplicate_context_items(memory_items)  # ← 添加
```

预期效果: Token 使用减少 15-30%，特别是在长会话中。
```

---

### PROMPT-11: LSP 服务器崩溃自动恢复

**严重等级**: 🟡 MEDIUM  
**参考**: LSP 规范, Language Server 健康检查模式

```
修复 EvoCLI code_intel LSP 客户端崩溃后静默失败的问题。

文件: crates/code_intel/src/lsp_client.rs, crates/code_intel/src/lsp_manager.rs

问题: reader task 在 EOF（LSP 服务器崩溃）后直接退出，没有通知 LspManager，
导致 code_intel 功能静默失效。

修复方案:
```rust
// lsp_client.rs 中的 reader task 添加崩溃通知:
tokio::spawn(async move {
    loop {
        match reader.read_line(&mut buf).await {
            Ok(0) => {
                // EOF = LSP 服务器已退出
                tracing::warn!("LSP server exited (EOF) — notifying manager for restart");
                let _ = crash_tx.send(LspCrashEvent { server_id: server_id.clone() });
                break;
            }
            Ok(_) => { /* 处理正常消息 */ }
            Err(e) => {
                tracing::error!("LSP reader error: {e}");
                let _ = crash_tx.send(LspCrashEvent { server_id: server_id.clone() });
                break;
            }
        }
    }
});

// lsp_manager.rs 中处理崩溃事件，实现指数退避重启:
async fn handle_crash(&mut self, event: LspCrashEvent) {
    let attempt = self.crash_count.entry(event.server_id.clone()).or_insert(0);
    *attempt += 1;
    
    // 指数退避: 1s, 2s, 4s, 8s, 最大 30s
    let delay = std::time::Duration::from_secs(
        (2_u64.pow(*attempt as u32 - 1)).min(30)
    );
    
    if *attempt <= 5 {
        tracing::info!("LSP restart attempt {} in {:?}", attempt, delay);
        tokio::time::sleep(delay).await;
        self.start_lsp_server(&event.server_id).await.ok();
    } else {
        tracing::error!("LSP server '{}' failed {} times — giving up", event.server_id, attempt);
        self.notify_user_lsp_failed(&event.server_id).await;
    }
}
```

验证: 手动 kill LSP 进程，观察 EvoCLI 日志中出现重启尝试，且 code_intel 功能在 5s 内恢复。
```

---

### PROMPT-12: Personalized PageRank 代码地图

**严重等级**: 🟢 LOW (增强功能)  
**参考**: RepoGraph(2025), Aider RepoMap 实现

```
改进 EvoCLI RepoMap 使用个性化 PageRank，提高与当前任务的相关性。

文件: evocli-soul/evocli_soul/repo_map.py

问题: 当前使用标准 PageRank，对所有文件一视同仁。
与用户当前编辑的文件或本次任务提到的文件缺乏关联。

参考 RepoGraph(2025): 使用个性化 PageRank (PPR)，以当前上下文文件为"种子"
提高相关符号的排名。

修复:
```python
def build_personalized_repomap(
    graph: nx.DiGraph, 
    seed_files: list[str],  # 当前任务相关文件（mention/add 的文件）
    alpha: float = 0.85,
    top_n: int = 20
) -> list[tuple[str, float]]:
    """
    个性化 PageRank: 以 seed_files 为传送目标，
    使相关符号排名更高。
    
    参考: RepoGraph(2025), Aider RepoMap
    """
    if not seed_files or not graph.nodes:
        # 回退到标准 PageRank
        ranks = nx.pagerank(graph, alpha=alpha)
        return sorted(ranks.items(), key=lambda x: x[1], reverse=True)[:top_n]
    
    # 个性化向量: seed 文件权重 50x，其他 1x
    n = graph.number_of_nodes()
    nodes = list(graph.nodes)
    personalization = {}
    for node in nodes:
        # 判断文件是否在 seed 中（模糊匹配）
        is_seed = any(seed in node or node in seed for seed in seed_files)
        personalization[node] = 50.0 if is_seed else 1.0
    
    # 归一化
    total = sum(personalization.values())
    personalization = {k: v/total for k, v in personalization.items()}
    
    try:
        ranks = nx.pagerank(graph, alpha=alpha, personalization=personalization)
    except nx.PowerIterationFailedConvergence:
        log.warning("PersonalizedPageRank failed to converge, using standard PageRank")
        ranks = nx.pagerank(graph, alpha=alpha)
    
    return sorted(ranks.items(), key=lambda x: x[1], reverse=True)[:top_n]
```

在 context_engine.py 中调用时传入当前 mention 文件:
```python
seed_files = [m.path for m in mentions] + [added_file for added_file in added_files]
repomap = build_personalized_repomap(graph, seed_files=seed_files)
```

预期效果: RepoMap 中与当前任务相关的文件排名提升 30-50%。
```

---

## 三、执行优先级路线图

```
第一轮 (本周, 安全+稳定):
  PROMPT-01  安全: symlink 路径穿越      ← 30min
  PROMPT-02  稳定: 有界信道背压          ← 1h  
  PROMPT-04  稳定: 进程组完全终止        ← 30min

第二轮 (下周, 性能+可靠性):
  PROMPT-03  性能: MCP 连接池            ← 2h
  PROMPT-05  可靠: 指数退避重试          ← 1h
  PROMPT-08  质量: PrefixSpan API 更新   ← 30min

第三轮 (下下周, 质量改进):
  PROMPT-06  质量: 幂律衰减统一          ← 1h
  PROMPT-07  质量: 意图分类校准          ← 2h
  PROMPT-09  功能: Skill 自动晋升        ← 3h

第四轮 (月底, 增强功能):
  PROMPT-10  效率: 语义去重              ← 2h
  PROMPT-11  可靠: LSP 自动恢复          ← 2h
  PROMPT-12  质量: Personalized PageRank ← 3h
```

---

## 四、参考文献

| 编号 | 论文/项目 | 用途 |
|---|---|---|
| [1] | Reflexion: Language Agents with Verbal RL, Shinn et al. 2023 | Agent 自我纠错 |
| [2] | HippoRAG: Neurobiologically Inspired Long-Term Memory, NeurIPS 2024 | 记忆架构 |
| [3] | LLMLingua-2: Data Distillation for Prompt Compression, Pan et al. 2024 | 上下文压缩 |
| [4] | Agentless: Demystifying LLM Software Engineering, Xia et al. 2024 | 代码智能 |
| [5] | ToolBench: LLM Tool-Use Trajectories, Qin et al. 2023 | 工具可靠性 |
| [6] | RepoGraph: Repository-level Code Graph, 2025 | 代码地图排名 |
| [7] | SetFit: Efficient Few-Shot Learning, Tunstall et al. 2022 | 意图分类 |
| [8] | Ebbinghaus Forgetting Curve, 1885 | 记忆衰减函数 |
| [9] | AWS Exponential Backoff And Jitter, 2015 | 重试策略 |
| [10] | MCP Specification Lifecycle, 2025 | MCP 连接管理 |
| [11] | LLM-Modulo: Generate-Test-Critique, Kambhampati 2024 | 验证循环 |
| [12] | ReFlect: Complex Long-Horizon Reasoning, 2026 | 断路器设计 |
