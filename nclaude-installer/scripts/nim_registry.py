#!/usr/bin/env python3
"""NVIDIA NIM candidate probing and LiteLLM/Claude Code config generation."""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
CLAUDE_ENV_KEYS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
)

def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise

def normalize_model_id(legacy_id: str) -> str:
    """Remove one legacy provider-like prefix; preserve the publisher namespace."""
    value = legacy_id.strip()
    value = value.removeprefix("nvidia/")
    parts = value.split("/")
    if (
        len(parts) < 2
        or any(not part for part in parts)
        or any(c.isspace() for c in value)
        or any(not all(ch.isalnum() or ch in "._-" for ch in part) for part in parts)
    ):
        raise ValueError(f"Invalid NVIDIA model ID: {legacy_id!r}")
    return value

def read_registry(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("models")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Model registry must contain a non-empty 'models' array.")
    seen: set[str] = set()
    result = []
    for row in rows:
        legacy = row.get("legacy_id")
        if not isinstance(legacy, str):
            raise TypeError("Every registry entry requires a string legacy_id.")
        model_id = normalize_model_id(legacy)
        if model_id in seen:
            raise ValueError(f"Duplicate normalized model ID: {model_id}")
        seen.add(model_id)
        result.append({"rank": int(row.get("rank", len(result) + 1)),
                       "legacy_id": legacy, "model_id": model_id})
    return result

def request_json(url: str, payload: dict[str, Any], headers: dict[str, str],
                 timeout: int) -> tuple[int, dict[str, Any] | str]:
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # nosec B310
            raw = response.read(2_000_000).decode("utf-8", "replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw[:1000]
    except urllib.error.HTTPError as exc:
        raw = exc.read(2_000_000).decode("utf-8", "replace")
        try:
            body: dict[str, Any] | str = json.loads(raw)
        except json.JSONDecodeError:
            body = raw[:1000]
        return exc.code, body
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)[:500]

def error_detail(body: dict[str, Any] | str) -> str:
    if isinstance(body, dict):
        detail = body.get("detail") or body.get("message") or body.get("error") or body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or detail
        return str(detail).replace("\n", " ")[:300]
    return str(body).replace("\n", " ")[:300]

def probe(args: argparse.Namespace) -> int:
    api_key = os.environ.get("NVIDIA_NIM_API_KEY", "")
    if not api_key:
        raise SystemExit("NVIDIA_NIM_API_KEY is required in the environment.")
    candidates = read_registry(Path(args.registry))
    url = args.base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    results = []
    print(f"Probing {len(candidates)} NVIDIA NIM candidates sequentially; max_tokens={args.max_tokens}.")
    for candidate in candidates:
        model_id = candidate["model_id"]
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content":
                "Reply with exactly NIM_OK. Do not include analysis or explanation."}],
            "max_tokens": args.max_tokens,
            "stream": False,
        }
        start = time.perf_counter()
        status, body = request_json(url, payload, headers, args.timeout)
        elapsed = time.perf_counter() - start
        state, reason = "UNAVAILABLE", ""
        if status == 200 and isinstance(body, dict):
            choices = body.get("choices")
            choice = choices[0] if isinstance(choices, list) and choices else {}
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, str) and content.strip():
                state = "ACTIVE"
                reason = "Chat completion returned non-empty assistant content."
            else:
                state = "INCONCLUSIVE"
                reason = ("HTTP 200 but no assistant text was returned; possibly reasoning "
                          "tokens exhausted the probe budget or unsupported response shape.")
        elif status == 410:
            state, reason = "EOL", error_detail(body)
        elif status == 401:
            state, reason = "AUTH_ERROR", error_detail(body)
        elif status == 403:
            state, reason = "FORBIDDEN", error_detail(body)
        elif status == 429:
            state, reason = "RATE_LIMITED", error_detail(body)
        elif status == 404:
            state, reason = "NOT_FOUND", error_detail(body)
        elif status == 0:
            state, reason = "NETWORK_ERROR", error_detail(body)
        else:
            state, reason = "UNAVAILABLE", error_detail(body)
        result = {
            **candidate,
            "status_code": status,
            "state": state,
            "reason": reason,
            "latency_seconds": round(elapsed, 4),
        }
        results.append(result)
        print(f"[{state:13}] HTTP {status or 'N/A':>3}  {model_id}")
        if reason and state != "ACTIVE":
            print(f"               {reason}")
        # Avoid hammering the shared API catalog.
        time.sleep(args.interval)
        if state in {"AUTH_ERROR", "FORBIDDEN"}:
            print("Stopping probes because the API key/account is not authorized.")
            break
    output = {
        "schema_version": 1,
        "checked_at_epoch": int(time.time()),
        "base_url": args.base_url,
        "probe_max_tokens": args.max_tokens,
        "results": results,
        "active_model_ids": [r["model_id"] for r in results if r["state"] == "ACTIVE"],
    }
    latencies = {
        r["model_id"]: r.get("latency_seconds")
        for r in results
        if r.get("latency_seconds") is not None
    }
    if latencies:
        output["model_latencies"] = dict(
            sorted(latencies.items(), key=lambda item: item[1])
        )
    atomic_write(Path(args.output), json.dumps(output, indent=2) + "\n", 0o600)
    active = output["active_model_ids"]
    print(f"\nProbe summary: {len(active)} ACTIVE / {len(results)} checked.")
    if not active:
        print("No model passed the inference probe; installer must not replace the current configuration.")
        return 2
    return 0

def build(args: argparse.Namespace) -> int:
    probe_data = json.loads(
        Path(args.probe_results).read_text(encoding="utf-8")
    )
    if not isinstance(probe_data, dict):
        raise TypeError("Probe results must be a JSON object.")

    results = probe_data.get("results")
    if not isinstance(results, list):
        raise TypeError("Probe results must contain a results array.")

    active = []
    seen = set()

    for item in results:
        if not isinstance(item, dict) or item.get("state") != "ACTIVE":
            continue

        legacy_id = item.get("legacy_id")
        if not isinstance(legacy_id, str):
            raise TypeError("ACTIVE probe result is missing legacy_id.")

        # Always derive the upstream ID from the canonical legacy ID.
        # Never trust model_id persisted by an older probe implementation.
        model_id = normalize_model_id(legacy_id)

        supplied_id = item.get("model_id")
        if supplied_id is not None and supplied_id not in (legacy_id, model_id):
            raise ValueError(
                f"Probe model_id mismatch for {legacy_id!r}: "
                f"{supplied_id!r}"
            )

        if model_id in seen:
            raise ValueError(f"Duplicate active model ID: {model_id}")

        seen.add(model_id)
        active.append({**item, "model_id": model_id})

    if not active:
        raise SystemExit(
            "No ACTIVE models in probe results; refusing to generate config."
        )

    if getattr(args, "sort_by_latency", False):
        active.sort(key=lambda item: item.get("latency_seconds") or float("inf"))

    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not master_key:
        master_key = "sk-" + secrets.token_urlsafe(40)

    model_list = []
    aliases = []

    for index, item in enumerate(active):
        alias = (
            "nclaude"
            if index == 0
            else f"nclaude-fallback-{index + 1:02d}"
        )
        aliases.append(alias)

        model_list.append({
            "model_name": alias,
            "litellm_params": {
                "model": "nvidia_nim/" + item["model_id"],
                "api_base": args.base_url.rstrip("/"),
                "api_key": "os.environ/NVIDIA_NIM_API_KEY",
                "drop_params": True,
                "timeout": args.provider_timeout,
            },
        })

    config = {
        "model_list": model_list,
        "litellm_settings": {
            "drop_params": True,
            "set_verbose": False,
        },
        "router_settings": {
            "num_retries": 0,
            "max_fallbacks": max(0, len(aliases) - 1),
            "cooldown_time": 60,
            "allowed_fails": 1,
            "fallbacks": (
                [{"nclaude": aliases[1:]}]
                if len(aliases) > 1 else []
            ),
        },
        "general_settings": {
            "master_key": "os.environ/LITELLM_MASTER_KEY"
        },
    }

    atomic_write(
        Path(args.output),
        json.dumps(config, indent=2) + "\n",
        0o600,
    )

    print(f"Generated {len(model_list)} verified model deployments.")
    print("Primary:", active[0]["model_id"])
    print(
        "Fallback order:",
        " -> ".join(x["model_id"] for x in active[1:]) or "(none)",
    )
    return 0

def merge_settings(args: argparse.Namespace) -> int:
    source = Path(args.input)
    if source.exists():
        try:
            settings = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"Refusing to overwrite invalid Claude settings: {exc}")
        if not isinstance(settings, dict):
            raise SystemExit("Claude settings root must be a JSON object.")
    else:
        settings = {}
    env = settings.setdefault("env", {})
    if not isinstance(env, dict):
        raise SystemExit("Claude settings 'env' must be a JSON object.")
    endpoint = f"http://127.0.0.1:{args.port}"
    env.update({
        "ANTHROPIC_BASE_URL": endpoint,
        "ANTHROPIC_AUTH_TOKEN": args.master_key,
        "ANTHROPIC_MODEL": "nclaude",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "nclaude",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "nclaude",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "nclaude",
    })
    atomic_write(Path(args.output), json.dumps(settings, indent=2, ensure_ascii=False) + "\n", 0o600)
    return 0

def smoke(args: argparse.Namespace) -> int:
    master_key = os.environ.get("LITELLM_MASTER_KEY", "")
    if not master_key:
        raise SystemExit("LITELLM_MASTER_KEY is missing.")
    payload = {
        "model": "nclaude",
        "max_tokens": args.max_tokens,
        "messages": [{"role": "user", "content": "Reply with exactly NCLAUDE_OK."}],
    }
    status, body = request_json(
        args.base_url.rstrip("/") + "/v1/messages",
        payload,
        {
            "x-api-key": master_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        args.timeout,
    )
    if status != 200 or not isinstance(body, dict):
        print(f"Claude Messages smoke test failed: HTTP {status}; {error_detail(body)}")
        return 1
    blocks = body.get("content", [])
    text_blocks = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    response_text = "\n".join(x for x in text_blocks if isinstance(x, str))
    if not response_text.strip():
        print("Claude Messages returned HTTP 200 but no text content.")
        return 1
    print("Claude Messages smoke test passed.")
    print("Response:", response_text[:180].replace("\n", " "))
    return 0

def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("probe")
    p.add_argument("--registry", required=True)
    p.add_argument("--output", required=True)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL)
    p.add_argument("--max-tokens", type=int, default=512)
    p.add_argument("--timeout", type=int, default=90)
    p.add_argument("--interval", type=float, default=1.0)
    p.set_defaults(func=probe)
    b = sub.add_parser("build")
    b.add_argument("--probe-results", required=True)
    b.add_argument("--output", required=True)
    b.add_argument("--base-url", default=DEFAULT_BASE_URL)
    b.add_argument("--provider-timeout", type=int, default=120)
    b.add_argument(
        "--sort-by-latency",
        action="store_true",
        default=False,
        help="Sort active models by probe latency (fastest becomes nclaude primary).",
    )
    b.set_defaults(func=build)
    m = sub.add_parser("merge-settings")
    m.add_argument("--input", required=True)
    m.add_argument("--output", required=True)
    m.add_argument("--port", type=int, default=4000)
    m.add_argument("--master-key", required=True)
    m.set_defaults(func=merge_settings)
    s = sub.add_parser("smoke")
    s.add_argument("--base-url", default="http://127.0.0.1:4000")
    s.add_argument("--max-tokens", type=int, default=1024)
    s.add_argument("--timeout", type=int, default=150)
    s.set_defaults(func=smoke)
    bm = sub.add_parser("benchmark")
    bm.add_argument("--registry", required=True)
    bm.add_argument("--output", required=True)
    bm.add_argument("--base-url", default=DEFAULT_BASE_URL)
    bm.add_argument("--max-tokens", type=int, default=512)
    bm.add_argument("--timeout", type=int, default=90)
    bm.add_argument("--interval", type=float, default=1.0)
    bm.set_defaults(func=benchmark)
    args = parser.parse_args()
    return args.func(args)

def benchmark(args: argparse.Namespace) -> int:
    """Run probe and report model latency ranking (fastest first)."""
    rc = probe(args)
    if rc != 0:
        return rc
    probe_data = json.loads(Path(args.output).read_text(encoding="utf-8"))
    results = probe_data.get("results", [])
    latencies = [
        (r.get("model_id"), r.get("latency_seconds"), r.get("state"))
        for r in results
        if r.get("latency_seconds") is not None
    ]
    latencies.sort(key=lambda x: x[1])
    print("\nModel latency ranking (fastest first):")
    print(f"{'Rank':<6} {'Latency (s)':<14} {'State':<14} {'Model ID'}")
    for rank, (model_id, latency, state) in enumerate(latencies, 1):
        print(f"{rank:<6} {latency:<14} {state:<14} {model_id}")
    fastest = latencies[0]
    print(f"\nFastest model: {fastest[0]} ({fastest[1]}s, state={fastest[2]})")
    return 0

if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
