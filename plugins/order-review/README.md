# Sales Order AI review Worker

Local implementation of an ERPBridge `after_response` plugin. It appends an
advisory `ai_assessment` to the normalized Sales Order response using the fixed
Workers AI model `@cf/cloudflare/clef-flash`.

**Not deployed.** No live inference, model-quality test, CPU-budget acceptance,
or end-to-end MCP acceptance has been performed. This Worker does not call
ERPNext, store ERP credentials, or change business records.

## Local verification

From the `ERPBridge-Plugins` repository root:

```sh
npm --prefix plugins/order-review ci
npm --prefix plugins/order-review run typecheck
npm --prefix plugins/order-review test
npm --prefix plugins/order-review run build
python3 -m unittest discover -s tests -p 'test_order_review_manifests.py' -v
```

Use Node 22.12+, Node 24, or Node 26+ as specified in `package.json`.
`build` runs `wrangler deploy --dry-run`, not deployment. Tests run inside the
local Workers runtime using `@cloudflare/vitest-plugin`. Their configuration
loads no AI binding; each request receives a local `AI.run` spy. Outbound
`fetch` is blocked by the test harness. No Cloudflare account/token is needed.

Do not turn mocked tests into real inference tests. Real Workers AI calls can
consume the shared daily allowance even from a local development session.

## HTTP contract

Only `POST /v1/process` is supported. Send `Content-Type: application/json` and
`X-API-Key` matching the Worker secret `PLUGIN_API_KEY`. The secret must contain
32 to 1,024 characters; use a high-entropy printable random value, not the
example or a memorable password. Authentication uses SHA-256 digests and the
Workers runtime's `crypto.subtle.timingSafeEqual` before parsing the body.

The exact request envelope is:

```json
{
  "protocolVersion": "v1",
  "invocationId": "synthetic-demo-request",
  "tool": {
    "name": "get_sales_order_with_ai_review",
    "version": "1.0.0"
  },
  "result": {
    "name": "SYNTHETIC-ORDER",
    "status": "To Deliver and Bill",
    "docstatus": 1,
    "transaction_date": "2026-03-01",
    "delivery_date": "2026-03-05",
    "per_delivered": 20,
    "per_billed": 0
  },
  "config": { "policyVersion": "order-review-v1" }
}
```

Successful responses contain only `{ "result": { ...originalOrder,
"ai_assessment": ... } }`. The original fields are preserved. An upstream
`ai_assessment` collision is rejected rather than overwritten. Every response
uses `Cache-Control: no-store`. Failure responses contain only a stable error
code, never provider errors, secrets, or ERP data.

The request is limited to 64 KiB, including streaming bodies without a declared
length. Selected model state is limited to 8 KiB. Oversize state is rejected,
not silently truncated. The model wait deadline is 8 seconds, with no retry.
Timing out does not guarantee cancellation of inference already started.

## Assessment and fixed safety policy

The Worker selects only normalized lifecycle status, docstatus, calendar dates,
delivery/billing percentages, and optional item delivery dates/quantities.
Names, customer/contact fields, money fields, descriptions, and arbitrary notes
are not sent to the model. The original response still goes back to the
already-authorized MCP caller, so this is not an output redaction plugin.

The model answers two fixed choice questions: review queue and review priority.
It receives no user-controlled prompt, model selector, destination, or threshold.
Its choices are checked against a deterministic policy guard:

- Draft, cancelled, closed, or completed orders are routine/low when their
  lifecycle facts are consistent. Historical due dates alone do not make them
  urgent.
- An active order with incomplete delivery and a past header or positive-quantity
  item delivery date enters delivery review. More than seven days past the
  earliest such date means high priority; otherwise medium.
- Otherwise incomplete billing enters billing review/medium. This is not proof
  of overdue payment.
- Otherwise the result is routine/low.
- Missing/inconsistent required facts produce `insufficient_data` without an
  inference request. For item rows, delivery date and quantity are required if
  rows are supplied. Unknown/malformed fields that participate in the policy
  are rejected.

This sample deliberately constrains AI recommendations. The guard computes an
allowed queue/priority and rejects a confident model recommendation that differs.
It is not an unconstrained business decision engine. The model may abstain even
when sufficient facts are present.

Model replies require the fixed model identity, both choice answers, the exact
allowed option keys, finite probabilities in [0,1] summing to approximately 1,
a highest-probability selected choice, bounded confidence, and nonnegative integer
usage counters. Top probability below 0.7, margin below 0.15, or reported
confidence below 0.7 yields `uncertain` with abstaining choices. These thresholds
are demo policy, not calibrated correctness guarantees.

The output includes the model identifier, policy version, UTC evaluation time,
assessment status, review queue/priority, `requires_human_review: true`, and
`advisory_only: true`. The plugin never approves, rejects, submits, or modifies
an order. It cannot infer stock availability, payment receipt, creditworthiness,
or missing payment terms.

## Schemas and binding

- `schema/ai-assessment.schema.json` defines the closed added object.
- `schema/tool-output.schema.json` permits native order fields and optional
  assessment, because ERPBridge validates the plain response before the plugin.
- `src/schema.ts` exports these sources and an enriched-output schema requiring
  assessment for Worker success.

A missing/inactive binding can still return a schema-valid plain order. Future
MCP acceptance must check the exact active binding and assessment presence.
The new tool disables bridge caching; existing tools remain unchanged.

The [integration runbook](../../docs/erpnext-order-review-plugin.md) explains
local manifest preparation, free-tier limits, deployment approval gates, the
current infrastructure state-recovery risk, and rollback. Use the standard
`AI` binding in `wrangler.jsonc` only for separately approved deployment or live
inference. Never put the plugin key in the binding config or source tree.
