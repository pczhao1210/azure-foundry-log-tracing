import { labels } from "./types";

export function Badge({ state }: { state: string }) {
  return <span className={`badge ${state}`}>{labels[state] ?? state}</span>;
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json">{JSON.stringify(value, null, 2)}</pre>;
}
