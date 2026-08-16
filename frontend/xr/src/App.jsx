import { useMemo } from 'react';
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
  return (
    <>
      <button className="xr-enter" onClick={() => store.enterVR()}>
        Enter VR
      </button>
      <div className="xr-hint">
        Scaffold — placeholder dome. Connect a headset via `adb reverse` and open
        http://localhost:5173 in the Quest Browser. See README.
      </div>
      {/*
        `rotation` must be given explicitly: without it R3F calls
        camera.lookAt(0, 0, 0) on a declaratively-configured camera, which from
        eye height aims straight down at the floor and puts the whole dome
        off-screen in the flat preview. An XR session supplies its own camera
        pose, so this only affects the desktop view. The fov is wide enough to
        contain the dome's full ±60°/±45° wrap (DEFAULT_DOME) — the layout
        bounds always push the outermost nodes to those extremes.
      */}
      <Canvas camera={{ position: [0, 1.5, 0], fov: 100, rotation: [0, 0, 0] }}>
        <XR store={store}>
          <ambientLight intensity={0.8} />
          <directionalLight position={[2, 4, 1]} intensity={1} />
          <DomeNodes nodes={PLACEHOLDER_NODES} />
        </XR>
      </Canvas>
    </>
  );
}
