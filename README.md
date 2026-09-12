# verify-agent-guardrails

[![governance](https://github.com/m-peker/verify-agent-guardrails/actions/workflows/governance.yml/badge.svg)](https://github.com/m-peker/verify-agent-guardrails/actions/workflows/governance.yml)

**Configuring a guardrail and having a guardrail are different things.**

A governance pipeline that returns `ALLOW` tells you one of two things: the control ran and the request was clean, or the control never ran at all. Both produce the same log line. If you only ever send your agent safe traffic, you never find out which one you have.

This repo is a small, runnable demonstration of how to tell the difference — a governed AI agent, plus a conformance harness that proves each of its controls is actually enforcing.

Built on [TealTiger](https://github.com/agentguard-ai/tealtiger), an open-source governance SDK for LLM applications. The technique is not specific to it.

---

## What's here

| File | What it does |
|---|---|
| `agent.py` | A governed order-support agent: tool allowlist, input PII scanning, session cost budget, output content moderation |
| `test_governance.py` | The conformance harness — proves every control the agent ships denies something |
| `.github/workflows/governance.yml` | Runs the harness on every push |

Both scripts run offline. No API key, no network, deterministic output.

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
          all 4 controls in the agent have a canary

  [PASS]  clean traffic
          legitimate request passes

  [PASS]  tool_allowlist: a tool nobody declared
          denied by tool_allowlist, allowed without it

  [PASS]  pii_scanner: a national ID in the prompt
          denied by pii_scanner, allowed without it

  [PASS]  pii_scanner: an IBAN in the prompt
          denied by pii_scanner, allowed without it

  [PASS]  pii_scanner: an IBAN written in groups of four
          denied by pii_scanner, allowed without it

  [PASS]  pii_scanner: an IBAN typed in lowercase
          denied by pii_scanner, allowed without it

  [PASS]  cost_budget: one request larger than the session budget
          denied by cost_budget, allowed without it

  [PASS]  cost_budget: small requests that add up past the session budget
          denied by cost_budget, allowed without it

  [PASS]  content_moderation: an abusive reply from the model
          denied by content_moderation, allowed without it

  10/10 checks passed (4 controls under test)
```

---

## The method

Every control gets two assertions, not one.

**1. Positive** — feed it a payload it must deny, and check the denial carries *that control's own* reason code.

**2. Ablation** — rebuild the pipeline *without* that control, and check the same payload now passes.

The second one is what people skip, and it is what does the work. Without it, a passing test proves only that *something* denied the request. Your PII test can pass for months because the tool allowlist happened to be rejecting that tool anyway — and the day someone legitimately adds the tool to the allowlist, you discover the PII scanner has been dead the whole time.

Four rules keep the harness from fooling itself:

- **Test the real agent, not a copy.** Controls come from `agent.build_controls()` and pipelines from `agent.build_pipeline()`. A harness that re-types the agent's configuration keeps passing after the agent breaks.
- **Derive coverage from the agent.** Every control the agent ships must have a canary, and every canary must name a control the agent ships. The list of controls is never hand-written in the test.
- **Test stateful controls with sequences.** A session budget checked with one oversized request is only proven to be a per-request cap. The canary that matters is five ordinary requests followed by a sixth that must be refused.
- **Test the formats people use.** A pattern is only proven for the inputs it has been shown. IBANs get canaries for compact, grouped, and lowercase forms.

And one check in the other direction: **clean traffic must still pass.** Governance that blocks everything is also broken; it just fails in a way your users report for you.

## Does it catch anything?

A test suite that always passes is decoration. Break the agent — not the test — by setting the PII scanner's threshold in `agent.py` to `0.99`, where no pattern can reach:

```
  [FAIL]  pii_scanner: a national ID in the prompt
          full pipeline ALLOWED a payload that must be denied

  [FAIL]  pii_scanner: an IBAN in the prompt
          full pipeline ALLOWED a payload that must be denied
  ...
  6/10 checks passed (4 controls under test)
```

Delete the PII scanner from the agent entirely, and the coverage check names the problem on the first line:

```
  [FAIL]  coverage
          canary for a control the agent does not ship: pii_scanner
```

Non-zero exit either way. In CI, a dead control becomes a red build instead of an incident.

## Mistakes the first version made

The first version of this repo had the exact failures it warns about. A review caught them. The two behavioural ones now have canaries that failed before the fix, and the git history keeps every step visible:

- **The harness tested a copy of the agent.** It rebuilt modules from re-typed configuration, so breaking `agent.py` left it green.
- **Coverage was circular.** It compared the test's canaries against the test's own list of controls, and missed that content moderation had no canary.
- **The session budget never accumulated.** `CostBudgetModule` only counts spend reported through `add_cost()`, and the pipeline does not call it. Without that wiring the "session" budget was a per-request cap. `agent.py` now syncs spend from the provider proxy before every budget check.
- **Grouped and lowercase IBANs were not detected.**

## Known limitations

This is a demonstration of a testing method, not a production PII detector.

- **The national ID pattern has no checksum.** Any 11-digit number that does not start with 0 matches, so an order number such as `20240912345` is a false positive. A real deployment should validate the TCKN checksum, which a regex cannot express.
- **National IDs written with spaces are not detected.** Allowing spaces would also match fragments of phone numbers.
- **The IBAN pattern does not verify the mod-97 check digits.**
- **The provider is a stub.** It reports usage and cost the way an `observe()`-wrapped client does, but no real model is called.

## Adapting this to your own stack

1. Expose the controls your agent actually runs from one place, and build the agent from it.
2. For each control, write a payload it must reject and name the reason code only it emits. Add format variants for pattern-based controls and request sequences for stateful ones.
3. Assert the denial, then assert the payload passes with that control removed.
4. Derive coverage from the agent, add a clean-traffic case, and run it in CI.

The question worth being able to answer, whatever you use: *when did this control last say no, and can I make it say no on demand?*

---

## Writeup

Accompanies the article **[Testing AI Agent Guardrails: Why ALLOW Is Not a Safety Signal](https://medium.com/@msapeker/testing-ai-agent-guardrails-why-allow-is-not-a-safety-signal-0a76cf0301b3)**, which walks through the agent, the method, and the mistakes the first version of this repo made.

## License

Apache-2.0. See `LICENSE`.
