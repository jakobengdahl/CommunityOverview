import { useMemo, useState } from 'react';
import { Canvas } from '@react-three/fiber';
import { XR, createXRStore } from '@react-three/xr';
import { domePosition, layoutBounds } from './domeLayout.js';

// Placeholder graph until the shared-session sync is wired in (next task).
// These are a handful of nodes with 2D layout positions — exactly the shape
// the real sessionSyncClient will provide — so the dome mapping can be seen
// and walked around on-device before any data plumbing exists.
const PLACEHOLDER_NODES = [
  { id: 'a', x: 0, y: 0, color: '#6ee7b7' },
  { id: 'b', x: 100, y: 20, color: '#60a5fa' },
  { id: 'c', x: 40, y: 80, color: '#f472b6' },
  { id: 'd', x: 90, y: 90, color: '#fbbf24' },
  { id: 'e', x: 10, y: 60, color: '#a78bfa' },
];

const store = createXRStore();

function DomeNodes({ nodes }) {
  const bounds = useMemo(() => layoutBounds(nodes), [nodes]);
  return (
    <group>
      {nodes.map((n) => {
        const p = domePosition(n.x, n.y, bounds);
        return (
          <mesh key={n.id} position={[p.x, p.y + 1.5, p.z]}>
            <boxGeometry args={[0.3, 0.3, 0.3]} />
            <meshStandardMaterial color={n.color} />
          </mesh>
        );
      })}
    </group>
  );
}

export default function App() {
  // enterVR() rejects on a browser without WebXR — which is every desktop
  // browser running the flat preview — and if the user denies the session.
  // Surface that instead of leaving an unhandled rejection and a dead button.
  const [error, setError] = useState(null);

  return (
    <>
      <button
        className="xr-enter"
        onClick={() => {
          setError(null);
          store.enterVR().catch((err) => setError(err.message));
        }}
      >
        Enter VR
      </button>
      <div className="xr-hint">
        {error ? (
          <span className="xr-error">Could not enter VR: {error}</span>
        ) : (
          <>
            Scaffold — placeholder dome. Connect a headset via `adb reverse` and open
            http://localhost:5173 in the Quest Browser. See README.
          </>
        )}
      </div>
      {/*
        Flat-preview camera only — inside an XR session the runtime owns the
        camera pose and projection, so neither of these props applies there.

        `rotation` must be passed explicitly: without it R3F calls
        camera.lookAt(0, 0, 0) on a declaratively-configured camera, which from
        eye height aims straight down at the floor and puts the dome off-screen.

        The dome wraps ±60° horizontally and ±45° vertically around its centre,
        which is more than a flat viewport can show from that centre: a frustum
        is rectangular, so a corner node's vertical screen angle is
        atan(tan(elevation) / cos(azimuth)) — 63° for the ±60°/±45° corner, not
        45°. Containing that from the dome centre would need a ~135° vertical
        fov. Backing the preview camera off along +Z instead keeps the whole
        wrap in frame at a sane fov (verified for every aspect ratio >= 1).
      */}
      <Canvas camera={{ position: [0, 1.5, 2.5], fov: 100, rotation: [0, 0, 0] }}>
        <XR store={store}>
          <ambientLight intensity={0.8} />
          <directionalLight position={[2, 4, 1]} intensity={1} />
          <DomeNodes nodes={PLACEHOLDER_NODES} />
        </XR>
      </Canvas>
    </>
  );
}
