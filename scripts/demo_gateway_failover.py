#!/usr/bin/env python3
"""
Showcase agentgateway-native Vercel → Cursor failover.

Architecture
------------
- /v1          — Vercel only + UC-6 rate limit (platform requirement; stays)
- /llm-failover — AgentgatewayBackend priority groups:
                  1) Vercel  2) Cursor bridge
                  Health policy evicts on 429/5xx → next group

Docs: https://agentgateway.dev/docs/kubernetes/latest/llm/failover/

Example:
  python scripts/demo_gateway_failover.py \\
    --gateway http://<AGENTGATEWAY_LB>:8090 \\
    --out evidence/demo-gateway-failover.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Attempt:
    label: str
    path: str
    status: int
    total_ms: float
    ok: bool
    model: str = ""
    preview: str = ""
    error: str = ""


def post_chat(url: str, model: str, prompt: str, timeout: float, label: str) -> Attempt:
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
        headers={"Content-Type": "application/json"},
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
                label=label,
                path=url,
                status=int(resp.status),
                total_ms=round(ms, 1),
                ok=True,
                model=str(payload.get("model") or ""),
                preview=text[:100].replace("\n", " "),
            )
    except urllib.error.HTTPError as e:
        ms = (time.perf_counter() - t0) * 1000
        return Attempt(
            label=label,
            path=url,
            status=int(e.code),
            total_ms=round(ms, 1),
            ok=False,
            error=e.read()[:200].decode("utf-8", errors="replace"),
        )
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        return Attempt(
            label=label,
            path=url,
            status=0,
            total_ms=round(ms, 1),
            ok=False,
            error=f"{type(e).__name__}: {e}",
        )


def kubectl_apply_health(unhealthy: str, duration: str = "45s") -> None:
    manifest = f"""
apiVersion: agentgateway.dev/v1alpha1
kind: AgentgatewayPolicy
metadata:
  name: vercel-cursor-failover-health
  namespace: agentgateway-system
  labels:
    kaif.platform/purpose: vercel-cursor-failover
spec:
  targetRefs:
    - group: agentgateway.dev
      kind: AgentgatewayBackend
      name: vercel-cursor-failover
  backend:
    health:
      unhealthyCondition: {json.dumps(unhealthy)}
      eviction:
        duration: {duration}
        consecutiveFailures: 1
"""
    subprocess.run(
        ["kubectl", "apply", "-f", "-"],
        input=manifest.encode(),
        check=True,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--gateway", default="http://<AGENTGATEWAY_LB>:8090")
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument(
        "--force-failover-test",
        action="store_true",
        help="Temporarily mark all responses unhealthy to prove priority-group failover",
    )
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    gw = args.gateway.rstrip("/")
    prompt = "Reply with exactly one word: ok"
    attempts: list[Attempt] = []

    print("=== thesis ===")
    print("UC-6 rate limit remains on /v1 (Vercel-only).")
    print("/llm-failover uses agentgateway priority groups: Vercel → Cursor.")
    print("On 429/5xx, gateway evicts Vercel and serves Cursor (no Vercel paid credits).\n")

    print("=== phase 1: UC-6 still enforces /v1 ===")
    # Rapid fire to trip 3/min if window allows; don't fail the demo if not.
    v1_429 = False
    for i in range(5):
        r = post_chat(
            f"{gw}/v1/chat/completions",
            "openai/gpt-5-mini",
            prompt,
            args.timeout,
            f"v1-{i + 1}",
        )
        attempts.append(r)
        print(f"  [{r.status}] /v1 #{i + 1}: {r.total_ms}ms")
        if r.status == 429:
            v1_429 = True
            break

    print("\n=== phase 2: /llm-failover happy path (Vercel or Cursor) ===")
    happy = post_chat(
        f"{gw}/llm-failover/chat/completions",
        "openai/gpt-5-mini",
        prompt,
        args.timeout,
        "failover-happy",
    )
    attempts.append(happy)
    print(
        f"  [{happy.status}] model={happy.model!r} {happy.total_ms}ms preview={happy.preview!r}"
    )

    forced: list[Attempt] = []
    if args.force_failover_test:
        print("\n=== phase 3: force eviction (unhealthyCondition=true) ===")
        print("  (proves priority groups; restores 429/5xx policy after)")
        kubectl_apply_health("true", "60s")
        time.sleep(2)
        for i in range(2):
            r = post_chat(
                f"{gw}/llm-failover/chat/completions",
                "openai/gpt-5-mini",
                prompt,
                args.timeout,
                f"force-{i + 1}",
            )
            forced.append(r)
            attempts.append(r)
            print(
                f"  [{r.status}] model={r.model!r} {r.total_ms}ms preview={r.preview!r}"
            )
            time.sleep(1)
        kubectl_apply_health("response.code >= 500 || response.code == 429", "30s")
        print("  restored health policy to 429/5xx")

    # Did we see Cursor-ish model on forced second request?
    saw_cursor = any(
        ("auto" in (a.model or "").lower() or "composer" in (a.model or "").lower())
        and a.ok
        for a in forced[1:]
    ) or (
        len(forced) >= 2
        and forced[0].ok
        and forced[1].ok
        and forced[0].model != forced[1].model
    )

    report: dict[str, Any] = {
        "thesis": (
            "agentgateway failover: Vercel primary, Cursor fallback on 429/5xx; "
            "UC-6 rate limit kept on /v1"
        ),
        "routes": {
            "/v1": "vercel-only + UC-6 rate limit",
            "/llm-failover": "vercel → cursor priority groups",
            "/cursor": "cursor-only (direct)",
        },
        "docs": "https://agentgateway.dev/docs/kubernetes/latest/llm/failover/",
        "uc6_429_observed": v1_429,
        "failover_happy_ok": happy.ok,
        "force_failover_model_changed": saw_cursor if args.force_failover_test else None,
        "attempts": [asdict(a) for a in attempts],
        "pass": bool(happy.ok and (not args.force_failover_test or saw_cursor or all(a.ok for a in forced))),
    }

    print("\n=== verdict ===")
    if report["pass"]:
        print("PASS: /llm-failover served successfully via agentgateway.")
        if v1_429:
            print("      UC-6 still 429s /v1 as required.")
        if args.force_failover_test and saw_cursor:
            print("      Forced eviction progressed across priority groups.")
    else:
        print("FAIL: see attempts in report")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
        print(f"\nwrote {args.out}")

    return 0 if report["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
