"""Local-only raw-response invoice extraction. Linux + Poppler required."""
from __future__ import annotations

from abc import abstractmethod
import base64
import binascii
from datetime import date
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
from typing import Protocol

MAX_REQUEST = 1024 * 1024
MAX_PDF = 512 * 1024
MAX_TEXT = 1024 * 1024
MAX_PAGES = 20
POLICY = "invoice-text-v1"


class Failure(Exception):
    def __init__(self, code: str, status: int = 422):
        self.code = code
        self.status = status
        super().__init__(code)


def strict_json(data: bytes):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    def constant(_):
        raise ValueError("non-finite number")

    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)
    except (ValueError, UnicodeError, RecursionError):
        raise Failure("INVALID_REQUEST", 400) from None


def decode_request(value) -> bytes:
    if not isinstance(value, dict) or set(value) != {"protocolVersion", "invocationId", "tool", "rawResponse", "config"}:
        raise Failure("INVALID_REQUEST", 400)
    tool = value["tool"]
    if (value["protocolVersion"] != "v1" or not isinstance(value["invocationId"], str)
            or not 1 <= len(value["invocationId"]) <= 256 or not isinstance(tool, dict)
            or set(tool) != {"name", "version"}
            or any(not isinstance(tool[k], str) or not 1 <= len(tool[k]) <= 128 for k in tool)
            or value["config"] != {"policyVersion": POLICY}):
        raise Failure("INVALID_REQUEST", 400)
    raw = value["rawResponse"]
    if not isinstance(raw, dict) or set(raw) != {"status", "contentType", "body"}:
        raise Failure("INVALID_REQUEST", 400)
    status = raw["status"]
    if type(status) is not int or not 100 <= status <= 599:
        raise Failure("INVALID_REQUEST", 400)
    if not 200 <= status < 300:
        raise Failure("ERP_RESPONSE_NOT_SUCCESSFUL")
    if raw["contentType"] != "application/pdf":
        raise Failure("UNSUPPORTED_CONTENT_TYPE", 415)
    body = raw["body"]
    if (not isinstance(body, dict) or set(body) != {"encoding", "value"}
            or body["encoding"] != "base64" or not isinstance(body["value"], str)):
        raise Failure("INVALID_REQUEST", 400)
    if len(body["value"]) > 4 * ((MAX_PDF + 2) // 3):
        raise Failure("PDF_TOO_LARGE", 413)
    try:
        pdf = base64.b64decode(body["value"], validate=True)
    except (ValueError, binascii.Error):
        raise Failure("INVALID_BASE64", 400) from None
    if len(pdf) > MAX_PDF:
        raise Failure("PDF_TOO_LARGE", 413)
    if not pdf.startswith(b"%PDF-"):
        raise Failure("INVALID_PDF")
    return pdf


class Extractor:
    def __init__(self):
        self.commands = {name: shutil.which(name) for name in ("pdfinfo", "pdftotext")}
        if not sys.platform.startswith("linux") or not all(self.commands.values()):
            raise RuntimeError("Linux and Poppler utilities are required")

    def _run(self, name: str, arguments: list[str], output: Path) -> bytes:
        # Limits are set in a separate process, not via preexec_fn in the HTTP server.
        command = [sys.executable, str(Path(__file__).with_name("limited_exec.py")),
                   self.commands[name], *arguments]
        try:
            with output.open("xb") as stream:
                result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=stream,
                                        stderr=subprocess.DEVNULL, timeout=4, check=False,
                                        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": str(output.parent)})
            if result.returncode != 0:
                raise Failure("PDF_EXTRACTION_FAILED")
            if output.stat().st_size >= MAX_TEXT:
                raise Failure("EXTRACTED_TEXT_TOO_LARGE", 413)
            return output.read_bytes()
        except subprocess.TimeoutExpired:
            raise Failure("PDF_EXTRACTION_TIMEOUT", 504) from None
        except OSError:
            raise Failure("PDF_EXTRACTION_FAILED") from None

    def extract(self, pdf: bytes) -> str:
        # TemporaryDirectory is private; no PDF, text, or parser error is logged.
        with tempfile.TemporaryDirectory(prefix="invoice-extract-") as directory:
            root = Path(directory)
            source = root / "invoice.pdf"
            with source.open("xb") as stream:
                stream.write(pdf)
            source.chmod(0o600)
            info = self._run("pdfinfo", [str(source)], root / "info.txt").decode("utf-8", errors="replace")
            if re.search(r"^Encrypted:\s+yes\b", info, re.MULTILINE):
                raise Failure("ENCRYPTED_PDF_UNSUPPORTED")
            page_fields = re.findall(r"^Pages:[ \t]+([0-9]{1,6})[ \t]*$", info, re.MULTILINE)
            try:
                pages = int(page_fields[0]) if len(page_fields) == 1 else 0
            except ValueError:
                pages = 0
            if not 1 <= pages <= MAX_PAGES:
                raise Failure("PDF_PAGE_LIMIT_EXCEEDED", 413)
            text = self._run("pdftotext", ["-layout", "-enc", "UTF-8", str(source), "-"], root / "text.txt")
            try:
                decoded = text.decode("utf-8")
            except UnicodeError:
                raise Failure("PDF_EXTRACTION_FAILED") from None
            if not decoded.strip():
                raise Failure("OCR_REQUIRED")
            return decoded


def parse_invoice(text: str) -> dict:
    """Conservative English-label parser, not a general invoice understanding model."""
    if not re.search(r"\binvoice\b", text, re.IGNORECASE):
        raise Failure("UNSUPPORTED_INVOICE_LAYOUT")
    lines = text.splitlines()
    warnings = []

    def field(name, pattern, convert):
        values = set()
        invalid = False
        for line in lines:
            match = re.fullmatch(pattern, line.strip(), re.IGNORECASE)
            if match:
                try:
                    values.add(convert(match[1].strip()))
                except (ValueError, InvalidOperation):
                    invalid = True
        if len(values) != 1 or invalid:
            warnings.append(name + "_missing_or_ambiguous")
            return None
        return values.pop()

    def identifier(value):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,63}", value):
            raise ValueError()
        return value

    def iso_date(value):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError()
        return date.fromisoformat(value).isoformat()

    def currency(value):
        # Deliberately small initial set. No currency is inferred from ambiguous symbols.
        if value.upper() not in {"LKR", "USD", "EUR", "GBP", "INR", "AUD", "CAD", "JPY"}:
            raise ValueError()
        return value.upper()

    def amount(value):
        if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?", value):
            raise ValueError()
        number = Decimal(value.replace(",", ""))
        if not 0 <= number <= Decimal("999999999999.99"):
            raise ValueError()
        return number

    number = field("invoice_number", r"Invoice\s+(?:Number|No\.?|#)\s*:\s*(.*)", identifier)
    invoice_date = field("invoice_date", r"Invoice\s+Date\s*:\s*(.*)", iso_date)
    unit = field("currency", r"Currency\s*:\s*(.*)", currency)
    total = field("total", r"(?:Grand\s+Total|Invoice\s+Total|Amount\s+Due|Total)\s*:\s*(.*)", amount)
    if number is None and invoice_date is None and unit is None and total is None:
        raise Failure("UNSUPPORTED_INVOICE_LAYOUT")
    try:
        total_number = float(total) if total is not None else None
    except (ValueError, OverflowError):
        total_number = None
        warnings.append("total_missing_or_ambiguous")
    return {"invoice_number": number, "invoice_date": invoice_date, "currency": unit,
            "total": total_number,
            "requires_human_review": True, "warnings": warnings}


class TextExtractor(Protocol):
    @abstractmethod
    def extract(self, pdf: bytes) -> str:
        """Return extracted UTF-8 text, or raise Failure. Implementations provide the body."""


def process(value, extractor: TextExtractor) -> dict:
    return {"result": parse_invoice(extractor.extract(decode_request(value)))}


def handler_for(secret: str, extractor: TextExtractor):
    if not 32 <= len(secret) <= 1024:
        raise ValueError("PLUGIN_API_KEY must contain 32 to 1024 characters")
    expected = hashlib.sha256(secret.encode()).digest()

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, format: str, *args):
            pass

        def reply(self, status, value):
            payload = json.dumps(value, allow_nan=False, separators=(",", ":")).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command != "HEAD":
                self.wfile.write(payload)

        def send_error(self, code, message=None, explain=None):
            # Do not reflect request-derived messages from the HTTP parser.
            status = 405 if code == 501 else code
            error_code = "METHOD_NOT_ALLOWED" if status == 405 else "INVALID_HTTP_REQUEST"
            self.reply(status, {"error": {"code": error_code}})

        def do_GET(self):
            self.reply(405, {"error": {"code": "METHOD_NOT_ALLOWED"}})

        def do_POST(self):
            try:
                if self.path != "/v1/process":
                    raise Failure("NOT_FOUND", 404)
                keys = self.headers.get_all("X-API-Key", [])
                if len(keys) != 1 or not hmac.compare_digest(hashlib.sha256(keys[0].encode()).digest(), expected):
                    raise Failure("UNAUTHORIZED", 401)
                if self.headers.get_content_type() != "application/json":
                    raise Failure("UNSUPPORTED_CONTENT_TYPE", 415)
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding", "identity") != "identity":
                    raise Failure("INVALID_REQUEST", 400)
                sizes = self.headers.get_all("Content-Length", [])
                if len(sizes) != 1 or not re.fullmatch(r"[0-9]{1,10}", sizes[0]):
                    raise Failure("INVALID_REQUEST", 400)
                size = int(sizes[0])
                if size > MAX_REQUEST:
                    raise Failure("REQUEST_TOO_LARGE", 413)
                body = self.rfile.read(size)
                if len(body) != size:
                    raise Failure("INVALID_REQUEST", 400)
                self.reply(200, process(strict_json(body), extractor))
            except Failure as error:
                self.reply(error.status, {"error": {"code": error.code}})
            except (TimeoutError, ConnectionError, OSError):
                self.close_connection = True
            except Exception:
                self.reply(500, {"error": {"code": "INTERNAL_ERROR"}})

    return Handler


def main():
    try:
        secret = os.environ.get("PLUGIN_API_KEY", "")
        handler = handler_for(secret, Extractor())
        host = os.environ.get("HOST", "127.0.0.1")
        port = int(os.environ.get("PORT", "8080"))
        with HTTPServer((host, port), handler) as server:
            print("Invoice extraction plugin ready", flush=True)
            server.serve_forever()
    except (ValueError, RuntimeError, OSError):
        print("Plugin startup failed: check local configuration and dependencies", file=sys.stderr)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
