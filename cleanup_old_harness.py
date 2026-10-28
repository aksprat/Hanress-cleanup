#!/usr/bin/env python3
"""Delete DigitalOcean Harness Runtime sessions and agent configs (environments)
older than a configurable number of days.

Uses an authenticated `doctl` context. Order matters: sessions are removed first,
then configs (config delete is blocked while sessions from that config are active).

Safety:
  - Default mode is dry-run (no deletes).
  - Pass `--execute` to delete.
  - Each delete re-checks `created_at` against the age cutoff.
  - Use `--exclude-config NAME` to keep specific environments.

Usage:
  python3 cleanup_old_harness.py --dry-run
  python3 cleanup_old_harness.py --execute --days 30
  python3 cleanup_old_harness.py --execute --days 10 \\
      --exclude-config claude-self-serve-model-onboarding
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from dateutil import parser as dateparser
except ImportError:
    print("Install python-dateutil: pip install python-dateutil", file=sys.stderr)
    sys.exit(1)


def current_doctl_context() -> str:
    """Return the active doctl auth context name, or 'default'."""
    r = subprocess.run(
        ["doctl", "auth", "list", "--format", "Context,IsCurrent", "--no-header"],
        capture_output=True,
        text=True,
    )
    if r.returncode != 0:
        return "default"
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].lower() in {"true", "yes", "current"}:
            return parts[0]
        # text table: "name    true" or "name *"
        if "*" in line:
            return line.replace("*", "").split()[0]
    # Fallback: first context name
    for line in (r.stdout or "").splitlines():
        name = line.split()[0] if line.split() else ""
        if name:
            return name
    return "default"


def doctl(args: list[str], context: str) -> tuple[str, str, int]:
    cmd = ["doctl", *args, "--context", context]
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.stdout, r.stderr or "", r.returncode


def parse_json_with_page_token(stdout: str, stderr: str) -> tuple[list[Any], str | None]:
    combined_token = re.search(r"Next page token:\s*(\S+)", stdout) or re.search(
        r"Next page token:\s*(\S+)", stderr
    )
    next_token = combined_token.group(1) if combined_token else None
    body = stdout
    if re.search(r"Next page token:", body):
        body = re.split(r"Next page token:", body, 1)[0].strip()
    if not body.strip():
        return [], next_token
    data = json.loads(body)
    if isinstance(data, dict):
        for key in ("sessions", "configs", "items", "templates"):
            if key in data:
                data = data[key]
                break
        else:
            data = [data]
    return data, next_token


def list_paged(args: list[str], context: str, page_size: int = 50) -> list[Any]:
    items: list[Any] = []
    seen: set[str] = set()
    token = ""
    page = 0
    while True:
        page += 1
        cmd = [*args, "-o", "json", "--page-size", str(page_size)]
        if token:
            cmd += ["--page-token", token]
        out, err, code = doctl(cmd, context)
        if code != 0:
            raise RuntimeError(f"doctl {' '.join(cmd)} failed:\n{err or out}")
        batch, next_token = parse_json_with_page_token(out, err)
        for it in batch:
            key = it.get("session_id") or it.get("id") or json.dumps(it, sort_keys=True)
            if key not in seen:
                seen.add(key)
                items.append(it)
        print(f"  page {page}: +{len(batch)} total={len(items)} next={next_token}")
        if not next_token or not batch:
            break
        token = next_token
        if page > 500:
            print("  safety stop at 500 pages", file=sys.stderr)
            break
    return items


def parse_created(item: dict[str, Any]) -> datetime | None:
    raw = item.get("created_at")
    if not raw:
        return None
    dt = dateparser.parse(raw)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def older_than(items: list[dict[str, Any]], cutoff: datetime) -> list[dict[str, Any]]:
    out = []
    for it in items:
        created = parse_created(it)
        if created is not None and created < cutoff:
            out.append(it)
    return out


def assert_older_than(item: dict[str, Any], cutoff: datetime, label: str) -> None:
    """Refuse to act unless created_at is strictly before cutoff."""
    created = parse_created(item)
    if created is None:
        raise RuntimeError(f"Refusing {label}: missing created_at")
    if created >= cutoff:
        raise RuntimeError(
            f"Refusing {label}: created_at={created.isoformat()} is not older than "
            f"cutoff={cutoff.isoformat()}"
        )


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Delete resources with created_at older than this many days (default: 30)",
    )
    p.add_argument(
        "--context",
        default=None,
        help="doctl auth context name (default: currently active doctl context)",
    )
    p.add_argument(
        "--execute",
        action="store_true",
        help="Actually delete. Without this flag, only a dry-run is performed.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Only list what would be deleted (default behavior)",
    )
    p.add_argument(
        "--sessions-only",
        action="store_true",
        help="Only clean sessions (skip configs/environments)",
    )
    p.add_argument(
        "--configs-only",
        action="store_true",
        help="Only clean configs/environments (run after sessions if needed)",
    )
    p.add_argument(
        "--page-size",
        type=int,
        default=50,
        help="Pagination page size for list APIs (default: 50)",
    )
    p.add_argument(
        "--exclude-config",
        action="append",
        default=[],
        metavar="NAME",
        help="Config/environment name to never delete (repeatable). "
        "Does not protect sessions started from that config.",
    )
    args = p.parse_args()

    if shutil.which("doctl") is None:
        print("doctl not found on PATH. Install and authenticate doctl first.", file=sys.stderr)
        return 1

    execute = args.execute and not args.dry_run
    exclude_names = {n.strip() for n in args.exclude_config if n and n.strip()}
    context = args.context or current_doctl_context()

    cutoff = datetime.now(timezone.utc) - timedelta(days=args.days)
    print(f"Context: {context}")
    print(f"Cutoff (UTC): {cutoff.isoformat()} ({args.days} days)")
    print(f"Mode: {'EXECUTE' if execute else 'DRY-RUN'}")
    if exclude_names:
        print(f"Excluded configs (never delete): {sorted(exclude_names)}")

    do_sessions = not args.configs_only
    do_configs = not args.sessions_only

    old_sessions: list[dict[str, Any]] = []
    if do_sessions:
        print("\nListing sessions...")
        sessions = list_paged(
            ["harness-runtime", "list"], context, page_size=args.page_size
        )
        active = [
            s for s in sessions if s.get("status") != "SESSION_STATUS_DESTROYED"
        ]
        old_sessions = older_than(active, cutoff)
        print(f"Sessions: {len(active)} active, {len(old_sessions)} older than {args.days}d")
        for s in sorted(old_sessions, key=lambda x: x.get("created_at") or "")[:20]:
            print(
                f"  {s.get('created_at', '')[:19]}  {s.get('status')}  "
                f"{s.get('name')}  {s.get('session_id')}"
            )
        if len(old_sessions) > 20:
            print(f"  ... and {len(old_sessions) - 20} more")

        if execute:
            ok = fail = 0
            for s in old_sessions:
                sid = s["session_id"]
                name = s.get("name", "")
                try:
                    assert_older_than(s, cutoff, f"session {name} ({sid})")
                except RuntimeError as e:
                    fail += 1
                    print(f"  SKIPPED SAFETY: {e}", file=sys.stderr)
                    continue
                print(f"Removing session {name} ({sid}) created={s.get('created_at')}...")
                out, err, code = doctl(["harness-runtime", "remove", sid], context)
                if code == 0:
                    ok += 1
                else:
                    fail += 1
                    print(f"  FAILED: {err or out}", file=sys.stderr)
            print(f"Sessions removed: {ok}, failed: {fail}")

    old_configs: list[dict[str, Any]] = []
    if do_configs:
        print("\nListing configs...")
        configs = list_paged(
            ["harness-runtime", "config", "list"], context, page_size=args.page_size
        )
        old_configs = older_than(configs, cutoff)
        protected = [c for c in old_configs if c.get("name") in exclude_names]
        old_configs = [c for c in old_configs if c.get("name") not in exclude_names]
        print(
            f"Configs: {len(configs)} total, {len(old_configs)} older than {args.days}d "
            f"to delete, {len(protected)} protected by --exclude-config"
        )
        for c in protected:
            print(
                f"  PROTECTED  {c.get('created_at', '')[:19]}  {c.get('name')}  {c.get('id')}"
            )
        for c in sorted(old_configs, key=lambda x: x.get("created_at") or "")[:10]:
            print(f"  {c.get('created_at', '')[:19]}  {c.get('name')}  {c.get('id')}")
        if len(old_configs) > 10:
            print(f"  ... and {len(old_configs) - 10} more")

        if execute:
            ok = fail = skipped = 0
            for c in old_configs:
                cid = c["id"]
                name = c.get("name", "")
                if name in exclude_names:
                    print(f"  SKIPPED EXCLUDE: {name} ({cid})")
                    continue
                try:
                    assert_older_than(c, cutoff, f"config {name} ({cid})")
                except RuntimeError as e:
                    fail += 1
                    print(f"  SKIPPED SAFETY: {e}", file=sys.stderr)
                    continue
                print(f"Deleting config {name} ({cid}) created={c.get('created_at')}...")
                out, err, code = doctl(
                    ["harness-runtime", "config", "delete", cid], context
                )
                msg = (err or out).strip()
                if code == 0:
                    ok += 1
                elif "active" in msg.lower() or "session" in msg.lower():
                    skipped += 1
                    print(f"  SKIPPED (active sessions?): {msg}")
                else:
                    fail += 1
                    print(f"  FAILED: {msg}", file=sys.stderr)
            print(f"Configs deleted: {ok}, skipped: {skipped}, failed: {fail}")

    if not execute:
        print(
            f"\nDry-run complete. Would remove {len(old_sessions)} sessions "
            f"and {len(old_configs)} configs."
        )
        print("Re-run with --execute to delete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
