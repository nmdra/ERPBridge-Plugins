# ERPBridge Plugins

External plugin processes for ERPBridge. Each plugin is independently buildable
and releasable under `plugins/<plugin-name>/`.

## Plugins

- `plugins/mock-plugin`: deterministic response fixture used by the ERPBridge
  black-box integration test.
- [`plugins/order-review`](plugins/order-review/README.md): Cloudflare Worker
  that adds an advisory Sales Order assessment with `@cf/cloudflare/clef-flash`.
  Local tests mock AI. No ERPNext requests or credentials are used by the Worker.
  See the [integration runbook](docs/erpnext-order-review-plugin.md) for manifest
  preparation and separately approved deployment steps.

ERPBridge does not build, install, or start these processes. A deployment
operator runs a pinned plugin image and configures an ERPBridge `Plugin`
resource with its network endpoint. Push a tag such as `mock-plugin-v0.1.0` to
publish the matching image to GHCR; the repository workflow publishes only the
image for that plugin version.
