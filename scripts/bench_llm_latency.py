#!/usr/bin/env python3
"""
Compare latency: Vercel (via agentgateway /v1) vs Cursor bridge (via /cursor).

Metrics per request:
  - total_ms: wall clock end-to-end
  - ttft_ms: time to first SSE content token (stream=true); null if non-stream
  - completion_tokens: from usage if present, else ~len(content)/4
  - tok_per_s: completion_tokens / (total_ms/1000)

Run inside the cluster (or from a host that can reach the gateway LB).

Example:
  python bench_llm_latency.py \\
    --gateway http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8090 \\
    --runs 3 --delay 25
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class RunResult:
    name: str
    ok: bool
    total_ms: float
    ttft_ms: float | None
    completion_tokens: int
    prompt_tokens: int
    tok_per_s: float
    chars: int
    error: str = ""
    preview: str = ""
    content: str = ""


PROMPTS = {
    "short": "Reply with exactly one sentence about Kubernetes.",
    "medium": (
        "In about 120 words, explain what an AI agent harness is on Kubernetes. "
        "Be concrete, no bullet lists."
    ),
    # Judgeable ops problem: latency + answer quality in one shot.
    "problem": (
        "You are debugging a KAIF cluster. Clients call agentgateway at "
        "POST /v1/chat/completions. After a few successes they get HTTP 429. "
        "An AgentgatewayPolicy applies a rate limit of 3 requests per minute "
        "with burst 1 on path prefix /v1.\n\n"
        "Respond with EXACTLY these four labeled sections and nothing else:\n"
        "ROOT_CAUSE: <one sentence>\n"
        "CONFIRM: <one kubectl or curl command>\n"
        "FIX: <smallest durable fix in one sentence or a tiny YAML snippet>\n"
        "WORKAROUND: <one sentence for a load test that must stay under the limit>\n"
    ),
}


def estimate_tokens(text: str) -> int:
    # Rough OpenAI-ish heuristic when upstream usage is missing/zero.
    return max(1, len(text) // 4) if text else 0


def post_json(url: str, body: dict[str, Any], timeout: float) -> tuple[int, dict[str, str], bytes]:
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            headers = {k.lower(): v for k, v in resp.headers.items()}
            return resp.status, headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, e.read()


def run_nonstream(name: str, url: str, model: str, prompt: str, timeout: float) -> RunResult:
    body = {
        "model": model,
        "stream": False,
        "messages": [{"role": "user", "content": prompt}],
    }
    t0 = time.perf_counter()
    try:
        status, _, raw = post_json(url, body, timeout)
        total_ms = (time.perf_counter() - t0) * 1000
        if status != 200:
            return RunResult(
                name=name,
                ok=False,
                total_ms=total_ms,
                ttft_ms=None,
                completion_tokens=0,
                prompt_tokens=0,
                tok_per_s=0.0,
                chars=0,
                error=f"HTTP {status}: {raw[:300]!r}",
            )
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        content = (
            ((payload.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        )
        usage = payload.get("usage") or {}
        ctok = int(usage.get("completion_tokens") or 0)
        ptok = int(usage.get("prompt_tokens") or 0)
        if ctok <= 0:
            ctok = estimate_tokens(content)
        tok_per_s = ctok / (total_ms / 1000.0) if total_ms > 0 else 0.0
        return RunResult(
            name=name,
            ok=True,
            total_ms=round(total_ms, 1),
            ttft_ms=None,
            completion_tokens=ctok,
            prompt_tokens=ptok,
            tok_per_s=round(tok_per_s, 2),
            chars=len(content),
            preview=content[:120].replace("\n", " "),
            content=content,
        )
    except Exception as e:
        total_ms = (time.perf_counter() - t0) * 1000
        return RunResult(
            name=name,
            ok=False,
            total_ms=round(total_ms, 1),
            ttft_ms=None,
            completion_tokens=0,
            prompt_tokens=0,
            tok_per_s=0.0,
            chars=0,
            error=f"{type(e).__name__}: {e}",
        )


def run_stream(name: str, url: str, model: str, prompt: str, timeout: float) -> RunResult:
    """SSE stream; measure TTFT on first non-empty content delta."""
    body = {
        "model": model,
        "stream": True,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    t0 = time.perf_counter()
    ttft_ms: float | None = None
    chunks: list[str] = []
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200:
                raw = resp.read()[:300]
                total_ms = (time.perf_counter() - t0) * 1000
                return RunResult(
                    name=name,
                    ok=False,
                    total_ms=round(total_ms, 1),
                    ttft_ms=None,
                    completion_tokens=0,
                    prompt_tokens=0,
                    tok_per_s=0.0,
                    chars=0,
                    error=f"HTTP {resp.status}: {raw!r}",
                )
            while True:
                line = resp.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text.startswith("data:"):
                    continue
                payload = text[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {})
                piece = delta.get("content") or ""
                if piece:
                    if ttft_ms is None:
                        ttft_ms = (time.perf_counter() - t0) * 1000
                    chunks.append(piece)
        total_ms = (time.perf_counter() - t0) * 1000
        content = "".join(chunks)
        ctok = estimate_tokens(content)
        tok_per_s = ctok / (total_ms / 1000.0) if total_ms > 0 else 0.0
        return RunResult(
            name=name,
            ok=True,
            total_ms=round(total_ms, 1),
            ttft_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
            completion_tokens=ctok,
            prompt_tokens=0,
            tok_per_s=round(tok_per_s, 2),
            chars=len(content),
            preview=content[:120].replace("\n", " "),
            content=content,
        )
    except Exception as e:
        total_ms = (time.perf_counter() - t0) * 1000
        return RunResult(
            name=name,
            ok=False,
            total_ms=round(total_ms, 1),
            ttft_ms=round(ttft_ms, 1) if ttft_ms is not None else None,
            completion_tokens=0,
            prompt_tokens=0,
            tok_per_s=0.0,
            chars=0,
            error=f"{type(e).__name__}: {e}",
        )


def score_problem(content: str) -> dict[str, Any]:
    """Lightweight rubric for the 'problem' prompt (structure + keyword hints)."""
    text = content or ""
    upper = text.upper()
    sections = ["ROOT_CAUSE", "CONFIRM", "FIX", "WORKAROUND"]
    present = [s for s in sections if s in upper]
    hints = {
        "mentions_rate_limit": any(
            k in text.lower() for k in ("rate limit", "ratelimit", "429", "3 request", "burst")
        ),
        "confirm_looks_like_cmd": any(
            k in text.lower() for k in ("kubectl", "curl", "httproute", "agentgateway")
        ),
        "fix_mentions_policy": any(
            k in text.lower() for k in ("policy", "requestsper", "limit", "quota", "raise", "increase")
        ),
        "workaround_mentions_spacing": any(
            k in text.lower() for k in ("delay", "sleep", "space", "throttle", "under", "wait", "25", "20")
        ),
    }
    structure = len(present) / len(sections)
    hint_score = sum(1 for v in hints.values() if v) / len(hints)
    return {
        "sections_present": present,
        "structure_score": round(structure, 2),
        "hint_score": round(hint_score, 2),
        "quality_score": round(0.6 * structure + 0.4 * hint_score, 2),
        "hints": hints,
    }


def summarize(label: str, results: list[RunResult]) -> dict[str, Any]:
    ok = [r for r in results if r.ok]
    if not ok:
        return {"label": label, "n_ok": 0, "n_fail": len(results), "error": "all failed"}
    totals = [r.total_ms for r in ok]
    tps = [r.tok_per_s for r in ok]
    ttfts = [r.ttft_ms for r in ok if r.ttft_ms is not None]
    out: dict[str, Any] = {
        "label": label,
        "n_ok": len(ok),
        "n_fail": len(results) - len(ok),
        "total_ms_mean": round(statistics.mean(totals), 1),
        "total_ms_p50": round(statistics.median(totals), 1),
        "total_ms_min": round(min(totals), 1),
        "total_ms_max": round(max(totals), 1),
        "tok_per_s_mean": round(statistics.mean(tps), 2),
        "tok_per_s_p50": round(statistics.median(tps), 2),
        "completion_tokens_mean": round(statistics.mean(r.completion_tokens for r in ok), 1),
    }
    if ttfts:
        out["ttft_ms_mean"] = round(statistics.mean(ttfts), 1)
        out["ttft_ms_p50"] = round(statistics.median(ttfts), 1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--gateway",
        default="http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8090",
    )
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument(
        "--delay",
        type=float,
        default=25.0,
        help="Seconds between requests (avoid agentgateway rate limit on /v1)",
    )
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--prompt", choices=sorted(PROMPTS), default="short")
    ap.add_argument("--stream", action="store_true", help="Also measure TTFT via SSE")
    ap.add_argument("--out", default="")
    ap.add_argument(
        "--save-answers",
        action="store_true",
        help="Keep full answer text in the JSON report (needed for quality compare)",
    )
    args = ap.parse_args()

    prompt = PROMPTS[args.prompt]
    targets = [
        ("vercel-/v1", f"{args.gateway.rstrip('/')}/v1/chat/completions", "openai/gpt-5-mini"),
        ("cursor-/cursor", f"{args.gateway.rstrip('/')}/cursor/chat/completions", "auto"),
    ]

    all_results: dict[str, list[RunResult]] = {t[0]: [] for t in targets}
    print(f"gateway={args.gateway} runs={args.runs} prompt={args.prompt!r} stream={args.stream}")
    print(f"prompt_text={prompt!r}\n")

    for i in range(args.runs):
        print(f"=== run {i + 1}/{args.runs} ===")
        for name, url, model in targets:
            fn = run_stream if args.stream else run_nonstream
            # Interleave with delay for vercel rate limit; still delay between all.
            r = fn(name, url, model, prompt, args.timeout)
            all_results[name].append(r)
            status = "OK" if r.ok else "FAIL"
            ttft = f" ttft={r.ttft_ms}ms" if r.ttft_ms is not None else ""
            print(
                f"  [{status}] {name}: total={r.total_ms}ms{ttft} "
                f"tok≈{r.completion_tokens} tps={r.tok_per_s} "
                f"preview={r.preview!r} err={r.error!r}"
            )
            if args.prompt == "problem" and r.ok and r.content:
                q = score_problem(r.content)
                print(f"       quality={q['quality_score']} sections={q['sections_present']}")
                print("--- answer begin ---")
                print(r.content.rstrip())
                print("--- answer end ---")
            if i < args.runs - 1 or name != targets[-1][0]:
                time.sleep(args.delay)

    print("\n=== summary ===")
    summaries = [summarize(k, v) for k, v in all_results.items()]
    for s in summaries:
        print(json.dumps(s, indent=2))

    quality: dict[str, Any] = {}
    if args.prompt == "problem":
        print("\n=== quality (problem rubric) ===")
        for label, runs in all_results.items():
            scored = []
            for r in runs:
                if r.ok:
                    s = score_problem(r.content)
                    s["total_ms"] = r.total_ms
                    scored.append(s)
            if scored:
                quality[label] = {
                    "quality_score_mean": round(
                        statistics.mean(x["quality_score"] for x in scored), 2
                    ),
                    "structure_score_mean": round(
                        statistics.mean(x["structure_score"] for x in scored), 2
                    ),
                    "runs": scored,
                }
                print(json.dumps({"label": label, **quality[label]}, indent=2))

    serialized: dict[str, list[dict[str, Any]]] = {}
    for k, runs in all_results.items():
        rows = []
        for r in runs:
            row = asdict(r)
            if not args.save_answers:
                row.pop("content", None)
            rows.append(row)
        serialized[k] = rows

    report = {
        "gateway": args.gateway,
        "runs": args.runs,
        "prompt": args.prompt,
        "prompt_text": prompt,
        "stream": args.stream,
        "delay_s": args.delay,
        "results": serialized,
        "summary": summaries,
        "quality": quality,
        "notes": [
            "Cursor bridge often reports usage tokens as 0; tok_per_s may use len(content)/4 estimate.",
            "Vercel /v1 may be rate-limited (e.g. 3/min) by agentgateway policy — delay between runs.",
            "Paths are not identical models: gpt-5-mini vs Cursor auto/agent harness — compare carefully.",
            "Cursor path includes CLI + agent harness overhead on top of model latency.",
            "problem prompt quality_score is a lightweight structure+keyword rubric, not human eval.",
        ],
    }
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
