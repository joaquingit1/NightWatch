#include "media_stitcher.h"

#include "h264_decoder.h"

#include <algorithm>
#include <cstring>
#include <iostream>

namespace {

class MediaStitchDelegate : public ins_camera::StreamDelegate {
public:
    explicit MediaStitchDelegate(const std::shared_ptr<ins::RealTimeStitcher>& stitcher)
        : stitcher_(stitcher) {}

    void OnAudioData(const uint8_t* /*data*/, size_t /*size*/, int64_t /*timestamp*/) override {}

    void OnVideoData(const uint8_t* data, size_t size, int64_t timestamp, uint8_t stream_type,
                     int stream_index) override {
        if (stitcher_) {
            stitcher_->HandleVideoData(data, size, timestamp, stream_type, stream_index);
        }
    }

    void OnGyroData(const std::vector<ins_camera::GyroData>& data) override {
        if (!stitcher_) {
            return;
        }
        std::vector<ins::GyroData> converted(data.size());
        std::memcpy(converted.data(), data.data(), data.size() * sizeof(ins_camera::GyroData));
        stitcher_->HandleGyroData(converted);
    }

    void OnExposureData(const ins_camera::ExposureData& data) override {
        if (!stitcher_) {
            return;
        }
        ins::ExposureData exposure{};
        exposure.exposure_time = data.exposure_time;
        exposure.timestamp = data.timestamp;
        stitcher_->HandleExposureData(exposure);
    }

private:
    std::shared_ptr<ins::RealTimeStitcher> stitcher_;
};

class InCameraStreamDelegate : public ins_camera::StreamDelegate {
public:
    InCameraStreamDelegate(H264Decoder& decoder, bool use_primary_stream_only)
        : decoder_(decoder), use_primary_stream_only_(use_primary_stream_only) {}

    void OnAudioData(const uint8_t* /*data*/, size_t /*size*/, int64_t /*timestamp*/) override {}

    void OnVideoData(const uint8_t* data, size_t size, int64_t timestamp, uint8_t stream_type,
                     int stream_index) override {
        (void)timestamp;
        (void)stream_type;
        if (use_primary_stream_only_ && stream_index != 0) {
            return;
        }
        decoder_.feed(data, size);
    }

    void OnGyroData(const std::vector<ins_camera::GyroData>& /*data*/) override {}
    void OnExposureData(const ins_camera::ExposureData& /*data*/) override {}

private:
    H264Decoder& decoder_;
    bool use_primary_stream_only_;
};

ins::CameraInfo build_camera_info(const std::shared_ptr<ins_camera::Camera>& camera) {
    ins::CameraInfo camera_info;
    const auto preview_param = camera->GetPreviewParam();
    camera_info.cameraName = preview_param.camera_name;
    camera_info.decode_type = static_cast<ins::VideoDecodeType>(preview_param.encode_type);
    camera_info.offset = preview_param.offset;
    camera_info.window_crop_info_.crop_offset_x = preview_param.crop_info.crop_offset_x;
    camera_info.window_crop_info_.crop_offset_y = preview_param.crop_info.crop_offset_y;
    camera_info.window_crop_info_.dst_width = preview_param.crop_info.dst_width;
    camera_info.window_crop_info_.dst_height = preview_param.crop_info.dst_height;
    camera_info.window_crop_info_.src_width = preview_param.crop_info.src_width;
    camera_info.window_crop_info_.src_height = preview_param.crop_info.src_height;
    camera_info.gyro_timestamp = preview_param.delay_timestamp;
    camera_info.sweep_timestamp = preview_param.sweep_time;
    return camera_info;
}

}  // namespace

StitcherMode parse_stitcher_mode(const std::string& value) {
    if (value == "media") {
        return StitcherMode::Media;
    }
    return StitcherMode::InCamera;
}

MediaStitcherPipeline::MediaStitcherPipeline(FrameBuffer& buffer, int output_width,
                                             int output_height)
    : buffer_(buffer), output_width_(output_width), output_height_(output_height) {}

MediaStitcherPipeline::~MediaStitcherPipeline() {
    stop();
}

bool MediaStitcherPipeline::setup(std::shared_ptr<ins_camera::Camera> camera) {
    camera_ = std::move(camera);
    if (!camera_) {
        return false;
    }
    ins::InitEnv();
    stitcher_ = std::make_shared<ins::RealTimeStitcher>();
    stitcher_->SetCameraInfo(build_camera_info(camera_));
    stitcher_->SetStitchType(ins::STITCH_TYPE::DYNAMICSTITCH);
    stitcher_->EnableFlowState(true);
    stitcher_->SetOutputSize(output_width_, output_height_);
    stitcher_->SetStitchRealTimeDataCallback(
        [this](uint8_t* data[4], int linesize[4], int width, int height, int format,
               int64_t timestamp) {
            (void)linesize;
            (void)format;
            (void)timestamp;
            if (data[0] == nullptr || width <= 0 || height <= 0) {
                return;
            }
            buffer_.update_rgba(data[0], width, height);
        });
    return true;
}

std::shared_ptr<ins_camera::StreamDelegate> MediaStitcherPipeline::create_delegate() {
    return std::make_shared<MediaStitchDelegate>(stitcher_);
}

bool MediaStitcherPipeline::start() {
    if (!stitcher_) {
        return false;
    }
    stitcher_->StartStitch();
    return true;
}

void MediaStitcherPipeline::stop() {
    if (stitcher_) {
        stitcher_->CancelStitch();
    }
}

InCameraPipeline::InCameraPipeline(FrameBuffer& buffer, int output_width, int output_height)
    : buffer_(buffer), output_width_(output_width), output_height_(output_height) {}

InCameraPipeline::~InCameraPipeline() {
    stop();
}

bool InCameraPipeline::setup(std::shared_ptr<ins_camera::Camera> camera) {
    camera_ = std::move(camera);
    if (!camera_) {
        return false;
    }
    decoder_ = std::make_unique<H264Decoder>(buffer_, output_width_, output_height_);
    return decoder_->initialize();
}

std::shared_ptr<ins_camera::StreamDelegate> InCameraPipeline::create_delegate() {
    return std::make_shared<InCameraStreamDelegate>(*decoder_, true);
}

bool InCameraPipeline::start() {
    if (!camera_) {
        return false;
    }
    if (!camera_->EnableInCameraStitching(true)) {
        std::cerr << "EnableInCameraStitching failed; try --stitcher media\n";
        return false;
    }
    return true;
}

void InCameraPipeline::stop() {
    if (camera_) {
        camera_->EnableInCameraStitching(false);
    }
}
