#!/usr/bin/env python3
"""Render additive plugin manifests locally. Never contact ERPBridge or Cloudflare."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
TOOL_NAME = 'get_sales_order_with_ai_review'
VERSION = '1.0.0'
ENDPOINT = 'https://erpnext.nimendra.online/api/resource/Sales%20Order/{name}'
SCHEMA = ROOT / 'plugins/order-review/schema/tool-output.schema.json'
TEMPLATES = ROOT / 'manifests/erpnext-order-review'


class PreparationError(Exception):
    """Safe error whose message never includes file contents or credentials."""


def load_json(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            raise PreparationError('Input JSON exceeds the local size limit.')
        return json.loads(path.read_text())
    except (OSError, UnicodeError, ValueError):
        raise PreparationError('Cannot read a valid local JSON input.') from None


def worker_origin(value):
    """Only the standard Worker host is in scope, not arbitrary plugin URLs."""
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ''
        valid_host = re.fullmatch(r'[a-z0-9-]+\.[a-z0-9-]+\.workers\.dev', host)
        if (parsed.scheme != 'https' or not valid_host or parsed.username
                or parsed.password or parsed.port not in (None, 443)
                or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise PreparationError('Use an HTTPS workers.dev origin without a path or credentials.')
        return f'https://{host}'
    except ValueError:
        raise PreparationError('Invalid Worker origin.') from None


def build_tool(base, schema):
    """Copy a reviewed local get_sales_order export without widening security."""
    try:
        if base['apiVersion'] != 'erpbridge.io/v1' or base['kind'] != 'MCPTool':
            raise PreparationError('Expected an MCPTool export.')
        if base['metadata']['name'] != 'get_sales_order':
            raise PreparationError('Expected the existing get_sales_order tool.')
        spec = base['spec']
        execution = spec['execution']
        allowed_execution = {'type', 'method', 'endpoint', 'responsePath',
                             'mapping', 'parameterLocations'}
        if (set(execution) - allowed_execution
                or execution['type'] != 'http' or execution['method'] != 'GET'
                or execution['endpoint'] != ENDPOINT
                or execution.get('responsePath') != 'data'
                or execution.get('mapping') != {'name': 'name'}
                or execution.get('parameterLocations') != {'name': 'path'}):
            raise PreparationError('Unexpected Sales Order execution contract; review before preparing.')
        security = spec['security']
        allowed_security = {'authType', 'authHeader', 'credentialRef', 'credentialSource',
                            'dataClass', 'allowedRoles'}
        if (not isinstance(security, dict) or set(security) - allowed_security
                or security.get('authType') != 'api-key'
                or security.get('authHeader') != 'Authorization'
                or security.get('credentialRef') != 'ERP_PRIMARY_KEY'
                or security.get('credentialSource', 'env') not in ('env', 'file')):
            raise PreparationError('Expected reference-only existing ERP security settings.')
        inputs = spec['inputSchema']
        if (inputs.get('type') != 'object' or inputs.get('required') != ['name']
                or set(inputs.get('properties', {})) != {'name'}
                or inputs['properties']['name'].get('type') != 'string'):
            raise PreparationError('Unexpected Sales Order input schema.')
        if (schema.get('type') != 'object'
                or 'ai_assessment' not in schema.get('properties', {})
                or 'ai_assessment' in schema.get('required', [])):
            raise PreparationError('Tool schema must accept the plain pre-plugin Sales Order.')
        # Rebuild the resource from approved sections rather than copying hidden extras.
        tool = {
            'apiVersion': 'erpbridge.io/v1', 'kind': 'MCPTool',
            'metadata': {
                'name': TOOL_NAME, 'version': VERSION,
                'module': base['metadata'].get('module', 'erpnext'),
            },
            'spec': {
                'description': {
                    'short': 'Read a Sales Order with an advisory Clef-flash review.',
                    'whenToUse': ['Review the supplied order for delivery or billing follow-up.'],
                    'whenNotToUse': [
                        'Do not approve orders or infer stock, payment, or creditworthiness.',
                        'No assessment is accepted unless the exact plugin binding is active.',
                    ],
                    'examples': ['Read a demo Sales Order and suggest a human review queue.'],
                },
                'annotations': {
                    'readOnlyHint': True, 'destructiveHint': False,
                    'idempotentHint': False, 'openWorldHint': True,
                },
                'inputSchema': deepcopy(inputs), 'outputSchema': deepcopy(schema),
                'execution': deepcopy(execution), 'security': deepcopy(security),
                'cache': {'enabled': False, 'ttlSeconds': 0,
                          'isReadOnly': True, 'flushOn': []},
            },
        }
        return tool
    except (KeyError, TypeError, AttributeError):
        raise PreparationError('Incomplete local Sales Order manifest or output schema.') from None


def render(base, origin, schema):
    origin = worker_origin(origin)
    plugin = load_json(TEMPLATES / 'plugin.json')
    binding = load_json(TEMPLATES / 'binding.json')
    plugin['metadata']['isActive'] = True
    plugin['spec']['endpoint'] = origin
    binding['metadata']['isActive'] = True
    return {
        f'{TOOL_NAME}.json': build_tool(base, schema),
        'plugin.json': plugin, 'binding.json': binding,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-tool', type=Path, required=True,
                        help='Reviewed local JSON export of existing get_sales_order.')
    parser.add_argument('--worker-url', required=True,
                        help='Actual account-issued HTTPS workers.dev origin.')
    parser.add_argument('--output-dir', type=Path, default=ROOT / 'artifacts/order-review')
    args = parser.parse_args(argv)
    try:
        files = render(load_json(args.base_tool), args.worker_url, load_json(SCHEMA))
        # Keep prepared manifests out of tracked sources and prevent accidental overwrites.
        output = args.output_dir.resolve()
        artifact_root = (ROOT / 'artifacts').resolve()
        if not output.is_relative_to(artifact_root) or output == artifact_root:
            raise PreparationError('Output directory must be a child of ignored artifacts/.')
        output.mkdir(parents=True, exist_ok=True, mode=0o700)
        paths = [output / name for name in files]
        if any(path.exists() or path.is_symlink() for path in paths):
            raise PreparationError('Output exists; choose a fresh artifacts/ subdirectory.')
        for name, content in files.items():
            with (output / name).open('x', encoding='utf-8') as handle:
                handle.write(json.dumps(content, indent=2, allow_nan=False) + '\n')
        print('Prepared three local manifests. No resources were applied.')
        print('Before deployment, review preserved security, output schema, and active binding.')
        return 0
    except PreparationError as error:
        print(f'Preparation failed: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
