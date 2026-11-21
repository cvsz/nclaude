# nclaude-installer

NVIDIA NIM model routing for Claude Code through a local LiteLLM Proxy, with an ordered fallback chain.

The maintained, distributable installer lives in `nclaude-installer/`. The root-level
`install`, `install.sh`, and `uninstall.sh` are thin delegates to
`nclaude-installer/install.sh` and `nclaude-installer/uninstall.sh`, so
`./install.sh` and `make install` perform the same operation.

## Architecture

`Claude Code -> LiteLLM Proxy (127.0.0.1:4000) -> NVIDIA NIM`

Claude Code uses the single model alias `nclaude`. LiteLLM is configured with the
NVIDIA NIM candidates that pass a real inference probe, in probe order: the first
passing candidate becomes `nclaude`; remaining passing candidates become sequential
fallbacks (`nclaude-fallback-02`, …).

## Installer behavior (nclaude-installer/)

- Probes NVIDIA NIM candidates against the real Chat Completions API **before**
  touching any installed files.
- Excludes `410 Gone` (EOL), `404`, authorization failures, rate-limited,
  network-failed, and `200` responses with no assistant text from the active chain.
- Backs up existing LiteLLM config, environment, systemd unit, and Claude Code
  settings before applying changes.
- Rolls back and restores the previous service state if the LiteLLM health check or
  the Claude Messages inference smoke test fails.
- Preserves an existing LiteLLM master key so Claude Code clients are not
  reconfigured on upgrades.
- Merges Claude Code settings instead of overwriting unrelated settings.
- Binds the proxy to `127.0.0.1` only; secrets are stored in a mode-0600
  systemd EnvironmentFile.

## Install

```bash
chmod +x nclaude-installer/install.sh
./nclaude-installer/install.sh
```

Equivalently, from the repository root:

```bash
./install.sh
```

or

```bash
make -C nclaude-installer install
```

The installer prompts for the NVIDIA API key if it is not already available as
`NVIDIA_NIM_API_KEY` or `NVIDIA_API_KEY`, then asks whether to probe all 14
candidates. Each probe makes a real inference request and may consume API quota.
Answer `Y` to build the full fallback chain from all passing candidates, or `n`
to probe only the first candidate.

Non-interactive:

```bash
NVIDIA_NIM_API_KEY='...' \
NCLAUDE_PROBE_MODE=all \
NCLAUDE_PROBE_MAX_TOKENS=512 \
./nclaude-installer/install.sh
```

- `NCLAUDE_PROBE_MODE=all|primary`
- `NCLAUDE_PROBE_MAX_TOKENS=512` (valid range: 128..4096)
- `NCLAUDE_PROVIDER_TIMEOUT=120`
- `NCLAUDE_PORT=4000` (loopback only)

See `nclaude-installer/README.md` for the full reference, including file paths,
validation commands, model-ID mapping, limitations, rollback, uninstall, and
security notes.

## Validation

```bash
make -C nclaude-installer lint
make -C nclaude-installer test
```

## Uninstall

```bash
./nclaude-installer/uninstall.sh
```

or, from the repository root, `./uninstall.sh`. This removes the service and
active nclaude configuration and removes only the Claude Code environment entries
owned by this installer. Backups and model probe history are retained.
