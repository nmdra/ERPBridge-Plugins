# Sales Order AI review plugin

This is an offline-prepared sample, not a deployed service or active MCP tool.
The Cloudflare Worker receives an ERPBridge-normalized Sales Order and adds an
advisory `ai_assessment` using `@cf/cloudflare/clef-flash`. It makes no ERPNext
requests and has no ERPNext credential. No AI decision approves, rejects,
submits, or changes an order.

## Resource identities

| Resource | Identity |
| --- | --- |
| Sample tool | `get_sales_order_with_ai_review@1.0.0` |
| Plugin | `sales-order-ai-review@1.0.0` |
| Binding | `sales-order-ai-review-v1` |
| Phase | `after_response` |
| Binding configuration | `{"policyVersion":"order-review-v1"}` |
| Worker authentication secret | `PLUGIN_API_KEY` |
| Bridge credential reference | `PLUGIN_ORDER_REVIEW_KEY` |
| Authentication header | `X-API-Key` |

The original `get_sales_order` and the existing ERP API registration remain
unchanged. Plugin registration does not register another ERP API. The standalone
`ERPBridge-Plugins` repository owns the plugin, manifests, tests, and docs.
Infrastructure remains in sibling `../erpbridge-infra` and is not changed here.

## Local checks

Run these from `/home/nimendra/Documents/Projects/ERPBridge-Plugins`:

```sh
npm --prefix plugins/order-review ci
npm --prefix plugins/order-review run typecheck
npm --prefix plugins/order-review test
npm --prefix plugins/order-review run build
python3 -m unittest discover -s tests -p 'test_order_review_manifests.py' -v
```

Tests supply mocked AI responses. The build is a Wrangler dry run, not a
deployment. Do not replace the mock with a real AI binding in tests. A real
Workers AI call can consume the account allowance even during local development.
See `plugins/order-review/README.md` for runtime details and test fixtures.

## How enrichment works

1. ERPBridge performs the existing permission-checked Sales Order GET.
2. It normalizes the `data` response into the order object.
3. The unprocessed order passes the sample tool output schema.
4. The authenticated Worker validates the v1 plugin envelope and exact tool.
5. It selects a bounded allowlist of business fields, excluding customer/contact
   data, arbitrary notes, and other free text from the model input.
6. Clef-flash answers fixed review-queue and review-priority choice questions.
7. The Worker validates the choice labels, probability distributions, and model
   identity. Missing facts or uncertain output lead to abstention, not a guessed
   decision. A malformed model reply, timeout, or inference error fails closed.
8. The original order is returned with `ai_assessment`. ERPBridge validates the
   final result against the declared schema.

The review cannot establish stock availability, payment receipt, overdue debt,
or creditworthiness from missing data. Billing percentage alone does not prove
late payment. Model probabilities are not calibrated correctness guarantees.
The internal inference deadline does not guarantee Cloudflare cancels an
already-started GPU operation. No automatic retries are included.

### Schema limitation

The tool schema declares `ai_assessment` but does not require it: ERPBridge uses
the schema before the after-response plugin runs. A missing/inactive binding can
therefore return a schema-valid plain order. Acceptance requires both the active
exact binding and an assessment in the MCP result. Do not claim that final schema
validation alone detects an absent binding. When the binding is active,
`failurePolicy: fail` prevents silent fallback on processing errors.

The tool cache is disabled and the Worker sends `Cache-Control: no-store`.
Schema sources are under `plugins/order-review/schema/`; do not edit a second
independent schema copy in a generated manifest.

## Prepare manifests locally

Committed `plugin.json` and `binding.json` templates are inactive. The plugin
endpoint uses a reserved `.invalid` hostname. Do not apply these templates.

After the deployment gates below are approved, obtain a fresh local JSON export
of the existing `get_sales_order` through the installed CLI's documented tool-get
command. Keep it in ignored `artifacts/` and review its authorization. Historical
local generated manifests are not proof of the current server state.

Render three additive manifests without contacting either service:

```sh
python3 scripts/prepare-order-review.py \
  --base-tool artifacts/reviewed-get-sales-order.json \
  --worker-url https://YOUR-WORKER.YOUR-SUBDOMAIN.workers.dev \
  --output-dir artifacts/order-review-prepared
```

Replace the example host with the actual account-issued origin. The preparation
script accepts only an HTTPS standard `workers.dev` origin, requires the known
ERPNext GET contract, preserves existing security and input constraints, adds the
shared output schema, and disables caching. It refuses existing output files.
It makes no HTTP calls, runs no apply commands, and reads no credential values.

Validate the rendered files locally:

```sh
bridgectl --context erpbridge-demo tool validate \
  -f artifacts/order-review-prepared/get_sales_order_with_ai_review.json
bridgectl --context erpbridge-demo plugin validate \
  -f artifacts/order-review-prepared/plugin.json
bridgectl --context erpbridge-demo plugin binding validate \
  -f artifacts/order-review-prepared/binding.json
```

The renderer sets the plugin and binding active in the rendered files. Validation
is local, but applying these files is a separately approved mutation. Inspect the
exact output before any apply. CLI validation is not server admission or runtime
acceptance. Some installed schema tooling may drop strict closure constraints;
compare stored schema readback after a future authorized apply.

## Free-tier feasibility and controls

Published Cloudflare allowances at design time are 100,000 Worker requests/day,
10 ms Worker CPU per invocation, and 10,000 Workers AI Neurons/day. Clef-flash is
listed at 3,455 Neurons per million input tokens with no output-token charge.
The allowance is shared with other account AI usage. Waiting for inference is
not local Worker CPU, but parsing/validation is. Free-tier quota exhaustion fails
requests rather than authorizing an upgrade. No billing plan is changed here.

Use one short text/JSON assessment per invocation. Do not submit images or an
unfiltered order. Measure actual input-token usage, Worker CPU, and latency
before accepting a free-tier deployment; model access and performance have not
been tested in the account. The plugin's 15-second outer timeout must be checked
against the Worker's internal deadline and measured latency. No paid plan,
AI Gateway credits, extra storage, or automatic retries are required by this
sample.

Sources:

- https://developers.cloudflare.com/workers-ai/models/clef-flash/
- https://developers.cloudflare.com/workers-ai/models/clef-flash/schema-input.json
- https://developers.cloudflare.com/workers-ai/models/clef-flash/schema-output.json
- https://developers.cloudflare.com/workers-ai/platform/pricing/
- https://developers.cloudflare.com/workers/platform/limits/
- https://developers.cloudflare.com/workers-ai/configuration/bindings/
- `../ERPBridge/docs/plugin-schema.md`

## Deployment gates: not executed by local implementation

1. Run the bridgectl-ops onboarding preflight. Confirm explicit context, installed
   client/server versions, existing API/tool security, admin access, and registry
   ownership. Check that the sample tool name is unused.
2. Confirm Cloudflare account permission for Workers and Workers AI. The existing
   DNS API token does not prove permission to deploy Workers. Confirm free-tier
   access to this exact model before an approved synthetic inference.
3. Obtain approval for the actual Worker deployment and secret write. Use the
   Wrangler version in the lockfile. Keep `PLUGIN_API_KEY` in provider secret
   storage, never in source/config/payloads. Use a random key of at least 32
   characters. Configure the `AI` binding from `wrangler.jsonc`.
4. Before changing ERPBridge, verify recovery of its SQLite registry and client
   token hashes. The current ACA database is ephemeral and a configuration
   revision can lose state. Do not use broad re-onboarding or assume local raw
   token files restore server hashes. Stop if recovery is not established.
5. Separately approve bridge configuration for the exact Worker `host:443` in
   `PLUGIN_ENDPOINT_ALLOWLIST`, preserving other entries. Supply the matching
   API key through the secret reference `PLUGIN_ORDER_REVIEW_KEY`. Do not print
   or pass keys as command-line arguments. Terraform/server changes are outside
   this local implementation.
6. Directly verify authenticated Worker requests and unauthorized rejection using
   synthetic non-personal data. Measure CPU, input usage, and latency. No live
   inference is authorized by the offline test commands.
7. Review and approve the exact sample tool, plugin, then binding applies in that
   order. Do not use broad onboarding scripts. Read back active exact versions,
   authorization, cache settings, and full output schema.
8. Call the sample through MCP using a permitted demo order. Confirm an assessment
   is present, original fields are preserved, no unexpected personal fields reach
   the model, and the original `get_sales_order` still works. Verify failure
   handling using local mocks or an explicitly approved isolated live rehearsal.
9. Record only redacted outcomes/timings, never full orders, keys, or model state.
   Update the active catalogue only after live acceptance.

## Rollback after a future deployment

Deactivate the new sample tool first, then its binding and plugin, using exact
identities and installed supported soft-delete/deactivation commands. This avoids
exposing an unbound plain order as an AI-reviewed result. Do not change existing
tools, ERP permissions, business data, or API registration. Confirm existing MCP
client authentication and original-tool calls still work.

Keep the Worker until rollback is confirmed. A code rollback can redeploy its
previous recorded version. Removing bridge configuration can cause another ACA
revision and requires the same state-recovery gate and separate approval.
Remove the Worker secret/deployment only after bindings are inactive and the
exact cleanup is approved. No hard delete or all-cache flush is necessary.
