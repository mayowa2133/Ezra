"use client";

import Link from "next/link";
import { use, useState } from "react";

import { Badge, ErrorNote, JobBar, Stat } from "@/components/ui";
import { money, num, post, useApi, useJob } from "@/lib/api";
import type { Campaign, Candidate, Earnings, Job } from "@/lib/types";

export default function CampaignPage({ params }: { params: Promise<{ slug: string }> }) {
  const { slug } = use(params);
  const { data: c, error } = useApi<Campaign>(`/campaigns/${slug}`);
  const { data: earn } = useApi<Earnings>(`/campaigns/${slug}/earnings`);
  const { data: cands, reload } = useApi<Candidate[]>(`/candidates?campaign=${slug}&top=40&include_failed=true`);
  const [jobId, setJobId] = useState<number | null>(null);
  const job = useJob(jobId, () => void reload());
  const [err, setErr] = useState<string | null>(null);

  async function run(top: number) {
    try {
      setJobId((await post<Job>(`/campaigns/${slug}/run`, { render_top: top })).id);
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  if (error) return <ErrorNote error={error} />;
  if (!c) return <p className="muted">Loading…</p>;
  return (
    <>
      <div className="row"><h1>{c.name}</h1><span className="spacer" />
        <button className="primary" onClick={() => run(5)}>Run campaign (render top 5)</button></div>
      <ErrorNote error={err} />
      <JobBar job={job} />
      <div className="grid cols-4">
        <Stat k="CPM" v={money(c.cpm, c.currency)} />
        <Stat k="Min qualified views" v={num(c.min_qualified_views)} />
        <Stat k="Qualified views" v={num(earn?.qualified_views)} />
        <Stat k="Estimated / confirmed" v={`${money(earn?.gross_estimated)} / ${money(earn?.confirmed)}`} />
      </div>
      {c.brief && <p>{c.brief}</p>}
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Rules</h2>
        <table>
          <thead><tr><th>rule</th><th>severity</th><th>origin</th><th>description</th></tr></thead>
          <tbody>{c.rules.map((r) => (
            <tr key={r.id}><td>{r.kind}</td><td>{r.severity}</td><td className="muted">{r.origin}</td><td>{r.description}</td></tr>
          ))}</tbody>
        </table>
      </div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Candidates</h2>
        <table>
          <thead><tr><th>#</th><th>rank</th><th>duration</th><th>hook</th><th>title</th><th>compliance</th><th>EV / post (p10–p90)</th><th>status</th></tr></thead>
          <tbody>{cands?.map((x) => (
            <tr key={x.id}>
              <td>{x.id}</td><td>{x.rank_score?.toFixed(1) ?? "–"}</td><td>{x.duration.toFixed(0)}s</td>
              <td>{x.hook_type}</td><td>{x.title}</td><td><Badge value={x.compliance.status} /></td>
              <td>{money(x.expected_value.ev_per_post)} <span className="muted small">({money(x.expected_value.p10)}–{money(x.expected_value.p90)})</span></td>
              <td><Badge value={x.status} /></td>
            </tr>
          ))}</tbody>
        </table>
        <p className="muted small">Rank and expected value are ranking estimates, not predictions. <Link href="/review">Review rendered clips →</Link></p>
      </div>
    </>
  );
}
