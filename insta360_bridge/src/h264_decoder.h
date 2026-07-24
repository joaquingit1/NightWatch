#pragma once

#include "frame_buffer.h"

#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>

class H264Decoder {
public:
    H264Decoder(FrameBuffer& buffer, int output_width, int output_height);
    ~H264Decoder();

    bool initialize();
    void feed(const uint8_t* data, size_t size);

private:
    bool decode_frame();
    bool ensure_decoder();

    FrameBuffer& buffer_;
    int output_width_;
    int output_height_;
    std::vector<uint8_t> bitstream_;
    std::mutex mutex_;
    bool initialized_{false};

#ifdef _WIN32
    struct Impl;
    std::unique_ptr<Impl> impl_;
#endif
};
