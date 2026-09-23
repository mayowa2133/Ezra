"use client";

import { useState } from "react";

import { Badge, ErrorNote } from "@/components/ui";
import { num, post, useApi } from "@/lib/api";
import type { Clip, Post } from "@/lib/types";

type Plan = { posts: { platform: string; account: string; provider: string; compliance: string; review_notes: string[] }[];
  problems: string[]; published: boolean; dry_run?: boolean };
type Account = { id: number; platform: string; provider: string; handle: string; has_credential: boolean };

export default function Publishing() {
  const { data: posts, error, reload } = useApi<Post[]>("/posts", 5000);
  const { data: approved } = useApi<Clip[]>("/clips?status=approved");
  const { data: accounts } = useApi<Account[]>("/accounts");
  const [clip, setClip] = useState<number | null>(null);
  const [platforms, setPlatforms] = useState("tiktok,instagram,youtube");
  const [visibility, setVisibility] = useState("public");
  const [schedule, setSchedule] = useState("");
  const [tz, setTz] = useState(Intl.DateTimeFormat().resolvedOptions().timeZone);
  const [plan, setPlan] = useState<Plan | null>(null);
  const [err, setErr] = useState<string | null>(null);

  async function run(confirm: boolean) {
    if (!clip) return;
    setErr(null);
    try {
      const out = await post<Plan>(`/clips/${clip}/publish`, {
        platforms: platforms.split(",").map((p) => p.trim()).filter(Boolean), visibility,
        schedule_at: schedule || null, timezone: tz, confirm,
      });
      setPlan(out);
      if (confirm) await reload();
    } catch (e) {
      setErr((e as Error).message);
    }
  }

  return (
    <>
      <h1>Publishing</h1>
      <ErrorNote error={error ?? err} />
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Publish an approved clip</h2>
        <div className="grid cols-3">
          <div><label>Clip</label><select value={clip ?? ""} onChange={(e) => { setClip(Number(e.target.value) || null); setPlan(null); }}>
            <option value="">choose…</option>{approved?.map((c) => <option key={c.id} value={c.id}>#{c.id} {c.title}</option>)}
          </select></div>
          <div><label>Platforms</label><input value={platforms} onChange={(e) => setPlatforms(e.target.value)} /></div>
          <div><label>Visibility</label><select value={visibility} onChange={(e) => setVisibility(e.target.value)}>
            <option>public</option><option>private</option><option>unlisted</option></select></div>
          <div><label>Schedule (optional)</label><input type="datetime-local" value={schedule} onChange={(e) => setSchedule(e.target.value)} /></div>
          <div><label>Time zone</label><input value={tz} onChange={(e) => setTz(e.target.value)} /></div>
        </div>
        <div className="row" style={{ marginTop: 10 }}>
          <button disabled={!clip} onClick={() => run(false)}>Preview</button>
          <button className="primary" disabled={!clip || !plan || plan.problems.length > 0 || plan.published} onClick={() => run(true)}>
            Confirm and publish</button>
        </div>
        {plan && (
          <div style={{ marginTop: 10 }}>
            {plan.problems.map((p) => <div key={p} className="error small" style={{ marginBottom: 4 }}>{p}</div>)}
            <table><tbody>{plan.posts.map((p) => (
              <tr key={p.platform}><td>{p.platform}</td><td>@{p.account}</td><td className="muted">{p.provider}</td>
                <td><Badge value={p.compliance} /></td><td className="small">{p.review_notes.join("; ")}</td></tr>
            ))}</tbody></table>
            {plan.published && <p className="small">Posts created; jobs queued.</p>}
          </div>
        )}
      </div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Posts</h2>
        <table>
          <thead><tr><th>#</th><th>clip</th><th>platform</th><th>provider</th><th>status</th><th>visibility</th><th>scheduled</th><th>views</th><th>link</th><th></th></tr></thead>
          <tbody>{posts?.map((p) => (
            <tr key={p.id}>
              <td>{p.id}</td><td>{p.clip_id}</td><td>{p.platform}</td><td className="muted">{p.provider}</td>
              <td><Badge value={p.status} />{p.error && <div className="small error">{p.error}</div>}</td>
              <td>{p.visibility}</td><td className="small">{p.scheduled_at ?? "–"}</td><td>{num(p.metrics?.views)}</td>
              <td>{p.url && <a href={p.url} target="_blank" rel="noreferrer">open</a>}</td>
              <td>{p.status === "scheduled" && <button onClick={async () => { await post(`/posts/${p.id}/cancel`); await reload(); }}>Cancel</button>}
                {p.status === "failed" && <button onClick={async () => { await post(`/posts/${p.id}/retry`); await reload(); }}>Retry</button>}</td>
            </tr>
          ))}</tbody>
        </table>
      </div>
      <div className="panel">
        <h2 style={{ marginTop: 0 }}>Accounts</h2>
        <table><tbody>{accounts?.map((a) => (
          <tr key={a.id}><td>{a.platform}</td><td>@{a.handle}</td><td className="muted">{a.provider}</td>
            <td>{a.has_credential || a.provider === "local-export" ? "ready" : "needs credential"}</td></tr>
        ))}</tbody></table>
        {accounts?.length === 0 && <p className="muted small">No accounts. Connect one under Integrations, or `ezra accounts add`.</p>}
      </div>
    </>
  );
}
