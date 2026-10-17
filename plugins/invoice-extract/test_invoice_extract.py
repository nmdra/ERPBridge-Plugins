import base64
import http.client
from http.server import HTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import invoice_extract as app

KEY = "local-test-only-" + "x" * 32
TEXT = "Invoice\nInvoice Number: INV-DEMO-001\nInvoice Date: 2026-10-17\nCurrency: LKR\nGrand Total: 12,500.25"


def make_pdf(text):
    """Generate a tiny synthetic PDF without a PDF authoring dependency."""
    escaped = [line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)") for line in text.splitlines()]
    commands = "BT /F1 12 Tf 40 760 Td 16 TL " + " T* ".join("(" + line + ") Tj" for line in escaped) + " ET"
    stream = commands.encode("ascii")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result)); result += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    return result + f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()


def envelope(pdf=None):
    return {"protocolVersion": "v1", "invocationId": "synthetic-test", "tool": {"name": "get_invoice_details", "version": "1.0.0"},
            "rawResponse": {"status": 200, "contentType": "application/pdf", "body": {
                "encoding": "base64", "value": base64.b64encode(pdf if pdf is not None else make_pdf(TEXT)).decode()}},
            "config": {"policyVersion": app.POLICY}}


class ParserTests(unittest.TestCase):
    def test_labeled_invoice(self):
        result = app.parse_invoice(TEXT)
        self.assertEqual(result, {"invoice_number": "INV-DEMO-001", "invoice_date": "2026-10-17", "currency": "LKR", "total": 12500.25,
                                  "requires_human_review": True, "warnings": []})

    def test_missing_values_are_null(self):
        result = app.parse_invoice("Invoice\nInvoice No.: INV-1")
        self.assertIsNone(result["total"])
        self.assertTrue(result["requires_human_review"])
        self.assertEqual(len(result["warnings"]), 3)

    def test_conflicting_totals_abstain(self):
        self.assertIsNone(app.parse_invoice(TEXT + "\nAmount Due: 1.00")["total"])

    def test_repeated_equal_values_are_allowed(self):
        self.assertEqual(app.parse_invoice(TEXT + "\nGrand Total: 12,500.25")["total"], 12500.25)

    def test_ambiguous_date_and_currency_abstain(self):
        text = TEXT.replace("2026-10-17", "10/11/2026").replace("LKR", "$")
        result = app.parse_invoice(text)
        self.assertIsNone(result["invoice_date"]); self.assertIsNone(result["currency"])

    def test_invalid_and_valid_total_still_abstain(self):
        self.assertIsNone(app.parse_invoice(TEXT + "\nTotal: -100")["total"])

    def test_empty_duplicate_fields_abstain(self):
        for field, label in (("invoice_number", "Invoice Number"), ("invoice_date", "Invoice Date"),
                             ("currency", "Currency"), ("total", "Grand Total")):
            for suffix in ("", "   "):
                with self.subTest(field=field, suffix=suffix):
                    value = app.parse_invoice(TEXT + "\n" + label + ":" + suffix)
                    self.assertIsNone(value[field])
                    self.assertIn(field + "_missing_or_ambiguous", value["warnings"])

    def test_subtotal_not_used_as_total(self):
        self.assertIsNone(app.parse_invoice(TEXT.replace("Grand Total:", "Subtotal:"))["total"])

    def test_unsupported_document(self):
        with self.assertRaises(app.Failure): app.parse_invoice("Purchase Order\nTotal: 10")

    def test_invalid_dates_and_number_formats(self):
        for value in ("12,50.25", "NaN", "1e9", "9999999999999999999", "1.999"):
            with self.subTest(value=value):
                self.assertIsNone(app.parse_invoice(TEXT.replace("12,500.25", value))["total"])
        self.assertIsNone(app.parse_invoice(TEXT.replace("2026-10-17", "2026-02-30"))["invoice_date"])


class ProtocolTests(unittest.TestCase):
    def test_accepts_base64_pdf(self):
        self.assertTrue(app.decode_request(envelope()).startswith(b"%PDF-"))

    def test_rejects_bad_envelopes(self):
        mutations = [lambda v: v.update(result={}), lambda v: v.update(config={}),
                     lambda v: v.update(protocolVersion="v2"), lambda v: v.update(tool={}),
                     lambda v: v["rawResponse"].update(status=True),
                     lambda v: v["rawResponse"]["body"].update(encoding="json"),
                     lambda v: v["rawResponse"]["body"].update(value="not base64"),
                     lambda v: v["rawResponse"]["body"].update(value=base64.b64encode(b"not pdf").decode())]
        for mutate in mutations:
            value = envelope(); mutate(value)
            with self.subTest(value=mutations.index(mutate)), self.assertRaises(app.Failure): app.decode_request(value)

    def test_erp_error_is_not_converted_to_success(self):
        value = envelope(); value["rawResponse"]["status"] = 500
        with self.assertRaises(app.Failure) as ctx: app.decode_request(value)
        self.assertEqual(ctx.exception.code, "ERP_RESPONSE_NOT_SUCCESSFUL")

    def test_content_type(self):
        value = envelope(); value["rawResponse"]["contentType"] = "text/html"
        with self.assertRaises(app.Failure) as ctx: app.decode_request(value)
        self.assertEqual(ctx.exception.status, 415)

    def test_pdf_size_limit(self):
        value = envelope(b"%PDF-" + b"a" * app.MAX_PDF)
        with self.assertRaises(app.Failure) as ctx: app.decode_request(value)
        self.assertEqual(ctx.exception.status, 413)

    def test_strict_json(self):
        for body in (b'{"a":1,"a":2}', b'{"a":NaN}', b'\xff', b'{'):
            with self.subTest(body=body), self.assertRaises(app.Failure): app.strict_json(body)


@unittest.skipUnless(shutil.which("pdfinfo") and shutil.which("pdftotext"), "Poppler required")
class PopplerTests(unittest.TestCase):
    def test_real_pdf_extraction(self):
        result = app.process(envelope(), app.Extractor())
        self.assertEqual(result["result"]["invoice_number"], "INV-DEMO-001")
        self.assertEqual(result["result"]["total"], 12500.25)

    def test_blank_pdf_requires_ocr(self):
        with self.assertRaises(app.Failure) as ctx: app.Extractor().extract(make_pdf(""))
        self.assertEqual(ctx.exception.code, "OCR_REQUIRED")

    def test_corrupt_pdf_fails(self):
        with self.assertRaises(app.Failure): app.Extractor().extract(b"%PDF-1.4\ninvalid")

    def test_timeout_is_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app.subprocess, "run", side_effect=subprocess.TimeoutExpired("sensitive", 4)):
            with self.assertRaises(app.Failure) as ctx:
                app.Extractor()._run("pdfinfo", [], Path(tmp) / "out")
        self.assertEqual(str(ctx.exception), "PDF_EXTRACTION_TIMEOUT")

    def test_duplicate_page_metadata_rejected(self):
        with patch.object(app.Extractor, "_run", return_value=b"Pages: 1\nPages: 21\n"):
            with self.assertRaises(app.Failure) as ctx: app.Extractor().extract(make_pdf(TEXT))
        self.assertEqual(ctx.exception.code, "PDF_PAGE_LIMIT_EXCEEDED")

    def test_page_limit(self):
        with patch.object(app.Extractor, "_run", return_value=b"Pages: 21\n"):
            with self.assertRaises(app.Failure) as ctx: app.Extractor().extract(make_pdf(TEXT))
        self.assertEqual(ctx.exception.code, "PDF_PAGE_LIMIT_EXCEEDED")

    def test_encryption_rejected(self):
        with patch.object(app.Extractor, "_run", return_value=b"Encrypted: yes\nPages: 1\n"):
            with self.assertRaises(app.Failure) as ctx: app.Extractor().extract(make_pdf(TEXT))
        self.assertEqual(ctx.exception.code, "ENCRYPTED_PDF_UNSUPPORTED")

    def test_text_not_retained(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(app.tempfile, "tempdir", tmp):
            app.process(envelope(), app.Extractor())
            self.assertEqual(list(Path(tmp).iterdir()), [])


class FakeExtractor:
    def extract(self, pdf: bytes) -> str:
        return TEXT


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("127.0.0.1", 0), app.handler_for(KEY, FakeExtractor()))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True); cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join()

    def request(self, body=None, headers=None, path="/v1/process", method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        h = {"Content-Type": "application/json", "X-API-Key": KEY}
        if headers: h.update(headers)
        connection.request(method, path, json.dumps(envelope()).encode() if body is None else body, h)
        response = connection.getresponse(); data = response.read(); result = response.status, json.loads(data), dict(response.getheaders())
        connection.close(); return result

    def test_http_success_and_no_store(self):
        status, value, headers = self.request()
        self.assertEqual(status, 200); self.assertEqual(value["result"]["total"], 12500.25)
        self.assertEqual(headers["Cache-Control"], "no-store")

    def test_auth_before_payload_validation(self):
        status, value, _ = self.request(b"invalid", {"X-API-Key": "wrong"})
        self.assertEqual(status, 401); self.assertEqual(value, {"error": {"code": "UNAUTHORIZED"}})

    def test_bad_json(self): self.assertEqual(self.request(b"{")[0], 400)
    def test_wrong_path(self): self.assertEqual(self.request(path="/other")[0], 404)
    def test_wrong_method(self): self.assertEqual(self.request(method="GET")[0], 405)
    def test_unsupported_method_uses_json(self):
        status, value, _ = self.request(method="OPTIONS")
        self.assertEqual(status, 405)
        self.assertEqual(value, {"error": {"code": "METHOD_NOT_ALLOWED"}})
    def test_head_error_has_no_body(self):
        client = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        client.request("HEAD", "/v1/process")
        response = client.getresponse()
        self.assertEqual(response.status, 405); self.assertEqual(response.read(), b"")
        self.assertEqual(response.getheader("Content-Type"), "application/json"); client.close()
    def test_wrong_content_type(self): self.assertEqual(self.request(headers={"Content-Type": "text/plain"})[0], 415)
    def test_request_limit_before_reading(self):
        self.assertEqual(self.request(b"{}", {"Content-Length": str(app.MAX_REQUEST + 1)})[0], 413)
    def test_invalid_secret(self):
        with self.assertRaises(ValueError): app.handler_for("short", FakeExtractor())


class ManifestTests(unittest.TestCase):
    def test_inactive_fail_closed_templates_and_schema(self):
        root = Path(__file__).resolve().parents[2]
        manifests = root / "manifests/invoice-extract"
        plugin = json.loads((manifests / "plugin.json").read_text())
        binding = json.loads((manifests / "binding.json").read_text())
        tool = json.loads((manifests / "tool.json").read_text())
        schema = json.loads((Path(__file__).parent / "schema/tool-output.schema.json").read_text())
        self.assertEqual(tool["spec"]["outputSchema"], schema)
        self.assertEqual(schema["properties"]["requires_human_review"]["enum"], [True])
        for resource in (plugin, binding, tool): self.assertIs(resource["metadata"]["isActive"], False)
        self.assertTrue(plugin["spec"]["endpoint"].endswith(".invalid"))
        self.assertEqual(plugin["spec"]["auth"]["credentialRef"], "PLUGIN_API_KEY")
        self.assertEqual(binding["spec"]["phase"], "raw_response")
        self.assertEqual(binding["spec"]["failurePolicy"], "fail")
        self.assertEqual(binding["spec"]["config"], {"policyVersion": app.POLICY})
        self.assertNotIn("responsePath", tool["spec"]["execution"])
        self.assertFalse(tool["spec"]["cache"]["enabled"])
        self.assertEqual(binding["spec"]["toolRef"], {"name": tool["metadata"]["name"], "version": tool["metadata"]["version"]})
        self.assertEqual(binding["spec"]["pluginRef"], {"name": plugin["metadata"]["name"], "version": plugin["metadata"]["version"]})


@unittest.skipUnless(shutil.which("pdfinfo") and shutil.which("pdftotext"), "Poppler required")
class EndToEndTests(unittest.TestCase):
    def test_real_http_pdf_extraction(self):
        server = HTTPServer(("127.0.0.1", 0), app.handler_for(KEY, app.Extractor()))
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        try:
            client = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=10)
            client.request("POST", "/v1/process", json.dumps(envelope()).encode(),
                           {"Content-Type": "application/json", "X-API-Key": KEY})
            response = client.getresponse(); value = json.loads(response.read()); client.close()
            self.assertEqual(response.status, 200)
            self.assertEqual(value["result"]["total"], 12500.25)
            self.assertTrue(value["result"]["requires_human_review"])
        finally:
            server.shutdown(); server.server_close(); thread.join()


if __name__ == "__main__": unittest.main()
