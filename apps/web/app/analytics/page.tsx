"use client";

import { useState } from "react";

import { ErrorNote } from "@/components/ui";
import { num, useApi } from "@/lib/api";
import type { Campaign } from "@/lib/types";

type Insights = {
  n_posts: number; median_views?: number; observations: string[];
  cohorts: { trait: string; value: string; n: number; median_views: number; shrunk_multiplier: number; interval80: number[] }[];
  calibration: { spearman_rank_vs_views: number | null; n: number; reading: string | null } | null;
  regression: { n: number; r2_in_sample: number; coefficients: { feature: string; std_effect: number }[] } | null;
};
type Experiment = { id: number; name: string; factor: string; variants: { key: string; value: unknown }[]; status: string;
  assignments: Record<string, number> };

export default function Analytics() {
  const { data: camps } = useApi<Campaign[]>("/campaigns");
  const [slug, setSlug] = useState("");
  const { data, error } = useApi<Insights>(slug ? `/campaigns/${slug}/insights` : null);
  const { data: exps } = useApi<Experiment[]>("/experiments");
  return (
    <>
      <div className="row"><h1>Analytics</h1><span className="spacer" />
        <select value={slug} onChange={(e) => setSlug(e.target.value)} style={{ width: 240 }}>
          <option value="">choose a campaign…</option>{camps?.map((c) => <option key={c.slug} value={c.slug}>{c.name}</option>)}
        </select></div>
      <ErrorNote error={error} />
      {data && (
        <>
          <div className="panel">
            <h2 style={{ marginTop: 0 }}>What the results say ({data.n_posts} posts)</h2>
            <ul>{data.observations.map((o) => <li key={o}>{o}</li>)}</ul>
            {data.calibration && (
              <p className="small muted">Rank vs views (Spearman): {data.calibration.spearman_rank_vs_views ?? "n/a"} over {data.calibration.n} posts — {data.calibration.reading ?? "not enough data"}</p>
            )}
          </div>
          <div className="panel">
            <h2 style={{ marginTop: 0 }}>Traits (shrunk toward the average; 80% intervals)</h2>
            <table>
              <thead><tr><th>trait</th><th>value</th><th>n</th><th>median views</th><th>× typical</th><th>80% interval</th></tr></thead>
              <tbody>{data.cohorts.map((r) => (
                <tr key={r.trait + r.value}><td>{r.trait}</td><td>{r.value}</td><td>{r.n}</td><td>{num(r.median_views)}</td>
                  <td>{r.shrunk_multiplier}×</td><td>{r.interval80[0]}–{r.interval80[1]}×</td></tr>
              ))}</tbody>
            </table>
          </div>
          {data.regression && (
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Regression (n={data.regression.n}, in-sample R² {data.regression.r2_in_sample})</h2>
              <table><tbody>{data.regression.coefficients.slice(0, 12).map((c) => (
                <tr key={c.feature}><td>{c.feature}</td><td>{c.std_effect}</td></tr>))}</tbody></table>
            </div>
          )}
        </>
      )}
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Experiments</h2>
        <table>
          <thead><tr><th>#</th><th>name</th><th>factor</th><th>variants</th><th>assigned</th><th>status</th></tr></thead>
          <tbody>{exps?.map((e) => (
            <tr key={e.id}><td>{e.id}</td><td>{e.name}</td><td>{e.factor}</td>
              <td>{e.variants.map((v) => `${v.key}=${String(v.value)}`).join(", ")}</td>
              <td>{Object.entries(e.assignments).map(([k, v]) => `${k}:${v}`).join(" ")}</td><td>{e.status}</td></tr>
          ))}</tbody>
        </table>
      </div>
    </>
  );
}
