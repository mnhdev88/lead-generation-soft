import { useCallback, useRef } from "react";

/** For quick repeated changes to one row (status A → B → C): answers can arrive out of order,
 *  so only the newest request's answer may update the screen.
 *
 *      const isLatest = latestRequest(row.id);
 *      const saved = await api.update(...);
 *      if (isLatest()) show(saved);
 */
export function useLatestRequest() {
  const counters = useRef(new Map<number | string, number>());
  return useCallback((key: number | string) => {
    const n = (counters.current.get(key) ?? 0) + 1;
    counters.current.set(key, n);
    return () => counters.current.get(key) === n;
  }, []);
}
