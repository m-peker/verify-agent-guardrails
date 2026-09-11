# verify-agent-guardrails

**Configuring a guardrail and having a guardrail are different things.**

A governance pipeline that returns `ALLOW` tells you one of two things: the control ran and the request was clean, or the control never ran at all. Both produce the same log line. If you only ever send your agent safe traffic, you never find out which one you have.

This repo is a small, runnable demonstration of how to tell the difference — a governed AI agent, plus a conformance harness that proves each of its controls is actually enforcing.

Built on [TealTiger](https://github.com/agentguard-ai/tealtiger), an open-source governance SDK for LLM applications. The technique is not specific to it.

---

## What's here

| File | What it does |
|---|---|
| `agent.py` | A governed order-support agent: tool allowlist, input PII scanning, session cost budget |
| `test_governance.py` | The conformance harness — proves every control denies something |

Both run offline. No API key, no network, deterministic output.

## Setup

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
```

## Run the agent

```bash
python agent.py
```

Four requests — one legitimate, three that must be stopped:

```
  legitimate lookup
    allowed       : True
    pre-exec      : ALLOW []
    latency       : 1.00 ms

  tool outside the allowlist
    allowed       : False
    blocked at    : PipelineStage.PRE_EXECUTION
    pre-exec      : DENY ['TOOL_NOT_ALLOWED']

  national ID in the prompt
    allowed       : False
    pre-exec      : DENY ['PII_DETECTED']

  IBAN in the prompt
    allowed       : False
    pre-exec      : DENY ['PII_DETECTED']

provider calls actually made : 1 of 4
session cost                 : $0.00025
```

Denied requests short-circuit before the provider is called, so a blocked request costs nothing and leaks nothing.

## Run the harness

```bash
python test_governance.py        # exits non-zero if any control is not enforcing
```

```
  [PASS]  coverage
          every control has a canary

  [PASS]  clean traffic
          legitimate request passes

  [PASS]  tool_allowlist: a tool nobody declared
          denied by tool_allowlist, allowed without it

  [PASS]  pii_scanner: a national ID in the prompt
          denied by pii_scanner, allowed without it

  [PASS]  pii_scanner: an IBAN in the prompt
          denied by pii_scanner, allowed without it

  [PASS]  cost_budget: a request that would blow the session budget
          denied by cost_budget, allowed without it

  6/6 checks passed (3 controls under test)
```

---

## The method

Every control gets two assertions, not one.

**1. Positive** — feed it a payload it must deny, and check the denial carries *that control's own* reason code.

**2. Ablation** — rebuild the pipeline *without* that module, and check the same payload now passes.

The second one is what people skip, and it is what does the work. Without it, a passing test proves only that *something* denied the request. That matters more than it sounds: your PII test can pass for months because the tool allowlist happened to be rejecting that tool anyway — and the day someone legitimately adds the tool to the allowlist, you discover the PII scanner has been dead the whole time.

Two more checks round it out:

- **Clean traffic** must still pass. Governance that blocks everything is also broken; it just fails in a way your users report for you.
- **Coverage**: every control wired into the pipeline must have a canary. A control with no test is exactly the situation this exercise exists to catch, so the harness audits itself too.

## Does it catch anything?

A test suite that always passes is decoration. Misconfigure the PII scanner so no pattern can reach its threshold — `threshold=0.99` when the highest-confidence pattern is `0.95` — and the scanner loads fine, reports healthy, and never fires again:

```
  [FAIL]  pii_scanner: a national ID in the prompt
          full pipeline ALLOWED a payload that must be denied

  [FAIL]  pii_scanner: an IBAN in the prompt
          full pipeline ALLOWED a payload that must be denied

  4/6 checks passed (3 controls under test)
  A FAIL here means a control is configured but not enforcing.
```

Non-zero exit. In CI, a dead control becomes a red build instead of an incident.

## Adapting this to your own stack

The harness is deliberately small — around 150 lines — because the point is the shape, not the code. To port it:

1. List the controls your pipeline actually loads.
2. For each, write one payload it must reject and name the reason code only it emits.
3. Assert the denial, then assert the payload passes with that control removed.
4. Add a clean-traffic case and a coverage check.
5. Run it in CI.

The question worth being able to answer, whatever you use: *when did this control last say no, and can I make it say no on demand?*

---

## Writeup

Accompanies the article *"ALLOW Is Not a Safety Signal"* — link to follow.

## License

Apache-2.0. See `LICENSE`.
