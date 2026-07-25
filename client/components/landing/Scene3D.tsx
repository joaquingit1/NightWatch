"use client";

import { Environment, PerspectiveCamera } from "@react-three/drei";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { Bloom, EffectComposer } from "@react-three/postprocessing";
import gsap from "gsap";
import { ScrollTrigger } from "gsap/ScrollTrigger";
import { Suspense, useEffect, useRef, useState } from "react";
import * as THREE from "three";

import { RobotDogModel } from "./RobotDogModel";
import { SceneLoader } from "./SceneLoader";

interface CameraRigProps {
  scrollProgress: number;
  pointer: { x: number; y: number };
}

function CameraRig({ scrollProgress, pointer }: CameraRigProps) {
  const { camera, size } = useThree();

  useFrame((state) => {
    const dt = Math.min(state.clock.getDelta(), 0.1);

    // Dolly back on narrow/portrait canvases so the full dog (legs +
    // body width) stays inside the frame instead of getting cropped.
    const aspect = size.width / Math.max(size.height, 1);
    const baseAspect = 0.8;
    const zoom = Math.max(1, baseAspect / aspect);

    // Mild scroll orbit around a front-facing baseline. lookAt stays on
    // the model center so framing is vertically + horizontally centered.
    const orbit = scrollProgress * 0.4;
    // Model is Center'd at the world origin. lookAt is biased slightly
    // RIGHT and DOWN of center so the dog reads left-of-center / higher
    // in the frame (lookAt left would push it right — inverted).
    const lookX = 0.14;
    const lookY = -0.08;
    const targetX = lookX + (0.2 + Math.sin(orbit) * 0.2 + pointer.x * 0.05) * zoom;
    const targetY = lookY + (0.45 + pointer.y * -0.03) * zoom;
    const targetZ = (6.8 - scrollProgress * 0.2) * zoom;

    camera.position.x = THREE.MathUtils.lerp(camera.position.x, targetX, 1 - Math.exp(-6 * dt));
    camera.position.y = THREE.MathUtils.lerp(camera.position.y, targetY, 1 - Math.exp(-6 * dt));
    camera.position.z = THREE.MathUtils.lerp(camera.position.z, targetZ, 1 - Math.exp(-6 * dt));
    camera.lookAt(lookX, lookY, 0);
  });

  return null;
}

function SceneContent({
  scrollProgress,
  pointer,
}: {
  scrollProgress: number;
  pointer: { x: number; y: number };
}) {
  return (
    <>
      <fog attach="fog" args={["#f2efe7", 10, 24]} />
      <PerspectiveCamera makeDefault position={[0.28, 0.43, 6.8]} fov={30} />
      <CameraRig scrollProgress={scrollProgress} pointer={pointer} />

      <ambientLight intensity={0.75} color="#fff8ec" />
      <directionalLight
        castShadow
        color="#fffaf0"
        intensity={1.6}
        position={[4.5, 6.5, 3.2]}
        shadow-mapSize={[2048, 2048]}
        shadow-camera-far={20}
        shadow-camera-left={-6}
        shadow-camera-right={6}
        shadow-camera-top={6}
        shadow-camera-bottom={-6}
      />
      <spotLight
        color="#173ed1"
        intensity={2.0}
        angle={0.42}
        penumbra={0.6}
        position={[-3.5, 4.5, 2.5]}
        castShadow={false}
      />
      <spotLight
        color="#ff4b13"
        intensity={1.5}
        angle={0.38}
        penumbra={0.7}
        position={[3.8, 2.2, -2.8]}
        castShadow={false}
      />

      <Environment preset="city" />

      <RobotDogModel scrollProgress={scrollProgress} />

      <EffectComposer multisampling={0}>
        <Bloom
          intensity={0.04}
          luminanceThreshold={0.97}
          luminanceSmoothing={0.2}
          mipmapBlur
        />
      </EffectComposer>
    </>
  );
}

interface Scene3DProps {
  className?: string;
}

export function Scene3D({ className }: Scene3DProps) {
  const [scrollProgress, setScrollProgress] = useState(0);
  const [pointer, setPointer] = useState({ x: 0, y: 0 });
  const [reducedMotion, setReducedMotion] = useState(false);
  const [sceneReady, setSceneReady] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const updateMotion = () => setReducedMotion(media.matches);
    updateMotion();
    media.addEventListener("change", updateMotion);

    gsap.registerPlugin(ScrollTrigger);

    const trigger = ScrollTrigger.create({
      trigger: "#landing-pinned",
      start: "top top",
      end: "bottom bottom",
      scrub: true,
      onUpdate: (self) => setScrollProgress(self.progress),
    });

    const onPointerMove = (event: PointerEvent) => {
      const rect = containerRef.current?.getBoundingClientRect();
      if (!rect) return;
      const x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
      const y = ((event.clientY - rect.top) / rect.height) * 2 - 1;
      setPointer({ x: THREE.MathUtils.clamp(x, -1, 1), y: THREE.MathUtils.clamp(y, -1, 1) });
    };

    const onResize = () => ScrollTrigger.refresh();

    window.addEventListener("pointermove", onPointerMove, { passive: true });
    window.addEventListener("resize", onResize, { passive: true });
    return () => {
      media.removeEventListener("change", updateMotion);
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("resize", onResize);
      trigger.kill();
    };
  }, []);

  return (
    <div ref={containerRef} className={className}>
      {!sceneReady && <SceneLoader />}
      <Canvas
        shadows
        dpr={reducedMotion ? 1 : [1, 1.75]}
        gl={{
          antialias: true,
          alpha: true,
          powerPreference: "high-performance",
          preserveDrawingBuffer: true,
        }}
        onCreated={() => setSceneReady(true)}
      >
        <Suspense fallback={null}>
          <SceneContent
            scrollProgress={reducedMotion ? 0 : scrollProgress}
            pointer={reducedMotion ? { x: 0, y: 0 } : pointer}
          />
        </Suspense>
      </Canvas>
    </div>
  );
}
