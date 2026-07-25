"use client";

import { Center } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { DRACOLoader } from "three/examples/jsm/loaders/DRACOLoader.js";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

const MODEL_PATH = "/models/robot-dog.glb";
const DRACO_DECODER = "https://www.gstatic.com/draco/versioned/decoders/1.5.7/";
// Clockwise from the previous side-on pose so the face reads toward camera.
const INITIAL_YAW = -0.35;

interface RobotDogModelProps {
  scrollProgress: number;
}

function FallbackRobot({ scrollProgress }: { scrollProgress: number }) {
  const groupRef = useRef<THREE.Group>(null);

  useFrame((_state, delta) => {
    const group = groupRef.current;
    if (!group) return;
    const dt = Math.min(delta, 0.1);
    const yaw = INITIAL_YAW + scrollProgress * Math.PI * 3.4;
    group.rotation.y = THREE.MathUtils.lerp(group.rotation.y, yaw, 1 - Math.exp(-8 * dt));
  });

  return (
    <group ref={groupRef} rotation={[0, INITIAL_YAW, 0]}>
      <Center>
        <group scale={1.6}>
          <mesh castShadow receiveShadow position={[0, 0.35, 0]}>
            <boxGeometry args={[0.55, 0.22, 0.9]} />
            <meshStandardMaterial color="#1a1f2e" metalness={0.35} roughness={0.45} />
          </mesh>
          <mesh castShadow receiveShadow position={[0.42, 0.42, 0.35]}>
            <boxGeometry args={[0.28, 0.18, 0.28]} />
            <meshStandardMaterial color="#222838" metalness={0.4} roughness={0.4} />
          </mesh>
          <mesh castShadow receiveShadow position={[0.22, 0.12, 0.38]}>
            <cylinderGeometry args={[0.07, 0.07, 0.32, 12]} />
            <meshStandardMaterial color="#2a3144" metalness={0.3} roughness={0.5} />
          </mesh>
          <mesh castShadow receiveShadow position={[-0.22, 0.12, 0.38]}>
            <cylinderGeometry args={[0.07, 0.07, 0.32, 12]} />
            <meshStandardMaterial color="#2a3144" metalness={0.3} roughness={0.5} />
          </mesh>
          <mesh castShadow receiveShadow position={[0.22, 0.12, -0.38]}>
            <cylinderGeometry args={[0.07, 0.07, 0.32, 12]} />
            <meshStandardMaterial color="#2a3144" metalness={0.3} roughness={0.5} />
          </mesh>
          <mesh castShadow receiveShadow position={[-0.22, 0.12, -0.38]}>
            <cylinderGeometry args={[0.07, 0.07, 0.32, 12]} />
            <meshStandardMaterial color="#2a3144" metalness={0.3} roughness={0.5} />
          </mesh>
        </group>
      </Center>
    </group>
  );
}

function LoadedRobot({
  scene,
  scrollProgress,
}: {
  scene: THREE.Group;
  scrollProgress: number;
}) {
  const groupRef = useRef<THREE.Group>(null);

  const prepared = useMemo(() => {
    const clone = scene.clone(true);
    const bodyMaterial = new THREE.MeshStandardMaterial({
      color: "#dfe1e6",
      metalness: 0.15,
      roughness: 0.65,
      envMapIntensity: 0.6,
    });
    const accentMaterial = new THREE.MeshStandardMaterial({
      color: "#20242c",
      metalness: 0.3,
      roughness: 0.55,
      envMapIntensity: 0.6,
    });

    clone.traverse((child) => {
      if (child instanceof THREE.Mesh) {
        child.castShadow = true;
        child.receiveShadow = true;

        // Compressed textures can lose their alpha/roughness maps, which makes
        // parts of the mesh render as translucent, wireframe-looking geometry.
        // Force every surface onto one of two flat, fully opaque materials so
        // the model reads as a solid object instead of a see-through outline.
        const original = child.material;
        const isDark =
          !Array.isArray(original) &&
          original instanceof THREE.MeshStandardMaterial &&
          original.color.getHSL({ h: 0, s: 0, l: 0 }).l < 0.35;
        child.material = isDark ? accentMaterial : bodyMaterial;
      }
    });

    return clone;
  }, [scene]);

  useFrame((_state, delta) => {
    const group = groupRef.current;
    if (!group) return;
    const dt = Math.min(delta, 0.1);
    const yaw = INITIAL_YAW + scrollProgress * Math.PI * 3.4;
    group.rotation.y = THREE.MathUtils.lerp(group.rotation.y, yaw, 1 - Math.exp(-8 * dt));
  });

  return (
    <group ref={groupRef} rotation={[0, INITIAL_YAW, 0]}>
      {/* Center puts the visual bbox middle at the group origin so the
          camera can lookAt(0,0,0) and get true center-center framing. */}
      <Center>
        <primitive object={prepared} scale={1.55} />
      </Center>
    </group>
  );
}

export function RobotDogModel({ scrollProgress }: RobotDogModelProps) {
  const [scene, setScene] = useState<THREE.Group | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const draco = new DRACOLoader();
    draco.setDecoderPath(DRACO_DECODER);
    const loader = new GLTFLoader();
    loader.setDRACOLoader(draco);

    loader.load(
      MODEL_PATH,
      (gltf) => {
        if (!cancelled) setScene(gltf.scene);
      },
      undefined,
      () => {
        if (!cancelled) setFailed(true);
      }
    );

    return () => {
      cancelled = true;
      draco.dispose();
    };
  }, []);

  if (failed || !scene) {
    return <FallbackRobot scrollProgress={scrollProgress} />;
  }

  return <LoadedRobot scene={scene} scrollProgress={scrollProgress} />;
}
