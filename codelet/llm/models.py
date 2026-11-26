"""Dynamic model catalog: fetch, parse, cache, and periodically refresh models.

Works with any OpenAI-compatible `/models` endpoint (ZenMux, OpenRouter,
DeepSeek, Moonshot, SiliconFlow, Ollama, vLLM, ...) as well as Anthropic's
native `/v1/models` endpoint. Parses rich metadata when provided -- in
particular ZenMux's `display_name`, `context_length`, `input_modalities`,
`output_modalities`, `capabilities.reasoning`, and `pricings` -- and degrades
gracefully to minimal `{id}` entries for simpler endpoints.

Cache layout (`~/.codelet/models_cache.json` by default, atomic writes):
    {
      "https://zenmux.ai/api/v1": {
        "fetched_at": 1764160000.0,
        "models": [ { "id": "...", "display_name": "...", ... }, ... ]
      }
    }

Design invariants:
  - Fetching never raises into the caller unless `fetch_models()` is called
    directly; `ModelCatalog` returns stale cache or an empty list on error.
  - Non-text output models (e.g. pure image/video/audio generators whose
    `output_modalities` excludes `"text"`) are filtered out for coding chat.
  - Pricing extracted from `pricings` is in USD per million tokens so it plugs
    straight into `Telemetry.update_pricing()`.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx

DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24h
DEFAULT_FETCH_TIMEOUT = 15.0
ANTHROPIC_DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class ModelInfo:
    """Normalized metadata for one selectable chat model."""

    id: str
    display_name: str = ""
    owned_by: str = ""
    created: int | None = None
    context_length: int | None = None
    input_modalities: list[str] = field(default_factory=list)
    output_modalities: list[str] = field(default_factory=list)
    reasoning: bool = False
    pricing: dict[str, float] | None = None  # {"input": USD/1M, "output": USD/1M}

    @property
    def vision(self) -> bool:
        return "image" in self.input_modalities

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["vision"] = self.vision
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "ModelInfo | None":
        mid = str(raw.get("id") or "").strip()
        if not mid:
            return None
        pricing_raw = raw.get("pricing")
        pricing: dict[str, float] | None = None
        if (
            isinstance(pricing_raw, dict)
            and "input" in pricing_raw
            and "output" in pricing_raw
        ):
            try:
                pricing = {
                    "input": float(pricing_raw["input"]),
                    "output": float(pricing_raw["output"]),
                }
            except (TypeError, ValueError):
                pricing = None
        created = raw.get("created")
        if created is not None:
            try:
                created = int(created)
            except (TypeError, ValueError):
                created = None
        ctx = raw.get("context_length")
        if ctx is not None:
            try:
                ctx = int(ctx)
            except (TypeError, ValueError):
                ctx = None
        return cls(
            id=mid,
            display_name=str(raw.get("display_name") or ""),
            owned_by=str(raw.get("owned_by") or ""),
            created=created,
            context_length=ctx,
            input_modalities=_str_list(raw.get("input_modalities")),
            output_modalities=_str_list(raw.get("output_modalities")),
            reasoning=bool(raw.get("reasoning", False)),
            pricing=pricing,
        )


def _str_list(val: Any) -> list[str]:
    if not isinstance(val, list):
        return []
    return [str(x).strip().lower() for x in val if x is not None and str(x).strip()]


def normalize_base_url(base_url: str | None, provider: str = "openai") -> str:
    """Canonical cache key & endpoint prefix for a provider/base_url pair."""
    raw = (base_url or "").strip().rstrip("/")
    if not raw:
        if provider == "anthropic":
            return ANTHROPIC_DEFAULT_BASE_URL
        return ""
    if raw.endswith("/models"):
        raw = raw[: -len("/models")].rstrip("/")
    return raw


def models_endpoint(base_url: str | None, provider: str = "openai") -> str:
    """Resolve the full GET /models URL for an endpoint."""
    norm = normalize_base_url(base_url, provider=provider)
    return f"{norm}/models" if norm else ""


def _extract_zenmux_tier_price(entries: Any) -> float | None:
    """Pick the base perMTokens USD price from a ZenMux pricings array.

    ZenMux shape:
        [{"value": 3.0, "unit": "perMTokens", "currency": "USD", "conditions": {...}}]
    Prefers unconditional tiers; falls back to the first perMTokens USD tier.
    """
    if not isinstance(entries, list):
        return None
    fallback: float | None = None
    for item in entries:
        if not isinstance(item, dict):
            continue
        unit = str(item.get("unit") or "").strip()
        if unit and unit != "perMTokens":
            continue
        currency = str(item.get("currency") or "USD").strip().upper()
        if currency != "USD":
            continue
        val = item.get("value")
        if val is None:
            continue
        try:
            price = float(val)
        except (TypeError, ValueError):
            continue
        if not item.get("conditions"):
            return price
        if fallback is None:
            fallback = price
    return fallback


def _extract_pricing(item: dict[str, Any]) -> dict[str, float] | None:
    """Extract USD per-1M-tokens pricing from ZenMux or OpenRouter-like schemas."""
    # 1. ZenMux: pricings.{prompt, completion} -> list[{value, unit, currency}]
    pricings = item.get("pricings")
    if isinstance(pricings, dict):
        inp = _extract_zenmux_tier_price(pricings.get("prompt"))
        out = _extract_zenmux_tier_price(pricings.get("completion"))
        if inp is not None and out is not None:
            return {"input": inp, "output": out}

    # 2. Flat pricing dicts (e.g. OpenRouter: per-token strings, or already per-M)
    pricing = item.get("pricing")
    if isinstance(pricing, dict):
        if "input" in pricing and "output" in pricing:
            try:
                return {"input": float(pricing["input"]), "output": float(pricing["output"])}
            except (TypeError, ValueError):
                pass
        if "prompt" in pricing and "completion" in pricing:
            try:
                p = float(pricing["prompt"])
                c = float(pricing["completion"])
                # OpenRouter reports USD per single token (e.g. "0.000003") -> scale to 1M.
                if 0 <= p < 0.01 and 0 <= c < 0.05 and (p > 0 or c > 0):
                    p *= 1_000_000
                    c *= 1_000_000
                return {"input": round(p, 6), "output": round(c, 6)}
            except (TypeError, ValueError):
                pass
    return None


def parse_models_payload(payload: Any) -> list[ModelInfo]:
    """Parse a `/models` JSON payload into normalized, text-capable ModelInfo rows."""
    if isinstance(payload, dict):
        items = payload.get("data")
        if not isinstance(items, list):
            items = payload.get("models")
    elif isinstance(payload, list):
        items = payload
    else:
        items = None

    if not isinstance(items, list):
        return []

    seen: set[str] = set()
    out: list[ModelInfo] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        mid = str(raw.get("id") or "").strip()
        if not mid or mid in seen:
            continue

        out_mods = _str_list(raw.get("output_modalities"))
        # Skip non-text models (image/video/audio generators) when modalities are declared.
        if out_mods and "text" not in out_mods:
            continue

        in_mods = _str_list(raw.get("input_modalities"))
        caps = raw.get("capabilities")
        reasoning = bool(caps.get("reasoning")) if isinstance(caps, dict) else False

        ctx_raw = (
            raw.get("context_length")
            or raw.get("context_window")
            or raw.get("max_context_length")
        )
        ctx: int | None = None
        if ctx_raw is not None:
            try:
                ctx_val = int(ctx_raw)
                if ctx_val > 0:
                    ctx = ctx_val
            except (TypeError, ValueError):
                pass

        created_raw = raw.get("created")
        created: int | None = None
        if created_raw is not None:
            try:
                created = int(created_raw)
            except (TypeError, ValueError):
                pass

        owned_by = str(raw.get("owned_by") or "").strip()
        if not owned_by and "/" in mid:
            owned_by = mid.split("/", 1)[0]

        display_name = str(raw.get("display_name") or raw.get("name") or "").strip()

        seen.add(mid)
        out.append(
            ModelInfo(
                id=mid,
                display_name=display_name,
                owned_by=owned_by,
                created=created,
                context_length=ctx,
                input_modalities=in_mods,
                output_modalities=out_mods,
                reasoning=reasoning,
                pricing=_extract_pricing(raw),
            )
        )

    # Sort deterministically: provider (owned_by) then newest-first (when created
    # is present) then id. Keeps related models grouped and fresh releases near top.
    out.sort(key=lambda m: (m.owned_by.lower(), -(m.created or 0), m.id.lower()))
    return out


def fetch_models(
    base_url: str | None,
    api_key: str | None = None,
    *,
    provider: str = "openai",
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> list[ModelInfo]:
    """Synchronously GET `/models` from the configured endpoint and parse it."""
    url = models_endpoint(base_url, provider=provider)
    if not url:
        raise ValueError("No base_url configured for model listing")
    headers = _auth_headers(api_key, provider=provider)
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
        return parse_models_payload(resp.json())


async def afetch_models(
    base_url: str | None,
    api_key: str | None = None,
    *,
    provider: str = "openai",
    timeout: float = DEFAULT_FETCH_TIMEOUT,
) -> list[ModelInfo]:
    """Asynchronously GET `/models` from the configured endpoint and parse it."""
    url = models_endpoint(base_url, provider=provider)
    if not url:
        raise ValueError("No base_url configured for model listing")
    headers = _auth_headers(api_key, provider=provider)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        return parse_models_payload(resp.json())


def _auth_headers(api_key: str | None, *, provider: str) -> dict[str, str]:
    headers: dict[str, str] = {"Accept": "application/json"}
    if provider == "anthropic":
        headers["anthropic-version"] = ANTHROPIC_VERSION
        if api_key:
            headers["x-api-key"] = api_key
    elif api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def default_cache_path() -> Path:
    return Path.home() / ".codelet" / "models_cache.json"


def _write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f"{path.stem}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@dataclass
class CatalogSnapshot:
    """Result of querying or refreshing the model catalog for one endpoint."""

    models: list[ModelInfo] = field(default_factory=list)
    fetched_at: float | None = None
    source: str = "empty"  # "remote" | "cache" | "stale_cache" | "empty"
    error: str | None = None

    def pricing_map(self) -> dict[str, dict[str, float]]:
        return {m.id: dict(m.pricing) for m in self.models if m.pricing}

    def get(self, model_id: str) -> ModelInfo | None:
        for m in self.models:
            if m.id == model_id:
                return m
        return None


class ModelCatalog:
    """Disk-backed, TTL-aware model catalog keyed by normalized base_url."""

    def __init__(
        self,
        cache_path: str | Path | None = None,
        *,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
        fetcher: Callable[..., list[ModelInfo]] | None = None,
        afetcher: Callable[..., Any] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.cache_path = Path(cache_path) if cache_path is not None else default_cache_path()
        self.ttl_seconds = max(0, int(ttl_seconds))
        self._fetcher = fetcher or fetch_models
        self._afetcher = afetcher or afetch_models
        self._clock = clock or time.time

    # ---- disk cache helpers ----

    def _read_all(self) -> dict[str, Any]:
        if not self.cache_path.is_file():
            return {}
        try:
            data = json.loads(self.cache_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_entry(self, key: str, models: list[ModelInfo], fetched_at: float) -> None:
        data = self._read_all()
        data[key] = {
            "fetched_at": fetched_at,
            "models": [m.to_dict() for m in models],
        }
        try:
            _write_json_atomic(self.cache_path, data)
        except OSError:
            pass

    def get_cached(
        self, base_url: str | None, *, provider: str = "openai"
    ) -> CatalogSnapshot:
        """Read the local cache without doing any network I/O."""
        key = normalize_base_url(base_url, provider=provider)
        if not key:
            return CatalogSnapshot()
        entry = self._read_all().get(key)
        if not isinstance(entry, dict):
            return CatalogSnapshot()
        raw_models = entry.get("models")
        if not isinstance(raw_models, list):
            return CatalogSnapshot()
        models = [m for r in raw_models if isinstance(r, dict) and (m := ModelInfo.from_dict(r))]
        fetched_at = entry.get("fetched_at")
        try:
            fetched_ts = float(fetched_at) if fetched_at is not None else None
        except (TypeError, ValueError):
            fetched_ts = None
        if not models:
            return CatalogSnapshot(models=[], fetched_at=fetched_ts, source="empty")
        stale = self._is_stale(fetched_ts)
        return CatalogSnapshot(
            models=models,
            fetched_at=fetched_ts,
            source="stale_cache" if stale else "cache",
        )

    def _is_stale(self, fetched_at: float | None) -> bool:
        if fetched_at is None:
            return True
        if self.ttl_seconds <= 0:
            return True
        return (self._clock() - fetched_at) >= self.ttl_seconds

    def needs_refresh(self, base_url: str | None, *, provider: str = "openai") -> bool:
        key = normalize_base_url(base_url, provider=provider)
        if not key:
            return False
        snap = self.get_cached(base_url, provider=provider)
        return snap.source in ("empty", "stale_cache")

    # ---- sync / async refresh ----

    def refresh(
        self,
        base_url: str | None,
        api_key: str | None = None,
        *,
        provider: str = "openai",
        force: bool = False,
    ) -> CatalogSnapshot:
        """Return fresh models, fetching from the endpoint if stale or forced."""
        key = normalize_base_url(base_url, provider=provider)
        if not key:
            return CatalogSnapshot()

        cached = self.get_cached(base_url, provider=provider)
        if not force and cached.source == "cache":
            return cached

        try:
            models = self._fetcher(base_url, api_key, provider=provider)
        except Exception as exc:
            if cached.models:
                return CatalogSnapshot(
                    models=cached.models,
                    fetched_at=cached.fetched_at,
                    source="stale_cache",
                    error=f"{type(exc).__name__}: {exc}",
                )
            return CatalogSnapshot(error=f"{type(exc).__name__}: {exc}")

        now = self._clock()
        if models:
            self._save_entry(key, models, now)
            return CatalogSnapshot(models=models, fetched_at=now, source="remote")
        # Empty response: keep previous cache if any instead of wiping it out.
        if cached.models:
            return CatalogSnapshot(
                models=cached.models,
                fetched_at=cached.fetched_at,
                source="stale_cache",
                error="endpoint returned 0 models",
            )
        return CatalogSnapshot(models=[], fetched_at=now, source="remote")

    async def arefresh(
        self,
        base_url: str | None,
        api_key: str | None = None,
        *,
        provider: str = "openai",
        force: bool = False,
    ) -> CatalogSnapshot:
        """Async version of `refresh()` for the Web GUI & background scheduler."""
        key = normalize_base_url(base_url, provider=provider)
        if not key:
            return CatalogSnapshot()

        cached = self.get_cached(base_url, provider=provider)
        if not force and cached.source == "cache":
            return cached

        try:
            models = await self._afetcher(base_url, api_key, provider=provider)
        except Exception as exc:
            if cached.models:
                return CatalogSnapshot(
                    models=cached.models,
                    fetched_at=cached.fetched_at,
                    source="stale_cache",
                    error=f"{type(exc).__name__}: {exc}",
                )
            return CatalogSnapshot(error=f"{type(exc).__name__}: {exc}")

        now = self._clock()
        if models:
            self._save_entry(key, models, now)
            return CatalogSnapshot(models=models, fetched_at=now, source="remote")
        if cached.models:
            return CatalogSnapshot(
                models=cached.models,
                fetched_at=cached.fetched_at,
                source="stale_cache",
                error="endpoint returned 0 models",
            )
        return CatalogSnapshot(models=[], fetched_at=now, source="remote")


def resolve_ttl_seconds(settings: dict[str, Any] | None = None) -> int:
    """Resolve cache TTL from `LLM_MODELS_TTL` env var > settings.json > 24h default."""
    env_val = os.environ.get("LLM_MODELS_TTL", "").strip()
    if env_val:
        try:
            return max(0, int(env_val))
        except ValueError:
            pass
    if isinstance(settings, dict) and "models_cache_ttl_seconds" in settings:
        try:
            return max(0, int(settings["models_cache_ttl_seconds"]))
        except (TypeError, ValueError):
            pass
    return DEFAULT_CACHE_TTL_SECONDS
