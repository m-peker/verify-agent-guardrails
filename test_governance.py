"""
A conformance harness for a governed agent.

The premise: configuring a control and having a control are different things.
A pipeline that returns ALLOW tells you either that the control ran and passed,
or that it never ran at all -- and those two look identical in a log.

So every test here does two things:

  1. POSITIVE  -- feed the control a payload it must deny, and assert the
                  denial carries that control's own reason code.
  2. ABLATION  -- rebuild the pipeline WITHOUT that module and assert the same
                  payload now passes.

Step 2 is the part people skip, and it is the part that matters. Without it, a
passing test proves only that *something* denied the request. With it, you have
shown the denial came from the control you think you are relying on.

Run:  python test_governance.py
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from typing import Any

from tealtiger.pipeline import DefensePipeline, PipelineConfig, PipelineRequest
from tealtiger.pipeline.modules.pre.cost_budget import CostBudgetConfig, CostBudgetModule
from tealtiger.pipeline.modules.pre.pii_scanner import PIIScannerConfig, PIIScannerModule
from tealtiger.pipeline.modules.pre.tool_allowlist import (
    ToolAllowlistConfig,
    ToolAllowlistModule,
)

from agent import ALLOWED_TOOLS, PII_PATTERNS, SESSION_BUDGET_USD, StubProxy


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def make_module(kind: str) -> Any:
    """Build one governance module by name, from the same config the agent uses."""
    if kind == "tool_allowlist":
        return ToolAllowlistModule(ToolAllowlistConfig(allowlist=ALLOWED_TOOLS))
    if kind == "pii_scanner":
        return PIIScannerModule(PIIScannerConfig(threshold=0.5, patterns=PII_PATTERNS))
    if kind == "cost_budget":
        return CostBudgetModule(
            CostBudgetConfig(session_budget=SESSION_BUDGET_USD, cost_per_token=0.000002)
        )
    raise ValueError(f"unknown module: {kind}")


def build(modules: list[str]) -> DefensePipeline:
    return DefensePipeline(
        PipelineConfig(
            pre_execution_modules=[make_module(m) for m in modules],
            post_execution_modules=[],
            observe_proxy=StubProxy(),
            agent_id="conformance-harness",
            fail_closed=True,
        )
    )


async def run_once(modules: list[str], payload: dict) -> Any:
    return await build(modules).execute(
        PipelineRequest(payload=payload, correlation_id="cid-conformance")
    )


def reasons(result: Any) -> list[str]:
    pre = result.pre_decision
    return list(getattr(pre, "reason_codes", None) or [])


@dataclass
class Case:
    """One control, one payload it must reject, one reason code it must cite."""

    control: str          # the module under test
    name: str             # what the canary represents
    payload: dict         # a request that control must deny
    expect_reason: str    # the reason code that control -- and only it -- emits


ALL_CONTROLS = ["tool_allowlist", "pii_scanner", "cost_budget"]

CASES = [
    Case(
        control="tool_allowlist",
        name="a tool nobody declared",
        payload={"tool": "issue_refund", "content": "Refund order 12345."},
        expect_reason="TOOL_NOT_ALLOWED",
    ),
    Case(
        control="pii_scanner",
        name="a national ID in the prompt",
        payload={"tool": "search_orders", "content": "Orders for 12345678901 please."},
        expect_reason="PII_DETECTED",
    ),
    Case(
        control="pii_scanner",
        name="an IBAN in the prompt",
        payload={"tool": "search_orders",
                 "content": "Match TR330006100519786457841326 to an order."},
        expect_reason="PII_DETECTED",
    ),
    Case(
        control="cost_budget",
        # $0.05 budget / $0.000002 per token = 25,000 tokens. Ask for more.
        name="a request that would blow the session budget",
        payload={"tool": "get_order_status",
                 "content": "Summarise every order this year.",
                 "max_tokens": 30_000},
        expect_reason="BUDGET_EXCEEDED",
    ),
]


def uncovered_controls() -> list[str]:
    """Controls that are wired into the pipeline but have no canary.

    This is the harness auditing itself. A control with no test is exactly the
    situation this whole exercise exists to catch -- there is no reason to
    exempt the test suite from its own rule.
    """
    covered = {case.control for case in CASES}
    return [control for control in ALL_CONTROLS if control not in covered]


async def check(case: Case) -> tuple[bool, str]:
    """Positive + ablation. Both must hold for the control to count as live."""
    # 1. POSITIVE: the full pipeline denies, citing this control's reason code.
    full = await run_once(ALL_CONTROLS, case.payload)
    if full.allowed:
        return False, "full pipeline ALLOWED a payload that must be denied"
    if case.expect_reason not in reasons(full):
        return False, (f"denied, but not by {case.control} -- "
                       f"reason codes were {reasons(full)}")

    # 2. ABLATION: drop the control; the same payload must now get through.
    without = [m for m in ALL_CONTROLS if m != case.control]
    ablated = await run_once(without, case.payload)
    if not ablated.allowed:
        return False, (f"payload still denied with {case.control} removed "
                       f"({reasons(ablated)}) -- the denial does not come from "
                       f"the control under test")

    return True, f"denied by {case.control}, allowed without it"


async def check_clean_traffic_survives() -> tuple[bool, str]:
    """A guard against the opposite failure: governance that blocks everything."""
    result = await run_once(
        ALL_CONTROLS, {"tool": "get_order_status", "content": "Where is order 12345?"}
    )
    if not result.allowed:
        return False, f"legitimate request was denied: {reasons(result)}"
    return True, "legitimate request passes"


async def main() -> int:
    print("=" * 72)
    print("Governance conformance harness")
    print("  each control must deny its canary -- and allow it once removed")
    print("=" * 72 + "\n")

    failures = 0
    checks = 0

    # Meta-check: does every wired-in control actually have a canary?
    uncovered = uncovered_controls()
    checks += 1
    failures += 0 if not uncovered else 1
    print(f"  [{'PASS' if not uncovered else 'FAIL'}]  coverage")
    print("          " + ("every control has a canary" if not uncovered
                          else f"no canary for: {', '.join(uncovered)}") + "\n")

    ok, detail = await check_clean_traffic_survives()
    checks += 1
    failures += 0 if ok else 1
    print(f"  [{'PASS' if ok else 'FAIL'}]  clean traffic")
    print(f"          {detail}\n")

    for case in CASES:
        ok, detail = await check(case)
        checks += 1
        failures += 0 if ok else 1
        print(f"  [{'PASS' if ok else 'FAIL'}]  {case.control}: {case.name}")
        print(f"          {detail}\n")

    print("-" * 72)
    print(f"  {checks - failures}/{checks} checks passed "
          f"({len(ALL_CONTROLS)} controls under test)")
    if failures:
        print("  A FAIL here means a control is configured but not enforcing.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
