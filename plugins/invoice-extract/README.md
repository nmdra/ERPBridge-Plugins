# Invoice PDF extraction plugin

Local Python plugin for ERPBridge's `raw_response` phase. It converts a supplied
text-based invoice PDF into a small JSON object. It does not fetch documents,
contact ERPNext, use a model, or make external service calls.

**Not deployed.** The example plugin, binding, and tool manifests are inactive
and contain `.invalid` endpoints. No usable ERP PDF endpoint is assumed.

## Supported first-version contract

- `POST /v1/process`, authenticated with `X-API-Key` from `PLUGIN_API_KEY`.
- ERPBridge v1 invocation with `rawResponse`, not an after-response `result`.
- Successful ERP HTTP status, content type `application/pdf`, base64 body.
- Exact config: `{"policyVersion":"invoice-text-v1"}`.
- English labels, one label/value per line: `Invoice Number:`, `Invoice No.:`,
  or `Invoice #:`, `Invoice Date:`, `Currency:`, and `Grand Total:`,
  `Invoice Total:`, `Amount Due:`, or `Total:`.
- Dates must use `YYYY-MM-DD`. Amounts use a decimal point, optional standard
  thousands commas, no currency symbols, and at most two decimal places.
- Explicit currencies: LKR, USD, EUR, GBP, INR, AUD, CAD, JPY.
- Missing, invalid, or conflicting fields become `null` with a warning.
  Repeated equal values are allowed. No currency or date format is guessed.
- Every successful result has `requires_human_review: true`, even when no
  warnings exist. These values are not approved accounting data. Printed labels
  can be misleading; this is not a general-purpose invoice understanding model.
- Layouts with no recognizable invoice fields fail. Documents with no extracted
  text fail with `OCR_REQUIRED`. Mixed image/text documents and scans with a text
  watermark are not reliably detected; partial results still require review.
  Image OCR, multi-column tables, tax calculations, line items,
  signatures, authenticity checks, and automatic payment actions are not supported.

Example successful response:

```json
{
  "result": {
    "invoice_number": "INV-DEMO-001",
    "invoice_date": "2026-10-17",
    "currency": "LKR",
    "total": 12500.25,
    "requires_human_review": true,
    "warnings": []
  }
}
```

`total` is a JSON number for display. It is not an exact decimal accounting
representation. If exact arithmetic is required, change the versioned contract
to a decimal string or minor units before adding accounting behavior.

## Run locally

Requires Linux, Python 3.11+, and Poppler's `pdfinfo` and `pdftotext` on PATH.
There are no third-party Python dependencies. Install Poppler through the
operating system package manager if it is absent.

```bash
cd plugins/invoice-extract
python3 -m unittest -v
read -rsp 'Local plugin key (32–1024 characters): ' PLUGIN_API_KEY
printf '\n'
export PLUGIN_API_KEY
python3 invoice_extract.py
```

The server defaults to `127.0.0.1:8080`. `HOST` and `PORT` override the address.
Do not expose this plain HTTP development server publicly. It handles one
request at a time. Never pass a key as a command-line argument.

Generate a synthetic request from the test PDF generator in another terminal:

```bash
mkdir -p ../../artifacts/invoice-extract
python3 - <<'PY' > ../../artifacts/invoice-extract/synthetic-request.json
import json
from test_invoice_extract import envelope
print(json.dumps(envelope()))
PY
```

With the same local key exported in that terminal, send it via stdin, not a
command-line argument:

```bash
printf 'X-API-Key: %s\n' "$PLUGIN_API_KEY" |
  curl --silent --show-error --max-time 15 --header @- \
    --header 'Content-Type: application/json' \
    --data-binary @../../artifacts/invoice-extract/synthetic-request.json \
    http://127.0.0.1:8080/v1/process
```

The Dockerfile provides a non-root Linux runtime and Poppler. It is included
for local packaging only. No image has been built or published as part of this
implementation. No deployment workflow is included.

## Limits and failures

- Maximum HTTP JSON request: 1 MiB. Maximum decoded PDF: 512 KiB.
- Maximum pages: 20. Metadata/text output: less than 1 MiB per parser process.
- Each parser process: 4-second wall timeout, 3-second CPU limit,
  256 MiB address-space limit, no core dumps, 64 file descriptors.
- Socket idle timeout: 5 seconds, not an absolute request deadline. A slow client
  can occupy this single-request server. Keep the listener local; shared use
  requires a front end with absolute header/body deadlines and connection limits.
  Plugin manifest timeout: 15 seconds.
- PDFs and extracted text are temporary files in a private directory. Cleanup
  runs on normal success and handled failure. An abrupt process/host termination
  can leave temporary files; production must use disposable, access-controlled
  temporary storage and startup cleanup.
- No payload, extracted text, keys, or Poppler diagnostics are logged. Parser
  subprocesses receive a minimal environment without `PLUGIN_API_KEY`.
- Request compression and chunked bodies are unsupported. Duplicate JSON keys,
  non-finite JSON numbers, unsupported encodings, and ERP error statuses fail.
- Errors return only `{"error":{"code":"..."}}`, never document contents.
- Password-protected PDFs are unsupported. Depending on how Poppler rejects
  them, the code can be `ENCRYPTED_PDF_UNSUPPORTED` or `PDF_EXTRACTION_FAILED`.

Resource limits are not a sandbox. Poppler is a native parser for untrusted
files. Before production use, require a patched parser, container memory/CPU/PID
limits, a read-only root filesystem, disposable limited temporary storage,
network egress denial, and transport TLS. This implementation has not undergone
malicious-PDF fuzzing or production load testing.

## ERPBridge manifests

Templates: [`../../manifests/invoice-extract`](../../manifests/invoice-extract).
Canonical output schema: [`schema/tool-output.schema.json`](schema/tool-output.schema.json).
The tool embeds an identical copy of that schema.

- Plugin: `invoice-pdf-extract@1.0.0`, `credentialRef: PLUGIN_API_KEY`.
- Tool: `get_invoice_details@1.0.0`, HTTP-backed with an object output schema.
- Binding: `invoice-pdf-extract-v1`, `phase: raw_response`,
  `failurePolicy: fail`, priority 10.
- Tool cache disabled. All three resources inactive.
- The tool has **no `responsePath`** because the plugin returns the final invoice
  object directly. Do not reuse an ERPNext `responsePath: data` unchanged.
- The ERP PDF URL and approved origin are deliberately fictional. Review the
  real PDF endpoint, ERP authorization, plugin TLS endpoint, endpoint allowlist,
  and separate approval before enabling anything. Do not bulk-apply templates.
- A raw plugin cannot change the ERP HTTP status or convert an ERP error into
  success. Another raw binding with `failurePolicy: continue` can fall back to
  the original ERP body. Review the entire chain before adding other bindings.

Local-only validation, not activation:

```bash
bridgectl --context erpbridge-demo tool validate -f manifests/invoice-extract/tool.json
bridgectl --context erpbridge-demo plugin validate -f manifests/invoice-extract/plugin.json
bridgectl --context erpbridge-demo plugin binding validate -f manifests/invoice-extract/binding.json
```

Run those commands from the repository root. No server calls are required.
