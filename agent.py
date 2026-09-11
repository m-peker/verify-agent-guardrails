"""
A governed customer-support agent, built on TealTiger's DefensePipeline.

Scenario: an order-support agent for a retail company. It can look up orders.
It must never issue refunds on its own, never send a customer's national ID or
IBAN to the model, never say something abusive to a customer, and never burn
more than a few cents per session.

Run:  python agent.py
No API key required -- the provider is stubbed so the run is deterministic.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Iterable

from tealtiger.pipeline import DefensePipeline, PipelineConfig, PipelineHooks, PipelineRequest
from tealtiger.pipeline.modules.post.content_moderation import (
    ContentModerationConfig,
    ContentModerationModule,
)
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

# ---------------------------------------------------------------------------
# 1. What the agent is allowed to do
# ---------------------------------------------------------------------------

ALLOWED_TOOLS = ["get_order_status", "search_orders", "get_shipping_eta"]

PII_PATTERNS = [
    # Turkish national ID: 11 digits, cannot start with 0.
    PIIPattern("tckn", r"\b[1-9][0-9]{10}\b", 0.90),
    # Turkish IBAN: TR + 24 digits.
    PIIPattern("iban", r"\bTR\d{24}\b", 0.95),
    PIIPattern("email", r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b", 0.80),
]

SESSION_BUDGET_USD = 0.05
PRICE_PER_TOKEN_USD = 0.000002


@dataclass(frozen=True)
class Control:
    stage: str   # "pre" runs before the provider, "post" inspects its reply
    module: Any


def build_controls() -> dict[str, Control]:
    """Every governance control this agent runs, by name.

    This is the single source of truth. build_pipeline() assembles the agent
    from it, and the conformance harness reads the same dict -- so the harness
    always tests the controls the agent actually ships with, not a copy.

    Returns fresh module instances on every call, because some controls (the
    cost budget) carry per-session state.
    """
    return {
        "tool_allowlist": Control(
            "pre", ToolAllowlistModule(ToolAllowlistConfig(allowlist=ALLOWED_TOOLS))
        ),
        "pii_scanner": Control(
            "pre", PIIScannerModule(PIIScannerConfig(threshold=0.5, patterns=PII_PATTERNS))
        ),
        "cost_budget": Control(
            "pre",
            CostBudgetModule(
                CostBudgetConfig(
                    session_budget=SESSION_BUDGET_USD,
                    cost_per_token=PRICE_PER_TOKEN_USD,
                )
            ),
        ),
        "content_moderation": Control(
            "post", ContentModerationModule(ContentModerationConfig())
        ),
    }


def build_pipeline(proxy: Any, exclude: Iterable[str] = ()) -> DefensePipeline:
    """Assemble the three-stage pipeline: pre -> execution -> post.

    `exclude` drops named controls. The agent never uses it; the harness does,
    to prove each denial comes from the control it expects.
    """
    excluded = set(exclude)
    controls = {name: c for name, c in build_controls().items() if name not in excluded}

    return DefensePipeline(
        PipelineConfig(
            pre_execution_modules=[c.module for c in controls.values() if c.stage == "pre"],
            post_execution_modules=[c.module for c in controls.values() if c.stage == "post"],
            observe_proxy=proxy,
            agent_id="order-support-01",
            fail_closed=True,
            hooks=_spend_hooks(controls.get("cost_budget"), proxy),
        )
    )


def _spend_hooks(budget: Control | None, proxy: Any) -> PipelineHooks | None:
    """Keep the budget's session total in step with what the provider has cost.

    CostBudgetModule only knows what it is told: its docs ask the integrator to
    call add_cost() after every response, and the pipeline does not do it for
    you. Without this hook the session total stays at $0 forever, and the
    "session budget" silently becomes a per-request cap.

    The sync runs before each request's budget check and reads the proxy's
    running total, rather than recording after each call. That way retries the
    pipeline makes on its own (resampling after a post-execution DENY, which
    does not fire the after_execution hook) are counted too.
    """
    if budget is None:
        return None

    recorded = 0.0

    def sync_spend(request: Any) -> None:
        nonlocal recorded
        cost = proxy.get_cost()
        total = cost["total_cost"] if isinstance(cost, dict) else cost.total_cost
        if total > recorded:
            budget.module.add_cost(total - recorded)
            recorded = total

    return PipelineHooks(before_pre_execution=sync_spend)


# ---------------------------------------------------------------------------
# 2. A stand-in for an observe()-wrapped provider client
# ---------------------------------------------------------------------------

DEFAULT_REPLY = "Order 12345 left the warehouse and is with the courier."


class StubProxy:
    """Deterministic stand-in for observe(OpenAI()).

    A real integration passes the observe() proxy here and gets cost tracking,
    audit logging and behavioural baselines for free. We stub it so this file
    runs offline and always produces the same output. Like the real proxy, it
    reports token usage and an accumulated cost.
    """

    def __init__(self, reply: str = DEFAULT_REPLY, tokens_per_call: int = 125) -> None:
        self.reply = reply
        self.tokens_per_call = tokens_per_call
        self.calls = 0

    async def call(self, payload: dict) -> dict:
        self.calls += 1
        prompt_tokens = self.tokens_per_call // 2
        return {
            "content": self.reply,
            "model": "gpt-4o-mini",
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": self.tokens_per_call - prompt_tokens,
                "total_tokens": self.tokens_per_call,
            },
        }

    def get_cost(self) -> dict:
        total = self.calls * self.tokens_per_call * PRICE_PER_TOKEN_USD
        return {"total_cost": total, "request_count": self.calls}


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


def render(turn: Turn, result: Any) -> None:
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
    print(f"  controls        : {', '.join(build_controls())}")
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
