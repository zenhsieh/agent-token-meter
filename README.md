# Agent Token Meter

A local, read-only token meter for Claude Code and Codex JSONL sessions. It reports monthly usage by provider, estimates weighted token equivalents, and shows the latest context size for active sessions. It uses only the Python standard library.

## Install and run

Clone or copy this repository, then run it with Python 3.9 or newer. No package installation is needed.

```sh
python3 agent_token_meter.py --json
python3 agent_token_meter.py --cache --json
python3 agent_token_meter.py --context --month 2026-10
```

The first command scans all logs using UTC dates. The second incrementally reads complete new JSONL lines, writes `state.json` and `summary.json` under `${XDG_STATE_HOME:-~/.local/state}/agent-token-meter`, and uses local dates. The third reports starting context statistics by month. In cache output, `live[].ctx` is the context size from each session's latest API call; the default live window is three hours.

Use `--data-dir` and `--codex-data-dir` to change the log roots, `--state-dir` to change the cache location, and `--pricing-file` to load another JSON weight table. `--ratio write=1.25,read=0.1,out=5` overrides individual weights. The included weights produce *full-price equivalent tokens*, an estimate rather than a currency charge. Model-specific prices are not inferred.

Example output from invented sessions:

```text
month    provider  sessions  API calls  cache write  cache read  output  full-price eq.
2026-10  claude    1         2          200          800         50      780.0
2026-10  codex     1         1          0            800         20      380.0
```

## Known pitfalls

OpenAI's `input_tokens` already includes `cached_input_tokens`. The meter subtracts cached input from `input_tokens` before applying the normal input weight, then applies the cache-read weight to the cached portion. Counting both values as separate full input can inflate monthly usage by roughly four times for cache-heavy sessions. A reported context size still uses the full input count.

The cache stores recent Claude message IDs to deduplicate streaming rows. Rewriting or truncating old log files can leave historical aggregates in the cache; remove the cache directory to rebuild it. Full scans use UTC dates, while cache summaries use local dates.

## How it reads your logs

By default the tool only reads local `~/.claude/projects` and `~/.codex/sessions` JSONL files. It does not upload logs or make network requests. The cache writes aggregate state locally to the XDG state directory. JSON output may contain local project paths and session IDs, so review it before sharing.

## Test

```sh
python3 -m unittest discover -s tests
```
