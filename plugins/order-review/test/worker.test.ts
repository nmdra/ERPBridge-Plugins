import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createWorker, type Env } from '../src/index';
import { MODEL, POLICY, QUEUES, PRIORITIES, aiAssessmentSchema, toolOutputSchema, enrichedOutputSchema } from '../src/schema';
import assessmentSchema from '../schema/ai-assessment.schema.json';
import outputSchema from '../schema/tool-output.schema.json';

const KEY = 'local-test-only-'.repeat(3);
const NOW = '2026-03-15T12:00:00.000Z';
const order = () => ({ name: 'SO-1', status: 'To Deliver and Bill', docstatus: 1,
  transaction_date: '2026-03-01', delivery_date: '2026-03-05', per_delivered: 20, per_billed: 0 });
const envelope = (result: unknown = order()) => ({ protocolVersion: 'v1', invocationId: 'test-1',
  tool: { name: 'get_sales_order_with_ai_review', version: '1.0.0' }, result, config: { policyVersion: POLICY } });
function choice(options: readonly string[], selected: string) {
  return { type: 'choice', choice: selected, confidence: 1,
    probabilities: Object.fromEntries(options.map(k => [k, k === selected ? 1 : 0])) };
}
function model(queue = 'delivery_review', priority = 'high') {
  return { model: MODEL, answers: { review_queue: choice(QUEUES, queue), review_priority: choice(PRIORITIES, priority) },
    usage: { input_tokens: 20, output_tokens: 2 } };
}
let run = vi.fn();
let env: Env;
let outbound = vi.fn();
beforeEach(() => {
  run = vi.fn().mockResolvedValue(model());
  env = { PLUGIN_API_KEY: KEY, AI: { run } };
  outbound = vi.fn(() => { throw new Error('Outbound fetch forbidden'); });
  vi.stubGlobal('fetch', outbound);
});
afterEach(() => { expect(outbound).not.toHaveBeenCalled(); vi.unstubAllGlobals(); });
async function call(body: unknown = envelope(), init: RequestInit = {}, overrides: Partial<Env> = {}, timeoutMs = 50) {
  const response = await createWorker({ now: () => new Date(NOW), timeoutMs }).fetch(new Request('https://local/v1/process', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'X-API-Key': KEY }, body: JSON.stringify(body), ...init
  }), { ...env, ...overrides });
  return { status: response.status, data: await response.json() as any, response };
}
async function reject(body: unknown, status = 400, code = 'INVALID_REQUEST') {
  const result = await call(body);
  expect(result.status).toBe(status); expect(result.data).toEqual({ error: { code } }); expect(run).not.toHaveBeenCalled();
}

describe('local Workers runtime', () => {
  it('uses the actual Workers timing-safe API', () => { expect(typeof crypto.subtle.timingSafeEqual).toBe('function'); });
  it('preserves native fields and returns only result with a required assessment', async () => {
    const native = { ...order(), customer: 'private', nested: { untouched: [1, null] }, items: [{ qty: 1, delivery_date: '2026-03-05', item_code: 'secret' }] };
    const { status, data, response } = await call(envelope(native));
    expect(status).toBe(200); expect(Object.keys(data)).toEqual(['result']);
    const { ai_assessment, ...original } = data.result;
    expect(original).toEqual(native);
    expect(ai_assessment).toEqual({ model: MODEL, policy_version: POLICY, evaluated_at: NOW, status: 'assessed',
      review_queue: 'delivery_review', review_priority: 'high', requires_human_review: true, advisory_only: true });
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(run).toHaveBeenCalledExactlyOnceWith(MODEL, expect.objectContaining({ model: 'clef-flash', state: {
      as_of_date: '2026-03-15', status: 'to deliver and bill', docstatus: 1, transaction_date: '2026-03-01',
      delivery_date: '2026-03-05', per_delivered: 20, per_billed: 0, items: [{ qty: 1, delivery_date: '2026-03-05' }]
    } }));
    const sent = JSON.stringify(run.mock.calls[0][1].state);
    for (const secret of ['private', 'secret', 'SO-1', 'customer', 'item_code', 'nested']) expect(sent).not.toContain(secret);
  });
  it.each(['', 'wrong', 'x'.repeat(1025)])('rejects unauthorized key without AI (%s)', async key => {
    const result = await call(envelope(), { headers: { 'X-API-Key': key, 'Content-Type': 'application/json' } });
    expect(result.status).toBe(401); expect(run).not.toHaveBeenCalled();
  });
  it('rejects a missing key without AI', async () => {
    expect((await call(envelope(), { headers: { 'Content-Type': 'application/json' } })).status).toBe(401);
    expect(run).not.toHaveBeenCalled();
  });
  it.each(['short', '', ' '.repeat(32), 'x'.repeat(1025)])('bounds configured secrets', async key => {
    expect((await call(envelope(), {}, { PLUGIN_API_KEY: key })).status).toBe(503); expect(run).not.toHaveBeenCalled();
  });
  it('requires an AI function but never calls a real binding', async () => {
    expect((await call(envelope(), {}, { AI: {} as Env['AI'] })).status).toBe(503); expect(run).not.toHaveBeenCalled();
  });
  it('rejects route and method before AI', async () => {
    const worker = createWorker();
    expect((await worker.fetch(new Request('https://local/wrong'), env)).status).toBe(404);
    expect((await worker.fetch(new Request('https://local/v1/process'), env)).status).toBe(405);
    expect(run).not.toHaveBeenCalled();
  });
  it('requires JSON and valid JSON bytes', async () => {
    expect((await call(envelope(), { headers: { 'X-API-Key': KEY, 'Content-Type': 'text/plain' } })).status).toBe(415);
    expect((await call(null, { body: '{' })).status).toBe(400);
    expect((await call(null, { body: new Uint8Array([255]) })).status).toBe(400);
    expect(run).not.toHaveBeenCalled();
  });
  it.each([
    { protocolVersion: 'v2' }, { invocationId: '' }, { invocationId: 'x'.repeat(257) },
    { tool: { name: 'other', version: '1.0.0' } }, { tool: { name: 'get_sales_order_with_ai_review', version: '2' } },
    { config: { policyVersion: 'other' } }, { config: { policyVersion: POLICY, model: MODEL } }, { extra: true }
  ])('requires exact protocol/tool/config: %j', async patch => reject({ ...envelope(), ...patch }));
  it('enforces body size before parsing via content-length', async () => {
    expect((await call(envelope(), { headers: { 'X-API-Key': KEY, 'Content-Type': 'application/json', 'Content-Length': '65537' } })).status).toBe(413);
    expect(run).not.toHaveBeenCalled();
  });
  it('accepts exactly 64KiB and rejects 64KiB plus one without truncation', async () => {
    const text = JSON.stringify(envelope({ ...order(), notes: '' }));
    const exact = JSON.stringify(envelope({ ...order(), notes: 'x'.repeat(65536 - text.length) }));
    expect(new TextEncoder().encode(exact).length).toBe(65536);
    expect((await call(null, { body: exact })).status).toBe(200);
    run.mockClear();
    expect((await call(null, { body: exact + ' ' })).status).toBe(413); expect(run).not.toHaveBeenCalled();
  });
  it('bounds streaming bodies without content-length', async () => {
    let cancelled = false;
    const stream = new ReadableStream<Uint8Array>({ start(c) { c.enqueue(new Uint8Array(32768)); c.enqueue(new Uint8Array(32769)); }, cancel() { cancelled = true; } });
    expect((await call(null, { body: stream })).status).toBe(413);
    expect(cancelled).toBe(true); expect(run).not.toHaveBeenCalled();
  });
  it('rejects oversized model state instead of truncating it', async () => {
    await reject(envelope({ ...order(), items: Array.from({ length: 220 }, () => ({ delivery_date: '2026-03-05', qty: 1 })) }), 413, 'STATE_TOO_LARGE');
  });
  it('passes state just below 8KiB in full', async () => {
    const items = Array.from({ length: 180 }, () => ({ delivery_date: '2026-03-05', qty: 1 }));
    expect((await call(envelope({ ...order(), items }))).status).toBe(200);
    const state = run.mock.calls[0][1].state;
    expect(new TextEncoder().encode(JSON.stringify(state)).length).toBeLessThanOrEqual(8192);
    expect(state.items).toEqual(items);
  });
  it.each(['Draft', 'Cancelled', 'Completed', 'Closed'])('handles native %s status regardless of historical due dates', async status => {
    run.mockResolvedValue(model('routine', 'low'));
    const original = { ...order(), status, docstatus: status === 'Draft' ? 0 : status === 'Cancelled' ? 2 : 1,
      per_delivered: status === 'Completed' ? 100 : 0, per_billed: status === 'Completed' ? 100 : 0 };
    const result = await call(envelope(original));
    expect(result.status).toBe(200); expect(result.data.result.status).toBe(status);
    expect(result.data.result.ai_assessment.review_queue).toBe('routine');
  });
  it.each([
    { delivery_date: '2026-02-30' }, { transaction_date: 'not-a-date' }, { per_delivered: -1 }, { per_billed: 101 },
    { per_billed: '10' }, { docstatus: 3 }, { status: 'Unknown' }, { ai_assessment: {} },
    { items: [{ delivery_date: '2026-02-30' }] }, { items: [{ qty: -1 }] }
  ])('rejects invalid dates/percentages/lifecycle: %j', async patch => reject(envelope({ ...order(), ...patch })));
  it.each([
    { status: undefined }, { docstatus: 0 }, { status: 'Completed' }, { status: 'To Bill' }, { status: 'To Deliver' },
    { transaction_date: '2026-03-16' }, { delivery_date: '2026-02-28' }, { items: [{}] }
  ])('returns insufficient data without inference for inconsistent state: %j', async patch => {
    const result = await call(envelope({ ...order(), ...patch }));
    expect(result.status).toBe(200); expect(result.data.result.ai_assessment.status).toBe('insufficient_data');
    expect(run).not.toHaveBeenCalled();
  });
  it.each([
    ['2026-03-14', 20, 0, 'To Deliver and Bill', 'delivery_review', 'medium'],
    ['2026-03-20', 20, 0, 'To Deliver and Bill', 'billing_review', 'medium'],
    ['2026-03-20', 100, 0, 'To Bill', 'billing_review', 'medium'],
    ['2026-03-20', 20, 100, 'To Deliver', 'routine', 'low']
  ])('applies calendar policy %s', async (delivery_date, per_delivered, per_billed, status, queue, priority) => {
    run.mockResolvedValue(model(queue as string, priority as string));
    const result = await call(envelope({ ...order(), delivery_date, per_delivered, per_billed, status }));
    expect(result.status).toBe(200); expect(result.data.result.ai_assessment.review_queue).toBe(queue);
  });
  it('uses positive-quantity item dates for earlier delivery review', async () => {
    const result = await call(envelope({ ...order(), delivery_date: '2026-03-20', items: [{ qty: 2, delivery_date: '2026-03-02' }] }));
    expect(result.status).toBe(200); expect(result.data.result.ai_assessment.review_priority).toBe('high');
  });
  it.each(['identity', 'extra', 'usage', 'fractional', 'negative', 'probabilities', 'sum', 'winner', 'confidence', 'answer'])('rejects malformed model %s', async kind => {
    const value: any = model();
    if (kind === 'identity') value.model = '@cf/other';
    if (kind === 'extra') value.text = 'unsafe';
    if (kind === 'usage') delete value.usage;
    if (kind === 'fractional') value.usage.input_tokens = 1.5;
    if (kind === 'negative') value.usage.output_tokens = -1;
    if (kind === 'probabilities') value.answers.review_queue.probabilities.routine = -1;
    if (kind === 'sum') value.answers.review_queue.probabilities.delivery_review = 0.8;
    if (kind === 'winner') value.answers.review_queue.choice = 'routine';
    if (kind === 'confidence') value.answers.review_queue.confidence = 2;
    if (kind === 'answer') value.answers.review_priority.type = 'text';
    run.mockResolvedValue(value);
    const result = await call(); expect(result.status).toBe(502); expect(result.data).toEqual({ error: { code: 'MODEL_INVALID' } });
  });
  it('accepts the short model identity but reports the fixed canonical identity', async () => {
    run.mockResolvedValue({ ...model(), model: 'clef-flash' });
    expect((await call()).data.result.ai_assessment.model).toBe(MODEL);
  });
  it.each(['confidence', 'probability', 'margin'])('uses uncertainty for low %s', async kind => {
    const value = model();
    if (kind === 'confidence') value.answers.review_queue.confidence = 0.6;
    else value.answers.review_queue.probabilities = { routine: kind === 'margin' ? 0.3 : 0.4, delivery_review: kind === 'margin' ? 0.4 : 0.6, billing_review: kind === 'margin' ? 0.3 : 0, insufficient_data: 0 };
    run.mockResolvedValue(value);
    const assessment = (await call()).data.result.ai_assessment;
    expect(assessment.status).toBe('uncertain'); expect(assessment.review_queue).toBe('insufficient_data');
  });
  it('honors model insufficient-data abstention', async () => {
    run.mockResolvedValue(model('insufficient_data', 'insufficient_data'));
    expect((await call()).data.result.ai_assessment.status).toBe('insufficient_data');
  });
  it('rejects confident decisions incompatible with the deterministic guard', async () => {
    run.mockResolvedValue(model('routine', 'high'));
    expect((await call()).data).toEqual({ error: { code: 'MODEL_INVALID' } });
  });
  it('bounds inference wait without exposing internal failures', async () => {
    run.mockImplementation(() => new Promise(() => {}));
    const result = await call(envelope(), {}, {}, 5);
    expect(result.status).toBe(504); expect(result.data).toEqual({ error: { code: 'MODEL_TIMEOUT' } });
  });
  it('redacts AI rejection details', async () => {
    run.mockRejectedValue(new Error('secret ERP token stack'));
    const result = await call(); expect(result.status).toBe(502);
    expect(result.data).toEqual({ error: { code: 'MODEL_UNAVAILABLE' } });
  });
  it('keeps published JSON schemas and enriched requirements in agreement', () => {
    expect(aiAssessmentSchema).toEqual(assessmentSchema); expect(toolOutputSchema).toEqual(outputSchema);
    expect(outputSchema.properties.ai_assessment).toEqual(assessmentSchema);
    expect(assessmentSchema.properties.model.const).toBe(MODEL);
    expect(assessmentSchema.properties.policy_version.const).toBe(POLICY);
    expect(assessmentSchema.properties.review_queue.enum).toEqual([...QUEUES]);
    expect(assessmentSchema.properties.review_priority.enum).toEqual([...PRIORITIES]);
    expect(toolOutputSchema.required).toEqual(['name']); expect(enrichedOutputSchema.required).toEqual(['name', 'ai_assessment']);
    expect(assessmentSchema.required.toSorted()).toEqual(Object.keys(assessmentSchema.properties).toSorted());
  });
});
