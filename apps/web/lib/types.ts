export type Compliance = {
  status: "PASS" | "FAIL" | "REVIEW_REQUIRED" | null;
  reasons: { rule_id: number | null; kind: string; outcome: string; message: string; stage: string }[];
};

export type ExpectedValue = {
  expected_views_median?: number;
  p_qualify?: number;
  ev_per_post?: number;
  ev_per_render?: number;
  ev_per_compute_minute?: number;
  p10?: number;
  p90?: number;
  basis?: string;
  currency?: string;
  note?: string;
};

export type Candidate = {
  id: number;
  campaign_id: number | null;
  source_id: number;
  start: number;
  end: number;
  duration: number;
  title: string | null;
  hook: string | null;
  hook_type: string | null;
  topic: string | null;
  origin: string;
  status: string;
  rank_score: number | null;
  content_score: number | null;
  confidence: number | null;
  scorer: string | null;
  scores: Record<string, number | null>;
  performance_prior: number | null;
  compliance: Compliance;
  expected_value: ExpectedValue;
  reason: string | null;
  transcript?: string;
  context_before?: string;
  explanations?: Record<string, unknown>;
  opens_with?: string;
  ends_with?: string;
  warnings?: string[];
};

export type ClipVersion = {
  id: number;
  version: number;
  status: string;
  duration: number | null;
  width: number | null;
  height: number | null;
  layout: string | null;
  edit_summary: Record<string, unknown>;
  spec: Record<string, unknown>;
  render_seconds: number | null;
  error: string | null;
};

export type Clip = {
  id: number;
  candidate_id: number;
  campaign_id: number | null;
  status: string;
  title: string | null;
  description: string | null;
  hashtags: string[];
  platform_metadata: Record<string, { title?: string; caption?: string; description?: string; hashtags?: string[] }>;
  current_version: ClipVersion | null;
  video_url: string | null;
  thumbnail_url: string | null;
  review_notes: string | null;
};

export type ReviewCard = {
  clip_id: number;
  status: string;
  title: string | null;
  description: string | null;
  hashtags: string[];
  platform_metadata: Clip["platform_metadata"];
  video_url: string | null;
  thumbnail_url: string | null;
  duration: number | null;
  layout: string | null;
  version: number | null;
  edit_summary: Record<string, unknown> | null;
  candidate: Candidate;
  compliance: Compliance;
  expected_value: ExpectedValue;
};

export type Job = {
  id: number;
  kind: string;
  status: "queued" | "running" | "completed" | "failed" | "cancelled";
  progress: number;
  message: string | null;
  result: Record<string, unknown> | null;
  error: { type: string; message: string; traceback?: string } | null;
  attempts: number;
  max_attempts: number;
  created_at: string | null;
  logs?: { at: string; level: string; message: string }[];
};

export type CampaignRule = { id: number; kind: string; params: Record<string, unknown>; severity: string; origin: string; description: string };

export type Campaign = {
  id: number;
  slug: string;
  name: string;
  description: string | null;
  provider: string | null;
  cpm: number;
  currency: string;
  min_qualified_views: number;
  max_payout_per_clip: number | null;
  budget: number | null;
  tracking_window_days: number | null;
  platforms: string[];
  min_duration: number;
  max_duration: number;
  hashtags: string[];
  brief: string | null;
  human_approval_required: boolean;
  weights: Record<string, number>;
  rules: CampaignRule[];
};

export type Source = {
  id: number;
  campaign_id: number | null;
  title: string;
  duration: number;
  width: number | null;
  height: number | null;
  status: string;
  rights_status: string;
  rights_basis: string | null;
  n_candidates: number;
  created_at: string | null;
  media_url?: string | null;
  analysis?: { products: Record<string, Record<string, unknown>> };
};

export type Post = {
  id: number;
  clip_id: number;
  platform: string;
  provider: string;
  status: string;
  visibility: string;
  url: string | null;
  scheduled_at: string | null;
  published_at: string | null;
  error: string | null;
  metrics?: { views: number | null; likes: number | null; comments: number | null; provider: string } | null;
};

export type Earnings = {
  campaign: string;
  currency: string;
  cpm: number;
  min_qualified_views: number;
  views: number;
  qualified_views: number;
  qualifying_posts: number;
  gross_estimated: number;
  budget_capped: boolean;
  confirmed: number;
  margin_estimate: number;
  costs: { total_usd: number; by_kind: Record<string, { usd: number; quantity: number; unit: string }> };
  posts: { post_id: number; platform: string; views: number; qualified_views: number; estimated: number; confirmed: number | null; metrics_provider: string | null }[];
};
