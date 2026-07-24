#include "frame_buffer.h"

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <windows.h>
#include <wincodec.h>
#pragma comment(lib, "windowscodecs.lib")
#endif

#include <algorithm>
#include <cstring>

namespace {

#ifdef _WIN32
std::vector<uint8_t> encode_jpeg_wic(const uint8_t* bgr, int width, int height, int quality) {
    std::vector<uint8_t> output;
    if (bgr == nullptr || width <= 0 || height <= 0) {
        return output;
    }

    IWICImagingFactory* factory = nullptr;
    if (FAILED(CoCreateInstance(CLSID_WICImagingFactory, nullptr, CLSCTX_INPROC_SERVER,
                                IID_PPV_ARGS(&factory)))) {
        return output;
    }

    IWICBitmap* bitmap = nullptr;
    if (FAILED(factory->CreateBitmap(static_cast<UINT>(width), static_cast<UINT>(height),
                                   GUID_WICPixelFormat24bppBGR, WICBitmapCacheOnDemand,
                                   &bitmap))) {
        factory->Release();
        return output;
    }

    WICRect rect{0, 0, width, height};
    IWICBitmapLock* lock = nullptr;
    if (FAILED(bitmap->Lock(&rect, WICBitmapLockWrite, &lock))) {
        bitmap->Release();
        factory->Release();
        return output;
    }

    UINT buffer_size = 0;
    BYTE* dest = nullptr;
    if (SUCCEEDED(lock->GetDataPointer(&buffer_size, &dest)) && dest != nullptr) {
        UINT stride = 0;
        lock->GetStride(&stride);
        const size_t row_bytes = static_cast<size_t>(width) * 3;
        for (int y = 0; y < height; ++y) {
            std::memcpy(dest + static_cast<size_t>(y) * stride, bgr + y * row_bytes, row_bytes);
        }
    }
    lock->Release();

    IStream* stream = nullptr;
    if (FAILED(CreateStreamOnHGlobal(nullptr, TRUE, &stream))) {
        bitmap->Release();
        factory->Release();
        return output;
    }

    IWICBitmapEncoder* encoder = nullptr;
    if (FAILED(factory->CreateEncoder(GUID_ContainerFormatJpeg, nullptr, &encoder))) {
        stream->Release();
        bitmap->Release();
        factory->Release();
        return output;
    }
    encoder->Initialize(stream, WICBitmapEncoderNoCache);

    IWICBitmapFrameEncode* frame = nullptr;
    IPropertyBag2* props = nullptr;
    if (SUCCEEDED(encoder->CreateNewFrame(&frame, &props))) {
        PROPBAG2 option{};
        option.pstrName = const_cast<LPOLESTR>(L"ImageQuality");
        VARIANT value;
        VariantInit(&value);
        value.vt = VT_R4;
        value.fltVal = static_cast<float>(std::clamp(quality, 1, 100)) / 100.0f;
        props->Write(1, &option, &value);
        frame->Initialize(props);
        frame->SetSize(static_cast<UINT>(width), static_cast<UINT>(height));
        WICPixelFormatGUID format = GUID_WICPixelFormat24bppBGR;
        frame->SetPixelFormat(&format);
        frame->WriteSource(bitmap, nullptr);
        frame->Commit();
        encoder->Commit();
        VariantClear(&value);
        props->Release();
        frame->Release();
    }

    STATSTG stats{};
    if (SUCCEEDED(stream->Stat(&stats, STATFLAG_NONAME))) {
        const ULONG size = static_cast<ULONG>(stats.cbSize.QuadPart);
        output.resize(size);
        LARGE_INTEGER seek{};
        stream->Seek(seek, STREAM_SEEK_SET, nullptr);
        ULONG read = 0;
        stream->Read(output.data(), size, &read);
        output.resize(read);
    }

    encoder->Release();
    stream->Release();
    bitmap->Release();
    factory->Release();
    return output;
}
#endif

}  // namespace

void FrameBuffer::update_jpeg(std::vector<uint8_t> jpeg) {
    std::lock_guard<std::mutex> lock(mutex_);
    latest_jpeg_ = std::move(jpeg);
}

std::vector<uint8_t> FrameBuffer::encode_jpeg_bgr(const uint8_t* bgr, int width, int height,
                                                  int quality) {
#ifdef _WIN32
    return encode_jpeg_wic(bgr, width, height, quality);
#else
    (void)bgr;
    (void)width;
    (void)height;
    (void)quality;
    return {};
#endif
}

void FrameBuffer::update_bgr(const uint8_t* bgr, int width, int height, int quality) {
    auto encoded = encode_jpeg_bgr(bgr, width, height, quality);
    if (!encoded.empty()) {
        update_jpeg(std::move(encoded));
    }
}

void FrameBuffer::update_rgba(const uint8_t* rgba, int width, int height, int quality) {
    if (rgba == nullptr || width <= 0 || height <= 0) {
        return;
    }
    std::vector<uint8_t> bgr(static_cast<size_t>(width) * static_cast<size_t>(height) * 3);
    for (int i = 0; i < width * height; ++i) {
        bgr[static_cast<size_t>(i) * 3 + 0] = rgba[i * 4 + 0];
        bgr[static_cast<size_t>(i) * 3 + 1] = rgba[i * 4 + 1];
        bgr[static_cast<size_t>(i) * 3 + 2] = rgba[i * 4 + 2];
    }
    update_bgr(bgr.data(), width, height, quality);
}

bool FrameBuffer::copy_latest_jpeg(std::vector<uint8_t>& out) const {
    std::lock_guard<std::mutex> lock(mutex_);
    if (latest_jpeg_.empty()) {
        return false;
    }
    out = latest_jpeg_;
    return true;
}
