"""Offline checks for additive Clef-flash plugin manifest preparation."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location(
    'prepare_order_review', ROOT / 'scripts/prepare-order-review.py')
assert MODULE_SPEC is not None and MODULE_SPEC.loader is not None
prepare = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(prepare)


def base_tool():
    return {
        'apiVersion': 'erpbridge.io/v1', 'kind': 'MCPTool',
        'metadata': {'name': 'get_sales_order', 'version': '1.0.0', 'module': 'erpnext'},
        'spec': {
            'inputSchema': {'type': 'object', 'required': ['name'],
                            'properties': {'name': {'type': 'string'}}},
            'execution': {
                'type': 'http', 'method': 'GET', 'endpoint': prepare.ENDPOINT,
                'mapping': {'name': 'name'}, 'parameterLocations': {'name': 'path'},
                'responsePath': 'data',
            },
            'security': {'authType': 'api-key', 'authHeader': 'Authorization',
                         'credentialRef': 'ERP_PRIMARY_KEY', 'credentialSource': 'env',
                         'dataClass': 'internal', 'allowedRoles': ['demo-reader']},
            'cache': {'enabled': True, 'ttlSeconds': 300},
        },
    }


class OrderReviewManifests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.schema = prepare.load_json(prepare.SCHEMA)

    def test_preserves_upstream_and_authorization_without_mutation(self):
        base = base_tool()
        before = deepcopy(base)
        files = prepare.render(base, 'https://order-review.demo-account.workers.dev', self.schema)
        self.assertEqual(base, before)
        tool = files['get_sales_order_with_ai_review.json']
        self.assertEqual(tool['metadata']['name'], prepare.TOOL_NAME)
        self.assertEqual(tool['spec']['execution'], base['spec']['execution'])
        self.assertEqual(tool['spec']['security'], base['spec']['security'])
        self.assertEqual(tool['spec']['inputSchema'], base['spec']['inputSchema'])
        self.assertEqual(tool['spec']['outputSchema'], self.schema)
        self.assertFalse(tool['spec']['cache']['enabled'])
        self.assertFalse(tool['spec']['annotations']['idempotentHint'])
        self.assertNotIn('ai_assessment', self.schema.get('required', []))
        self.assertTrue(files['plugin.json']['metadata']['isActive'])
        self.assertTrue(files['binding.json']['metadata']['isActive'])
        self.assertEqual(files['plugin.json']['spec']['auth']['credentialRef'],
                         'PLUGIN_ORDER_REVIEW_KEY')
        self.assertEqual(files['binding.json']['spec']['toolRef'],
                         {'name': prepare.TOOL_NAME, 'version': prepare.VERSION})
        self.assertEqual(files['binding.json']['spec']['phase'], 'after_response')
        self.assertEqual(files['binding.json']['spec']['failurePolicy'], 'fail')
        self.assertEqual(files['binding.json']['spec']['config'],
                         {'policyVersion': 'order-review-v1'})

    def test_templates_are_inactive_and_have_no_real_endpoint(self):
        plugin = prepare.load_json(prepare.TEMPLATES / 'plugin.json')
        binding = prepare.load_json(prepare.TEMPLATES / 'binding.json')
        self.assertFalse(plugin['metadata']['isActive'])
        self.assertFalse(binding['metadata']['isActive'])
        self.assertTrue(plugin['spec']['endpoint'].endswith('.invalid'))

    def test_rejects_unreviewed_origins(self):
        values = [
            'http://review.account.workers.dev', 'https://review.example.com',
            'https://review.account.workers.dev/v1/process',
            'https://secret@review.account.workers.dev',
            'https://review.account.workers.dev?secret=bad',
            'https://review.account.workers.dev#bad',
            'https://review.account.workers.dev:8443',
            'https://review.account.workers.dev:invalid',
            'https://127.0.0.1', 'https://order-review.example.invalid',
        ]
        for value in values:
            with self.subTest(origin=value), self.assertRaises(prepare.PreparationError):
                prepare.worker_origin(value)

    def test_normalizes_explicit_https_port(self):
        self.assertEqual(prepare.worker_origin('https://review.account.workers.dev:443/'),
                         'https://review.account.workers.dev')

    def test_rejects_wrong_tool_or_execution_and_literal_auth(self):
        mutations = [
            lambda b: b['metadata'].update(name='get_item'),
            lambda b: b['spec']['execution'].update(method='POST'),
            lambda b: b['spec']['execution'].update(endpoint='https://other.example/api'),
            lambda b: b['spec']['execution'].update(responsePath='other'),
            lambda b: b['spec']['security'].update(token='must-not-be-copied'),
            lambda b: b['spec']['security'].update(credentialRef='OTHER_KEY'),
            lambda b: b['spec']['inputSchema']['properties'].update(cmd={'type': 'string'}),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutate):
                base = base_tool()
                mutate(base)
                with self.assertRaises(prepare.PreparationError):
                    prepare.build_tool(base, self.schema)

    def test_rejects_required_enrichment_before_plugin(self):
        schema = deepcopy(self.schema)
        schema['required'] = ['name', 'ai_assessment']
        with self.assertRaises(prepare.PreparationError):
            prepare.build_tool(base_tool(), schema)

    def test_render_requires_fresh_ignored_output(self):
        artifact_root = ROOT / 'artifacts'
        artifact_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=artifact_root, prefix='order-review-test-') as tmp:
            folder = Path(tmp)
            source = folder / 'source.json'
            source.write_text(json.dumps(base_tool()))
            output = folder / 'rendered'
            args = ['--base-tool', str(source), '--worker-url',
                    'https://review.account.workers.dev', '--output-dir', str(output)]
            self.assertEqual(prepare.main(args), 0)
            self.assertEqual(len(list(output.glob('*.json'))), 3)
            before = (output / 'plugin.json').read_text()
            self.assertEqual(prepare.main(args), 1)
            self.assertEqual((output / 'plugin.json').read_text(), before)

    def test_rejects_output_outside_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            source = folder / 'source.json'
            source.write_text(json.dumps(base_tool()))
            output = folder / 'not-allowed'
            self.assertEqual(prepare.main([
                '--base-tool', str(source), '--worker-url',
                'https://review.account.workers.dev', '--output-dir', str(output),
            ]), 1)
            self.assertFalse(output.exists())

    def test_invalid_json_error_does_not_expose_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'bad.json'
            source.write_text('private-secret-is-not-json')
            with self.assertRaises(prepare.PreparationError) as caught:
                prepare.load_json(source)
            self.assertNotIn('private-secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
