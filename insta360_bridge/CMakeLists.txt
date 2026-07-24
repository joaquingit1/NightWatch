cmake_minimum_required(VERSION 3.20)
project(insta360_bridge LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

if(NOT DEFINED INSTA360_SDK_ROOT)
    set(INSTA360_SDK_ROOT "${CMAKE_SOURCE_DIR}/../Windows_CameraSDK-2.1.1_MediaSDK-3.1.3")
endif()

set(CAMERA_SDK_DIR "${INSTA360_SDK_ROOT}/CameraSDK-20250812_192505-2.1.1-win64")
set(MEDIA_SDK_DIR "${INSTA360_SDK_ROOT}/MediaSDK-3.1.3-20260128-win64/MediaSDK")

if(NOT EXISTS "${CAMERA_SDK_DIR}/lib/CameraSDK.lib")
    message(FATAL_ERROR "CameraSDK not found. Set -DINSTA360_SDK_ROOT=...")
endif()

add_executable(insta360_bridge
    src/main.cpp
    src/mjpeg_server.cpp
    src/h264_decoder.cpp
    src/media_stitcher.cpp
    src/frame_buffer.cpp
)

target_include_directories(insta360_bridge PRIVATE
    "${CAMERA_SDK_DIR}/include"
    "${MEDIA_SDK_DIR}/include"
    "${MEDIA_SDK_DIR}/include/stitcher"
    "${CMAKE_SOURCE_DIR}/src"
)

target_link_directories(insta360_bridge PRIVATE
    "${CAMERA_SDK_DIR}/lib"
    "${MEDIA_SDK_DIR}/lib"
)

target_link_libraries(insta360_bridge PRIVATE
    CameraSDK
    MediaSDK
    ws2_32
    ole32
    oleaut32
    windowscodecs
    mfplat
    mfuuid
    mfreadwrite
)

if(MSVC)
    target_compile_options(insta360_bridge PRIVATE /W4 /EHsc)
endif()

add_custom_command(TARGET insta360_bridge POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${CAMERA_SDK_DIR}/bin"
        "$<TARGET_FILE_DIR:insta360_bridge>"
    COMMAND ${CMAKE_COMMAND} -E copy_directory
        "${MEDIA_SDK_DIR}/bin"
        "$<TARGET_FILE_DIR:insta360_bridge>"
    COMMENT "Copying Insta360 SDK runtime DLLs next to insta360_bridge.exe"
)
