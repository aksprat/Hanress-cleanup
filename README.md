# Hanress-cleanup

Cleanup tool for **DigitalOcean Harness Runtime** (Managed Agents Runtime Services / M.A.R.S).

It deletes old **sessions** and **agent configs** (also called environments in the product UI) so unused sandboxes and manifests do not pile up on a team account.

## What it does

1. Lists Harness Runtime **sessions** via `doctl harness-runtime list`
2. Lists Harness Runtime **configs / environments** via `doctl harness-runtime config list`
3. Selects resources whose `created_at` is **strictly older** than a configurable age (default **30 days**)
4. **Dry-run by default** — prints what would be deleted
5. With `--execute`:
   - Removes matching **sessions** first (`doctl harness-runtime remove`)
   - Then deletes matching **configs** (`doctl harness-runtime config delete`)
6. Re-checks age immediately before each delete (safety guard)
7. Optionally **protects** named configs with `--exclude-config` (sessions from those configs are still age-filtered and can be deleted)

Configs that still have active (non-destroyed) sessions cannot be deleted by the API. Those are skipped with a message so you can retry after younger sessions age out or are removed separately.

## Prerequisites

- Python 3.9+
- [`doctl`](https://docs.digitalocean.com/reference/doctl/) installed and authenticated
- A doctl context with access to Harness Runtime (`doctl harness-runtime list` should work)
- `doctl` version that includes `harness-runtime` (recent beta/release builds)

```bash
doctl auth list
doctl auth switch --context <your-context>   # if needed
doctl harness-runtime list --page-size 5
```

## Install

```bash
git clone https://github.com/aksprat/Hanress-cleanup.git
cd Hanress-cleanup
pip install -r requirements.txt
```

## How to use

### 1. Always dry-run first

```bash
python3 cleanup_old_harness.py --dry-run
```

Or with a custom age:

```bash
python3 cleanup_old_harness.py --dry-run --days 10
```

### 2. Execute when the list looks correct

```bash
python3 cleanup_old_harness.py --execute --days 10
```

### 3. Protect a specific environment/config

Keep one named config forever (or until you remove the exclude), while still deleting old sessions — including sessions started from that config:

```bash
python3 cleanup_old_harness.py --execute --days 10 \
  --exclude-config claude-self-serve-model-onboarding
```

Repeat `--exclude-config` for multiple names:

```bash
python3 cleanup_old_harness.py --execute --days 30 \
  --exclude-config keep-me \
  --exclude-config also-keep-me
```

### 4. Sessions only or configs only

```bash
python3 cleanup_old_harness.py --execute --days 10 --sessions-only
python3 cleanup_old_harness.py --execute --days 10 --configs-only
```

Prefer sessions-first when cleaning both; that is the default full run order.

## Customization (flags / variables)

| Flag | Default | Meaning |
|------|---------|---------|
| `--days N` | `30` | Delete only if `created_at` is older than **N** days |
| `--context NAME` | Active doctl context | Which DigitalOcean / team auth context to use |
| `--execute` | off | Perform deletes. Without it, dry-run only |
| `--dry-run` | on (implicit) | List candidates; do not delete |
| `--exclude-config NAME` | none | Never delete this config/environment name (repeatable). Does **not** protect its sessions |
| `--sessions-only` | off | Skip config/environment cleanup |
| `--configs-only` | off | Skip session cleanup |
| `--page-size N` | `50` | Page size for list API pagination |

### Recommended workflow

```bash
# Preview
python3 cleanup_old_harness.py --dry-run --days 10 --context my-team

# Delete
python3 cleanup_old_harness.py --execute --days 10 --context my-team \
  --exclude-config important-env-name
```

## Terminology

| This repo / doctl | Product UI (approx.) |
|-------------------|----------------------|
| Session | Running/paused agent sandbox session |
| Config (`harness-runtime config`) | Environment / reusable agent definition |

## Safety notes

- **Dry-run is the default.** Nothing is deleted unless you pass `--execute`.
- Age is based on `created_at` (UTC), not last activity.
- Destroyed sessions are ignored.
- Excluded configs are never deleted by this tool; their old sessions still can be.
- Review dry-run output carefully on shared team accounts.

## License

Use freely within your organization. Adjust as needed for your team’s retention policy.
