"""
model_context.py — 模型 Context Window 动态探测

核心设计：不依赖任何本地静态数据库，让 API 自己告诉我们。

策略（解决"模型名称一直在变"问题）：
  1. litellm DB 精确匹配 + 前缀剥离  — 已知模型立即返回
  2. 异步探针 (1 token call)         — 让 API 返回带版本号的 response.model，
                                       再用这个精确名称查 litellm DB
                                       新模型、别名、relay 前缀 全部解决
  3. litellm DB 在线刷新              — 每次启动检查是否有更新的模型数据
  4. API /v1/models 端点              — OpenRouter/vLLM 返回 context_length
  5. 保守兜底 + 明确错误              — 告知用户如何配置

为什么探针有效：
  API 返回 response.model = "gpt-4o-mini-2024-07-18" (带版本号)
  litellm 有 "gpt-4o-mini-2024-07-18" = 128,000 tokens
  用户发送 "gpt-4o-mini" → API 知道是哪个版本 → 我们也知道了
"""
from __future__ import annotations

import asyncio
import importlib.util
import logging
from typing import Optional

log = logging.getLogger("evocli.model_context")

_FALLBACK_CONTEXT  = 8_192
_FALLBACK_OUTPUT   = 4_096
_cache: dict[str, dict] = {}         # model@base_url → context_info
_probe_futures: dict[str, asyncio.Future] = {}   # 正在进行的探针

_PROVIDER_PREFIXES = [
    "openai/", "anthropic/", "google/", "meta/", "meta-llama/",
    "mistral/", "cohere/", "groq/", "fireworks/", "together/",
    "vertex_ai/", "bedrock/", "azure/", "azure_ai/", "huggingface/",
    "perplexity/", "replicate/", "databricks/", "vllm/", "ollama/",
]


def get_model_context(
    model: str,
    base_url: Optional[str] = None,
    api_key:  Optional[str] = None,
    config_override: Optional[int] = None,
) -> dict:
    """
    同步版本：返回已知上下文信息，或发起后台探针。
    第一次调用返回 conservative_fallback，探针完成后 _cache 自动更新。
    """
    key = f"{model}@{base_url or ''}"
    if key in _cache:
        return _cache[key]

    # 1. 用户显式配置（最高优先）
    if config_override and config_override > 0:
        r = _make(model, config_override, _FALLBACK_OUTPUT, "user_config")
        _cache[key] = r
        return r

    # 2. litellm DB 精确匹配
    r = _query_litellm(model)
    if r:
        _cache[key] = r
        return r

    # 3. 前缀剥离后再查 litellm DB
    stripped = _strip_prefix(model)
    if stripped != model:
        r = _query_litellm(stripped)
        if r:
            r = {**r, "model": model, "source": r["source"] + "+stripped"}
            _cache[key] = r
            return r

    # 4. API /v1/models 端点（OpenRouter/vLLM 可能返回 context_length）
    if base_url and api_key:
        r = _probe_models_endpoint(model, base_url, api_key)
        if r:
            _cache[key] = r
            return r

    # 5. 发起异步探针，本次返回保守兜底
    if base_url and api_key and key not in _probe_futures:
        _schedule_probe(key, model, base_url, api_key)

    r = _make(model, _FALLBACK_CONTEXT, _FALLBACK_OUTPUT, "conservative_pending_probe")
    _cache[key] = r
    log.info(
        "Context window unknown for '%s' — probe scheduled. "
        "Using conservative %d tokens until probe completes. "
        "Or set [context] max_total in ~/.evocli/config.toml",
        model, _FALLBACK_CONTEXT
    )
    return r


async def get_model_context_async(
    model: str,
    base_url: Optional[str] = None,
    api_key:  Optional[str] = None,
    config_override: Optional[int] = None,
) -> dict:
    """
    异步版本：等待探针完成后返回精确值。
    在 agent startup 时调用以预热缓存。
    """
    key = f"{model}@{base_url or ''}"
    
    # 先走同步路径（可能立即命中 litellm DB）
    r = get_model_context(model, base_url, api_key, config_override)
    if "pending_probe" not in r.get("source", ""):
        return r

    # 等待探针
    if key in _probe_futures:
        try:
            await asyncio.wait_for(_probe_futures[key], timeout=8.0)
        except (asyncio.TimeoutError, Exception):
            pass
    
    return _cache.get(key, r)


def _schedule_probe(key: str, model: str, base_url: str, api_key: str) -> None:
    """发起后台探针：1 token call → 获取 response.model → 查 litellm DB"""
    try:
        loop = asyncio.get_running_loop()
        fut  = loop.create_future()
        _probe_futures[key] = fut
        asyncio.create_task(_run_probe(key, model, base_url, api_key, fut))
    except RuntimeError:
        pass  # 没有运行中的 event loop，跳过


async def _run_probe(key: str, model: str, base_url: str, api_key: str,
                     future: asyncio.Future) -> None:
    """
    探针：1 token call → response.model（带版本号）→ litellm DB。

    关键修复：
    - 用剥离前缀的名称调用，避免 litellm 把 anthropic/ 当路由前缀直连 Anthropic
    - 始终通过 base_url (relay) 发送请求
    """
    try:
        import litellm as _ll
        _ll.suppress_debug_info = True

        # 剥离前缀后作为 model name 调用 relay（不让 litellm 自己路由）
        probe_model = _strip_prefix(model)

        resp = await _ll.acompletion(
            model=probe_model,          # 用剥离前缀的名称
            messages=[{"role": "user", "content": "."}],
            max_tokens=1,
            api_base=base_url,          # 始终走 relay
            api_key=api_key,
        )

        canonical = resp.model or probe_model
        log.debug("Probe: %s (probe_model=%s) -> response.model = %s", model, probe_model, canonical)

        # 用 response.model 查 litellm DB（优先带版本号的精确名称）
        for try_name in [canonical, _strip_version_suffix(canonical), probe_model]:
            r = _query_litellm(try_name)
            if r:
                r = {**r, "model": model, "source": r["source"] + f"+probe({try_name})"}
                _cache[key] = r
                log.info(
                    "Context learned: '%s' -> response='%s', matched='%s' = %d tokens",
                    model, canonical, try_name, r["max_input_tokens"]
                )
                return

        log.warning(
            "Probe got response.model='%s' but not in litellm DB. "
            "Model '%s' is genuinely unknown. "
            "Options: (1) pip install -U litellm  (2) set [context] max_total in config.toml",
            canonical, model
        )
        _cache[key] = {**_cache.get(key, _make(model, _FALLBACK_CONTEXT, _FALLBACK_OUTPUT, "")),
                       "source": f"probe_done_unknown(response={canonical})"}

    except Exception as e:
        log.debug("Probe failed for '%s': %s", model, e)
        if key in _cache:
            _cache[key] = {**_cache[key], "source": "probe_failed_using_fallback"}
    finally:
        if not future.done():
            future.set_result(None)
        _probe_futures.pop(key, None)


def _query_litellm(model: str) -> Optional[dict]:
    """查 litellm 内置数据库。使用 max_input_tokens（真实 context window）。"""
    if not importlib.util.find_spec("litellm"):
        return None
    try:
        import litellm
        info = litellm.get_model_info(model)
        inp  = info.get("max_input_tokens")
        out  = info.get("max_output_tokens") or info.get("max_tokens", _FALLBACK_OUTPUT)
        if inp and inp > 0:
            return _make(model, inp, out, "litellm_db")
    except Exception:
        pass
    return None


def _probe_models_endpoint(model: str, base_url: str, api_key: str) -> Optional[dict]:
    """API /v1/models 端点（OpenRouter/vLLM 会返回 context_length）。"""
    try:
        import urllib.request
        import json as _j
        req = urllib.request.Request(
            base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = _j.loads(resp.read())
        for m in data.get("data", []):
            if m.get("id") != model:
                continue
            ctx = (m.get("context_length") or m.get("context_window")
                   or m.get("max_model_len")
                   or (m.get("config") or {}).get("max_position_embeddings"))
            if ctx and int(ctx) > 0:
                out = m.get("max_completion_tokens") or m.get("max_output_tokens") or min(int(ctx)//8, 16384)
                return _make(model, int(ctx), int(out), "api_models_endpoint")
    except Exception:
        pass
    return None


def _strip_prefix(model: str) -> str:
    """剥离 relay API 添加的 provider/ 路由前缀（不猜测上下文大小，仍查权威 DB）。"""
    for p in _PROVIDER_PREFIXES:
        if model.startswith(p):
            return model[len(p):]
    return model


def _strip_version_suffix(model: str) -> str:
    """去掉末尾的日期版本号，如 gpt-4o-mini-2024-07-18 -> gpt-4o-mini。"""
    import re
    return re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)


def _make(model: str, inp: int, out: int, source: str) -> dict:
    return {"model": model, "max_input_tokens": inp, "max_output_tokens": out, "source": source}


# ── 同步便捷接口 ──────────────────────────────────────────────────────

def get_input_context(model: str, base_url: Optional[str] = None,
                      api_key: Optional[str] = None,
                      config_override: Optional[int] = None) -> int:
    return get_model_context(model, base_url, api_key, config_override)["max_input_tokens"]


def describe_all(models: list[str], base_url: Optional[str] = None,
                 api_key: Optional[str] = None) -> list[dict]:
    return [get_model_context(m, base_url=base_url, api_key=api_key) for m in models]


async def warmup(models: list[str], base_url: str, api_key: str) -> None:
    """
    启动时预热：并发探测所有模型，完成后所有查询均精确。
    在 evocli_soul/main.py 的 main() 中调用。
    """
    tasks = [get_model_context_async(m, base_url=base_url, api_key=api_key) for m in models]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    for m, r in zip(models, results):
        if isinstance(r, dict):
            log.info("Warmup: %s = %d tokens (%s)", m, r["max_input_tokens"], r["source"])
