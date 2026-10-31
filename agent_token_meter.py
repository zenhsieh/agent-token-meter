#!/usr/bin/env python3
"""Read-only token usage report from local Claude Code and Codex session logs."""

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import tempfile
import time


FIELDS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
DEFAULT_PRICING_FILE = Path(__file__).with_name("pricing.json")


def load_pricing(path):
    """Load nonnegative equivalent-token weights from a JSON pricing file."""
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data) != {"input", "cache_write", "cache_read", "output"}:
        raise ValueError("pricing must contain input, cache_write, cache_read, output")
    try:
        weights = {key: float(value) for key, value in data.items()}
    except (TypeError, ValueError) as exc:
        raise ValueError("pricing weights must be numbers") from exc
    if weights["input"] != 1 or any(not 0 <= value < float("inf") for value in weights.values()):
        raise ValueError("pricing weights must be finite and nonnegative; input must equal 1")
    return {"write": weights["cache_write"], "read": weights["cache_read"], "out": weights["output"]}


def parse_ratio(value):
    result = {}
    for part in value.split(","):
        key, sep, number = part.partition("=")
        if not sep or key not in ("write", "read", "out"):
            raise argparse.ArgumentTypeError("ratio must use write=,read=,out=")
        try:
            result[key] = float(number)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"invalid ratio: {part}") from exc
        if not 0 <= result[key] < float("inf"):
            raise argparse.ArgumentTypeError("ratios must be finite and nonnegative")
    return result


def date_arg(value):
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc
    return value


def percentile(values, percent):
    values = sorted(values)
    if not values:
        return None
    pos = (len(values) - 1) * percent
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (pos - lo)


def collect(root):
    """Return one usage event per (session, message.id), plus scan diagnostics."""
    events = []
    duplicates = conflicts = invalid = 0
    for path in sorted(root.glob("*/*.jsonl")):
        seen = {}
        try:
            lines = path.open(encoding="utf-8", errors="replace")
        except OSError:
            invalid += 1
            continue
        with lines:
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if row.get("type") != "assistant":
                    continue
                message = row.get("message") or {}
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                mid = message.get("id")
                stamp = row.get("timestamp")
                if not mid or not isinstance(stamp, str):
                    invalid += 1
                    continue
                try:
                    day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
                    counts = {key: int(usage.get(key) or 0) for key in FIELDS}
                except (ValueError, TypeError, OverflowError):
                    invalid += 1
                    continue
                if any(v < 0 for v in counts.values()):
                    invalid += 1
                    continue
                if mid in seen:
                    duplicates += 1
                    if seen[mid] != counts:
                        conflicts += 1
                    continue
                seen[mid] = counts
                events.append({"session": row.get("sessionId") or path.stem, "provider": "claude",
                               "project": path.parent.name, "day": day,
                               "timestamp": stamp, "month": day[:7], **counts})
    return events, {"duplicate_rows": duplicates, "conflicting_duplicates": conflicts, "invalid_rows": invalid}


def codex_usage(row):
    """Split Codex input into uncached input and its cached subset for pricing."""
    payload = row.get("payload") or {}
    if payload.get("type") != "token_count":
        return None
    usage = (payload.get("info") or {}).get("last_token_usage")
    if not isinstance(usage, dict) or not usage:
        return None
    try:
        input_tokens = int(usage.get("input_tokens") or 0)
        cached = int(usage.get("cached_input_tokens") or 0)
        counts = {"input_tokens": max(0, input_tokens - cached),
                  "cache_creation_input_tokens": int(usage.get("cache_write_input_tokens") or 0),
                  "cache_read_input_tokens": cached,
                  "output_tokens": int(usage.get("output_tokens") or 0)}
    except (TypeError, ValueError, OverflowError):
        return None
    if input_tokens < 0 or any(value < 0 for value in counts.values()):
        return None
    counts["context_tokens"] = input_tokens
    return counts


def codex_project(cwd):
    return str(cwd).replace("/", "-") if cwd else "codex"


def collect_codex(root):
    events = []
    invalid = 0
    for path in sorted(root.rglob("*.jsonl")):
        session, cwd = path.stem, ""
        try:
            lines = path.open(encoding="utf-8", errors="replace")
        except OSError:
            invalid += 1
            continue
        with lines:
            for line in lines:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                payload = row.get("payload") or {}
                if row.get("type") == "session_meta":
                    session = payload.get("id") or path.stem
                    cwd = payload.get("cwd") or ""
                    continue
                counts = codex_usage(row)
                if counts is None:
                    continue
                stamp = row.get("timestamp")
                try:
                    day = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone(timezone.utc).date().isoformat()
                except (AttributeError, ValueError, TypeError):
                    invalid += 1
                    continue
                events.append({"session": session, "provider": "codex", "project": codex_project(cwd),
                               "cwd": cwd, "day": day, "timestamp": stamp, "month": day[:7], **counts})
    return events, invalid


def summarize_providers(events, by, ratio, month=None, since=None):
    rows = []
    for provider in sorted({e.get("provider", "claude") for e in events}):
        rows.extend({"provider": provider, **row} for row in
                    summarize([e for e in events if e.get("provider", "claude") == provider],
                              by, ratio, month, since))
    return sorted(rows, key=lambda row: (row["key"], row["provider"]))


def equivalent(row, ratio):
    return (row["input_tokens"] + row["cache_creation_input_tokens"] * ratio["write"]
            + row["cache_read_input_tokens"] * ratio["read"]
            + row["output_tokens"] * ratio["out"])


def summarize(events, by, ratio, month=None, since=None):
    selected = [e for e in events if (not month or e["month"] == month) and (not since or e["day"] >= since)]
    groups = {}
    for e in selected:
        key = e["month"] if by == "month" else e[by]
        if key not in groups:
            groups[key] = {"key": key, "sessions": set(), "api_calls": 0, **{f: 0 for f in FIELDS}}
        g = groups[key]
        g["sessions"].add(e["session"])
        g["api_calls"] += 1
        for field in FIELDS:
            g[field] += e[field]
    output = []
    for key in sorted(groups):
        g = groups[key]
        g["sessions"] = len(g["sessions"])
        g["full_price_equivalent"] = round(equivalent(g, ratio), 2)
        output.append(g)
    return output


def context_months(events, month=None, since=None):
    first = {}
    for e in events:
        session = e["session"]
        if session not in first or e["timestamp"] < first[session]["timestamp"]:
            first[session] = e
    grouped = defaultdict(list)
    for e in first.values():
        context = (e.get("context_tokens", e["input_tokens"] + e["cache_read_input_tokens"]) if e.get("provider") == "codex"
                   else sum(e[f] for f in FIELDS[:3]))
        grouped[e["month"]].append(context)
    output = []
    previous = None
    for key in sorted(grouped):
        vals = grouped[key]
        median = percentile(vals, .5)
        if (not month or key == month) and (not since or key >= since[:7]):
            output.append({"month": key, "sessions": len(vals), "p25": percentile(vals, .25),
                           "median": median, "p75": percentile(vals, .75),
                           "median_change": None if previous is None else median - previous})
        previous = median
    return output


def render_table(rows, by):
    provider_column = any("provider" in row for row in rows)
    head = [by] + (["provider"] if provider_column else []) + ["sessions", "API calls", "cache write", "cache read", "output", "full-price eq."]
    data = [[str(r["key"])] + ([r["provider"]] if provider_column else []) + [str(r["sessions"]), str(r["api_calls"]),
             str(r["cache_creation_input_tokens"]), str(r["cache_read_input_tokens"]),
             str(r["output_tokens"]), f'{r["full_price_equivalent"]:,.1f}'] for r in rows]
    widths = [max(len(str(x[i])) for x in [head] + data) for i in range(len(head))]
    print("  ".join(x.ljust(widths[i]) for i, x in enumerate(head)))
    for row in data:
        print("  ".join(x.ljust(widths[i]) for i, x in enumerate(row)))


# The cache reads only complete new JSONL lines. Its daily buckets use local time;
# the full-scan report uses UTC, so their month boundaries can differ.
def default_state_dir():
    base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    return base / "agent-token-meter"


RECENT_IDS = 400
LIVE_WINDOW = 10800


def _local_day(stamp):
    dt = datetime.fromisoformat(stamp.replace("Z", "+00:00")).astimezone()
    return dt.date().isoformat()


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False)
    os.replace(tmp, path)


def update_cache(root, state):
    """把 root 底下 jsonl 的新增部分加進 state['agg']。agg key = day|project|session。"""
    files = state.setdefault("files", {})
    agg = state.setdefault("agg", {})
    first = state.setdefault("first", {})      # session → {ts, ctx}：起始 context
    last = state.setdefault("last", {})        # session → 最後一次呼叫的 context（「該不該 /clear」看這個）
    providers = state.setdefault("providers", {})
    for path in sorted(root.glob("*/*.jsonl")):
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        st = files.setdefault(key, {"offset": 0, "recent": []})
        if size < st["offset"]:          # 被截短／重寫：從頭來（事件無法回收，只能接受重複）
            st["offset"], st["recent"] = 0, []
        if size == st["offset"]:
            continue
        recent = list(st["recent"])
        rset = set(recent)
        try:
            with path.open("rb") as fh:
                fh.seek(st["offset"])
                chunk = fh.read(size - st["offset"])
        except OSError:
            continue
        cut = chunk.rfind(b"\n")
        if cut < 0:
            continue                      # 只有半行，下次再讀
        consumed = cut + 1
        for raw in chunk[:cut].split(b"\n"):
            if b'"usage"' not in raw:
                continue
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if row.get("type") != "assistant":
                continue
            message = row.get("message") or {}
            usage = message.get("usage")
            mid, stamp = message.get("id"), row.get("timestamp")
            if not isinstance(usage, dict) or not mid or not isinstance(stamp, str):
                continue
            if mid in rset:
                continue
            recent.append(mid); rset.add(mid)
            try:
                day = _local_day(stamp)
                counts = [int(usage.get(k) or 0) for k in FIELDS]
            except (ValueError, TypeError, OverflowError):
                continue
            session = row.get("sessionId") or path.stem
            providers[session] = "claude"
            k = f"{day}|{path.parent.name}|{session}"
            a = agg.setdefault(k, [0, 0, 0, 0, 0])      # calls, in, cw, cr, out
            a[0] += 1
            for i, v in enumerate(counts):
                a[i + 1] += v
            ctx = counts[0] + counts[1] + counts[2]
            f0 = first.get(session)
            if f0 is None or stamp < f0["ts"]:
                first[session] = {"ts": stamp, "ctx": ctx, "day": day}
            l0 = last.get(session)
            if l0 is None or stamp >= l0["ts"]:
                last[session] = {"ts": stamp, "ctx": ctx, "project": path.parent.name, "provider": "claude",
                                 "model": message.get("model") or "", "out": counts[3]}
        st["offset"] += consumed
        st["recent"] = recent[-RECENT_IDS:]
    return state


def update_codex_cache(root, state):
    """Append complete Codex token_count lines, retaining session metadata across tails."""
    files = state.setdefault("files", {})
    agg = state.setdefault("agg", {})
    first = state.setdefault("first", {})
    last = state.setdefault("last", {})
    providers = state.setdefault("providers", {})
    for path in sorted(root.rglob("*.jsonl")):
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        st = files.setdefault(key, {"offset": 0, "provider": "codex", "session": path.stem, "cwd": ""})
        if size < st["offset"]:
            st.update(offset=0, session=path.stem, cwd="")
        if size == st["offset"]:
            continue
        try:
            fh = path.open("rb")
            fh.seek(st["offset"])
        except OSError:
            continue
        with fh:
            while True:
                raw = fh.readline()
                if not raw or not raw.endswith(b"\n"):
                    break
                st["offset"] = fh.tell()
                if b'"session_meta"' not in raw and b'"token_count"' not in raw:
                    continue
                try:
                    row = json.loads(raw)
                except ValueError:
                    continue
                payload = row.get("payload") or {}
                if row.get("type") == "session_meta":
                    st["session"] = payload.get("id") or path.stem
                    st["cwd"] = payload.get("cwd") or ""
                    continue
                counts_map = codex_usage(row)
                stamp = row.get("timestamp")
                if counts_map is None or not isinstance(stamp, str):
                    continue
                try:
                    day = _local_day(stamp)
                except (ValueError, TypeError):
                    continue
                session = st["session"]
                project = codex_project(st["cwd"])
                providers[session] = "codex"
                counts = [counts_map[field] for field in FIELDS]
                k = f"{day}|{project}|{session}"
                a = agg.setdefault(k, [0, 0, 0, 0, 0])
                a[0] += 1
                for i, value in enumerate(counts):
                    a[i+1] += value
                ctx = counts_map["context_tokens"]
                f0 = first.get(session)
                if f0 is None or stamp < f0["ts"]:
                    first[session] = {"ts": stamp, "ctx": ctx, "day": day, "provider": "codex"}
                l0 = last.get(session)
                if l0 is None or stamp >= l0["ts"]:
                    last[session] = {"ts": stamp, "ctx": ctx, "project": project, "cwd": st["cwd"],
                                     "provider": "codex", "model": "", "out": counts_map["output_tokens"]}
    return state


def migrate_codex_accounting(state):
    """Discard only old Codex aggregates, then stream source logs on this run."""
    if state.get("codex_accounting_version", 0) >= 3:
        return state
    codex_sessions = {sid for sid, provider in state.get("providers", {}).items() if provider == "codex"}
    state["agg"] = {key: value for key, value in state.get("agg", {}).items()
                    if key.rsplit("|", 1)[-1] not in codex_sessions}
    for session in codex_sessions:
        state.get("first", {}).pop(session, None)
        state.get("last", {}).pop(session, None)
        state["providers"].pop(session, None)
    for filename, file_state in state.get("files", {}).items():
        if file_state.get("provider") == "codex":
            file_state.update(offset=0, session=Path(filename).stem, cwd="")
    state["codex_accounting_version"] = 3
    return state


def build_summary(state, ratio, now=None):
    now = now or time.time()
    today = time.strftime("%Y-%m-%d", time.localtime(now))
    month = today[:7]
    pm = (datetime.fromisoformat(today[:7] + "-01").replace(day=1) - __import__("datetime").timedelta(days=1)).strftime("%Y-%m")

    def blank():
        return {"sessions": set(), "api_calls": 0, **{f: 0 for f in FIELDS}}

    def add(g, a):
        g["api_calls"] += a[0]
        for i, f in enumerate(FIELDS):
            g[f] += a[i + 1]

    def close(g):
        g["sessions"] = len(g["sessions"])
        g["full_price_equivalent"] = round(equivalent(g, ratio))
        return g

    mon, prev, tod = blank(), blank(), blank()
    mon_by_provider = {}
    days = {}
    sess = {}
    for k, a in state.get("agg", {}).items():
        day, project, session = k.split("|", 2)
        if day[:7] == month:
            add(mon, a); mon["sessions"].add(session)
            provider = state.get("providers", {}).get(session, "claude")
            pg = mon_by_provider.setdefault(provider, blank())
            add(pg, a); pg["sessions"].add(session)
            s = sess.setdefault(session, {"project": project, "date": day, **blank()})
            add(s, a); s["date"] = min(s["date"], day)
        elif day[:7] == pm:
            add(prev, a); prev["sessions"].add(session)
        if day == today:
            add(tod, a); tod["sessions"].add(session)
        d = days.setdefault(day, blank()); add(d, a); d["sessions"].add(session)
    cutoff = time.strftime("%Y-%m-%d", time.localtime(now - 29 * 86400))
    series = []
    for d in sorted(days):
        if cutoff <= d <= today:
            g = close(days[d]); series.append({"day": d, "api_calls": g["api_calls"], "output_tokens": g["output_tokens"],
                                               "full_price_equivalent": g["full_price_equivalent"]})
    top = sorted((close(v) | {"session": k} for k, v in sess.items()), key=lambda r: -r["full_price_equivalent"])[:5]
    for t in top:
        t.pop("sessions", None)
    ctx = sorted(v["ctx"] for v in state.get("first", {}).values() if v["day"][:7] == month)
    # live ＝ 最近 LIVE_WINDOW 秒內有呼叫的 session，帶「目前 context 大小」（最後一次呼叫的
    # input+cache_write+cache_read ＝ 下一次呼叫要重讀的量）。這是「什麼時候該 /clear」的直接讀數。
    live = []
    for sid, v in state.get("last", {}).items():
        try:
            ts = datetime.fromisoformat(v["ts"].replace("Z", "+00:00")).timestamp()
        except ValueError:
            continue
        if now - ts <= LIVE_WINDOW:
            live.append({"session": sid, "project": v["project"], "ctx": v["ctx"], "model": v.get("model", ""),
                         "provider": v.get("provider", state.get("providers", {}).get(sid, "claude")),
                         "cwd": v.get("cwd", ""),
                         "idle_s": int(now - ts)})
    live.sort(key=lambda r: -r["ctx"])
    by_provider = {provider: close(group) for provider, group in mon_by_provider.items()}
    return {"updated": now, "live": live, "live_window_s": LIVE_WINDOW, "updated_iso": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
            "timezone": "local", "ratio": {"input": 1, **ratio},
            "month": {"key": month, **close(mon), "by_provider": by_provider},
            "month_by_provider": by_provider,
            "prev_month": {"key": pm, **close(prev)},
            "today": {"key": today, **close(tod)}, "days": series, "top_sessions": top,
            "start_context_month": {"n": len(ctx), "median": ctx[len(ctx) // 2] if ctx else None},
            "note": "Full-price equivalent is a weighted token estimate, not a bill."}


def run_cache(root, ratio, state_dir=None, codex_root=None):
    state_dir = state_dir or default_state_dir()
    state_file = state_dir / "state.json"
    summary_file = state_dir / "summary.json"
    try:
        state = json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    migrate_codex_accounting(state)
    update_cache(root, state)
    update_codex_cache(codex_root if codex_root is not None else Path.home() / ".codex/sessions", state)
    _atomic_write(state_file, state)
    summary = build_summary(state, ratio)
    _atomic_write(summary_file, summary)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--by", choices=("month", "project", "session", "day"), default="month")
    parser.add_argument("--month", help="YYYY-MM")
    parser.add_argument("--since", type=date_arg, help="YYYY-MM-DD (UTC)")
    parser.add_argument("--pricing-file", type=Path, default=DEFAULT_PRICING_FILE,
                        help="JSON file with equivalent-token weights")
    parser.add_argument("--ratio", type=parse_ratio,
                        help="override write=,read=,out= weights from pricing file")
    parser.add_argument("--context", action="store_true")
    parser.add_argument("--top", type=int, metavar="N")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=Path.home() / ".claude/projects",
                        help="Claude Code projects directory")
    parser.add_argument("--codex-data-dir", type=Path, default=Path.home() / ".codex/sessions",
                        help="Codex sessions directory")
    parser.add_argument("--state-dir", type=Path, default=default_state_dir(),
                        help="cache directory (default: XDG_STATE_HOME/agent-token-meter)")
    parser.add_argument("--cache", action="store_true",
                        help="incrementally update state.json and summary.json using local time")
    args = parser.parse_args(argv)
    try:
        ratio = load_pricing(args.pricing_file)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(f"cannot load pricing: {exc}")
    if args.ratio:
        ratio.update(args.ratio)
    if args.cache:
        summary = run_cache(args.data_dir, ratio, state_dir=args.state_dir, codex_root=args.codex_data_dir)
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            m, t = summary["month"], summary["today"]
            print(f'{m["key"]}  sessions {m["sessions"]}  calls {m["api_calls"]:,}  read {m["cache_read_input_tokens"]:,}  '
                  f'out {m["output_tokens"]:,}  eq {m["full_price_equivalent"]:,}')
            print(f'today {t["key"]}  calls {t["api_calls"]:,}  eq {t["full_price_equivalent"]:,}   (updated {summary["updated_iso"]})')
            for r in summary["live"]:
                print(f'live  {r["ctx"]:>9,} ctx  {r["idle_s"]:>5}s idle  {r["project"]}  {r["session"][:8]}  {r["model"]}')
        return 0
    if args.month and not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", args.month):
        parser.error("--month must be YYYY-MM")
    if args.top is not None and args.top < 1:
        parser.error("--top must be positive")
    events, diagnostics = collect(args.data_dir)
    codex_events, codex_invalid = collect_codex(args.codex_data_dir)
    events.extend(codex_events)
    diagnostics["invalid_rows"] += codex_invalid
    by = args.by
    rows = summarize_providers(events, by, ratio, args.month, args.since) if by == "month" else summarize(events, by, ratio, args.month, args.since)
    selected = [e for e in events if (not args.month or e["month"] == args.month) and
                (not args.since or e["day"] >= args.since)]
    top = []
    if args.top:
        session_meta = {}
        for event in selected:
            meta = session_meta.setdefault(event["session"], {"project": event["project"], "date": event["day"]})
            meta["date"] = min(meta["date"], event["day"])
        for row in summarize(selected, "session", ratio):
            meta = session_meta[row["key"]]
            top.append({"session": row["key"], "project": meta["project"],
                        "date": meta["date"], "api_calls": row["api_calls"],
                        "full_price_equivalent": row["full_price_equivalent"]})
        top.sort(key=lambda r: r["full_price_equivalent"], reverse=True)
        top = top[:args.top]
    context = context_months(events, args.month, args.since) if args.context else None
    result = {"group_by": by, "timezone": "UTC", "ratio": {"input": 1, **ratio},
              "rows": rows, "top_sessions": top, "context": context,
              "diagnostics": diagnostics,
              "note": "Full-price equivalent is a weighted token estimate, not a bill."}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    render_table(rows, by)
    if args.top:
        print("\nTop sessions")
        for r in top:
            print(f'{r["session"]}  {r["project"]}  {r["date"]}  {r["api_calls"]} calls  {r["full_price_equivalent"]:,.1f} eq.')
    if context is not None:
        print("\nStarting context by month (first call per session; UTC)")
        for r in context:
            trend = "—" if r["median_change"] is None else f'{r["median_change"]:+,.0f}'
            print(f'{r["month"]}  n={r["sessions"]}  p25={r["p25"]:,.0f}  median={r["median"]:,.0f}  p75={r["p75"]:,.0f}  Δmedian={trend}')
    print("Full-price equivalent assumes input×1, write×{write:g}, read×{read:g}, output×{out:g}; estimate, not a bill.".format(**ratio))
    if diagnostics["conflicting_duplicates"]:
        print(f'WARNING: {diagnostics["conflicting_duplicates"]} duplicate IDs had conflicting usage; first occurrence used.')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
