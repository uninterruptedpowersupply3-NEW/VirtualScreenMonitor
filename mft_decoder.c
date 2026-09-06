#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <oleauto.h>
#include <initguid.h>
#include <mfapi.h>
#include <mfidl.h>
#include <mftransform.h>
#include <mferror.h>
#include <wmcodecdsp.h>
#include <codecapi.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <immintrin.h>

static IMFTransform* g_pDecoder = NULL;
static DWORD g_inStreamID = 0;
static DWORD g_outStreamID = 0;
static int g_width = 0;
static int g_height = 0;
static int g_stride = 0;
static int g_is_native_rgb32 = 0;
static int g_mf_started = 0;

static CRITICAL_SECTION g_cs;
static int g_cs_init = 0;

/* Scratch buffer for Annex-B start code enforcement (up to 16 MB) */
static uint8_t* g_annexb_scratch = NULL;
static int g_annexb_cap = 0;

static void ensure_cs(void) {
    if (!g_cs_init) {
        InitializeCriticalSection(&g_cs);
        g_cs_init = 1;
    }
}

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpReserved) {
    if (fdwReason == DLL_PROCESS_ATTACH) {
        ensure_cs();
    } else if (fdwReason == DLL_PROCESS_DETACH) {
        if (g_cs_init) {
            DeleteCriticalSection(&g_cs);
            g_cs_init = 0;
        }
        if (g_annexb_scratch) {
            free(g_annexb_scratch);
            g_annexb_scratch = NULL;
            g_annexb_cap = 0;
        }
    }
    return TRUE;
}

static inline uint8_t clamp_u8(int v) {
    return (uint8_t)(v < 0 ? 0 : (v > 255 ? 255 : v));
}

/* AVX2/SSE2 SIMD accelerated NV12 to BGRX/RGBA uint32 converter */
static void nv12_to_bgrx_simd(const uint8_t* pY, int pitch_y,
                              const uint8_t* pUV, int pitch_uv,
                              uint32_t* pDst, int dst_pitch_px,
                              int w, int h) {
    for (int y = 0; y < h; y++) {
        const uint8_t* y_row = pY + (y * pitch_y);
        const uint8_t* uv_row = pUV + ((y >> 1) * pitch_uv);
        uint32_t* dst_row = pDst + (y * dst_pitch_px);

        int x = 0;
        // Process in 8-pixel blocks with SSE2/AVX2
        for (; x <= w - 8; x += 8) {
            // Load 8 luma bytes
            __m128i y8 = _mm_loadl_epi64((const __m128i*)(y_row + x));
            __m128i y16 = _mm_unpacklo_epi8(y8, _mm_setzero_si128());
            // 6-bit fixed point math prevents 16-bit signed integer overflow (max intermediate < 16,500)
            __m128i c16 = _mm_sub_epi16(y16, _mm_set1_epi16(16));
            __m128i y_term = _mm_add_epi16(_mm_mullo_epi16(c16, _mm_set1_epi16(75)), _mm_set1_epi16(32));

            // Load 8 chroma bytes (4 pairs of U, V): [u0, v0, u1, v1, u2, v2, u3, v3]
            __m128i uv_bytes = _mm_loadl_epi64((const __m128i*)(uv_row + ((x >> 1) * 2)));
            __m128i uv16 = _mm_unpacklo_epi8(uv_bytes, _mm_setzero_si128());
            __m128i de16 = _mm_sub_epi16(uv16, _mm_set1_epi16(128));

            int16_t de_arr[8];
            _mm_storeu_si128((__m128i*)de_arr, de16);

            int16_t d_dup[8] = { de_arr[0], de_arr[0], de_arr[2], de_arr[2], de_arr[4], de_arr[4], de_arr[6], de_arr[6] };
            int16_t e_dup[8] = { de_arr[1], de_arr[1], de_arr[3], de_arr[3], de_arr[5], de_arr[5], de_arr[7], de_arr[7] };

            __m128i d_vec = _mm_loadu_si128((const __m128i*)d_dup);
            __m128i e_vec = _mm_loadu_si128((const __m128i*)e_dup);

            // r_term = y_term + 102 * E
            __m128i r_term = _mm_add_epi16(y_term, _mm_mullo_epi16(e_vec, _mm_set1_epi16(102)));
            // g_term = y_term - 25 * D - 52 * E
            __m128i g_sub = _mm_add_epi16(_mm_mullo_epi16(d_vec, _mm_set1_epi16(25)), _mm_mullo_epi16(e_vec, _mm_set1_epi16(52)));
            __m128i g_term = _mm_sub_epi16(y_term, g_sub);
            // b_term = y_term + 129 * D
            __m128i b_term = _mm_add_epi16(y_term, _mm_mullo_epi16(d_vec, _mm_set1_epi16(129)));

            __m128i r16 = _mm_srai_epi16(r_term, 6);
            __m128i g16 = _mm_srai_epi16(g_term, 6);
            __m128i b16 = _mm_srai_epi16(b_term, 6);

            __m128i r8 = _mm_packus_epi16(r16, _mm_setzero_si128());
            __m128i g8 = _mm_packus_epi16(g16, _mm_setzero_si128());
            __m128i b8 = _mm_packus_epi16(b16, _mm_setzero_si128());
            __m128i a8 = _mm_set1_epi8((char)0xFF);

            // Interleave into Windows DIB uint32 BGRX format: [B, G, R, 0xFF]
            // bg_lo = [b0, g0, b1, g1, b2, g2, b3, g3]
            __m128i bg_lo = _mm_unpacklo_epi8(b8, g8);
            // ra_lo = [r0, a0, r1, a1, r2, a2, r3, a3]
            __m128i ra_lo = _mm_unpacklo_epi8(r8, a8);

            __m128i bgrx_03 = _mm_unpacklo_epi16(bg_lo, ra_lo);
            __m128i bgrx_47 = _mm_unpackhi_epi16(bg_lo, ra_lo);

            _mm_storeu_si128((__m128i*)(dst_row + x), bgrx_03);
            _mm_storeu_si128((__m128i*)(dst_row + x + 4), bgrx_47);
        }

        // Remainder scalar loop
        for (; x < w; x++) {
            int y_val = (int)y_row[x];
            int uv_idx = (x & ~1);
            int d = (int)uv_row[uv_idx] - 128;
            int e = (int)uv_row[uv_idx + 1] - 128;
            int c = y_val - 16;
            int y_term = 75 * c + 32;
            int r = clamp_u8((y_term + 102 * e) >> 6);
            int g = clamp_u8((y_term - 25 * d - 52 * e) >> 6);
            int b = clamp_u8((y_term + 129 * d) >> 6);
            dst_row[x] = (0xFF << 24) | (r << 16) | (g << 8) | b;
        }
    }
}

__declspec(dllexport) void mft_nv12_to_bgrx(const uint8_t* pY, int pitch_y,
                                           const uint8_t* pUV, int pitch_uv,
                                           uint8_t* pDst, int dst_pitch_bytes,
                                           int w, int h) {
    if (!pY || !pUV || !pDst || w <= 0 || h <= 0) return;
    if (pitch_y <= 0) pitch_y = w;
    if (pitch_uv <= 0) pitch_uv = w;
    if (dst_pitch_bytes <= 0) dst_pitch_bytes = w * 4;
    nv12_to_bgrx_simd(pY, pitch_y, pUV, pitch_uv, (uint32_t*)pDst, dst_pitch_bytes / 4, w, h);
}

static HRESULT configure_output_type(void) {
    HRESULT hr = E_FAIL;
    IMFMediaType* pOutputType = NULL;
    DWORD typeIndex = 0;

    /* 1. Attempt to request native RGB32 output if supported by pipeline */
    IMFMediaType* pRgbType = NULL;
    if (SUCCEEDED(MFCreateMediaType(&pRgbType))) {
        pRgbType->lpVtbl->SetGUID(pRgbType, &MF_MT_MAJOR_TYPE, &MFMediaType_Video);
        pRgbType->lpVtbl->SetGUID(pRgbType, &MF_MT_SUBTYPE, &MFVideoFormat_RGB32);
        hr = g_pDecoder->lpVtbl->SetOutputType(g_pDecoder, g_outStreamID, pRgbType, 0);
        if (SUCCEEDED(hr)) {
            g_is_native_rgb32 = 1;
            UINT64 frameSize = 0;
            if (SUCCEEDED(pRgbType->lpVtbl->GetUINT64(pRgbType, &MF_MT_FRAME_SIZE, &frameSize))) {
                g_width = (int)(frameSize >> 32);
                g_height = (int)(frameSize & 0xFFFFFFFF);
                g_stride = g_width * 4;
            }
            pRgbType->lpVtbl->Release(pRgbType);
            return hr;
        }
        pRgbType->lpVtbl->Release(pRgbType);
    }

    /* 2. Fallback to native hardware NV12 output */
    g_is_native_rgb32 = 0;
    while (SUCCEEDED(g_pDecoder->lpVtbl->GetOutputAvailableType(g_pDecoder, g_outStreamID, typeIndex++, &pOutputType))) {
        GUID subtype;
        if (SUCCEEDED(pOutputType->lpVtbl->GetGUID(pOutputType, &MF_MT_SUBTYPE, &subtype))) {
            if (IsEqualGUID(&subtype, &MFVideoFormat_NV12)) {
                hr = g_pDecoder->lpVtbl->SetOutputType(g_pDecoder, g_outStreamID, pOutputType, 0);
                if (SUCCEEDED(hr)) {
                    UINT64 frameSize = 0;
                    if (SUCCEEDED(pOutputType->lpVtbl->GetUINT64(pOutputType, &MF_MT_FRAME_SIZE, &frameSize))) {
                        g_width = (int)(frameSize >> 32);
                        g_height = (int)(frameSize & 0xFFFFFFFF);
                        UINT32 defaultStride = 0;
                        if (SUCCEEDED(pOutputType->lpVtbl->GetUINT32(pOutputType, &MF_MT_DEFAULT_STRIDE, &defaultStride))) {
                            g_stride = (int)defaultStride;
                        } else {
                            g_stride = g_width;
                        }

                        /* Query display aperture if available to prevent 1088 macroblock padding collapse */
                        MFVideoArea aperture;
                        UINT32 cbBlob = sizeof(aperture);
                        if (SUCCEEDED(pOutputType->lpVtbl->GetBlob(pOutputType, &MF_MT_MINIMUM_DISPLAY_APERTURE, (UINT8*)&aperture, sizeof(aperture), &cbBlob))) {
                            if (aperture.Area.cx > 0 && aperture.Area.cy > 0) {
                                g_width = (int)aperture.Area.cx;
                                g_height = (int)aperture.Area.cy;
                            }
                        } else if (SUCCEEDED(pOutputType->lpVtbl->GetBlob(pOutputType, &MF_MT_GEOMETRIC_APERTURE, (UINT8*)&aperture, sizeof(aperture), &cbBlob))) {
                            if (aperture.Area.cx > 0 && aperture.Area.cy > 0) {
                                g_width = (int)aperture.Area.cx;
                                g_height = (int)aperture.Area.cy;
                            }
                        }
                    }
                    pOutputType->lpVtbl->Release(pOutputType);
                    return hr;
                }
            }
        }
        pOutputType->lpVtbl->Release(pOutputType);
    }
    return hr;
}

__declspec(dllexport) void mft_shutdown(void) {
    ensure_cs();
    EnterCriticalSection(&g_cs);
    if (g_pDecoder) {
        g_pDecoder->lpVtbl->Release(g_pDecoder);
        g_pDecoder = NULL;
    }
    if (g_mf_started) {
        MFShutdown();
        CoUninitialize();
        g_mf_started = 0;
    }
    g_width = 0;
    g_height = 0;
    g_stride = 0;
    g_is_native_rgb32 = 0;
    LeaveCriticalSection(&g_cs);
}

__declspec(dllexport) int mft_init(void) {
    ensure_cs();
    EnterCriticalSection(&g_cs);

    if (g_pDecoder != NULL) {
        mft_shutdown();
    }

    HRESULT hr = CoInitializeEx(NULL, COINIT_MULTITHREADED);
    if (FAILED(hr) && hr != RPC_E_CHANGED_MODE) {
        LeaveCriticalSection(&g_cs);
        return -1;
    }
    hr = MFStartup(MF_VERSION, MFSTARTUP_FULL);
    if (FAILED(hr)) {
        CoUninitialize();
        LeaveCriticalSection(&g_cs);
        return -2;
    }
    g_mf_started = 1;

    hr = CoCreateInstance(&CLSID_CMSH264DecoderMFT, NULL, CLSCTX_INPROC_SERVER,
                          &IID_IMFTransform, (void**)&g_pDecoder);
    if (FAILED(hr) || !g_pDecoder) {
        mft_shutdown();
        LeaveCriticalSection(&g_cs);
        return -3;
    }

    /* Set CODECAPI_AVLowLatencyMode = TRUE on Codec API */
    ICodecAPI* pCodecAPI = NULL;
    if (SUCCEEDED(g_pDecoder->lpVtbl->QueryInterface(g_pDecoder, &IID_ICodecAPI, (void**)&pCodecAPI))) {
        VARIANT var;
        VariantInit(&var);
        var.vt = VT_UI4;
        var.ulVal = 1;
        pCodecAPI->lpVtbl->SetValue(pCodecAPI, &CODECAPI_AVLowLatencyMode, &var);
        pCodecAPI->lpVtbl->Release(pCodecAPI);
    }

    /* Set MF_LOW_LATENCY = TRUE on transform attributes */
    IMFAttributes* pAttrs = NULL;
    if (SUCCEEDED(g_pDecoder->lpVtbl->GetAttributes(g_pDecoder, &pAttrs))) {
        pAttrs->lpVtbl->SetUINT32(pAttrs, &MF_LOW_LATENCY, 1);
        pAttrs->lpVtbl->Release(pAttrs);
    }

    /* Set Input Media Type to H.264 */
    IMFMediaType* pInputType = NULL;
    hr = MFCreateMediaType(&pInputType);
    if (FAILED(hr)) {
        mft_shutdown();
        LeaveCriticalSection(&g_cs);
        return -4;
    }
    pInputType->lpVtbl->SetGUID(pInputType, &MF_MT_MAJOR_TYPE, &MFMediaType_Video);
    pInputType->lpVtbl->SetGUID(pInputType, &MF_MT_SUBTYPE, &MFVideoFormat_H264);
    hr = g_pDecoder->lpVtbl->SetInputType(g_pDecoder, g_inStreamID, pInputType, 0);
    pInputType->lpVtbl->Release(pInputType);
    if (FAILED(hr)) {
        mft_shutdown();
        LeaveCriticalSection(&g_cs);
        return -5;
    }

    /* Configure Output Type (RGB32 opportunistic, NV12 standard) */
    hr = configure_output_type();
    if (FAILED(hr)) {
        mft_shutdown();
        LeaveCriticalSection(&g_cs);
        return -6;
    }

    /* Notify start of streaming */
    g_pDecoder->lpVtbl->ProcessMessage(g_pDecoder, MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
    g_pDecoder->lpVtbl->ProcessMessage(g_pDecoder, MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);

    LeaveCriticalSection(&g_cs);
    return 0;
}

__declspec(dllexport) void mft_flush(void) {
    ensure_cs();
    EnterCriticalSection(&g_cs);
    if (g_pDecoder) {
        g_pDecoder->lpVtbl->ProcessMessage(g_pDecoder, MFT_MESSAGE_COMMAND_FLUSH, 0);
    }
    LeaveCriticalSection(&g_cs);
}

__declspec(dllexport) int mft_decode_frame(const uint8_t* nal_buf, int nal_size,
                                           uint8_t* out_bgrx, int max_out,
                                           int* out_w, int* out_h, int* out_pitch) {
    ensure_cs();
    EnterCriticalSection(&g_cs);

    if (!g_pDecoder || !nal_buf || nal_size <= 0 || !out_bgrx || !out_w || !out_h) {
        if (out_w) *out_w = 0;
        if (out_h) *out_h = 0;
        LeaveCriticalSection(&g_cs);
        return -1;
    }

    /* 1. Annex-B stream feeding: ensure 4-byte start code (00 00 00 01) is present */
    const uint8_t* feed_buf = nal_buf;
    int feed_size = nal_size;
    int has_start_code = 0;
    if (nal_size >= 4 && nal_buf[0] == 0x00 && nal_buf[1] == 0x00 &&
        ((nal_buf[2] == 0x01) || (nal_buf[2] == 0x00 && nal_buf[3] == 0x01))) {
        has_start_code = 1;
    }

    if (!has_start_code) {
        int needed = nal_size + 4;
        if (needed > g_annexb_cap) {
            int new_cap = needed + 65536;
            uint8_t* new_mem = (uint8_t*)realloc(g_annexb_scratch, new_cap);
            if (!new_mem) {
                LeaveCriticalSection(&g_cs);
                return -2;
            }
            g_annexb_scratch = new_mem;
            g_annexb_cap = new_cap;
        }
        g_annexb_scratch[0] = 0x00;
        g_annexb_scratch[1] = 0x00;
        g_annexb_scratch[2] = 0x00;
        g_annexb_scratch[3] = 0x01;
        memcpy(g_annexb_scratch + 4, nal_buf, nal_size);
        feed_buf = g_annexb_scratch;
        feed_size = needed;
    }

    /* 2. Feed input sample to MFT */
    IMFMediaBuffer* pInMediaBuffer = NULL;
    HRESULT hr = MFCreateMemoryBuffer((DWORD)feed_size, &pInMediaBuffer);
    if (FAILED(hr)) {
        LeaveCriticalSection(&g_cs);
        return -3;
    }

    BYTE* pInBytes = NULL;
    DWORD maxLen = 0, curLen = 0;
    hr = pInMediaBuffer->lpVtbl->Lock(pInMediaBuffer, &pInBytes, &maxLen, &curLen);
    if (SUCCEEDED(hr)) {
        memcpy(pInBytes, feed_buf, feed_size);
        pInMediaBuffer->lpVtbl->Unlock(pInMediaBuffer);
        pInMediaBuffer->lpVtbl->SetCurrentLength(pInMediaBuffer, (DWORD)feed_size);
    } else {
        pInMediaBuffer->lpVtbl->Release(pInMediaBuffer);
        LeaveCriticalSection(&g_cs);
        return -4;
    }

    IMFSample* pInSample = NULL;
    hr = MFCreateSample(&pInSample);
    if (SUCCEEDED(hr)) {
        pInSample->lpVtbl->AddBuffer(pInSample, pInMediaBuffer);
    }
    pInMediaBuffer->lpVtbl->Release(pInMediaBuffer);

    hr = g_pDecoder->lpVtbl->ProcessInput(g_pDecoder, g_inStreamID, pInSample, 0);
    pInSample->lpVtbl->Release(pInSample);
    if (FAILED(hr)) {
        LeaveCriticalSection(&g_cs);
        return (int)hr;
    }

    /* 3. Retrieve output sample */
    MFT_OUTPUT_STREAM_INFO streamInfo;
    memset(&streamInfo, 0, sizeof(streamInfo));
    g_pDecoder->lpVtbl->GetOutputStreamInfo(g_pDecoder, g_outStreamID, &streamInfo);

    DWORD cbSize = streamInfo.cbSize;
    if (cbSize == 0) {
        cbSize = (g_width > 0 && g_height > 0) ? (g_width * g_height * 4) : (3840 * 2160 * 4);
    }

    IMFMediaBuffer* pOutMediaBuffer = NULL;
    hr = MFCreateMemoryBuffer(cbSize, &pOutMediaBuffer);
    if (FAILED(hr)) {
        LeaveCriticalSection(&g_cs);
        return -5;
    }

    IMFSample* pOutSample = NULL;
    hr = MFCreateSample(&pOutSample);
    if (SUCCEEDED(hr)) {
        pOutSample->lpVtbl->AddBuffer(pOutSample, pOutMediaBuffer);
    }
    pOutMediaBuffer->lpVtbl->Release(pOutMediaBuffer);

    MFT_OUTPUT_DATA_BUFFER outputDataBuffer;
    memset(&outputDataBuffer, 0, sizeof(outputDataBuffer));
    outputDataBuffer.dwStreamID = g_outStreamID;
    outputDataBuffer.pSample = pOutSample;

    DWORD dwStatus = 0;
    hr = g_pDecoder->lpVtbl->ProcessOutput(g_pDecoder, 0, 1, &outputDataBuffer, &dwStatus);

    if (hr == MF_E_TRANSFORM_STREAM_CHANGE) {
        /* Format or geometry dynamic change */
        configure_output_type();

        pOutSample->lpVtbl->Release(pOutSample);
        if (outputDataBuffer.pEvents) outputDataBuffer.pEvents->lpVtbl->Release(outputDataBuffer.pEvents);

        g_pDecoder->lpVtbl->GetOutputStreamInfo(g_pDecoder, g_outStreamID, &streamInfo);
        cbSize = streamInfo.cbSize;
        if (cbSize == 0) cbSize = g_width * g_height * 4;

        MFCreateMemoryBuffer(cbSize, &pOutMediaBuffer);
        MFCreateSample(&pOutSample);
        pOutSample->lpVtbl->AddBuffer(pOutSample, pOutMediaBuffer);
        pOutMediaBuffer->lpVtbl->Release(pOutMediaBuffer);

        memset(&outputDataBuffer, 0, sizeof(outputDataBuffer));
        outputDataBuffer.dwStreamID = g_outStreamID;
        outputDataBuffer.pSample = pOutSample;
        hr = g_pDecoder->lpVtbl->ProcessOutput(g_pDecoder, 0, 1, &outputDataBuffer, &dwStatus);
    }

    if (outputDataBuffer.pEvents) {
        outputDataBuffer.pEvents->lpVtbl->Release(outputDataBuffer.pEvents);
    }

    if (hr == MF_E_TRANSFORM_NEED_MORE_INPUT) {
        pOutSample->lpVtbl->Release(pOutSample);
        *out_w = 0;
        *out_h = 0;
        LeaveCriticalSection(&g_cs);
        return 1; /* Non-error: Need more input (e.g. SPS/PPS) */
    }

    if (FAILED(hr)) {
        pOutSample->lpVtbl->Release(pOutSample);
        if (out_w) *out_w = 0;
        if (out_h) *out_h = 0;
        LeaveCriticalSection(&g_cs);
        return (int)hr;
    }

    /* 4. Convert / Copy frame to output buffer */
    IMFMediaBuffer* pResultBuffer = NULL;
    hr = pOutSample->lpVtbl->ConvertToContiguousBuffer(pOutSample, &pResultBuffer);
    if (FAILED(hr)) {
        pOutSample->lpVtbl->Release(pOutSample);
        if (out_w) *out_w = 0;
        if (out_h) *out_h = 0;
        LeaveCriticalSection(&g_cs);
        return -6;
    }

    IMF2DBuffer* p2DBuffer = NULL;
    BYTE* pDecBytes = NULL;
    LONG lPitch = 0;
    int pitch_y = g_stride > 0 ? g_stride : g_width;
    int pitch_uv = pitch_y;
    BOOL is2DLocked = FALSE;

    hr = pResultBuffer->lpVtbl->QueryInterface(pResultBuffer, &IID_IMF2DBuffer, (void**)&p2DBuffer);
    if (SUCCEEDED(hr) && p2DBuffer) {
        hr = p2DBuffer->lpVtbl->Lock2D(p2DBuffer, &pDecBytes, &lPitch);
        if (SUCCEEDED(hr) && pDecBytes) {
            is2DLocked = TRUE;
            pitch_y = (int)lPitch;
            pitch_uv = (int)lPitch;
        }
    }

    if (!is2DLocked) {
        DWORD decLen = 0;
        hr = pResultBuffer->lpVtbl->Lock(pResultBuffer, &pDecBytes, &maxLen, &decLen);
        if (g_stride > 0) {
            pitch_y = g_stride;
            pitch_uv = g_stride;
        }
    }

    if (SUCCEEDED(hr) && pDecBytes) {
        int w = g_width;
        int h = g_height;
        int needed_px = w * h;
        int needed_bytes = needed_px * 4;

        if (needed_bytes <= max_out) {
            if (g_is_native_rgb32) {
                if (pitch_y == w * 4) {
                    memcpy(out_bgrx, pDecBytes, needed_bytes);
                } else {
                    for (int y = 0; y < h; y++) {
                        memcpy(out_bgrx + (y * w * 4), pDecBytes + (y * pitch_y), w * 4);
                    }
                }
            } else {
                /* Hardware produced NV12 -> SIMD vector conversion to strictly tightly packed BGRX uint32 */
                int alloc_h = (h + 15) & ~15; /* 16-pixel macroblock boundary alignment: 1080 -> 1088 */
                const uint8_t* pY = (const uint8_t*)pDecBytes;
                const uint8_t* pUV = pY + (pitch_y * alloc_h);
                nv12_to_bgrx_simd(pY, pitch_y, pUV, pitch_uv, (uint32_t*)out_bgrx, w, w, h);
            }
            *out_w = w;
            *out_h = h;
            if (out_pitch) *out_pitch = w * 4;
        }

        if (is2DLocked) {
            p2DBuffer->lpVtbl->Unlock2D(p2DBuffer);
        } else {
            pResultBuffer->lpVtbl->Unlock(pResultBuffer);
        }
    }

    if (p2DBuffer) {
        p2DBuffer->lpVtbl->Release(p2DBuffer);
    }

    pResultBuffer->lpVtbl->Release(pResultBuffer);
    pOutSample->lpVtbl->Release(pOutSample);

    LeaveCriticalSection(&g_cs);
    return 0;
}
