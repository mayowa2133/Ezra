"use client";

import Link from "next/link";
import { useState } from "react";

import { ErrorNote } from "@/components/ui";
import { money, num, post, useApi } from "@/lib/api";
import type { Campaign } from "@/lib/types";

const EXAMPLE = `campaign:
  id: my-campaign
  name: My Campaign
  cpm: 2.00
  minimum_views: 5000
  maximum_payout: 1000
  platforms: [tiktok, instagram, youtube]
  min_duration: 15
  max_duration: 60
  hashtags: ["#mycampaign"]
  forbidden: [profanity, competitor mentions]
  competitors: []
  brief: What this campaign wants.`;

export default function Campaigns() {
  const { data, error, reload } = useApi<Campaign[]>("/campaigns");
  const [text, setText] = useState(EXAMPLE);
  const [format, setFormat] = useState("");
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function importIt() {
    setErr(null);
    try {
      const out = await post<Campaign[]>("/campaigns/import", { text, format: format || null });
      setMsg(`imported ${out.map((c) => c.slug).join(", ")}`);
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  return (
    <>
      <h1>Campaigns</h1>
      <ErrorNote error={error} />
      <div className="panel">
        <table>
          <thead><tr><th>campaign</th><th>provider</th><th>CPM</th><th>min views</th><th>max / clip</th><th>budget</th><th>platforms</th><th>rules</th></tr></thead>
          <tbody>
            {data?.map((c) => (
              <tr key={c.id}>
                <td><Link href={`/campaigns/${c.slug}`}>{c.name}</Link> <span className="muted small">{c.slug}</span></td>
                <td>{c.provider ?? "–"}</td><td>{money(c.cpm, c.currency)}</td><td>{num(c.min_qualified_views)}</td>
                <td>{money(c.max_payout_per_clip, c.currency)}</td><td>{money(c.budget, c.currency)}</td>
                <td>{c.platforms.join(", ")}</td><td>{c.rules.length}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Import (YAML, JSON or CSV)</h2>
        <textarea rows={16} value={text} onChange={(e) => setText(e.target.value)} />
        <div className="row" style={{ marginTop: 8 }}>
          <select value={format} onChange={(e) => setFormat(e.target.value)} style={{ width: 160 }}>
            <option value="">detect format</option><option value="yaml">YAML</option>
            <option value="json">JSON</option><option value="csv">CSV</option>
          </select>
          <button className="primary" onClick={importIt}>Import</button>
          {msg && <span className="muted">{msg}</span>}
        </div>
        <ErrorNote error={err} />
      </div>
    </>
  );
}
