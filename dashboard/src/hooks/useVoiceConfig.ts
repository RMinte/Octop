import { useCallback, useEffect, useState } from "react";
import { voiceApi, type ActiveVoice } from "../api/modules/voice";

let cachedActive: ActiveVoice | null = null;
let inflight: Promise<ActiveVoice> | null = null;

/** Read the cached voice config synchronously (no network). */
export function cachedActiveVoice(): ActiveVoice | null {
  return cachedActive;
}

export async function fetchActiveVoice(force = false): Promise<ActiveVoice> {
  if (!force && cachedActive) return cachedActive;
  if (!force && inflight) return inflight;
  const request: Promise<ActiveVoice> = voiceApi
    .getActive()
    .then((active) => {
      if (inflight === request) cachedActive = active;
      return active;
    })
    .finally(() => {
      if (inflight === request) inflight = null;
    });
  inflight = request;
  return request;
}

export function invalidateVoiceConfigCache() {
  cachedActive = null;
  inflight = null;
}

/** Pre-fetch the active voice config so `cachedActiveVoice()` returns it. */
export function prefetchVoiceConfig(): void {
  void fetchActiveVoice().catch(() => {
    // A failed preload is retried when the user starts a voice action.
  });
}

export function useVoiceConfig() {
  const [active, setActive] = useState<ActiveVoice>({
    stt: "browser",
    tts: "browser",
  });
  const [loading, setLoading] = useState(true);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await fetchActiveVoice(true);
      setActive(next);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  return { active, loading, refresh };
}
