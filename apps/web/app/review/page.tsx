"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { Badge, ComplianceBox, ErrorNote, ScoreBars } from "@/components/ui";
import { api, money, post, useApi } from "@/lib/api";
import type { ReviewCard } from "@/lib/types";

type Meta = Record<string, { title?: string; caption?: string; description?: string; hashtags?: string[] }>;

/** Per-platform copy for one clip. Keyed by clip id, so its state resets when the card changes. */
function CopyEditor({ clipId, initial, onError, onSaved }: {
  clipId: number; initial: Meta; onError: (m: string) => void; onSaved: (m: string) => void;
}) {
  const [meta, setMeta] = useState<Meta>(initial);
  const [busy, setBusy] = useState(false);

  const generate = useCallback(async () => {
    setBusy(true);
    try {
      setMeta((await post<{ metadata: Meta }>(`/clips/${clipId}/metadata`, {})).metadata);
    } catch (e) {
      onError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [clipId, onError]);

  const save = useCallback(async () => {
    try {
      await api(`/clips/${clipId}`, { method: "PATCH", body: JSON.stringify({ platform_metadata: meta }) });
      onSaved("metadata saved");
    } catch (e) {
      onError((e as Error).message);
    }
  }, [clipId, meta, onError, onSaved]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName)) return;
      if (e.key === "g") void generate();
      else if (e.key === "s") void save();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [generate, save]);

  return (
    <div className="panel">
      <div className="row"><h2 style={{ margin: 0 }}>Post copy</h2><span className="spacer" />
        <button onClick={generate} disabled={busy}>Generate</button><button onClick={save}>Save</button></div>
      {Object.keys(meta).length === 0 && <p className="muted small">No copy yet. Press G to generate.</p>}
      {Object.entries(meta).map(([p, m]) => (
        <div key={p}>
          <label>{p}{m.title !== undefined ? " — title" : ""}</label>
          {m.title !== undefined && (
            <input value={m.title ?? ""} onChange={(e) => setMeta({ ...meta, [p]: { ...m, title: e.target.value } })} />
          )}
          <textarea rows={3} value={m.caption ?? ""}
                    onChange={(e) => setMeta({ ...meta, [p]: { ...m, caption: e.target.value } })} />
        </div>
      ))}
    </div>
  );
}

export default function Review() {
  const { data, error, reload } = useApi<ReviewCard[]>("/review");
  const [i, setI] = useState(0);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);
  const video = useRef<HTMLVideoElement>(null);
  const cards = data ?? [];
  const card = cards[Math.min(i, Math.max(0, cards.length - 1))];

  const decide = useCallback(async (action: "approve" | "reject") => {
    if (!card || busy) return;
    const notes = action === "reject" ? window.prompt("Reason for rejecting (optional)") ?? undefined : undefined;
    setBusy(true);
    setErr(null);
    try {
      await post(`/clips/${card.clip_id}/${action}`, { notes, reviewer: "dashboard" });
      setFlash(`clip ${card.clip_id} ${action === "approve" ? "approved" : "rejected"}`);
      await reload();
    } catch (e) {
      setErr((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, [card, busy, reload]);

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (["INPUT", "TEXTAREA", "SELECT"].includes((e.target as HTMLElement).tagName)) return;
      if (e.key === "a") void decide("approve");
      else if (e.key === "r") void decide("reject");
      else if (e.key === "j") setI((x) => Math.min(x + 1, cards.length - 1));
      else if (e.key === "k") setI((x) => Math.max(x - 1, 0));
      else if (e.key === " " && video.current) {
        e.preventDefault();
        if (video.current.paused) void video.current.play();
        else video.current.pause();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [decide, cards.length]);

  return (
    <>
      <div className="row">
        <h1>Review</h1><span className="spacer" />
        <span className="small muted">
          <span className="kbd">A</span> approve <span className="kbd">R</span> reject{" "}
          <span className="kbd">J</span>/<span className="kbd">K</span> next/prev <span className="kbd">G</span> generate copy{" "}
          <span className="kbd">S</span> save <span className="kbd">Space</span> play
        </span>
      </div>
      <ErrorNote error={error ?? err} />
      {flash && <p className="muted small">{flash}</p>}
      {cards.length === 0 && !error && (
        <div className="panel muted">Nothing waiting for review. Render some candidates first.</div>
      )}
      {card && (
        <div className="review">
          <div>
            {card.video_url ? (
              <video ref={video} key={card.video_url} src={card.video_url} controls
                     poster={card.thumbnail_url ?? undefined} />
            ) : <div className="panel muted">no video</div>}
            <div className="row" style={{ marginTop: 10 }}>
              <button className="primary" disabled={busy || card.compliance.status === "FAIL"}
                      onClick={() => decide("approve")}>Approve</button>
              <button className="danger" disabled={busy} onClick={() => decide("reject")}>Reject</button>
              <span className="spacer" />
              <span className="muted small">{i + 1} / {cards.length}</span>
            </div>
            <div className="panel small" style={{ marginTop: 12 }}>
              <div>version {card.version} · {card.duration?.toFixed(1)}s · layout {card.layout}</div>
              {card.edit_summary && (
                <div className="muted">removed {String(card.edit_summary.removed_seconds ?? 0)}s of silence and fillers</div>
              )}
            </div>
            <div className="panel small">
              {cards.map((c, j) => (
                <div key={c.clip_id} className="row" style={{ cursor: "pointer", fontWeight: j === i ? 700 : 400 }}
                     onClick={() => setI(j)}>
                  <span>#{c.clip_id}</span><span style={{ flex: 1 }}>{c.title}</span>
                  <span>{c.candidate.rank_score?.toFixed(0)}</span>
                </div>
              ))}
            </div>
          </div>
          <div>
            <div className="panel">
              <div className="row"><h2 style={{ margin: 0 }}>{card.title}</h2><span className="spacer" />
                <Badge value={card.status} /></div>
              <p className="small muted">
                rank {card.candidate.rank_score?.toFixed(1)} · scored by {card.candidate.scorer} (confidence{" "}
                {card.candidate.confidence}) · hook type {card.candidate.hook_type} · performance prior{" "}
                {card.candidate.performance_prior ?? "–"}
              </p>
              <ScoreBars scores={card.candidate.scores}
                         why={card.candidate.explanations?.heuristic as Record<string, string> | undefined} />
              {card.candidate.reason && <p className="small"><b>Why:</b> {card.candidate.reason}</p>}
            </div>
            <div className="grid cols-2">
              <div className="panel"><h2 style={{ marginTop: 0 }}>Compliance</h2><ComplianceBox c={card.compliance} /></div>
              <div className="panel">
                <h2 style={{ marginTop: 0 }}>Expected value</h2>
                <div className="stat"><div className="v">{money(card.expected_value.ev_per_post)}</div></div>
                <div className="small muted">per post · p10–p90 {money(card.expected_value.p10)}–
                  {money(card.expected_value.p90)} · P(qualify) {card.expected_value.p_qualify} · {card.expected_value.basis}</div>
                <div className="small muted">{card.expected_value.note}</div>
              </div>
            </div>
            <CopyEditor key={card.clip_id} clipId={card.clip_id} initial={card.platform_metadata ?? {}}
                        onError={setErr} onSaved={setFlash} />
            <div className="panel">
              <h2 style={{ marginTop: 0 }}>Transcript</h2>
              {card.candidate.context_before && (
                <p className="small muted">…{card.candidate.context_before.slice(-240)}</p>
              )}
              <p>{card.candidate.transcript}</p>
              {card.candidate.warnings?.map((w) => <p key={w} className="error small">{w}</p>)}
            </div>
          </div>
        </div>
      )}
    </>
  );
}
