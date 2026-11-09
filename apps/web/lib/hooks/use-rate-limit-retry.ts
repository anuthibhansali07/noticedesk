"use client";

// Countdown + single automatic retry for AI calls that hit the provider's
// rate limit (HTTP 429). Callers schedule the retry from their catch block:
//
//   if (isRateLimited(e) && attempt === 0) schedule(e.retryAfterSeconds, () => run(1));
//
// Only one automatic retry is made so a dead daily quota doesn't loop; after
// that the partner sees the error and can retry by hand.

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api";

export function isRateLimited(e: unknown): e is ApiError {
  return e instanceof ApiError && e.isRateLimited;
}

export function useRateLimitRetry() {
  const [countdown, setCountdown] = useState<number | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const pending = useRef<(() => void) | null>(null);

  const stop = useCallback(() => {
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
    setCountdown(null);
  }, []);

  const fire = useCallback(() => {
    const fn = pending.current;
    pending.current = null;
    stop();
    fn?.();
  }, [stop]);

  const schedule = useCallback(
    (seconds: number | null, retry: () => void) => {
      stop();
      pending.current = retry;
      let left = Math.max(1, Math.round(seconds ?? 30));
      setCountdown(left);
      timer.current = setInterval(() => {
        left -= 1;
        if (left <= 0) {
          fire();
        } else {
          setCountdown(left);
        }
      }, 1000);
    },
    [fire, stop],
  );

  const cancel = useCallback(() => {
    pending.current = null;
    stop();
  }, [stop]);

  useEffect(() => () => {
    if (timer.current) clearInterval(timer.current);
  }, []);

  return { countdown, schedule, cancel, retryNow: fire };
}
