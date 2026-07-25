# Insta360 Bridge (Night Watch)

Local-only C++ sidecar that streams stitched Insta360 X4/X5 preview frames as MJPEG for the Night Watch booth stack.

**Important:** The Insta360 CameraSDK and MediaSDK are proprietary and **must not be committed or redistributed**. Keep them in the git-ignored folder:

`Windows_CameraSDK-2.1.1_MediaSDK-3.1.3/`

## Prerequisites

- Windows 10/11
- CMake 3.20+
- **Visual Studio 2019 or 2022 Build Tools** with the **Desktop development with C++** workload (MSVC required; MinGW will not link the Insta360 `.lib` files)
- Insta360 camera connected via USB or Wi-Fi

## Build

```powershell
$env:INSTA360_SDK_ROOT = "C:\Users\ASUS\Documents\Computer Science\NightWatch\Windows_CameraSDK-2.1.1_MediaSDK-3.1.3"
cd insta360_bridge
.\build.ps1
```

`build.ps1` auto-detects VS 2019 or 2022 via `vswhere`. Manual configure:

```powershell
cmake -B build -G "Visual Studio 16 2019" -A x64 -DINSTA360_SDK_ROOT="$env:INSTA360_SDK_ROOT"
cmake --build build --config Release
```

Runtime DLLs from the SDK are copied next to `insta360_bridge.exe` automatically after build.

## Run

```powershell
.\build\Release\insta360_bridge.exe --port 5556 --stitcher incamera
```

Options:

| Flag | Default | Description |
|------|---------|-------------|
| `--port` | `5556` | MJPEG HTTP port |
| `--width` | `960` | Output width |
| `--height` | `480` | Output height |
| `--stitcher incamera` | default | X4/X5 in-camera stitch + H.264 decode |
| `--stitcher media` | | MediaSDK `RealTimeStitcher` fallback |
| `--serial` | first camera | Lock to a specific camera serial |

Open `http://127.0.0.1:5556/video` in a browser to verify the stream.

## Night Watch wiring

In `server/.env.local`:

```env
CAMERA_SOURCE=insta360
INSTA360_MJPEG_URL=http://127.0.0.1:5556/video
SCORER_BACKEND=live
```

For Go2 robot POV when the bridge runs on the robot laptop:

```env
CAMERA_SOURCE=robot
ROBOT_CAMERA_URL=http://<robot-laptop-ip>:5556/video
```

## Troubleshooting

### No camera found

The bridge and Insta360's own `CameraSDKTest.exe` both use `DeviceDiscovery`. If they report **no device**, the camera is not in SDK control mode yet.

**USB (X4/X5 — most common at the booth):**

1. Wake the camera and connect with the official **data** cable (not charge-only).
2. On the camera screen, pick **Android** mode when prompted — **not U-Disk / storage**.
   (Older models: Settings → General → USB Mode → Android.)
3. On Windows, install the **libusbK** driver with [Zadig](https://zadig.akeo.ie/) for the **Arashi Vision** / `VID_2E1A` device. The Insta360 SDK README requires this.
4. Close Insta360 Studio or any app that may lock the camera.
5. Replug USB after switching modes.

**Quick SDK check (run from the SDK `bin` folder):**

```powershell
cd ..\Windows_CameraSDK-2.1.1_MediaSDK-3.1.3\CameraSDK-*\bin
.\CameraSDKTest.exe
```

You should see a serial number and camera type before the bridge will work.

**Wi-Fi:**

1. Connect this PC to the camera hotspot or the same LAN.
2. Wake the camera and enable Wi-Fi.

**Bridge diagnostics:**

```powershell
.\build\Release\insta360_bridge.exe --discover-only --verbose
.\build\Release\insta360_bridge.exe --retry-seconds 120 --verbose
```

### Other issues

- **Black stream with `--stitcher incamera`:** retry with `--stitcher media`
- **OpenCV not found at build time:** not required — JPEG encoding uses Windows WIC
- **Visual Studio 17 2022 not found:** you likely have VS 2019 Build Tools; run `.\build.ps1` or use `-G "Visual Studio 16 2019" -A x64`
