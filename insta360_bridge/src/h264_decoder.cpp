#include "h264_decoder.h"

#include <algorithm>
#include <cstring>
#include <iostream>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mftransform.h>

#pragma comment(lib, "mfplat.lib")
#pragma comment(lib, "mfuuid.lib")

struct H264Decoder::Impl {
    IMFTransform* decoder{nullptr};
    DWORD input_stream_id{0};
    DWORD output_stream_id{0};
};

namespace {

HRESULT set_input_type(IMFTransform* transform, DWORD stream_id) {
    IMFMediaType* media_type = nullptr;
    HRESULT hr = MFCreateMediaType(&media_type);
    if (FAILED(hr)) {
        return hr;
    }
    hr = media_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (SUCCEEDED(hr)) {
        hr = media_type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
    }
    if (SUCCEEDED(hr)) {
        hr = transform->SetInputType(stream_id, media_type, 0);
    }
    media_type->Release();
    return hr;
}

HRESULT set_output_type(IMFTransform* transform, DWORD stream_id) {
    IMFMediaType* media_type = nullptr;
    HRESULT hr = MFCreateMediaType(&media_type);
    if (FAILED(hr)) {
        return hr;
    }
    hr = media_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
    if (SUCCEEDED(hr)) {
        hr = media_type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
    }
    if (SUCCEEDED(hr)) {
        hr = transform->SetOutputType(stream_id, media_type, 0);
    }
    media_type->Release();
    return hr;
}

bool nv12_to_bgr(const uint8_t* nv12, int width, int height, std::vector<uint8_t>& bgr) {
    bgr.resize(static_cast<size_t>(width) * static_cast<size_t>(height) * 3);
    const int y_size = width * height;
    for (int y = 0; y < height; ++y) {
        for (int x = 0; x < width; ++x) {
            const int y_index = y * width + x;
            const int uv_index = y_size + (y / 2) * width + (x & ~1);
            const float Y = static_cast<float>(nv12[y_index]);
            const float U = static_cast<float>(nv12[uv_index]) - 128.0f;
            const float V = static_cast<float>(nv12[uv_index + 1]) - 128.0f;
            const float R = Y + 1.402f * V;
            const float G = Y - 0.344136f * U - 0.714136f * V;
            const float B = Y + 1.772f * U;
            const size_t out = static_cast<size_t>(y_index) * 3;
            bgr[out + 0] = static_cast<uint8_t>(std::clamp(B, 0.0f, 255.0f));
            bgr[out + 1] = static_cast<uint8_t>(std::clamp(G, 0.0f, 255.0f));
            bgr[out + 2] = static_cast<uint8_t>(std::clamp(R, 0.0f, 255.0f));
        }
    }
    return true;
}

void resize_bgr_nearest(const std::vector<uint8_t>& src, int src_w, int src_h,
                        std::vector<uint8_t>& dst, int dst_w, int dst_h) {
    dst.resize(static_cast<size_t>(dst_w) * static_cast<size_t>(dst_h) * 3);
    for (int y = 0; y < dst_h; ++y) {
        const int src_y = y * src_h / dst_h;
        for (int x = 0; x < dst_w; ++x) {
            const int src_x = x * src_w / dst_w;
            const size_t src_index =
                (static_cast<size_t>(src_y) * static_cast<size_t>(src_w) + src_x) * 3;
            const size_t dst_index =
                (static_cast<size_t>(y) * static_cast<size_t>(dst_w) + x) * 3;
            dst[dst_index + 0] = src[src_index + 0];
            dst[dst_index + 1] = src[src_index + 1];
            dst[dst_index + 2] = src[src_index + 2];
        }
    }
}

}  // namespace
#endif

H264Decoder::H264Decoder(FrameBuffer& buffer, int output_width, int output_height)
    : buffer_(buffer), output_width_(output_width), output_height_(output_height) {
#ifdef _WIN32
    impl_ = std::make_unique<Impl>();
#endif
}

H264Decoder::~H264Decoder() {
#ifdef _WIN32
    if (impl_ && impl_->decoder) {
        impl_->decoder->Release();
        impl_->decoder = nullptr;
    }
#endif
}

bool H264Decoder::initialize() {
#ifdef _WIN32
    HRESULT hr = MFStartup(MF_VERSION);
    if (FAILED(hr)) {
        std::cerr << "MFStartup failed\n";
        return false;
    }
    initialized_ = true;
    return true;
#else
    std::cerr << "H264Decoder only supported on Windows\n";
    return false;
#endif
}

bool H264Decoder::ensure_decoder() {
#ifdef _WIN32
    if (impl_->decoder) {
        return true;
    }

    const MFT_REGISTER_TYPE_INFO input = {MFMediaType_Video, MFVideoFormat_H264};
    const MFT_REGISTER_TYPE_INFO output = {MFMediaType_Video, MFVideoFormat_NV12};
    IMFActivate** activates = nullptr;
    UINT32 count = 0;
    HRESULT hr = MFTEnumEx(MFT_CATEGORY_VIDEO_DECODER,
                           MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER, &input,
                           &output, &activates, &count);
    if (FAILED(hr) || count == 0 || activates == nullptr) {
        std::cerr << "No H264 decoder MFT found\n";
        return false;
    }

    hr = activates[0]->ActivateObject(IID_PPV_ARGS(&impl_->decoder));
    for (UINT32 i = 0; i < count; ++i) {
        activates[i]->Release();
    }
    CoTaskMemFree(activates);
    if (FAILED(hr) || impl_->decoder == nullptr) {
        std::cerr << "Failed to activate H264 decoder MFT\n";
        return false;
    }

    DWORD input_count = 0;
    DWORD output_count = 0;
    hr = impl_->decoder->GetStreamCount(&input_count, &output_count);
    if (FAILED(hr) || input_count == 0 || output_count == 0) {
        return false;
    }
    impl_->input_stream_id = 0;
    impl_->output_stream_id = 0;

    hr = set_input_type(impl_->decoder, impl_->input_stream_id);
    if (FAILED(hr)) {
        return false;
    }
    hr = set_output_type(impl_->decoder, impl_->output_stream_id);
    if (FAILED(hr)) {
        return false;
    }
    hr = impl_->decoder->ProcessMessage(MFT_MESSAGE_COMMAND_FLUSH, 0);
    if (FAILED(hr)) {
        return false;
    }
    hr = impl_->decoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
    if (FAILED(hr)) {
        return false;
    }
    hr = impl_->decoder->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);
    return SUCCEEDED(hr);
#else
    return false;
#endif
}

void H264Decoder::feed(const uint8_t* data, size_t size) {
    if (data == nullptr || size == 0) {
        return;
    }
    std::lock_guard<std::mutex> lock(mutex_);
    bitstream_.insert(bitstream_.end(), data, data + size);
    decode_frame();
}

bool H264Decoder::decode_frame() {
#ifdef _WIN32
    if (!initialized_ || bitstream_.empty()) {
        return false;
    }
    if (!ensure_decoder()) {
        return false;
    }

    IMFMediaBuffer* media_buffer = nullptr;
    HRESULT hr = MFCreateMemoryBuffer(static_cast<DWORD>(bitstream_.size()), &media_buffer);
    if (FAILED(hr)) {
        return false;
    }

    BYTE* buffer_ptr = nullptr;
    hr = media_buffer->Lock(&buffer_ptr, nullptr, nullptr);
    if (FAILED(hr)) {
        media_buffer->Release();
        return false;
    }
    std::memcpy(buffer_ptr, bitstream_.data(), bitstream_.size());
    media_buffer->Unlock();
    media_buffer->SetCurrentLength(static_cast<DWORD>(bitstream_.size()));

    IMFSample* sample = nullptr;
    hr = MFCreateSample(&sample);
    if (FAILED(hr)) {
        media_buffer->Release();
        return false;
    }
    sample->AddBuffer(media_buffer);
    media_buffer->Release();

    sample->SetSampleTime(0);
    sample->SetSampleDuration(333333);

    hr = impl_->decoder->ProcessInput(impl_->input_stream_id, sample, 0);
    sample->Release();
    if (FAILED(hr)) {
        return false;
    }

    MFT_OUTPUT_DATA_BUFFER output_data{};
    output_data.dwStreamID = impl_->output_stream_id;
    DWORD status = 0;
    hr = impl_->decoder->ProcessOutput(0, 1, &output_data, &status);
    if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) {
        return false;
    }
    if (FAILED(hr) || output_data.pSample == nullptr) {
        return false;
    }

    IMFMediaBuffer* output_buffer = nullptr;
    hr = output_data.pSample->ConvertToContiguousBuffer(&output_buffer);
    output_data.pSample->Release();
    if (FAILED(hr) || output_buffer == nullptr) {
        return false;
    }

    BYTE* out_ptr = nullptr;
    DWORD out_len = 0;
    hr = output_buffer->Lock(&out_ptr, nullptr, &out_len);
    if (FAILED(hr)) {
        output_buffer->Release();
        return false;
    }

    UINT32 width = static_cast<UINT32>(output_width_);
    UINT32 height = static_cast<UINT32>(output_height_);
    IMFMediaType* output_type = nullptr;
    if (SUCCEEDED(impl_->decoder->GetOutputCurrentType(impl_->output_stream_id, &output_type))) {
        MFGetAttributeSize(output_type, MF_MT_FRAME_SIZE, &width, &height);
        output_type->Release();
    }

    std::vector<uint8_t> bgr;
    if (nv12_to_bgr(out_ptr, static_cast<int>(width), static_cast<int>(height), bgr)) {
        if (static_cast<int>(width) != output_width_ ||
            static_cast<int>(height) != output_height_) {
            std::vector<uint8_t> resized;
            resize_bgr_nearest(bgr, static_cast<int>(width), static_cast<int>(height), resized,
                               output_width_, output_height_);
            buffer_.update_bgr(resized.data(), output_width_, output_height_);
        } else {
            buffer_.update_bgr(bgr.data(), static_cast<int>(width), static_cast<int>(height));
        }
    }

    output_buffer->Unlock();
    output_buffer->Release();
    bitstream_.clear();
    return true;
#else
    return false;
#endif
}
