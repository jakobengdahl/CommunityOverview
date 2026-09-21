import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { XR, createXRStore } from '@react-three/xr';
import * as THREE from 'three';
import {
  domeAnglesFromRay,
  domeView,
  layoutBounds,
  layoutPositionFromRay,
  panDomeView,
  zoomToDensity,
  zoomToRadius,
} from './domeLayout.js';
import { domeSceneData, selectionDetail } from './domeScene.js';
import { EMPTY_SCENE } from './sceneModel.js';
import { SceneSession, isValidSessionId } from './sceneSession.js';
// The REST layer is reused from the 2D client as-is (ADR 0003 reuse map); see
// sceneSession.js for why the cross-workspace import is deliberate for now.
import * as api from '../../web/src/services/api.js';

// Shown until a session is connected, so the dome geometry is still walkable
// on-device with no backend running. These carry the same 2D {x, y} positions
// the session protocol uses.
const PLACEHOLDER_NODES = [
  { id: 'a', name: 'Alpha', type: 'Actor', x: 0, y: 0, hydrated: true },
  { id: 'b', name: 'Beta', type: 'Initiative', x: 100, y: 20, hydrated: true },
  { id: 'c', name: 'Gamma', type: 'Resource', x: 40, y: 80, hydrated: true },
  { id: 'd', name: 'Delta', type: 'Goal', x: 90, y: 90, hydrated: true },
  { id: 'e', name: 'Epsilon', type: 'Risk', x: 10, y: 60, hydrated: true },
];

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

const PLACEHOLDER_SCENE = {
  ...EMPTY_SCENE,
  nodes: Object.fromEntries(PLACEHOLDER_NODES.map((node) => [node.id, node])),
  edges: {
    ab: { id: 'ab', source: 'a', target: 'b', type: null },
    ac: { id: 'ac', source: 'a', target: 'c', type: null },
    bd: { id: 'bd', source: 'b', target: 'd', type: null },
    ce: { id: 'ce', source: 'c', target: 'e', type: null },
  },
};

// Own the SceneSession for the active session id: the SSE subscription, the
// scene reduction and the teardown when the id changes or the app unmounts.
function useSceneSession(sessionId) {
  const [state, setState] = useState(IDLE_SESSION_STATE);
  const sessionRef = useRef(null);

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
    sessionRef.current = session;
    session.connect();
    return () => {
      session.close();
      if (sessionRef.current === session) sessionRef.current = null;
    };
  }, [sessionId]);

  const setLocalSelection = useCallback((nodeId) => {
    sessionRef.current?.setLocalSelection(nodeId);
  }, []);

  const moveNode = useCallback((nodeId, position, opts) => {
    return sessionRef.current?.moveNode(nodeId, position, opts) ?? false;
  }, []);

  // The session reports which id its state belongs to, so switching sessions
  // never renders the previous one's scene while the new stream is still
  // opening — and the effect needs no synchronous setState to reset it.
  const visibleState =
    state.sessionId === sessionId
      ? state
      : { ...IDLE_SESSION_STATE, sessionId, status: sessionId ? 'connecting' : 'idle' };
  return { ...visibleState, setLocalSelection, moveNode };
}

function makeTextTexture({
  title,
  subtitle,
  footer,
  color,
  selected = false,
  width = 512,
  height = 224,
}) {
  const canvas = document.createElement('canvas');
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext('2d');
  ctx.fillStyle = 'rgba(13, 17, 24, 0.94)';
  ctx.fillRect(0, 0, width, height);
  ctx.strokeStyle = selected ? '#ffffff' : color;
  ctx.lineWidth = selected ? 12 : 8;
  ctx.strokeRect(6, 6, width - 12, height - 12);
  ctx.fillStyle = color;
  ctx.fillRect(0, 0, 16, height);

  ctx.fillStyle = '#f8fafc';
  ctx.font = '700 42px system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
  ctx.textBaseline = 'top';
  ctx.fillText(trimText(ctx, title, width - 70), 42, 32);

  ctx.fillStyle = '#cbd5e1';
  ctx.font = '600 24px system-ui, -apple-system, BlinkMacSystemFont, sans-serif';
  ctx.fillText(trimText(ctx, subtitle, width - 70), 42, 92);

  if (footer) {
    ctx.fillStyle = '#94a3b8';
    ctx.font = '500 20px ui-monospace, SFMono-Regular, Menlo, monospace';
    ctx.fillText(trimText(ctx, footer, width - 70), 42, 144);
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.needsUpdate = true;
  return texture;
}

function trimText(ctx, value, maxWidth) {
  const text = String(value || '');
  if (ctx.measureText(text).width <= maxWidth) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (ctx.measureText(`${text.slice(0, mid)}...`).width <= maxWidth) lo = mid;
    else hi = mid - 1;
  }
  return `${text.slice(0, lo)}...`;
}

function plural(count, singular, pluralForm = `${singular}s`) {
  return `${count} ${count === 1 ? singular : pluralForm}`;
}

function Billboard({ children, position }) {
  const ref = useRef(null);
  const { camera } = useThree();
  useFrame(() => {
    ref.current?.lookAt(camera.position);
  });
  return (
    <group ref={ref} position={[position.x, position.y, position.z]}>
      {children}
    </group>
  );
}

function NodeCard({ node, selected, onSelect }) {
  const texture = useMemo(
    () =>
      makeTextTexture({
        title: node.title,
        subtitle: node.subtitle,
        footer: node.id,
        color: node.color,
        selected,
      }),
    [node.color, node.id, node.subtitle, node.title, selected]
  );

  useEffect(() => () => texture.dispose(), [texture]);

  return (
    <Billboard position={node.position}>
      <mesh
        userData={{ xrNodeId: node.id }}
        onClick={(event) => {
          event.stopPropagation();
          onSelect(node.id);
        }}
      >
        <planeGeometry args={[1.25, 0.55]} />
        <meshBasicMaterial map={texture} transparent toneMapped={false} />
      </mesh>
      {node.claim ? (
        <mesh position={[0, -0.38, 0.01]}>
          <planeGeometry args={[0.84, 0.08]} />
          <meshBasicMaterial color={node.claim.color || '#ffffff'} />
        </mesh>
      ) : null}
    </Billboard>
  );
}

function poseToRay(inputSource, frame, referenceSpace, target) {
  const matrix = new THREE.Matrix4();
  const quat = new THREE.Quaternion();
  const origin = target.origin;
  const direction = target.direction;
  let pose = inputSource.targetRaySpace
    ? frame.getPose(inputSource.targetRaySpace, referenceSpace)
    : null;

  if (!pose && inputSource.hand) {
    const tip = inputSource.hand.get?.('index-finger-tip');
    const wrist = inputSource.hand.get?.('wrist');
    const tipPose = tip ? frame.getJointPose?.(tip, referenceSpace) : null;
    const wristPose = wrist ? frame.getJointPose?.(wrist, referenceSpace) : null;
    if (!tipPose || !wristPose) return false;
    origin.set(
      tipPose.transform.position.x,
      tipPose.transform.position.y,
      tipPose.transform.position.z
    );
    direction
      .set(
        tipPose.transform.position.x - wristPose.transform.position.x,
        tipPose.transform.position.y - wristPose.transform.position.y,
        tipPose.transform.position.z - wristPose.transform.position.z
      )
      .normalize();
    return direction.lengthSq() > 0;
  }

  if (!pose) return false;
  matrix.fromArray(pose.transform.matrix);
  origin.setFromMatrixPosition(matrix);
  quat.setFromRotationMatrix(matrix);
  direction.set(0, 0, -1).applyQuaternion(quat).normalize();
  return true;
}

function XrRayInput({
  enabled,
  layoutBounds,
  domeOptions,
  onSelect,
  onMovePreview,
  onMoveCommit,
  onPan,
}) {
  const { gl, scene } = useThree();
  const raycaster = useMemo(() => new THREE.Raycaster(), []);
  const raysRef = useRef(new Map());
  const activeRef = useRef(new Map());
  const listenersRef = useRef(null);
  const latestRef = useRef({
    anglesForRay: null,
    domeOptions,
    enabled,
    hitNode: null,
    onMoveCommit,
    onPan,
    onSelect,
  });

  const rayForSource = useCallback((inputSource) => raysRef.current.get(inputSource) || null, []);

  const hitNode = useCallback(
    (ray) => {
      if (!ray) return null;
      const targets = [];
      scene.traverse((object) => {
        if (object.userData?.xrNodeId) targets.push(object);
      });
      raycaster.set(ray.origin, ray.direction);
      return raycaster.intersectObjects(targets, false)[0]?.object.userData.xrNodeId || null;
    },
    [raycaster, scene]
  );

  const layoutPointForRay = useCallback(
    (ray) =>
      ray
        ? layoutPositionFromRay(ray.origin, ray.direction, layoutBounds, {
            ...domeOptions,
            eyeHeight: EYE_HEIGHT,
          })
        : null,
    [domeOptions, layoutBounds]
  );

  const anglesForRay = useCallback(
    (ray) =>
      ray
        ? domeAnglesFromRay(ray.origin, ray.direction, {
            ...domeOptions,
            eyeHeight: EYE_HEIGHT,
          })
        : null,
    [domeOptions]
  );

  useEffect(() => {
    latestRef.current = {
      anglesForRay,
      domeOptions,
      enabled,
      hitNode,
      onMoveCommit,
      onPan,
      onSelect,
    };
  }, [anglesForRay, domeOptions, enabled, hitNode, onMoveCommit, onPan, onSelect]);

  const attachSessionListeners = useCallback(
    (session) => {
      if (listenersRef.current?.session === session) return;
      if (listenersRef.current) {
        const { session: previous, handleSelectStart, handleSelectEnd } = listenersRef.current;
        previous.removeEventListener('selectstart', handleSelectStart);
        previous.removeEventListener('selectend', handleSelectEnd);
      }
      if (!session) {
        listenersRef.current = null;
        return;
      }

      const handleSelectStart = (event) => {
        const ray = rayForSource(event.inputSource);
        const nodeId = latestRef.current.hitNode?.(ray);
        if (nodeId) {
          latestRef.current.onSelect(nodeId);
          if (latestRef.current.enabled) {
            activeRef.current.set(event.inputSource, { mode: 'move', nodeId, lastPosition: null });
          }
          return;
        }
        if (latestRef.current.enabled) {
          const angles = latestRef.current.anglesForRay?.(ray);
          if (angles) {
            activeRef.current.set(event.inputSource, {
              mode: 'pan',
              lastAngles: angles,
              view: latestRef.current.domeOptions,
            });
          }
        }
      };

      const handleSelectEnd = (event) => {
        const active = activeRef.current.get(event.inputSource);
        activeRef.current.delete(event.inputSource);
        if (active?.mode === 'move' && active.lastPosition) {
          latestRef.current.onMoveCommit(active.nodeId, active.lastPosition);
        }
      };

      session.addEventListener('selectstart', handleSelectStart);
      session.addEventListener('selectend', handleSelectEnd);
      listenersRef.current = { session, handleSelectStart, handleSelectEnd };
    },
    [anglesForRay, rayForSource]
  );

  useEffect(() => {
    return () => {
      if (!listenersRef.current) return;
      const { session, handleSelectStart, handleSelectEnd } = listenersRef.current;
      session.removeEventListener('selectstart', handleSelectStart);
      session.removeEventListener('selectend', handleSelectEnd);
      listenersRef.current = null;
    };
  }, []);

  useFrame((_state, _delta, frame) => {
    const session = gl.xr.getSession?.();
    attachSessionListeners(session || null);
    const referenceSpace = gl.xr.getReferenceSpace?.();
    if (!session || !referenceSpace || !frame) return;

    const liveSources = new Set(session.inputSources || []);
    for (const inputSource of liveSources) {
      const ray = raysRef.current.get(inputSource) || {
        origin: new THREE.Vector3(),
        direction: new THREE.Vector3(0, 0, -1),
      };
      if (poseToRay(inputSource, frame, referenceSpace, ray)) {
        raysRef.current.set(inputSource, ray);
      }

      const active = activeRef.current.get(inputSource);
      if (!active) continue;
      if (active.mode === 'move') {
        const position = layoutPointForRay(ray);
        if (!position) continue;
        active.lastPosition = position;
        onMovePreview(active.nodeId, position);
      } else if (active.mode === 'pan') {
        const angles = anglesForRay(ray);
        if (!angles) continue;
        const nextView = panDomeView(
          active.view,
          {
            azimuth: angles.azimuth - active.lastAngles.azimuth,
            elevation: angles.elevation - active.lastAngles.elevation,
          },
          layoutBounds
        );
        active.lastAngles = angles;
        active.view = nextView;
        onPan(nextView);
      }
    }

    for (const inputSource of raysRef.current.keys()) {
      if (!liveSources.has(inputSource)) {
        raysRef.current.delete(inputSource);
        activeRef.current.delete(inputSource);
      }
    }
  });

  return null;
}

function EdgeLine({ edge }) {
  const geometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setFromPoints(edge.points.map((p) => new THREE.Vector3(p.x, p.y, p.z)));
    return g;
  }, [edge.points]);
  useEffect(() => () => geometry.dispose(), [geometry]);

  return (
    <line geometry={geometry}>
      <lineBasicMaterial color="#64748b" transparent opacity={0.72} />
    </line>
  );
}

function InWorldHud({ detail }) {
  const texture = useMemo(() => {
    if (!detail) return null;
    return makeTextTexture({
      title: detail.name,
      subtitle: detail.type,
      footer: detail.summary || detail.id,
      color: '#6ee7b7',
      selected: true,
      height: 256,
    });
  }, [detail]);
  useEffect(() => () => texture?.dispose(), [texture]);
  if (!detail || !texture) return null;
  return (
    <Billboard position={{ x: 0, y: EYE_HEIGHT - 0.75, z: -1.7 }}>
      <mesh>
        <planeGeometry args={[1.55, 0.78]} />
        <meshBasicMaterial map={texture} transparent toneMapped={false} />
      </mesh>
    </Billboard>
  );
}

function DomeGraph({ data, selectedNodeId, onSelect, selectedDetail }) {
  return (
    <group>
      {data.edges.map((edge) => (
        <EdgeLine key={edge.id} edge={edge} />
      ))}
      {data.cards.map((node) => (
        <NodeCard
          key={node.id}
          node={node}
          selected={node.id === selectedNodeId}
          onSelect={onSelect}
        />
      ))}
      <InWorldHud detail={selectedDetail} />
    </group>
  );
}

function DomeNavigationControls({ view, zoom, onZoom, onReset }) {
  return (
    <div className="xr-nav" aria-label="Dome navigation">
      <button type="button" onClick={() => onZoom(1 / 1.2)} aria-label="Zoom out">
        -
      </button>
      <span>{Math.round(view.density * 100)}%</span>
      <button type="button" onClick={() => onZoom(1.2)} aria-label="Zoom in">
        +
      </button>
      <button type="button" onClick={onReset}>
        Reset
      </button>
      {view.atTop ? <span>Top edge</span> : null}
      {view.atBottom ? <span>Bottom edge</span> : null}
    </div>
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
          if (e.key === 'Enter' && !busy && isValidSessionId(draft)) onConnect(draft.trim());
        }}
      />
      <button
        type="button"
        onClick={() => onConnect(draft.trim())}
        disabled={busy || !isValidSessionId(draft)}
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
  const [selectedNodeId, setSelectedNodeId] = useState(null);
  const [domeNav, setDomeNav] = useState({ zoom: 1, centerX: null, centerY: null });
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);
  const {
    scene,
    status,
    error: sessionError,
    setLocalSelection,
    moveNode,
  } = useSceneSession(sessionId);
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
    setSelectedNodeId(null);
    setSessionId(id);
  }, []);

  const activeScene = sessionId ? scene || EMPTY_SCENE : PLACEHOLDER_SCENE;
  const activeLayoutBounds = useMemo(
    () => layoutBounds(Object.values(activeScene.nodes)),
    [activeScene]
  );
  const domeOptions = useMemo(() => {
    const density = zoomToDensity(domeNav.zoom);
    const view = domeView(activeLayoutBounds, { ...domeNav, density });
    return { ...view, radius: zoomToRadius(domeNav.zoom) };
  }, [activeLayoutBounds, domeNav]);
  const domeData = useMemo(
    () => domeSceneData(activeScene, { eyeHeight: EYE_HEIGHT, ...domeOptions }),
    [activeScene, domeOptions]
  );
  const selectedDetail = useMemo(
    () => selectionDetail(activeScene, selectedNodeId),
    [activeScene, selectedNodeId]
  );

  useEffect(() => {
    if (!selectedNodeId || selectedDetail) return;
    if (sessionId && status === 'connected') setLocalSelection(null);
    let cancelled = false;
    queueMicrotask(() => {
      if (!cancelled) setSelectedNodeId(null);
    });
    return () => {
      cancelled = true;
    };
  }, [selectedDetail, selectedNodeId, sessionId, setLocalSelection, status]);

  const handleSelectNode = useCallback(
    (nodeId) => {
      setSelectedNodeId(nodeId);
      if (sessionId && status === 'connected') setLocalSelection(nodeId);
    },
    [sessionId, setLocalSelection, status]
  );

  const handleMovePreview = useCallback(
    (nodeId, position) => {
      if (sessionId && status === 'connected') moveNode(nodeId, position, { sync: false });
    },
    [moveNode, sessionId, status]
  );

  const handleMoveCommit = useCallback(
    (nodeId, position) => {
      if (sessionId && status === 'connected') moveNode(nodeId, position, { sync: true });
    },
    [moveNode, sessionId, status]
  );

  const handlePan = useCallback((nextView) => {
    setDomeNav((nav) => ({ ...nav, centerX: nextView.centerX, centerY: nextView.centerY }));
  }, []);

  const handleZoom = useCallback((factor) => {
    setDomeNav((nav) => ({ ...nav, zoom: zoomToDensity(nav.zoom * factor) }));
  }, []);

  const handleResetView = useCallback(() => {
    setDomeNav({ zoom: 1, centerX: null, centerY: null });
  }, []);

  const connectedSummary =
    status === 'connected'
      ? `, ${plural(domeData.cards.length, 'node')}, ${plural(domeData.edges.length, 'edge')}, ${plural(activeScene.roster.length, 'client')}`
      : null;

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
      <DomeNavigationControls
        view={domeOptions}
        zoom={domeNav.zoom}
        onZoom={handleZoom}
        onReset={handleResetView}
      />
      <div className="xr-hint">
        {error !== null ? (
          <span className="xr-error">Could not enter VR: {error}</span>
        ) : sessionId ? (
          <>
            Session {sessionId} — {status}
            {connectedSummary}
          </>
        ) : (
          <>
            Placeholder dome — create or connect to a session to render its nodes. Connect a headset
            via `adb reverse` and open http://localhost:5173 in the Quest Browser. See README.
          </>
        )}
      </div>
      {selectedDetail ? (
        <aside className="xr-detail" aria-label="Selected node detail">
          <div className="xr-detail-kicker">{selectedDetail.type}</div>
          <h2>{selectedDetail.name}</h2>
          <p>{selectedDetail.summary || 'No additional details are loaded for this node.'}</p>
          <code>{selectedDetail.id}</code>
        </aside>
      ) : null}
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
          <DomeGraph
            data={domeData}
            selectedNodeId={selectedNodeId}
            selectedDetail={selectedDetail}
            onSelect={handleSelectNode}
          />
          <XrRayInput
            enabled
            layoutBounds={activeLayoutBounds}
            domeOptions={domeOptions}
            onSelect={handleSelectNode}
            onMovePreview={handleMovePreview}
            onMoveCommit={handleMoveCommit}
            onPan={handlePan}
          />
        </XR>
      </Canvas>
    </>
  );
}
