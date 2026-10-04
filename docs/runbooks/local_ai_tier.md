# Runbook: local AI tier (Ollama on the host)

The gateway sends `triage`, `summarize` and `embed` to Ollama on the workstation GPU; everything else
goes to Gemini. Design: [docs/ai-first-program.md](../ai-first-program.md) and [ADR 0005](../adr/0005-local-tier-and-learning.md).

## Setup (needs sudo, done once)

- Ollama runs as the systemd service `ollama` from `/usr/local/bin/ollama` (one install only; the snap was
  removed).
- Drop-in `/etc/systemd/system/ollama.service.d/cryptonite.conf`:

  ```ini
  [Unit]
  After=docker.service
  [Service]
  Environment=OLLAMA_HOST=172.17.0.1:11434
  Environment=OLLAMA_KEEP_ALIVE=-1
  Environment=OLLAMA_NUM_PARALLEL=2
  Environment=OLLAMA_CONTEXT_LENGTH=8192
  ```

  `172.17.0.1` is the docker bridge. **Never** bind Ollama to `0.0.0.0`, the LAN address or a public
  interface (invariant 8). Tailscale is the only allowed remote path.
- Compose gives `api`, `agent_worker`, `debate_worker`, `learner` and `research_worker`
  `OLLAMA_BASE_URL=http://host.docker.internal:11434/v1` and `extra_hosts: host.docker.internal:host-gateway`.

## Everyday commands

The host CLI must be pointed at the bridge address:

```bash
export OLLAMA_HOST=172.17.0.1:11434
ollama list        # pulled models
ollama ps          # loaded models and VRAM
systemctl status ollama
```

Models in use: `gemma4:12b-it-qat` (triage, summarize; about 7.2 GB) and `embeddinggemma` (768-d
embeddings). `qwen3.5:9b` is kept as the runner-up from the bake-off.

## Health and fallback

- The gateway probes `/api/tags` and publishes `local_tier:health` in Redis; `/api/v1/ai/status` shows
  `local_tier` and the web shows a banner when it is down.
- `triage` and `summarize` are `DEGRADE` routes: when Ollama is down or the model is not pulled, calls go
  straight to Gemini Flash-Lite / Flash (no timeout stall), and back to local on the next call after
  recovery.
- `embed` has **no** fallback: vectors from different models are not comparable, so embedding fails fast
  and reflections wait until Ollama is back.
- Drill: `scripts/local_tier_drill.py` (healthy → `systemctl stop ollama` → `systemctl start ollama`).

## Changing a route

Routes are rows in `ai_model_routes` (they override `routes.py`):

```sql
UPDATE ai_model_routes SET primary_model = 'ollama:gemma4:12b-it-qat',
       fallback_model = 'google:gemini-2.5-flash-lite' WHERE role = 'triage';
```

Workers pick the change up on their next route refresh. Trade-decision roles (`debate.verdict`,
`debate.cro`, `exit.verdict`) are protected and cannot be rerouted this way.

**Changing the `embed` model** must re-embed every reflection in the same transaction:
`scripts/reembed_reflections.py` (evaluate first with `scripts/eval_embeddings.py`).

## GPU time for training

Training (A7.4, `~/cryptonite-train`) needs the GPU that Ollama uses. Before training: route `triage` and
`summarize` to Gemini (`fallback_model` NULL is fine for the duration) and unload the models
(`ollama stop gemma4:12b-it-qat`). Afterwards restore the routes above and check that one triage call is
answered locally (`ai_decision_traces.model`).

## Upgrading Ollama

The service runs 0.33.2. The v0.35.1 upgrade is paused; resume steps are in
`~/Downloads/ollama-v0.35.1/README-resume.txt`. After an upgrade, check that the drop-in still applies
(`systemctl cat ollama`) and run the drill.
