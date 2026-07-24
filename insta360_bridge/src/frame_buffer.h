#pragma once

#include <cstdint>
#include <mutex>
#include <vector>

class FrameBuffer {
public:
    void update_jpeg(std::vector<uint8_t> jpeg);
    void update_bgr(const uint8_t* bgr, int width, int height, int quality = 80);
    void update_rgba(const uint8_t* rgba, int width, int height, int quality = 80);
    bool copy_latest_jpeg(std::vector<uint8_t>& out) const;

private:
    static std::vector<uint8_t> encode_jpeg_bgr(const uint8_t* bgr, int width, int height,
                                                int quality);

    mutable std::mutex mutex_;
    std::vector<uint8_t> latest_jpeg_;
};
