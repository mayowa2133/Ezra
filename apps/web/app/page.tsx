"use client";

import Link from "next/link";

import { Badge, ErrorNote, Stat } from "@/components/ui";
import { money, num, useApi } from "@/lib/api";
import type { Earnings, Job } from "@/lib/types";

type Dash = {
  campaigns: { slug: string; name: string; cpm: number }[];
  sources: number;
  candidates: number;
  review_pending: number;
  clips: Record<string, number>;
  posts: Record<string, number>;
  jobs: Job[];
  earnings: Earnings[];
  costs: { total_usd: number };
};

export default function Dashboard() {
  const { data, error } = useApi<Dash>("/dashboard", 4000);
  return (
    <>
      <h1>Dashboard</h1>
      <ErrorNote error={error} />
      {data && (
        <>
          <div className="grid cols-4">
            <Stat k="Review queue" v={<Link href="/review">{data.review_pending}</Link>} />
            <Stat k="Candidates" v={num(data.candidates)} />
            <Stat k="Published posts" v={num(data.posts.published)} />
            <Stat k="Estimated earnings" v={money(data.earnings.reduce((a, e) => a + e.gross_estimated, 0))} />
          </div>
          <div className="grid cols-2">
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Campaigns</h2>
              <table>
                <thead><tr><th>campaign</th><th>CPM</th><th>qualified views</th><th>estimated</th><th>confirmed</th></tr></thead>
                <tbody>
                  {data.earnings.map((e) => (
                    <tr key={e.campaign}>
                      <td><Link href={`/campaigns/${e.campaign}`}>{e.campaign}</Link></td>
                      <td>{money(e.cpm)}</td><td>{num(e.qualified_views)}</td>
                      <td>{money(e.gross_estimated)}</td><td>{money(e.confirmed)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {data.campaigns.length === 0 && <p className="muted">No campaigns yet. <Link href="/campaigns">Import one</Link>.</p>}
            </div>
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Recent jobs</h2>
              <table>
                <tbody>
                  {data.jobs.map((j) => (
                    <tr key={j.id}>
                      <td>{j.id}</td><td>{j.kind}</td><td><Badge value={j.status} /></td>
                      <td style={{ width: 120 }}><div className="bar"><div style={{ width: `${j.progress * 100}%` }} /></div></td>
                      <td className="muted small">{j.message}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </div>
          <div className="grid cols-4">
            {Object.entries(data.clips).map(([k, v]) => <Stat key={k} k={`clips ${k}`} v={v} />)}
          </div>
          <p className="muted small">Processing cost to date (estimated): {money(data.costs.total_usd)}</p>
        </>
      )}
    </>
  );
}
