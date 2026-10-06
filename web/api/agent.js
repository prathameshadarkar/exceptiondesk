/**
 * Serverless model endpoint — the only place an API key exists.
 *
 * The browser posts {system, user, tier}. It never sees a key and never names a
 * model: which model serves which tier is a server concern, so changing it is a
 * config change and not a redeploy of the page.
 *
 * Mirrors exception_desk/providers.py. Two tiers:
 *   fast   — drafting and translation (broker note, PM brief, triage)
 *   reason — the handover judge, the one agent doing real evaluation
 *
 * Environment (Vercel → Project → Settings → Environment Variables):
 *   OPENAI_API_KEY     …or ANTHROPIC_API_KEY
 *   MODEL_FAST         optional override, e.g. gpt-6-luna
 *   MODEL_REASON       optional override, e.g. gpt-6-sol
 *   MODEL_PROVIDER     optional, "openai" | "anthropic"
 */
export const config = { runtime: 'edge' };

const MAX_OUTPUT = 1400;

const DEFAULTS = {
  openai: {
    fast: 'gpt-6-luna',
    reason: 'gpt-6-sol',
    url: 'https://api.openai.com/v1/responses',
    keyEnv: 'OPENAI_API_KEY',
  },
  anthropic: {
    fast: 'claude-haiku-4-5-20251001',
    reason: 'claude-sonnet-5',
    url: 'https://api.anthropic.com/v1/messages',
    keyEnv: 'ANTHROPIC_API_KEY',
  },
};

function activeProvider() {
  const explicit = (process.env.MODEL_PROVIDER || '').trim().toLowerCase();
  if (DEFAULTS[explicit]) return explicit;
  if (process.env.OPENAI_API_KEY) return 'openai';
  if (process.env.ANTHROPIC_API_KEY) return 'anthropic';
  return 'openai';
}

const modelFor = (tier, provider) =>
  process.env[`MODEL_${tier.toUpperCase()}`] || DEFAULTS[provider][tier];

function buildRequest(system, user, tier, provider) {
  const conf = DEFAULTS[provider];
  const key = process.env[conf.keyEnv];
  const model = modelFor(tier, provider);

  if (provider === 'openai') {
    return {
      url: conf.url,
      headers: { 'content-type': 'application/json', authorization: `Bearer ${key}` },
      // OpenAI carries the system prompt as a message rather than a top-level
      // field — the main shape difference from Anthropic.
      body: {
        model,
        input: [
          { role: 'developer', content: system },
          { role: 'user', content: user },
        ],
        max_output_tokens: MAX_OUTPUT,
      },
    };
  }
  return {
    url: conf.url,
    headers: {
      'content-type': 'application/json',
      'x-api-key': key,
      'anthropic-version': '2023-06-01',
    },
    body: { model, max_tokens: MAX_OUTPUT, system, messages: [{ role: 'user', content: user }] },
  };
}

/** Walk several known response shapes rather than indexing one path. A shape
 *  change should degrade to a visible error, never to a blank panel. */
function extractText(data) {
  if (!data || typeof data !== 'object') return '';
  if (typeof data.output_text === 'string' && data.output_text.trim()) return data.output_text;

  const parts = [];
  for (const item of data.output || []) {
    for (const b of (item && item.content) || []) {
      if (b && (b.type === 'output_text' || b.type === 'text') && b.text) parts.push(b.text);
    }
  }
  if (parts.length) return parts.join('\n');

  for (const c of data.choices || []) {
    const m = (c && c.message) || {};
    if (typeof m.content === 'string' && m.content) parts.push(m.content);
  }
  if (parts.length) return parts.join('\n');

  for (const b of data.content || []) {
    if (b && b.type === 'text' && b.text) parts.push(b.text);
  }
  return parts.join('\n');
}

const usageOf = (d) => {
  const u = (d && d.usage) || {};
  return {
    input: u.input_tokens ?? u.prompt_tokens ?? 0,
    output: u.output_tokens ?? u.completion_tokens ?? 0,
  };
};

export default async function handler(req) {
  if (req.method !== 'POST') return json({ error: 'POST only' }, 405);

  const provider = activeProvider();
  const conf = DEFAULTS[provider];
  if (!process.env[conf.keyEnv]) {
    return json({ error: 'no_key', message: `${conf.keyEnv} is not set on the server.` }, 503);
  }

  let payload;
  try { payload = await req.json(); } catch { return json({ error: 'bad_json' }, 400); }
  const { system, user } = payload || {};
  const tier = payload && payload.tier === 'reason' ? 'reason' : 'fast';
  if (typeof system !== 'string' || typeof user !== 'string') {
    return json({ error: 'bad_request', message: 'system and user are required strings' }, 400);
  }
  // This endpoint is public on a deployed site: cap what it will forward.
  if (system.length + user.length > 24000) return json({ error: 'too_large' }, 413);

  const { url, headers, body } = buildRequest(system, user, tier, provider);

  let r = await fetch(url, { method: 'POST', headers, body: JSON.stringify(body) });

  // The output-cap parameter name differs between API generations and is not
  // load bearing — the prompts already constrain length. Retry once without it.
  if (r.status === 400) {
    const detail = await r.clone().text();
    if (detail.includes('max_output_tokens') || detail.includes('max_tokens')) {
      const retry = { ...body };
      delete retry.max_output_tokens;
      delete retry.max_tokens;
      r = await fetch(url, { method: 'POST', headers, body: JSON.stringify(retry) });
    }
  }

  if (!r.ok) {
    const detail = await r.text();
    return json({ error: 'upstream', status: r.status, detail: detail.slice(0, 500) }, 502);
  }

  const data = await r.json();
  return json({ text: extractText(data), usage: usageOf(data), model: body.model, provider });
}

function json(obj, status = 200) {
  return new Response(JSON.stringify(obj), {
    status, headers: { 'content-type': 'application/json' },
  });
}
