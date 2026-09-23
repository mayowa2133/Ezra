"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import type { Job } from "./types";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api/ezra${path}`, {
    ...init,
    headers: init?.body instanceof FormData ? init.headers : { "content-type": "application/json", ...init?.headers },
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    const detail = data?.detail;
    throw new ApiError(res.status, typeof detail === "string" ? detail : JSON.stringify(detail ?? res.statusText));
  }
  return data as T;
}

export const post = <T,>(path: string, body?: unknown) =>
  api<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export function useApi<T>(path: string | null, pollMs?: number) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const reload = useCallback(async () => {
    if (!path) return;
    try {
      setData(await api<T>(path));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  }, [path]);
  useEffect(() => {
    if (!path) return;
    let cancelled = false;
    const load = () =>
      api<T>(path).then(
        (d) => { if (!cancelled) { setData(d); setError(null); setLoading(false); } },
        (e: Error) => { if (!cancelled) { setError(e.message); setLoading(false); } },
      );
    void load();
    const t = pollMs ? setInterval(() => void load(), pollMs) : undefined;
    return () => {
      cancelled = true;
      if (t) clearInterval(t);
    };
  }, [path, pollMs]);
  return { data, error, loading, reload };
}

/** Poll a job until it finishes; returns the live job. */
export function useJob(jobId: number | null, onDone?: (job: Job) => void) {
  const [job, setJob] = useState<Job | null>(null);
  const done = useRef(onDone);
  useEffect(() => {
    done.current = onDone;
  }, [onDone]);
  useEffect(() => {
    if (jobId === null) return;
    let stop = false;
    const tick = async () => {
      try {
        const j = await api<Job>(`/jobs/${jobId}?logs=false`);
        if (stop) return;
        setJob(j);
        if (["completed", "failed", "cancelled"].includes(j.status)) {
          done.current?.(j);
          return;
        }
      } catch {
        /* transient; retry */
      }
      if (!stop) setTimeout(tick, 1000);
    };
    void tick();
    return () => {
      stop = true;
    };
  }, [jobId]);
  return job;
}

export const money = (v: number | null | undefined, currency = "USD") =>
  v === null || v === undefined ? "–" : new Intl.NumberFormat("en-US", { style: "currency", currency }).format(v);

export const num = (v: number | null | undefined) =>
  v === null || v === undefined ? "–" : new Intl.NumberFormat("en-US").format(v);

export const secs = (v: number | null | undefined) => (v === null || v === undefined ? "–" : `${v.toFixed(0)}s`);
