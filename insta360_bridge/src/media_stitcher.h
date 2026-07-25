#pragma once

#include "frame_buffer.h"

#include <camera/camera.h>
#include <ins_realtime_stitcher.h>
#include <stream/stream_delegate.h>

#include <memory>
#include <string>

enum class StitcherMode {
    InCamera,
    Media,
};

class MediaStitcherPipeline {
public:
    MediaStitcherPipeline(FrameBuffer& buffer, int output_width, int output_height);
    ~MediaStitcherPipeline();

    bool setup(std::shared_ptr<ins_camera::Camera> camera);
    std::shared_ptr<ins_camera::StreamDelegate> create_delegate();
    bool start();
    void stop();

private:
    FrameBuffer& buffer_;
    int output_width_;
    int output_height_;
    std::shared_ptr<ins::RealTimeStitcher> stitcher_;
    std::shared_ptr<ins_camera::Camera> camera_;
};

class InCameraPipeline {
public:
    InCameraPipeline(FrameBuffer& buffer, int output_width, int output_height);
    ~InCameraPipeline();

    bool setup(std::shared_ptr<ins_camera::Camera> camera);
    std::shared_ptr<ins_camera::StreamDelegate> create_delegate();
    bool start();
    void stop();

private:
    FrameBuffer& buffer_;
    int output_width_;
    int output_height_;
    std::unique_ptr<class H264Decoder> decoder_;
    std::shared_ptr<ins_camera::Camera> camera_;
};

StitcherMode parse_stitcher_mode(const std::string& value);
