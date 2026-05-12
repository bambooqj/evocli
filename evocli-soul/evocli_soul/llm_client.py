"""
LLM Client — LiteLLM Router with Smart Tiering

研究来源:
- Aider: 使用 litellm 作为统一 LLM 接口，支持 100+ provider
- 原实现: 自定义 _resolve_model() 路由逻辑（~50行手写路由代码）
- 改为: litellm.Router — 原生支持重试、fallback、负载均衡、成本追踪

litellm.Router 优势（vs 自写 _resolve_model）:
  - 自动重试（次数可配置）
  - Provider 级别 fallback（当主 provider 挂掉时自动切换）
  - 负载均衡（多 key 时轮询）
  - 内置成本计算、速率限制感知
  - 无需手动 PROVIDER_PREFIXES 映射

Tiers:
  - fast: gpt-4o-mini / claude-3-5-haiku — 快速任务、commit 消息、lint
  - smart: gpt-4o / claude-3-7-sonnet — 架构分析、复杂推理（Architect 模式）
"""
from __future__ import annotations

import logging
import os
from typing import AsyncGenerator

import litellm
from litellm import Router

log = logging.getLogger("evocli.soul.llm")

litellm.suppress_debug_info = True
logging.getLogger("LiteLLM").setLevel(logging.WARNING)

# Fallback model names used ONLY when config.toml has no [llm.tiers] section.
# These match the Rust config.rs defaults (provider=openai).
# In practice, evocli init always writes correct provider-specific models.
_FALLBACK_MODELS = {
    "fast":  "gpt-4o-mini",
    "smart": "gpt-4o",
}


class LLMClient:
    """
    Multi-provider LLM client using litellm.Router.
    研究: litellm.Router 替代自定义 _resolve_model()，
    原生支持重试/fallback/负载均衡，无需手写 PROVIDER_PREFIXES 映射。
    """

    def __init__(self, config: dict | None = None):
        # If no config provided (or empty), auto-load from ~/.evocli/config.toml
        self._config = config or {}
        if not self._config or not self._config.get("api_key"):
            self._config = self._load_config_from_disk() or self._config

        self._provider  = self._config.get("provider", "openai")
        tiers           = self._config.get("tiers", {})
        self._fast_model  = tiers.get("fast",  _FALLBACK_MODELS["fast"])
        self._smart_model = tiers.get("smart", _FALLBACK_MODELS["smart"])
        self._base_url  = self._config.get("base_url")
        api_key = self._config.get("api_key")
        if api_key:
            self._ensure_api_key(api_key)

        # ── Read global params from config (Rust is ground truth) ─────────
        _params = self._config.get("params", {})
        self._default_max_tokens = int(_params.get("max_tokens", 4096))
        self._default_temperature = float(_params.get("temperature", 0.7))
        self._max_retries = int(_params.get("max_retries", 3))

        # ── Task routing: [llm.tasks] section ─────────────────────────────
        self._tasks = self._config.get("tasks", {})

        # ── Role configs: [llm.roles.<name>] sections ─────────────────────
        # Each role can have: base_url, api_key, model
        # Roles take priority over tasks routing when present.
        self._roles = self._config.get("roles", {})

        # ── Task parameters: [llm.params.<task>] sections ─────────────────
        self._task_params = {
            k: v for k, v in _params.items() if isinstance(v, dict)
        }

        # ── litellm.Router ────────────────────────────────────────────────
        model_list = self._build_router_model_list(api_key)
        self._router = Router(
            model_list    = model_list,
            num_retries   = self._max_retries,
            retry_after   = 5,
            allowed_fails = 2,
            cooldown_time = 30,
        )

        # ── Anthropic prompt caching flag ─────────────────────────────────
        # When provider is Anthropic, add cache_control to static message parts
        # (system prompt + tool definitions). This enables 90% cost reduction on
        # cache reads and ~80% reduction on per-turn input token costs.
        # Only Anthropic supports this; other providers ignore the field.
        self._use_prompt_cache = self._provider == "anthropic" or "anthropic" in (
            self._fast_model + self._smart_model
        ).lower()

        self._models = {"fast": self._fast_model, "smart": self._smart_model}
        log.info("LLMClient (Router): provider=%s fast=%s smart=%s base_url=%s cache=%s",
                 self._provider, self._fast_model, self._smart_model,
                 self._base_url or "(default)", "on" if self._use_prompt_cache else "off")

    @staticmethod
    def _load_config_from_disk() -> dict:
        """Load LLM config from config.toml files with project-local override.

        Merge order (highest priority wins):
          1. project-local  {cwd}/.evocli/config.toml
          2. global         ~/.evocli/config.toml
          3. env var overrides (OPENAI_API_KEY etc.)

        This mirrors the Rust host merge logic in config.rs so Python-side
        LLM routing honours per-project model/key configuration.
        """
        import os as _os
        try:
            from pathlib import Path
            import tomllib

            def _read_toml(path: Path) -> dict:
                if not path.exists():
                    return {}
                try:
                    with open(path, "rb") as f:
                        return tomllib.load(f)
                except Exception as _e:
                    log.debug("LLMClient: failed to read %s: %s", path, _e)
                    return {}

            global_cfg  = _read_toml(Path.home() / ".evocli" / "config.toml")
            project_cfg = _read_toml(Path.cwd() / ".evocli" / "config.toml")

            # Deep-merge: project overrides global at the llm section level.
            # For nested dicts (llm.roles, llm.tasks, llm.params) we merge keys
            # so project can override individual roles without clobbering others.
            def _deep_merge(base: dict, override: dict) -> dict:
                result = dict(base)
                for k, v in override.items():
                    if k in result and isinstance(result[k], dict) and isinstance(v, dict):
                        result[k] = _deep_merge(result[k], v)
                    else:
                        result[k] = v
                return result

            merged_llm = _deep_merge(
                global_cfg.get("llm", {}),
                project_cfg.get("llm", {}),
            )

            # Apply env var overrides for api_key
            if not merged_llm.get("api_key"):
                for env_var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DEEPSEEK_API_KEY"):
                    val = _os.environ.get(env_var)
                    if val:
                        merged_llm["api_key"] = val
                        break

            return merged_llm
        except Exception as e:
            log.debug("LLMClient: config load failed: %s", e)
            return {}

    def _build_router_model_list(self, api_key: str | None) -> list[dict]:
        """Build litellm.Router model list from config."""
        common_params: dict = {}
        if self._base_url:
            common_params["api_base"] = self._base_url
        if api_key:
            common_params["api_key"] = api_key

        return [
            {
                "model_name":    "fast",
                "litellm_params": {"model": self._fast_model, **common_params},
            },
            {
                "model_name":    "smart",
                "litellm_params": {"model": self._smart_model, **common_params},
            },
        ]

    def _ensure_api_key(self, key: str) -> None:
        """Set API key in environment if not already present."""
        env_map = {
            "anthropic": "ANTHROPIC_API_KEY",
            "openai": "OPENAI_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
        }
        env_var = env_map.get(self._provider)
        if env_var and not os.environ.get(env_var):
            os.environ[env_var] = key

    def _with_cache_control(self, messages: list[dict]) -> list[dict]:
        """Add Anthropic cache_control to the system message when prompt caching is enabled.

        Anthropic prompt caching rules:
        - Minimum cacheable block: 1024 tokens (system) or 2048 tokens (user messages)
        - Adding cache_control to the system message costs +25% on first write,
          then saves 90% on all subsequent reads within the 5-minute TTL.
        - Only add to the FIRST (system) message — the static part that doesn't change.
        - For user/assistant history we do NOT add cache_control (they change every turn).
        """
        if not self._use_prompt_cache:
            return messages
        result = []
        for i, msg in enumerate(messages):
            if i == 0 and msg.get("role") == "system":
                content = msg.get("content", "")
                # Convert to content-block format required by Anthropic's caching API
                result.append({
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": content,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                })
            else:
                result.append(msg)
        return result

    def _resolve_model(self, hint: str = "auto", task_context: dict | None = None) -> str:
        """
        返回 Router model group alias ("fast" or "smart").
        研究: 保留此方法供旧调用路径使用，但现在只是返回 group alias
        让 litellm.Router 处理实际模型选择和 fallback。
        """
        if hint in ("fast", "smart"):
            return hint
        # auto routing
        if task_context:
            if (task_context.get("file_count", 0) > 3 or
                task_context.get("involves_architecture", False) or
                task_context.get("previous_tier_failed", False)):
                return "smart"
        return "fast"

    def resolve_task_model(self, task_name: str) -> str:
        """Resolve the model/tier for a named task from [llm.tasks] config.

        Returns the Router alias ("fast"/"smart") or a specific model name
        as configured in config.toml [llm.tasks] section.

        This is the primary routing method for fine-grained per-task model
        selection. All Python Soul callers should use this instead of
        hardcoding tier="fast" or tier="smart".

        Usage:
            tier = llm.resolve_task_model("commit")  # → "fast" (from config)
            tier = llm.resolve_task_model("architect")  # → "smart" (from config)
            tier = llm.resolve_task_model("code_review")  # → "gpt-4o" (user override)
        """
        # Look up in [llm.tasks] (from config.toml via bridge.call("config.get"))
        # Default: smart for reasoning tasks, fast for routine tasks
        _TASK_DEFAULTS: dict[str, str] = {
            "chat":         "smart",
            "architect":    "smart",
            "editor":       "fast",
            "summarize":    "fast",
            "commit":       "fast",
            "lint":         "fast",
            "memory_label": "fast",
            "code_review":  "smart",
            "wiki":         "fast",
        }
        return self._tasks.get(task_name, _TASK_DEFAULTS.get(task_name, "fast"))

    def get_task_params(self, task_name: str) -> dict:
        """Get max_tokens and temperature for a named task from [llm.params.<task>] config.

        Returns a dict with keys 'max_tokens' and 'temperature', falling back
        to global defaults from [llm.params] if the task has no override.

        All hardcoded values (max_tokens=4096, temperature=0.7) in Python files
        should be replaced with: params = llm.get_task_params("task_name")

        Usage:
            p = llm.get_task_params("commit")
            response = await llm.complete(prompt, tier=..., **p)
        """
        task_override = self._task_params.get(task_name, {})
        return {
            "max_tokens":  int(task_override.get("max_tokens",  self._default_max_tokens)),
            "temperature": float(task_override.get("temperature", self._default_temperature)),
        }

    async def complete_for_task(
        self,
        task_name: str,
        prompt: str,
        *,
        system: str | None = None,
        extra_params: dict | None = None,
    ) -> str:
        """Complete a prompt for a named task, reading ALL parameters from config.

        Resolution order (highest to lowest priority):
        1. [llm.roles.<task>] — role has its own base_url/api_key/model
        2. [llm.tasks.<task>] — task routing (tier alias or model name)
        3. [llm.params.<task>] — task-specific token/temperature overrides
        4. [llm] global defaults

        For roles with different providers (e.g. Anthropic for architect, DeepSeek for editor):
        - Creates a one-shot LLMClient with the role's specific configuration
        - The role's base_url and api_key override the global settings
        - Falls back to global settings for any unset fields

        Usage: await llm.complete_for_task("commit", prompt)
        """
        role_cfg = self._roles.get(task_name, {}) if isinstance(self._roles, dict) else {}

        if role_cfg and role_cfg.get("model"):
            # Role has custom config — may use different provider/endpoint
            model = role_cfg["model"]
            role_base_url = role_cfg.get("base_url") or self._base_url
            role_api_key  = role_cfg.get("api_key")  or self._config.get("api_key")

            # Build a temporary config for this role's provider
            role_config = dict(self._config)
            if role_base_url:
                role_config["base_url"] = role_base_url
            if role_api_key:
                role_config["api_key"] = role_api_key

            # Create a one-shot client for this role (lightweight — reuses litellm cache)
            role_client = LLMClient(role_config)
            params = self.get_task_params(task_name)
            if extra_params:
                params.update(extra_params)
            return await role_client.complete(
                prompt,
                model=model,  # bypass tier routing, use exact model
                system=system,
                **params,
            )
        else:
            # No role override — use standard tier routing
            tier   = self.resolve_task_model(task_name)
            params = self.get_task_params(task_name)
            if extra_params:
                params.update(extra_params)
            return await self.complete(prompt, tier=tier, system=system, **params)

    async def complete(
        self,
        prompt: str,
        *,
        tier: str = "smart",
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        task_context: dict | None = None,
    ) -> str:
        """
        Non-streaming completion. Returns full text.
        Prefer complete_for_task() for named tasks — it reads params from config.
        """
        resolved = model if model else self._resolve_model(hint=tier, task_context=task_context)
        prompt = self._maybe_chunk_input(prompt, resolved)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        # Apply Anthropic prompt caching to the system message (static part)
        messages = self._with_cache_control(messages)

        # litellm.Router.acompletion() — 研究: 替代手写 litellm.acompletion()
        # Router 处理: 重试、provider fallback、负载均衡（无需自写逻辑）
        kwargs: dict = {
            "model":       resolved,   # Router group alias ("fast"/"smart")
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
        }

        log.debug("complete: tier=%s model=%s tokens=%d", tier, resolved, max_tokens)
        try:
            response = await self._router.acompletion(**kwargs)
        except litellm.ContextWindowExceededError:
            log.warning("Context window exceeded, truncating input by 50%%...")
            messages[-1]["content"] = prompt[:len(prompt)//2] + "\n...[truncated]"
            response = await self._router.acompletion(**kwargs)
        except Exception as e:
            if "context" in str(e).lower() or "length" in str(e).lower() or "tokens" in str(e).lower():
                messages[-1]["content"] = prompt[:len(prompt)//2] + "\n...[truncated]"
                response = await self._router.acompletion(**kwargs)
            else:
                raise
        text = response.choices[0].message.content or ""

        # Always emit cost_update with token counts even if cost is 0.
        # Previously: only emitted when cost_usd > 0, which silently dropped
        # token stats for providers without pricing data (custom endpoints, etc.).
        try:
            from evocli_soul.rpc import emit_event
            usage = getattr(response, 'usage', None) or {}
            in_tok  = int(getattr(usage, "prompt_tokens",     0))
            out_tok = int(getattr(usage, "completion_tokens", 0))
            if in_tok > 0 or out_tok > 0:
                try:
                    cost_usd = litellm.completion_cost(completion_response=response)
                except Exception:
                    cost_usd = 0.0
                await emit_event("cost_update", {
                    "model":         resolved,
                    "input_tokens":  in_tok,
                    "output_tokens": out_tok,
                    "cost_usd":      cost_usd or 0.0,
                })
        except Exception as e:
            log.debug("Cost tracking failed (non-fatal): %s", e)

        return text

    async def stream(
        self,
        prompt: str,
        *,
        tier: str = "smart",
        model: str | None = None,
        system: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        task_context: dict | None = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming completion. Yields text chunks."""
        resolved = model if model else self._resolve_model(hint=tier, task_context=task_context)
        # FIX-ORACLE-3: 流式也需要截断超长输入（与 complete() 保持一致）
        prompt = self._maybe_chunk_input(prompt, resolved)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        kwargs: dict = {
            "model":       resolved,
            "messages":    messages,
            "max_tokens":  max_tokens,
            "temperature": temperature,
            "stream":      True,
        }

        log.debug("stream: tier=%s tokens=%d", resolved, max_tokens)
        try:
            response = await self._router.acompletion(**kwargs)
        except Exception as e:
            # Surface the error as a readable chunk then stop iteration,
            # rather than letting an unhandled exception crash the handler.
            log.warning("stream: acompletion failed (%s)", e)
            yield f"\n\n⚠️ Stream error: {type(e).__name__}: {e}"
            return
        try:
            async for chunk in response:
                text = ""
                if chunk.choices:
                    text = chunk.choices[0].delta.content or ""
                if text:
                    yield text
        except Exception as e:
            log.warning("stream: chunk iteration failed (%s)", e)
            yield f"\n\n⚠️ Stream interrupted: {type(e).__name__}: {e}"

    async def complete_messages(
        self,
        messages: list[dict],
        *,
        tier: str = "smart",
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        task_context: dict | None = None,
    ) -> str:
        """Completion with full message list (for agent use). Uses Router for retries/fallback."""
        resolved = model if model else self._resolve_model(hint=tier, task_context=task_context)
        response = await self._router.acompletion(
            model=resolved, messages=messages,
            max_tokens=max_tokens, temperature=temperature,
        )
        return response.choices[0].message.content or ""

    async def stream_messages(
        self,
        messages: list[dict],
        *,
        tier: str = "smart",
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        task_context: dict | None = None,
    ) -> AsyncGenerator[str, None]:
        """Streaming completion with full message list. Uses Router for retries/fallback."""
        resolved = model if model else self._resolve_model(hint=tier, task_context=task_context)
        response = await self._router.acompletion(
            model=resolved, messages=messages,
            max_tokens=max_tokens, temperature=temperature, stream=True,
        )
        async for chunk in response:
            text = chunk.choices[0].delta.content or "" if chunk.choices else ""
            if text:
                yield text

    def _maybe_chunk_input(self, prompt: str, model: str) -> str:
        """
        检测超长输入，智能压缩到模型 context window 的安全范围内。
        使用 model_context.py 的三级检测策略：
          1. API /v1/models 端点（可能含 context_length）
          2. litellm 内置数据库（max_input_tokens，覆盖主流模型）
          3. 模型名关键词推断 + 保守兜底
        按行边界截断，保留代码语法完整性。
        """
        from evocli_soul.model_context import get_input_context
        estimated_tokens = max(1, len(prompt) // 4)
        # 使用正确的 max_input_tokens（而非 max_tokens/max_output_tokens）
        max_input = get_input_context(
            model,
            base_url=self._base_url,
            api_key=self._config.get("api_key"),
        )
        safe_limit = int(max_input * 0.75)  # 留 25% 给 system prompt 和 output
        if estimated_tokens <= safe_limit:
            return prompt
        target_chars = safe_limit * 4
        log.warning(
            "Input too long (~%d tokens > %d safe limit of %d), chunking by line boundary",
            estimated_tokens, safe_limit, max_input
        )
        lines = prompt.splitlines(keepends=True)
        result, total = [], 0
        for line in lines:
            if total + len(line) > target_chars:
                break
            result.append(line)
            total += len(line)
        omitted = len(lines) - len(result)
        return (
            "".join(result)
            + f"\n\n...[输入过长，已截断 {omitted} 行。"
            f"原始约 {estimated_tokens} tokens，"
            f"模型 '{model}' 上限 {max_input:,} tokens。"
            f"如需分析完整内容请分段提交。]"
        )
