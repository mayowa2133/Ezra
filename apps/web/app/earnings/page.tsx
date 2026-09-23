"use client";

import { ErrorNote, Stat } from "@/components/ui";
import { money, num, useApi } from "@/lib/api";
import type { Campaign, Earnings } from "@/lib/types";

function CampaignEarnings({ slug }: { slug: string }) {
  const { data: e, error } = useApi<Earnings>(`/campaigns/${slug}/earnings`);
  if (error) return <ErrorNote error={error} />;
  if (!e) return null;
  return (
    <div className="panel">
      <h2 style={{ marginTop: 0 }}>{slug}</h2>
      <div className="grid cols-4">
        <Stat k="Qualified views" v={num(e.qualified_views)} />
        <Stat k="Estimated" v={money(e.gross_estimated, e.currency)} />
        <Stat k="Confirmed" v={money(e.confirmed, e.currency)} />
        <Stat k="Margin (est.)" v={money(e.margin_estimate, e.currency)} />
      </div>
      {e.budget_capped && <p className="small muted">Budget cap reached.</p>}
      <table>
        <thead><tr><th>post</th><th>platform</th><th>views</th><th>qualified</th><th>estimated</th><th>confirmed</th><th>metrics source</th></tr></thead>
        <tbody>{e.posts.map((p) => (
          <tr key={p.post_id}><td>{p.post_id}</td><td>{p.platform}</td><td>{num(p.views)}</td><td>{num(p.qualified_views)}</td>
            <td>{money(p.estimated, e.currency)}</td><td>{money(p.confirmed, e.currency)}</td><td className="muted">{p.metrics_provider}</td></tr>
        ))}</tbody>
      </table>
      <p className="small muted">Processing cost (estimated): {money(e.costs.total_usd)} ·{" "}
        {Object.entries(e.costs.by_kind).map(([k, v]) => `${k} ${money(v.usd)}`).join(" · ")}</p>
    </div>
  );
}

export default function EarningsPage() {
  const { data, error } = useApi<Campaign[]>("/campaigns");
  return (
    <>
      <h1>Earnings</h1>
      <ErrorNote error={error} />
      <p className="muted small">Estimated = observed views × CPM under each campaign&apos;s thresholds, caps and tracking window.
        Confirmed = payouts you record or import.</p>
      {data?.map((c) => <CampaignEarnings key={c.slug} slug={c.slug} />)}
    </>
  );
}
