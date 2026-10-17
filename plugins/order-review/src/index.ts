import { MODEL, POLICY, QUEUES, PRIORITIES, type Assessment, type Queue, type Priority } from './schema.js';

export interface Env {
  PLUGIN_API_KEY: string;
  AI: { run(model: string, request: unknown): Promise<unknown> };
}
type Obj = Record<string, unknown>;
class Failure extends Error {
  constructor(readonly code: string, readonly http: number) { super(code); }
}
const BODY_LIMIT = 64 * 1024;
const STATE_LIMIT = 8 * 1024;
const DEADLINE_MS = 8000;
const STATUSES = ['draft', 'on hold', 'to deliver and bill', 'to bill', 'to deliver', 'completed', 'cancelled', 'closed'];
const obj = (v: unknown): v is Obj => v !== null && typeof v === 'object' && !Array.isArray(v);
const finite = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const own = (v: Obj, key: string) => Object.hasOwn(v, key);
function invalid(): never { throw new Failure('INVALID_REQUEST', 400); }
function keys(v: Obj, expected: string[]): boolean {
  return Object.keys(v).length === expected.length && expected.every(k => own(v, k));
}
function date(v: unknown): v is string {
  if (typeof v !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(v)) return false;
  const parsed = new Date(v + 'T00:00:00.000Z');
  return Number.isFinite(parsed.getTime()) && parsed.toISOString().slice(0, 10) === v;
}
function optional(v: Obj, key: string, valid: (value: unknown) => boolean) {
  if (own(v, key) && !valid(v[key])) invalid();
}
function validateOrder(value: unknown): Obj {
  if (!obj(value) || typeof value.name !== 'string' || !value.name.trim() || own(value, 'ai_assessment')) invalid();
  optional(value, 'status', v => typeof v === 'string' && STATUSES.includes(v.toLowerCase()));
  optional(value, 'docstatus', v => v === 0 || v === 1 || v === 2);
  for (const k of ['delivery_date', 'transaction_date']) optional(value, k, date);
  optional(value, 'grand_total', v => finite(v) && v >= 0);
  optional(value, 'currency', v => typeof v === 'string' && /^[A-Z]{3}$/.test(v));
  for (const k of ['per_delivered', 'per_billed']) optional(value, k, v => finite(v) && v >= 0 && v <= 100);
  optional(value, 'items', v => Array.isArray(v) && v.every(item => {
    if (!obj(item)) return false;
    optional(item, 'delivery_date', date);
    optional(item, 'qty', q => finite(q) && q >= 0);
    return true;
  }));
  return value;
}
async function readBody(request: Request): Promise<unknown> {
  const length = request.headers.get('content-length');
  if (length && (!/^\d+$/.test(length) || Number(length) > BODY_LIMIT)) throw new Failure('BODY_TOO_LARGE', 413);
  if (!request.body) invalid();
  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let size = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      size += value.byteLength;
      if (size > BODY_LIMIT) { await reader.cancel(); throw new Failure('BODY_TOO_LARGE', 413); }
      chunks.push(value);
    }
  } finally { reader.releaseLock(); }
  const bytes = new Uint8Array(size);
  let offset = 0;
  for (const chunk of chunks) { bytes.set(chunk, offset); offset += chunk.byteLength; }
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true, ignoreBOM: false }).decode(bytes)); }
  catch { return invalid(); }
}
function assessment(at: string, status: Assessment['status'], queue: Queue = 'insufficient_data', priority: Priority = 'insufficient_data'): Assessment {
  return { model: MODEL, policy_version: POLICY, evaluated_at: at, status, review_queue: queue,
    review_priority: priority, requires_human_review: true, advisory_only: true };
}
/** Only normalized enum, numeric, and calendar data can enter the model state. */
function stateFor(order: Obj, asOf: string): Obj {
  const state: Obj = { as_of_date: asOf };
  for (const key of ['status', 'docstatus', 'delivery_date', 'transaction_date', 'per_delivered', 'per_billed']) {
    if (own(order, key)) state[key] = key === 'status' ? (order[key] as string).toLowerCase() : order[key];
  }
  if (Array.isArray(order.items)) state.items = order.items.map(item => {
    const safe: Obj = {};
    for (const key of ['delivery_date', 'qty']) if (own(item as Obj, key)) safe[key] = (item as Obj)[key];
    return safe;
  });
  if (new TextEncoder().encode(JSON.stringify(state)).byteLength > STATE_LIMIT) throw new Failure('STATE_TOO_LARGE', 413);
  return state;
}
/** Deterministic policy guard. The model cannot add unsupported conclusions. */
function expected(state: Obj): { queue: Queue; priority: Priority } | undefined {
  if (!['status', 'docstatus', 'delivery_date', 'transaction_date', 'per_delivered', 'per_billed'].every(k => own(state, k))) return;
  const { status, docstatus, delivery_date, transaction_date, per_delivered, per_billed } = state;
  if ((transaction_date as string) > (delivery_date as string) || (transaction_date as string) > (state.as_of_date as string)) return;
  if ((status === 'draft' && docstatus !== 0) || (status === 'cancelled' && docstatus !== 2) ||
      (!['draft', 'cancelled'].includes(status as string) && docstatus !== 1)) return;
  if (status === 'completed' && (per_delivered !== 100 || per_billed !== 100)) return;
  if (status === 'to bill' && (per_delivered !== 100 || per_billed === 100)) return;
  if (status === 'to deliver' && (per_billed !== 100 || per_delivered === 100)) return;
  if (status === 'to deliver and bill' && (per_delivered === 100 || per_billed === 100)) return;
  if (Array.isArray(state.items) && state.items.some(item => !own(item as Obj, 'delivery_date') || !own(item as Obj, 'qty') ||
      ((item as Obj).delivery_date as string) < (transaction_date as string))) return;
  if (['draft', 'cancelled', 'closed', 'completed'].includes(status as string)) return { queue: 'routine', priority: 'low' };
  const dates = [delivery_date as string];
  if (Array.isArray(state.items)) for (const item of state.items) {
    if (((item as Obj).qty as number) > 0) dates.push((item as Obj).delivery_date as string);
  }
  const days = Math.max(...dates.map(d => (Date.parse(state.as_of_date as string) - Date.parse(d)) / 86400000));
  if ((per_delivered as number) < 100 && days > 0) return { queue: 'delivery_review', priority: days > 7 ? 'high' : 'medium' };
  if ((per_billed as number) < 100) return { queue: 'billing_review', priority: 'medium' };
  return { queue: 'routine', priority: 'low' };
}
const INSTRUCTIONS = `Apply order-review-v1 using only state calendar dates and percentages. Never infer stock, payment default, credit, payment terms, customer risk or monetary value risk. Missing or inconsistent required status, docstatus, transaction_date, delivery_date, per_delivered or per_billed means insufficient_data. Item entries, if supplied, require valid delivery_date and qty. Draft (docstatus 0), cancelled (2), closed and completed orders are routine/low regardless of historical delivery dates. Completed requires both percentages 100. Other statuses require docstatus 1. transaction_date must not exceed delivery_date or as_of_date. To bill requires per_delivered 100 and per_billed <100; to deliver requires per_billed 100 and per_delivered <100; to deliver and bill requires both <100. For active orders with per_delivered <100, delivery_date or a positive-qty item date earlier than as_of_date means delivery_review; high only when the earliest such date is more than 7 days earlier, otherwise medium. Otherwise per_billed <100 means billing_review/medium, not overdue payment. Otherwise routine/low. Use only the fixed choice options.`;
function requestFor(state: Obj): Obj {
  return { model: 'clef-flash', state, questions: {
    review_queue: { type: 'choice', instructions: INSTRUCTIONS, criteria: {
      routine: 'Inactive or completed order, or no delivery or billing review condition.',
      delivery_review: 'Active order with incomplete delivery and a past delivery date.',
      billing_review: 'Active order with incomplete billing and no delivery-review condition. Not payment default.',
      insufficient_data: 'Missing or inconsistent required calendar, lifecycle or percentage facts.' } },
    review_priority: { type: 'choice', instructions: INSTRUCTIONS, criteria: {
      low: 'Routine queue, including draft, cancelled, closed and completed.',
      medium: 'Delivery at most 7 days past due or billing review.',
      high: 'Active incomplete delivery more than 7 days past due.',
      insufficient_data: 'Missing or inconsistent required assessment facts.' } }
  } };
}
function answer<T extends string>(value: unknown, options: readonly T[]): { choice: T; uncertain: boolean } {
  if (!obj(value) || !keys(value, ['type', 'choice', 'probabilities', 'confidence']) || value.type !== 'choice' ||
      !options.includes(value.choice as T) || !finite(value.confidence) || value.confidence < 0 || value.confidence > 1 ||
      !obj(value.probabilities) || !keys(value.probabilities, [...options])) throw new Failure('MODEL_INVALID', 502);
  const probabilities = value.probabilities;
  const scores = options.map(k => probabilities[k]);
  if (!scores.every(v => finite(v) && v >= 0 && v <= 1)) throw new Failure('MODEL_INVALID', 502);
  const sorted = (scores as number[]).toSorted((a, b) => b - a);
  if (Math.abs((scores as number[]).reduce((a, b) => a + b, 0) - 1) > 0.001 || probabilities[value.choice as string] !== sorted[0]) throw new Failure('MODEL_INVALID', 502);
  return { choice: value.choice as T, uncertain: sorted[0] < 0.7 || sorted[0] - sorted[1] < 0.15 || value.confidence < 0.7 };
}
function parseModel(value: unknown): { queue: Queue; priority: Priority; uncertain: boolean } {
  if (!obj(value) || !keys(value, ['model', 'answers', 'usage']) || ![MODEL, 'clef-flash'].includes(value.model as string) ||
      !obj(value.answers) || !keys(value.answers, ['review_queue', 'review_priority']) || !obj(value.usage) ||
      !keys(value.usage, ['input_tokens', 'output_tokens']) || !Object.values(value.usage).every(v => finite(v) && Number.isSafeInteger(v) && v >= 0)) throw new Failure('MODEL_INVALID', 502);
  const queue = answer(value.answers.review_queue, QUEUES);
  const priority = answer(value.answers.review_priority, PRIORITIES);
  return { queue: queue.choice, priority: priority.choice, uncertain: queue.uncertain || priority.uncertain };
}
function validAssessment(value: Assessment): boolean {
  return value.model === MODEL && value.policy_version === POLICY && Number.isFinite(Date.parse(value.evaluated_at)) &&
    ['assessed', 'insufficient_data', 'uncertain'].includes(value.status) && QUEUES.includes(value.review_queue) &&
    PRIORITIES.includes(value.review_priority) && value.requires_human_review === true && value.advisory_only === true &&
    (value.status === 'assessed' ? value.review_queue !== 'insufficient_data' && value.review_priority !== 'insufficient_data' :
      value.review_queue === 'insufficient_data' && value.review_priority === 'insufficient_data');
}
function reply(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' } });
}
const SECRET_LIMIT = 1024;
async function sameSecret(a: string, b: string): Promise<boolean> {
  const encoder = new TextEncoder();
  const [left, right] = await Promise.all([
    crypto.subtle.digest('SHA-256', encoder.encode(a)),
    crypto.subtle.digest('SHA-256', encoder.encode(b))
  ]);
  return crypto.subtle.timingSafeEqual(left, right);
}
export function createWorker(options: { now?: () => Date; timeoutMs?: number } = {}) {
  return { async fetch(request: Request, env: Env): Promise<Response> {
    try {
      if (new URL(request.url).pathname !== '/v1/process') throw new Failure('NOT_FOUND', 404);
      if (request.method !== 'POST') throw new Failure('METHOD_NOT_ALLOWED', 405);
      if (typeof env.PLUGIN_API_KEY !== 'string' || env.PLUGIN_API_KEY.length < 32 || env.PLUGIN_API_KEY.length > SECRET_LIMIT || !env.PLUGIN_API_KEY.trim() || !env.AI || typeof env.AI.run !== 'function') throw new Failure('CONFIGURATION_ERROR', 503);
      const supplied = request.headers.get('X-API-Key') ?? '';
      if (supplied.length > SECRET_LIMIT || !await sameSecret(supplied, env.PLUGIN_API_KEY)) throw new Failure('UNAUTHORIZED', 401);
      if (request.headers.get('content-type')?.split(';')[0].trim().toLowerCase() !== 'application/json') throw new Failure('UNSUPPORTED_MEDIA_TYPE', 415);
      const body = await readBody(request);
      if (!obj(body) || !keys(body, ['protocolVersion', 'invocationId', 'tool', 'result', 'config']) || body.protocolVersion !== 'v1' ||
          typeof body.invocationId !== 'string' || !body.invocationId.trim() || body.invocationId.length > 256 ||
          !obj(body.tool) || !keys(body.tool, ['name', 'version']) || body.tool.name !== 'get_sales_order_with_ai_review' || body.tool.version !== '1.0.0' ||
          !obj(body.config) || !keys(body.config, ['policyVersion']) || body.config.policyVersion !== POLICY) invalid();
      const order = validateOrder(body.result);
      const at = (options.now?.() ?? new Date()).toISOString();
      const state = stateFor(order, at.slice(0, 10));
      const target = expected(state);
      let result = assessment(at, 'insufficient_data');
      if (target) {
        let timer: ReturnType<typeof setTimeout> | undefined;
        let raw: unknown;
        try {
          raw = await Promise.race([
            Promise.resolve().then(() => env.AI.run(MODEL, requestFor(state))),
            new Promise<never>((_, reject) => { timer = setTimeout(() => reject(new Failure('MODEL_TIMEOUT', 504)), options.timeoutMs ?? DEADLINE_MS); })
          ]);
        } catch (e) {
          if (e instanceof Failure) throw e;
          throw new Failure('MODEL_UNAVAILABLE', 502);
        } finally { if (timer !== undefined) clearTimeout(timer); }
        const model = parseModel(raw);
        if (model.uncertain) result = assessment(at, 'uncertain');
        else if (model.queue === 'insufficient_data' || model.priority === 'insufficient_data') result = assessment(at, 'insufficient_data');
        else {
          if (model.queue !== target.queue || model.priority !== target.priority) throw new Failure('MODEL_INVALID', 502);
          result = assessment(at, 'assessed', model.queue, model.priority);
        }
      }
      if (!validAssessment(result)) throw new Failure('INTERNAL_ERROR', 500);
      return reply({ result: { ...order, ai_assessment: result } });
    } catch (error) {
      const safe = error instanceof Failure ? error : new Failure('INTERNAL_ERROR', 500);
      return reply({ error: { code: safe.code } }, safe.http);
    }
  } };
}
export default createWorker();
