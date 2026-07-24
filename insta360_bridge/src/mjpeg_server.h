#pragma once

#include "frame_buffer.h"

#include <atomic>
#include <cstdint>
#include <thread>

class MjpegServer {
public:
    explicit MjpegServer(FrameBuffer& buffer);
    ~MjpegServer();

    bool start(uint16_t port);
    void stop();

private:
    void serve_loop(uint16_t port);

    FrameBuffer& buffer_;
    std::thread thread_;
    std::atomic<bool> running_{false};
};
