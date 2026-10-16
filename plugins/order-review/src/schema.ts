import assessment from '../schema/ai-assessment.schema.json';
import output from '../schema/tool-output.schema.json';

/** Tool results may reach the plugin before enrichment. Native fields stay at root. */
export const toolOutputSchema = output;
export const aiAssessmentSchema = assessment;
/** Worker success responses always contain an assessment. */
export const enrichedOutputSchema = { ...output, required: ['name', 'ai_assessment'] };

export const MODEL = '@cf/cloudflare/clef-flash' as const;
export const POLICY = 'order-review-v1' as const;
export const QUEUES = ['routine', 'delivery_review', 'billing_review', 'insufficient_data'] as const;
export const PRIORITIES = ['low', 'medium', 'high', 'insufficient_data'] as const;
export type Queue = typeof QUEUES[number];
export type Priority = typeof PRIORITIES[number];
export interface Assessment {
  model: typeof MODEL;
  policy_version: typeof POLICY;
  evaluated_at: string;
  status: 'assessed' | 'insufficient_data' | 'uncertain';
  review_queue: Queue;
  review_priority: Priority;
  requires_human_review: true;
  advisory_only: true;
}
