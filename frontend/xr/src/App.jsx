import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Canvas } from '@react-three/fiber';
import { XR, createXRStore } from '@react-three/xr';
import { domePosition, layoutBounds } from './domeLayout.js';
import { renderableNodes } from './sceneModel.js';
import { SceneSession, isValidSessionId } from './sceneSession.js';
// The REST layer is reused from the 2D client as-is (ADR 0003 reuse map); see
// sceneSession.js for why the cross-workspace import is deliberate for now.
import * as api from '../../web/src/services/api.js';

// Shown until a session is connected, so the dome geometry is still walkable
// on-device with no backend running. These carry the same 2D {x, y} positions
// the session protocol uses; `color` is local to the placeholder.
const PLACEHOLDER_NODES = [
  { id: 'a', x: 0, y: 0, color: '#6ee7b7' },
  { id: 'b', x: 100, y: 20, color: '#60a5fa' },
  { id: 'c', x: 40, y: 80, color: '#f472b6' },
  { id: 'd', x: 90, y: 90, color: '#fbbf24' },
  { id: 'e', x: 10, y: 60, color: '#a78bfa' },
];

const SESSION_NODE_COLOR = '#93c5fd';

const store = createXRStore();

// domeLayout returns positions around the origin; the dome centre belongs at
// eye height. The flat-preview camera below sits at this same height, and the
// framing argument in its comment depends on the two staying equal.
const EYE_HEIGHT = 1.5;

// Read `?session=<short-id>` once at startup. Sharing a session as a link is
// the desktop client's contract (§5) and is the only bearable way to join one
// from inside a headset, where typing sixteen digits is the worst part of the
// workflow.
function sessionIdFromUrl() {
  try {
    const fromQuery = new URL(window.location.href).searchParams.get('session');
    return isValidSessionId(fromQuery) ? fromQuery.trim() : null;
  } catch {
    return null;
  }
}

const IDLE_SESSION_STATE = { sessionId: null, scene: null, status: 'idle', error: null };

// Own the SceneSession for the active session id: the SSE subscription, the
// scene reduction and the teardown when the id changes or the app unmounts.
function useSceneSession(sessionId) {
  const [state, setState] = useState(IDLE_SESSION_STATE);

  useEffect(() => {
    if (!sessionId) return undefined;
    const session = new SceneSession({
      sessionId,
      clientId: api.getClientId(),
      displayName: api.getDisplayName(),
      streamUrl: api.getSessionStreamUrl(sessionId),
      opsUrl: api.getSessionOpsUrl(sessionId),
      loadSession: api.getSession,
      loadNodeDetails: api.getNodeDetails,
      onChange: setState,
    });
    session.connect();
    return () => session.close();
  }, [sessionId]);

  // The session reports which id its state belongs to, so switching sessions
  // never renders the previous one's scene while the new stream is still
  // opening — and the effect needs no synchronous setState to reset it.
  if (!sessionId) return IDLE_SESSION_STATE;
  return state.sessionId === sessionId
    ? state
    : { ...IDLE_SESSION_STATE, sessionId, status: 'connecting' };
}

function DomeNodes({ nodes }) {
  const bounds = useMemo(() => layoutBounds(nodes), [nodes]);
  return (
    <group>
      {nodes.map((n) => {
        const p = domePosition(n.x, n.y, bounds);
        return (
          <mesh key={n.id} position={[p.x, p.y + EYE_HEIGHT, p.z]}>
            <boxGeometry args={[0.3, 0.3, 0.3]} />
            <meshStandardMaterial color={n.color} />
          </mesh>
        );
      })}
    </group>
  );
}

function SessionControls({ error, onCreate, onConnect, busy }) {
  const [draft, setDraft] = useState('');
  const invalid = draft.trim() !== '' && !isValidSessionId(draft);

  return (
    <div className="xr-session">
      <button type="button" onClick={onCreate} disabled={busy}>
        New session
      </button>
      <input
        aria-label="Session ID"
        placeholder="0000-0000-0000-0000"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && isValidSessionId(draft)) onConnect(draft.trim());
        }}
      />
      <button
        type="button"
        onClick={() => onConnect(draft.trim())}
        disabled={!isValidSessionId(draft)}
      >
        Connect
      </button>
      {invalid ? (
        <span className="xr-error">Session IDs look like 0000-0000-0000-0000.</span>
      ) : null}
      {error ? <span className="xr-error">{error}</span> : null}
    </div>
  );
}

export default function App() {
  // enterVR() rejects when the user denies the session or the runtime refuses
  // it. Surface that instead of leaving an unhandled rejection and a dead
  // button. Note it does NOT reject merely for lacking native WebXR: on
  // localhost @react-three/xr injects the IWER emulator and enters a simulated
  // Quest 3 (see README). requestSession rejects with a DOMException, whose
  // message can be empty, so fall back rather than storing a blank error.
  const [error, setError] = useState(null);
  const [sessionId, setSessionId] = useState(sessionIdFromUrl);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);
  const { scene, status, error: sessionError } = useSceneSession(sessionId);
  // Guards against a second create being started before the first resolves —
  // each one materialises a session server-side, so a double tap in a headset
  // must not leave an orphan behind.
  const creatingRef = useRef(false);

  // Keep the address bar on the session that is actually connected, so the tab
  // can be shared or reloaded straight back into it.
  useEffect(() => {
    if (!sessionId) return;
    const url = new URL(window.location.href);
    if (url.searchParams.get('session') === sessionId) return;
    url.searchParams.set('session', sessionId);
    window.history.replaceState(null, '', url.toString());
  }, [sessionId]);

  const handleCreate = useCallback(async () => {
    if (creatingRef.current) return;
    creatingRef.current = true;
    setCreating(true);
    setCreateError(null);
    try {
      const payload = await api.createSession(null);
      if (payload?.id) setSessionId(payload.id);
      else setCreateError('The server returned a session without an id.');
    } catch (err) {
      setCreateError(err?.message || 'Could not create a session.');
    } finally {
      creatingRef.current = false;
      setCreating(false);
    }
  }, []);

  const handleConnect = useCallback((id) => {
    setCreateError(null);
    setSessionId(id);
  }, []);

  const sceneNodes = useMemo(
    () =>
      scene
        ? renderableNodes(scene).map((n) => ({ ...n, color: n.claim?.color || SESSION_NODE_COLOR }))
        : [],
    [scene]
  );
  const nodes = sessionId ? sceneNodes : PLACEHOLDER_NODES;

  return (
    <>
      <button
        className="xr-enter"
        onClick={() => {
          setError(null);
          store
            .enterVR()
            .catch((err) => setError(err?.message || String(err ?? '') || 'unknown error'));
        }}
      >
        Enter VR
      </button>
      <SessionControls
        error={createError || sessionError}
        onCreate={handleCreate}
        onConnect={handleConnect}
        busy={creating}
      />
      <div className="xr-hint">
        {error !== null ? (
          <span className="xr-error">Could not enter VR: {error}</span>
        ) : sessionId ? (
          <>
            Session {sessionId} — {status}
            {status === 'connected'
              ? `, ${sceneNodes.length} node${sceneNodes.length === 1 ? '' : 's'}, ${scene.roster.length} client${scene.roster.length === 1 ? '' : 's'}`
              : null}
          </>
        ) : (
          <>
            Placeholder dome — create or connect to a session to render its nodes. Connect a headset
            via `adb reverse` and open http://localhost:5173 in the Quest Browser. See README.
          </>
        )}
      </div>
      {/*
        Flat-preview camera only — inside an XR session the runtime owns the
        camera pose and projection, so neither of these props applies there.

        `rotation` must be passed explicitly: without it R3F calls
        camera.lookAt(0, 0, 0) on a declaratively-configured camera, which tilts
        it down by atan(EYE_HEIGHT / 2.5) ≈ 31° toward the floor origin. That
        pushes the top of the dome ~63° off the view axis, outside the 50° half-
        fov, so the upper rows fall off-screen.

        The dome wraps ±60° horizontally and ±45° vertically around its centre,
        which is more than a flat viewport can show from that centre: a frustum
        is rectangular, so a corner node's vertical screen angle is
        atan(tan(elevation) / cos(azimuth)) — 63° for the ±60°/±45° corner, not
        45°. Containing that from the dome centre would need a ~127° vertical
        fov. Backing the preview camera off along +Z instead keeps the whole
        wrap in frame at a sane fov (verified for every aspect ratio >= 1).
      */}
      <Canvas camera={{ position: [0, EYE_HEIGHT, 2.5], fov: 100, rotation: [0, 0, 0] }}>
        <XR store={store}>
          <ambientLight intensity={0.8} />
          <directionalLight position={[2, 4, 1]} intensity={1} />
          <DomeNodes nodes={nodes} />
        </XR>
      </Canvas>
    </>
  );
}
