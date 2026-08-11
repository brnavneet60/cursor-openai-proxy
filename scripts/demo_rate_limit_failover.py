#!/usr/bin/env python3
"""
Showcase: keep agentgateway rate limit on Vercel (/v1); survive with Cursor (/cursor).

Thesis
------
UC-6 keeps 3 req/min + burst 1 on vercel HTTPRoutes only.
Cursor route (/cursor) is intentionally NOT on that policy.
When /v1 returns 429, failover to /cursor continues the PoC without buying
Vercel AI Gateway credits (uses existing Cursor Pro usage via the bridge).

Example:
  python scripts/demo_rate_limit_failover.py \\
    --gateway http://<AGENTGATEWAY_LB>:8090 \\
    --out evidence/demo-rate-limit-failover.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Attempt:
    path: str
    status: int
    total_ms: float
    ok: bool
    preview: str = ""
    error: str = ""


def post_chat(url: str, model: str, prompt: str, timeout: float) -> Attempt:
    body = json.dumps(
        {
            "model": model,
            "stream": False,
            "messages": [{"role": "user", "content": prompt}],
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            ms = (time.perf_counter() - t0) * 1000
            payload = json.loads(raw.decode("utf-8", errors="replace"))
            text = (
                ((payload.get("choices") or [{}])[0].get("message") or {}).get("content")
                or ""
            )
            return Attempt(
                path=url,
                status=int(resp.status),
                total_ms=round(ms, 1),
                ok=True,
                preview=text[:120].replace("\n", " "),
            )
    except urllib.error.HTTPError as e:
        ms = (time.perf_counter() - t0) * 1000
        err = e.read()[:300].decode("utf-8", errors="replace")
        return Attempt(
            path=url,
            status=int(e.code),
            total_ms=round(ms, 1),
            ok=False,
            error=err,
        )
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return Attempt(
            path=url,
            status=0,
            total_ms=round(ms, 1),
            ok=False,
            error=f"{type(e).__name__}: {e}",
        )


def chat_with_failover(
    gateway: str,
    prompt: str,
    timeout: float,
) -> dict[str, Any]:
    """App-level pattern: primary Vercel/gateway, fallback Cursor on 429."""
    primary = f"{gateway.rstrip('/')}/v1/chat/completions"
    fallback = f"{gateway.rstrip('/')}/cursor/chat/completions"
    first = post_chat(primary, "openai/gpt-5-mini", prompt, timeout)
    if first.ok:
        return {"used": "vercel-/v1", "attempts": [asdict(first)]}
    if first.status == 429:
        second = post_chat(fallback, "auto", prompt, timeout)
        return {
            "used": "cursor-/cursor" if second.ok else "failed",
            "failover_reason": "HTTP 429 on /v1 (gateway and/or Vercel free tier)",
            "attempts": [asdict(first), asdict(second)],
        }
    return {"used": "failed", "attempts": [asdict(first)]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--gateway",
        default="http://agentgateway-proxy.agentgateway-system.svc.cluster.local:8090",
    )
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument(
        "--burst",
        type=int,
        default=6,
        help="Rapid /v1 calls to trip UC-6 (3/min burst 1)",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    gw = args.gateway.rstrip("/")
    vercel_url = f"{gw}/v1/chat/completions"
    cursor_url = f"{gw}/cursor/chat/completions"
    prompt = "Reply with exactly one word: ok"

    print("=== thesis ===")
    print("Keep UC-6 rate limit on Vercel routes (/v1, /llm).")
    print("Cursor route (/cursor) is outside that policy.")
    print("On 429 → failover to Cursor bridge (no Vercel paid credits).\n")

    print(f"=== phase 1: hammer /v1 ({args.burst} rapid calls) ===")
    vercel_attempts: list[Attempt] = []
    saw_429 = False
    for i in range(args.burst):
        r = post_chat(vercel_url, "openai/gpt-5-mini", prompt, args.timeout)
        vercel_attempts.append(r)
        mark = "OK" if r.ok else f"FAIL:{r.status}"
        print(f"  [{mark}] /v1 #{i + 1}: {r.total_ms}ms preview={r.preview!r} err={r.error[:80]!r}")
        if r.status == 429:
            saw_429 = True
            break
        # no sleep — trip local rate limit

    print("\n=== phase 2: Cursor /cursor immediately after ===")
    cursor = post_chat(cursor_url, "auto", prompt, args.timeout)
    print(
        f"  [{'OK' if cursor.ok else 'FAIL:' + str(cursor.status)}] /cursor: "
        f"{cursor.total_ms}ms preview={cursor.preview!r} err={cursor.error[:80]!r}"
    )

    print("\n=== phase 3: failover helper (one call) ===")
    # May still be in the rate-limit window — expect failover path.
    fo = chat_with_failover(gw, "Say hello in three words.", args.timeout)
    print(json.dumps(fo, indent=2))

    report: dict[str, Any] = {
        "thesis": (
            "Agentgateway UC-6 rate-limits Vercel /v1; Cursor /cursor stays available. "
            "Failover proves LLM PoCs continue without buying Vercel credits."
        ),
        "policy": {
            "name": "uc6-llm-rate-limit",
            "targets": ["vercel-llm-v1", "vercel-llm"],
            "not_targeted": ["cursor-llm"],
            "limit": "3 requests / minute, burst 1",
        },
        "saw_429_on_v1": saw_429,
        "cursor_ok_after_429": bool(cursor.ok),
        "vercel_attempts": [asdict(x) for x in vercel_attempts],
        "cursor_attempt": asdict(cursor),
        "failover_demo": fo,
        "pass": bool(saw_429 and cursor.ok),
    }

    print("\n=== verdict ===")
    if report["pass"]:
        print("PASS: /v1 hit 429 under UC-6; /cursor still succeeded (no extra Vercel $).")
    elif not saw_429:
        print("INCONCLUSIVE: no 429 yet — increase --burst or wait and retry (limit may have reset).")
        report["pass"] = False
    else:
        print("FAIL: saw 429 on /v1 but Cursor path did not succeed.")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nwrote {args.out}")

    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
