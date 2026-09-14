'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { createAuthorizedFetch, type AuthorizedFetch } from './local-service';

export type LocalConnection = {
  authorizedFetch: AuthorizedFetch;
  isLive: boolean;
  retainAudio: boolean;
  message: string;
  failed: boolean;
};

/** Own the one-time launch credential independently of recording and panels. */
export function useLocalConnection(): LocalConnection {
  const [token, setToken] = useState<string | null>(null);
  const [retainAudio, setRetainAudio] = useState(true);
  const [message, setMessage] = useState('Open through voxink-app to connect');
  const [failed, setFailed] = useState(false);
  // Reuse the one-time exchange if React replays the mount effect in development.
  const bootstrapRequest = useRef<Promise<{ token: string; retainAudio: boolean }> | null>(null);

  useEffect(() => {
    if (!bootstrapRequest.current) {
      const values = new URLSearchParams(window.location.hash.slice(1));
      const bootstrap = values.get('bootstrap');
      const retainAudio = values.get('retain_audio') === '1';
      window.history.replaceState(null, '', window.location.pathname + window.location.search);
      if (!bootstrap) return;
      bootstrapRequest.current = fetch('/v1/ui/bootstrap', {
        method: 'POST', headers: { 'X-VoxInk-Bootstrap': bootstrap }, cache: 'no-store',
      }).then(async (response) => {
        if (!response.ok) throw new Error('The UI launch link expired. Run voxink-app again.');
        const payload = (await response.json()) as { token?: unknown };
        if (typeof payload.token !== 'string' || !payload.token) throw new Error('The local helper returned no launch credential.');
        return { token: payload.token, retainAudio };
      });
    }
    let cancelled = false;
    void bootstrapRequest.current.then((launch) => {
      if (cancelled) return;
      setToken(launch.token);
      setRetainAudio(launch.retainAudio);
      setMessage(launch.retainAudio ? 'Local helper ready — saving audio' : 'Authenticated local helper ready');
    }).catch((error: unknown) => {
      if (cancelled) return;
      setFailed(true);
      setMessage(error instanceof Error ? error.message : 'Cannot authenticate the local UI');
    });
    return () => { cancelled = true; };
  }, []);

  const authorizedFetch = useMemo(() => createAuthorizedFetch(token), [token]);
  return { authorizedFetch, isLive: token !== null, retainAudio, message, failed };
}
