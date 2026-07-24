#include "frame_buffer.h"
#include "media_stitcher.h"
#include "mjpeg_server.h"

#include <camera/camera.h>
#include <camera/device_discovery.h>
#include <camera/photography_settings.h>

#include <atomic>
#include <chrono>
#include <csignal>
#include <cstring>
#include <iostream>
#include <memory>
#include <string>
#include <thread>

namespace {

std::atomic<bool> g_running{true};

void handle_signal(int) {
    g_running = false;
}

struct Options {
    uint16_t port{5556};
    int output_width{960};
    int output_height{480};
    StitcherMode stitcher_mode{StitcherMode::InCamera};
    std::string serial;
    int retry_seconds{60};
    bool verbose{false};
    bool discover_only{false};
    std::string log_file;
};

Options parse_args(int argc, char** argv) {
    Options options;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--port" && i + 1 < argc) {
            options.port = static_cast<uint16_t>(std::stoi(argv[++i]));
        } else if (arg == "--width" && i + 1 < argc) {
            options.output_width = std::stoi(argv[++i]);
        } else if (arg == "--height" && i + 1 < argc) {
            options.output_height = std::stoi(argv[++i]);
        } else if (arg == "--stitcher" && i + 1 < argc) {
            options.stitcher_mode = parse_stitcher_mode(argv[++i]);
        } else if (arg == "--serial" && i + 1 < argc) {
            options.serial = argv[++i];
        } else if (arg == "--retry-seconds" && i + 1 < argc) {
            options.retry_seconds = std::stoi(argv[++i]);
        } else if (arg == "--verbose") {
            options.verbose = true;
        } else if (arg == "--discover-only") {
            options.discover_only = true;
        } else if (arg == "--log-file" && i + 1 < argc) {
            options.log_file = argv[++i];
        } else if (arg == "--help" || arg == "-h") {
            std::cout
                << "Usage: insta360_bridge [options]\n"
                << "  --port 5556           MJPEG HTTP port\n"
                << "  --width 960           Output width\n"
                << "  --height 480          Output height\n"
                << "  --stitcher incamera   In-camera stitch + H264 decode (default)\n"
                << "  --stitcher media      MediaSDK RealTimeStitcher fallback\n"
                << "  --serial <sn>         Lock to camera serial number\n"
                << "  --retry-seconds 60    Keep polling for a camera (0 = fail immediately)\n"
                << "  --verbose             Enable CameraSDK verbose logging\n"
                << "  --log-file <path>     Write CameraSDK logs to file\n"
                << "  --discover-only       List cameras and exit\n";
            std::exit(0);
        }
    }
    return options;
}

bool is_x4_or_newer(ins_camera::CameraType type) {
    return type == ins_camera::CameraType::Insta360X4 ||
           type == ins_camera::CameraType::Insta360X5 ||
           type == ins_camera::CameraType::Insta360X4Air;
}

bool configure_x4_live_stream(const std::shared_ptr<ins_camera::Camera>& camera,
                            ins_camera::CameraType camera_type) {
    if (!is_x4_or_newer(camera_type)) {
        return true;
    }
    if (!camera->SetVideoSubMode(ins_camera::SubVideoMode::VIDEO_LIVEVIEW)) {
        std::cerr << "SetVideoSubMode(VIDEO_LIVEVIEW) failed\n";
        return false;
    }
    ins_camera::RecordParams record_params;
    record_params.resolution = ins_camera::VideoResolution::RES_1920_960P30;
    record_params.bitrate = 0;
    if (!camera->SetVideoCaptureParams(
            record_params, ins_camera::CameraFunctionMode::FUNCTION_MODE_LIVE_STREAM)) {
        std::cerr << "SetVideoCaptureParams failed\n";
        return false;
    }
    return true;
}

void print_connection_help() {
    std::cerr
        << "\nInsta360 not visible to CameraSDK. Common fixes:\n"
        << "  USB (X4/X5):\n"
        << "    1. Wake the camera and plug in with the official data cable.\n"
        << "    2. On the camera screen, choose Android mode (not U-Disk / storage).\n"
        << "    3. Install the libusbK driver with Zadig (https://zadig.akeo.ie/)\n"
        << "       for the Arashi Vision / VID_2E1A device.\n"
        << "    4. Close Insta360 Studio or any app that may lock the camera.\n"
        << "  Wi-Fi:\n"
        << "    1. Connect this PC to the camera hotspot or the same LAN.\n"
        << "    2. Wake the camera and enable Wi-Fi.\n"
        << "  Verify SDK discovery:\n"
        << "    ..\\Windows_CameraSDK-*\\CameraSDK-*\\bin\\CameraSDKTest.exe\n"
        << "  Retry while plugging in: --retry-seconds 120 --verbose\n\n";
}

const char* connection_type_name(ins_camera::ConnectionType type) {
    switch (type) {
        case ins_camera::ConnectionType::USB:
            return "USB";
        case ins_camera::ConnectionType::Wifi:
            return "Wi-Fi";
        default:
            return "unknown";
    }
}

std::vector<ins_camera::DeviceDescriptor> discover_devices(
    ins_camera::DeviceDiscovery& discovery) {
    return discovery.GetAvailableDevices();
}

bool start_live_stream(const std::shared_ptr<ins_camera::Camera>& camera) {
    ins_camera::LiveStreamParam param;
    param.video_resolution = ins_camera::VideoResolution::RES_1920_960P30;
    param.lrv_video_resulution = ins_camera::VideoResolution::RES_1440_720P30;
    param.video_bitrate = 1024 * 1024 / 2;
    param.enable_audio = false;
    param.using_lrv = false;
    param.enable_gyro = true;
    return camera->StartLiveStreaming(param);
}

}  // namespace

#ifdef _WIN32
#include <objbase.h>
#endif

int main(int argc, char** argv) {
#ifdef _WIN32
    CoInitializeEx(nullptr, COINIT_MULTITHREADED);
#endif

    const Options options = parse_args(argc, argv);
    std::signal(SIGINT, handle_signal);
    std::signal(SIGTERM, handle_signal);

    if (!options.log_file.empty()) {
        ins_camera::SetLogPath(options.log_file);
    }
    ins_camera::SetLogLevel(
        options.verbose ? ins_camera::LogLevel::VERBOSE
                        : ins_camera::LogLevel::WARNING);

    ins_camera::DeviceDiscovery discovery;
    std::vector<ins_camera::DeviceDescriptor> devices;
    const int wait_seconds = options.retry_seconds < 0 ? 0 : options.retry_seconds;
    const auto deadline = std::chrono::steady_clock::now() +
                          std::chrono::seconds(wait_seconds);
    do {
        devices = discover_devices(discovery);
        if (!devices.empty()) {
            break;
        }
        if (wait_seconds <= 0) {
            break;
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            break;
        }
        std::cerr << "Waiting for Insta360 camera...\n";
        std::this_thread::sleep_for(std::chrono::seconds(2));
    } while (g_running);

    if (devices.empty()) {
        std::cerr << "No Insta360 camera found. Connect via USB or Wi-Fi.\n";
        print_connection_help();
        return 1;
    }

    if (options.discover_only) {
        for (const auto& device : devices) {
            std::cout << "serial=" << device.serial_number
                      << " name=" << device.camera_name
                      << " fw=" << device.fw_version
                      << " link=" << connection_type_name(device.info.connection_type)
                      << "\n";
        }
        discovery.FreeDeviceDescriptors(devices);
        return 0;
    }

    FrameBuffer frame_buffer;
    MjpegServer server(frame_buffer);
    if (!server.start(options.port)) {
        std::cerr << "Failed to start MJPEG server\n";
        discovery.FreeDeviceDescriptors(devices);
        return 1;
    }

    const ins_camera::DeviceDescriptor* selected = &devices.front();
    if (!options.serial.empty()) {
        bool found = false;
        for (const auto& device : devices) {
            if (device.serial_number == options.serial) {
                selected = &device;
                found = true;
                break;
            }
        }
        if (!found) {
            std::cerr << "Camera serial not found: " << options.serial << "\n";
            discovery.FreeDeviceDescriptors(devices);
            return 1;
        }
    }

    const auto camera_type = selected->camera_type;
    const std::string camera_name = selected->camera_name;
    const std::string camera_serial = selected->serial_number;
    const ins_camera::DeviceConnectionInfo connection_info = selected->info;

    std::cout << "Using camera " << camera_name << " (" << camera_serial << ") via "
              << connection_type_name(connection_info.connection_type) << "\n";

    auto camera = std::make_shared<ins_camera::Camera>(connection_info);
    discovery.FreeDeviceDescriptors(devices);

    if (!camera->Open()) {
        std::cerr << "Failed to open camera\n";
        return 1;
    }

    std::shared_ptr<ins_camera::StreamDelegate> delegate;
    std::unique_ptr<MediaStitcherPipeline> media_pipeline;
    std::unique_ptr<InCameraPipeline> incamera_pipeline;

    if (options.stitcher_mode == StitcherMode::Media) {
        media_pipeline = std::make_unique<MediaStitcherPipeline>(
            frame_buffer, options.output_width, options.output_height);
        if (!media_pipeline->setup(camera)) {
            std::cerr << "Failed to setup MediaSDK stitcher\n";
            camera->Close();
            return 1;
        }
        delegate = media_pipeline->create_delegate();
    } else {
        incamera_pipeline = std::make_unique<InCameraPipeline>(
            frame_buffer, options.output_width, options.output_height);
        if (!incamera_pipeline->setup(camera)) {
            std::cerr << "Failed to setup in-camera pipeline\n";
            camera->Close();
            return 1;
        }
        delegate = incamera_pipeline->create_delegate();
    }

    camera->SetStreamDelegate(delegate);

    if (!configure_x4_live_stream(camera, camera_type)) {
        camera->Close();
        return 1;
    }

    if (incamera_pipeline && !incamera_pipeline->start()) {
        std::cerr << "In-camera stitching unavailable, retry with --stitcher media\n";
        camera->Close();
        return 1;
    }

    if (!start_live_stream(camera)) {
        std::cerr << "StartLiveStreaming failed\n";
        camera->Close();
        return 1;
    }

    if (media_pipeline) {
        media_pipeline->start();
    }

    std::cout << "Streaming. Open http://127.0.0.1:" << options.port
              << "/video in a browser or set INSTA360_MJPEG_URL for NightWatch.\n";

    while (g_running) {
        std::this_thread::sleep_for(std::chrono::milliseconds(200));
    }

    std::cout << "Stopping...\n";
    camera->StopLiveStreaming();
    if (media_pipeline) {
        media_pipeline->stop();
    }
    if (incamera_pipeline) {
        incamera_pipeline->stop();
    }
    camera->Close();
    server.stop();

#ifdef _WIN32
    CoUninitialize();
#endif
    return 0;
}
