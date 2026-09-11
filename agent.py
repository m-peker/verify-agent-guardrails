"""
A governed customer-support agent, built on TealTiger's DefensePipeline.

Scenario: an order-support agent for a retail company. It can look up orders.
It must never issue refunds on its own, never send a customer's national ID or
IBAN to the model, and never burn more than a few cents per session.

Run:  python agent.py
No API key required -- the provider is stubbed so the run is deterministic.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from tealtiger.pipeline import DefensePipeline, PipelineConfig, PipelineRequest
from tealtiger.pipeline.modules.pre.cost_budget import CostBudgetConfig, CostBudgetModule
from tealtiger.pipeline.modules.pre.pii_scanner import (
    PIIPattern,
    PIIScannerConfig,
    PIIScannerModule,
)
from tealtiger.pipeline.modules.pre.tool_allowlist import (
    ToolAllowlistConfig,
    ToolAllowlistModule,
)
from tealtiger.pipeline.modules.post.content_moderation import (
    ContentModerationConfig,
    ContentModerationModule,
)

# ---------------------------------------------------------------------------
# 1. What the agent is allowed to touch
# ---------------------------------------------------------------------------

ALLOWED_TOOLS = ["get_order_status", "search_orders", "get_shipping_eta"]

# Turkish national ID (11 digits, cannot start with 0) and IBAN.
PII_PATTERNS = [
    PIIPattern("tckn", r"\b[1-9][0-9]{10}\b", 0.90),
    PIIPattern("iban", r"\bTR\d{24}\b", 0.95),
    PIIPattern("email", r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b", 0.80),
]

SESSION_BUDGET_USD = 0.05


def build_pipeline(proxy) -> DefensePipeline:
    """Assemble the three-stage pipeline: pre -> execution -> post."""
    return DefensePipeline(
        PipelineConfig(
            pre_execution_modules=[
                ToolAllowlistModule(ToolAllowlistConfig(allowlist=ALLOWED_TOOLS)),
                PIIScannerModule(PIIScannerConfig(threshold=0.5, patterns=PII_PATTERNS)),
                CostBudgetModule(
                    CostBudgetConfig(
                        session_budget=SESSION_BUDGET_USD,
                        cost_per_token=0.000002,
                    )
                ),
            ],
            post_execution_modules=[
                ContentModerationModule(ContentModerationConfig()),
            ],
            observe_proxy=proxy,
            agent_id="order-support-01",
            fail_closed=True,
        )
    )


# ---------------------------------------------------------------------------
# 2. A stand-in for an observe()-wrapped provider client
# ---------------------------------------------------------------------------


class StubProxy:
    """Deterministic stand-in for observe(OpenAI()).

    A real integration passes the observe() proxy here and gets cost tracking,
    audit logging and behavioural baselines for free. We stub it so this file
    runs offline and always produces the same output.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def call(self, payload: dict) -> dict:
        self.calls += 1
        return {
            "content": "Order 12345 left the warehouse and is with the courier.",
            "model": "gpt-4o-mini",
            "usage": {"prompt_tokens": 90, "completion_tokens": 35, "total_tokens": 125},
        }

    def get_cost(self) -> dict:
        return {"total_cost": 0.00025 * self.calls, "request_count": self.calls}


# ---------------------------------------------------------------------------
# 3. Traffic: one legitimate request and three that must be stopped
# ---------------------------------------------------------------------------


@dataclass
class Turn:
    label: str
    tool: str
    content: str


TRAFFIC = [
    Turn("legitimate lookup",
         "get_order_status", "Where is order 12345?"),
    Turn("tool outside the allowlist",
         "issue_refund", "Refund order 12345 to the customer."),
    Turn("national ID in the prompt",
         "search_orders", "Find every order for customer 12345678901."),
    Turn("IBAN in the prompt",
         "search_orders", "Match this account TR330006100519786457841326 to an order."),
]


def render(turn: Turn, result) -> None:
    pre = result.pre_decision
    print(f"  {turn.label}")
    print(f"    tool          : {turn.tool}")
    print(f"    allowed       : {result.allowed}")
    print(f"    blocked at    : {result.blocked_stage}")
    print(f"    pre-exec      : {getattr(pre, 'action', None)} "
          f"{getattr(pre, 'reason_codes', None)}")
    print(f"    latency       : {result.total_latency_ms:.2f} ms")
    print()


async def main() -> None:
    proxy = StubProxy()
    pipeline = build_pipeline(proxy)

    print("=" * 68)
    print("Governed order-support agent")
    print(f"  allowlist       : {', '.join(ALLOWED_TOOLS)}")
    print(f"  session budget  : ${SESSION_BUDGET_USD:.2f}")
    print("=" * 68 + "\n")

    for turn in TRAFFIC:
        result = await pipeline.execute(
            PipelineRequest(
                payload={"tool": turn.tool, "content": turn.content},
                correlation_id=f"cid-{turn.tool}",
            )
        )
        render(turn, result)

    print(f"provider calls actually made : {proxy.calls} of {len(TRAFFIC)}")
    print(f"session cost                 : ${proxy.get_cost()['total_cost']:.5f}")


if __name__ == "__main__":
    asyncio.run(main())
