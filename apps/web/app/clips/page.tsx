"use client";

import Link from "next/link";
import { useState } from "react";

import { Badge, ErrorNote } from "@/components/ui";
import { useApi } from "@/lib/api";
import type { Clip } from "@/lib/types";

const STATUSES = ["", "rendered", "approved", "rejected", "published", "exported", "failed"];

export default function Clips() {
  const [status, setStatus] = useState("");
  const { data, error } = useApi<Clip[]>(`/clips${status ? `?status=${status}` : ""}`, 5000);
  return (
    <>
      <div className="row"><h1>Clips</h1><span className="spacer" />
        <select value={status} onChange={(e) => setStatus(e.target.value)} style={{ width: 180 }}>
          {STATUSES.map((s) => <option key={s} value={s}>{s || "all statuses"}</option>)}
        </select></div>
      <ErrorNote error={error} />
      <div className="thumbs">
        {data?.map((c) => (
          <div key={c.id} className="panel" style={{ padding: 8 }}>
            {c.thumbnail_url ? (
              // Signed, short-lived API media URL on another origin: not something next/image should cache.
              // eslint-disable-next-line @next/next/no-img-element
              <img src={c.thumbnail_url} alt={c.title ?? ""} />
            ) : <div className="muted small">no thumbnail</div>}
            <div className="small" style={{ marginTop: 6 }}><b>#{c.id}</b> {c.title}</div>
            <div className="row small"><Badge value={c.status} /><span className="muted">
              v{c.current_version?.version} · {c.current_version?.duration?.toFixed(0)}s · {c.current_version?.layout}</span></div>
            {c.video_url && <a className="small" href={c.video_url} target="_blank" rel="noreferrer">open video</a>}
          </div>
        ))}
      </div>
      {data?.length === 0 && <p className="muted">No clips. <Link href="/campaigns">Run a campaign</Link> to render some.</p>}
    </>
  );
}
