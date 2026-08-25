# ERPBridge Plugins

External plugin processes for ERPBridge. Each plugin is independently buildable
and releasable under `plugins/<plugin-name>/`.

## Plugins

- `plugins/mock-plugin` — deterministic response fixture used by the ERPBridge
  black-box integration test.

ERPBridge does not build, install, or start these processes. A deployment
operator runs a pinned plugin image and configures an ERPBridge `Plugin`
resource with its network endpoint.
