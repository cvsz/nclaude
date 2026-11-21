# nclaude-installer

A local-only LiteLLM gateway that exposes NVIDIA NIM models through Claude Code's Anthropic-compatible Messages API.

## What changed in this release

- Normalizes the legacy 14-model list by removing exactly one leading `nvidia/` prefix before sending model IDs to NVIDIA.
- Probes candidates against the real NVIDIA Chat Completions API before touching the existing installation.
- Excludes `410 Gone` (EOL), `404`, authorization failures, rate-limited, network-failed, and inconclusive candidates from the active fallback chain.
- Keeps the original candidate order. The first model that passes a real text-generation probe becomes `nclaude`; remaining passing models become sequential fallbacks.
- Uses LiteLLM's documented `nvidia_nim/` provider prefix and ordered router fallbacks.
- Preserves the existing LiteLLM master key on upgrades.
- Backs up existing files and restores them if service health or Claude Messages inference validation fails.
- Merges Claude Code settings instead of overwriting unrelated settings.
- Binds the proxy to `127.0.0.1` only.
- `make install` is an explicit target; it will not trigger Make's implicit copy rule.

## Requirements

- Linux with a systemd user manager
- Python 3.10+
- `curl`
- `pipx`
- NVIDIA NIM API key
- `litellm[proxy]` (the installer installs it with pipx if absent)

## Install

```bash
unzip nclaude-installer.zip
cd nclaude-installer
make
make install
```

The installer prompts for the NVIDIA API key if it is not already available as `NVIDIA_NIM_API_KEY` or `NVIDIA_API_KEY`. It can reuse the key in the existing protected environment file without sourcing that file as shell code.

It then asks whether to probe all 14 candidates. Each probe makes a real inference request and may consume API quota. Answer `Y` to build the full fallback chain from all passing candidates, or `n` to probe only the first candidate.

### Non-interactive controls

```bash
NVIDIA_NIM_API_KEY='...' \
NCLAUDE_PROBE_MODE=all \
NCLAUDE_PROBE_MAX_TOKENS=512 \
make install
```

Do not place API keys directly in shell history in normal use. Prefer the interactive prompt or a protected secret manager.

- `NCLAUDE_PROBE_MODE=all|primary`
- `NCLAUDE_PROBE_MAX_TOKENS=512` (valid range: 128..4096)
- `NCLAUDE_PROVIDER_TIMEOUT=120`
- `NCLAUDE_PORT=4000` (loopback only; cannot bind to a public interface)

## Model ID mapping

The original candidate list used legacy IDs such as `nvidia/nvidia/<model>` and `nvidia/meta/<model>`. The NVIDIA API model ID is normalized by removing exactly one leading `nvidia/`:

| Legacy registry ID | NVIDIA API model ID | LiteLLM model |
|---|---|---|
| `nvidia/nvidia/nemotron-3.5-lightning-30b-a3b` | `nvidia/nemotron-3.5-lightning-30b-a3b` | `nvidia_nim/nvidia/nemotron-3.5-lightning-30b-a3b` |
| `nvidia/meta/llama-3.2-11b-vision-instruct` | `meta/llama-3.2-11b-vision-instruct` | `nvidia_nim/meta/llama-3.2-11b-vision-instruct` |
| `nvidia/z-ai/glm-5.3-flash` | `z-ai/glm-5.3-flash` | `nvidia_nim/z-ai/glm-5.3-flash` |

The first example is the model the user has independently verified with HTTP 200 and a non-empty `NIM_OK` response. The installer still probes it at install time instead of assuming availability.

## Validation and operation

```bash
make lint
make test
make status
make logs
```

Files:

- `~/.config/nclaude/litellm_config.yaml`
- `~/.config/nclaude/environment` (mode 0600)
- `~/.config/systemd/user/nclaude.service`
- `~/.local/state/nclaude/model-registry.json`
- `~/.local/state/nclaude/backups/`

The generated LiteLLM config is JSON, which is valid YAML. The registry report contains model IDs, probe statuses, and error summaries, but no credentials.

## Important behavior and limitations

- The model list is a candidate registry, not a claim that every model is currently available. NVIDIA may retire or restrict models at any time.
- Probe results are point-in-time. A model may become unavailable after installation; LiteLLM then walks the configured fallback chain for request errors.
- A `410` EOL response is excluded. A `200` response with no assistant text is marked `INCONCLUSIVE` and is not activated.
- Probes use up to the configured `max_tokens` for each model. This is real inference traffic and may consume quota.
- Fallbacks can only transparently switch before a stream has delivered output. A failure after partial streaming output cannot be made invisible to an already-connected client.
- Vision-only and other modality-specific models may not pass a text-only probe; they are deliberately excluded from this text-generation fallback chain.
- This is a local development gateway, not a public multi-tenant service. Do not expose port 4000 to the network.

## Rollback

The installer snapshots the existing LiteLLM config, environment, service unit, and Claude settings before applying changes. If the health check or Anthropic Messages inference test fails, it restores those files and restarts the previous service state.

Manual backup location is printed at the end of installation.

## Uninstall

```bash
make uninstall
```

This removes the service and active nclaude config, and removes only the Claude Code environment entries owned by this installer. Backups and model probe history are retained under `~/.local/state/nclaude/`.

## Security

- API credentials are stored in a mode-0600 systemd EnvironmentFile.
- Secrets are not embedded in the LiteLLM YAML/JSON config.
- The NVIDIA API key is not passed as a command-line argument.
- Proxy listens only on loopback.
- systemd uses `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict`, and `ProtectHome=read-only`.
