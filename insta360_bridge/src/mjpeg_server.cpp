#include "mjpeg_server.h"

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "ws2_32.lib")
#endif

#include <chrono>
#include <cstring>
#include <iostream>
#include <sstream>
#include <string>
#include <thread>
#include <vector>

namespace {

#ifdef _WIN32
using socket_t = SOCKET;
constexpr socket_t kInvalidSocket = INVALID_SOCKET;

void close_socket(socket_t sock) {
    closesocket(sock);
}

bool send_all(socket_t sock, const char* data, size_t size) {
    size_t sent = 0;
    while (sent < size) {
        const int chunk = send(sock, data + sent, static_cast<int>(size - sent), 0);
        if (chunk <= 0) {
            return false;
        }
        sent += static_cast<size_t>(chunk);
    }
    return true;
}
#else
using socket_t = int;
constexpr socket_t kInvalidSocket = -1;

void close_socket(socket_t sock) {
    close(sock);
}

bool send_all(socket_t sock, const char* data, size_t size) {
    size_t sent = 0;
    while (sent < size) {
        const ssize_t chunk = send(sock, data + sent, size - sent, 0);
        if (chunk <= 0) {
            return false;
        }
        sent += static_cast<size_t>(chunk);
    }
    return true;
}
#endif

}  // namespace

MjpegServer::MjpegServer(FrameBuffer& buffer) : buffer_(buffer) {}

MjpegServer::~MjpegServer() {
    stop();
}

bool MjpegServer::start(uint16_t port) {
    if (running_.exchange(true)) {
        return true;
    }
    thread_ = std::thread(&MjpegServer::serve_loop, this, port);
    return true;
}

void MjpegServer::stop() {
    if (!running_.exchange(false)) {
        return;
    }
    if (thread_.joinable()) {
        thread_.join();
    }
}

void MjpegServer::serve_loop(uint16_t port) {
#ifdef _WIN32
    WSADATA wsa_data{};
    if (WSAStartup(MAKEWORD(2, 2), &wsa_data) != 0) {
        std::cerr << "WSAStartup failed\n";
        running_ = false;
        return;
    }
#endif

    socket_t listen_sock = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (listen_sock == kInvalidSocket) {
        std::cerr << "socket() failed\n";
#ifdef _WIN32
        WSACleanup();
#endif
        running_ = false;
        return;
    }

    int reuse = 1;
    setsockopt(listen_sock, SOL_SOCKET, SO_REUSEADDR,
               reinterpret_cast<const char*>(&reuse), sizeof(reuse));

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    addr.sin_port = htons(port);

    if (bind(listen_sock, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        std::cerr << "bind() failed on port " << port << "\n";
        close_socket(listen_sock);
#ifdef _WIN32
        WSACleanup();
#endif
        running_ = false;
        return;
    }

    if (listen(listen_sock, 8) != 0) {
        std::cerr << "listen() failed\n";
        close_socket(listen_sock);
#ifdef _WIN32
        WSACleanup();
#endif
        running_ = false;
        return;
    }

    std::cout << "MJPEG server listening on http://127.0.0.1:" << port << "/video\n";

    while (running_) {
        fd_set read_set;
        FD_ZERO(&read_set);
        FD_SET(listen_sock, &read_set);
        timeval timeout{};
        timeout.tv_sec = 0;
        timeout.tv_usec = 200000;
        const int ready = select(static_cast<int>(listen_sock) + 1, &read_set, nullptr, nullptr, &timeout);
        if (ready <= 0) {
            continue;
        }

        socket_t client = accept(listen_sock, nullptr, nullptr);
        if (client == kInvalidSocket) {
            continue;
        }

        std::thread([this, client]() {
            char request[1024]{};
            recv(client, request, sizeof(request) - 1, 0);

            const std::string header =
                "HTTP/1.1 200 OK\r\n"
                "Connection: close\r\n"
                "Cache-Control: no-cache, no-store, must-revalidate\r\n"
                "Pragma: no-cache\r\n"
                "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n"
                "\r\n";
            if (!send_all(client, header.c_str(), header.size())) {
                close_socket(client);
                return;
            }

            while (running_) {
                std::vector<uint8_t> jpeg;
                if (!buffer_.copy_latest_jpeg(jpeg)) {
                    std::this_thread::sleep_for(std::chrono::milliseconds(50));
                    continue;
                }

                std::ostringstream part;
                part << "--frame\r\n"
                     << "Content-Type: image/jpeg\r\n"
                     << "Content-Length: " << jpeg.size() << "\r\n\r\n";
                const std::string part_header = part.str();
                if (!send_all(client, part_header.c_str(), part_header.size())) {
                    break;
                }
                if (!send_all(client, reinterpret_cast<const char*>(jpeg.data()), jpeg.size())) {
                    break;
                }
                if (!send_all(client, "\r\n", 2)) {
                    break;
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(100));
            }

            close_socket(client);
        }).detach();
    }

    close_socket(listen_sock);
#ifdef _WIN32
    WSACleanup();
#endif
}
