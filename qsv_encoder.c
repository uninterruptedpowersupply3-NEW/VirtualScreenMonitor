#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <immintrin.h>

#include "mfx/mfxvideo.h"

/* MFX Function Pointer Prototypes */
typedef mfxStatus (MFX_CDECL *t_MFXInit)(mfxU32 impl, mfxVersion* ver, mfxSession* session);
typedef mfxStatus (MFX_CDECL *t_MFXClose)(mfxSession session);
typedef mfxStatus (MFX_CDECL *t_MFXVideoENCODE_Query)(mfxSession session, mfxVideoParam *in, mfxVideoParam *out);
typedef mfxStatus (MFX_CDECL *t_MFXVideoENCODE_Init)(mfxSession session, mfxVideoParam* par);
typedef mfxStatus (MFX_CDECL *t_MFXVideoENCODE_Close)(mfxSession session);
typedef mfxStatus (MFX_CDECL *t_MFXVideoENCODE_EncodeFrameAsync)(mfxSession session, mfxEncodeCtrl* ctrl, mfxFrameSurface1* surface, mfxBitstream* bs, mfxSyncPoint* syncp);
typedef mfxStatus (MFX_CDECL *t_MFXVideoCORE_SyncOperation)(mfxSession session, mfxSyncPoint syncp, mfxU32 wait);

static HMODULE g_hMfx = NULL;
static t_MFXInit f_MFXInit = NULL;
static t_MFXClose f_MFXClose = NULL;
static t_MFXVideoENCODE_Query f_MFXVideoENCODE_Query = NULL;
static t_MFXVideoENCODE_Init f_MFXVideoENCODE_Init = NULL;
static t_MFXVideoENCODE_Close f_MFXVideoENCODE_Close = NULL;
static t_MFXVideoENCODE_EncodeFrameAsync f_MFXVideoENCODE_EncodeFrameAsync = NULL;
static t_MFXVideoCORE_SyncOperation f_MFXVideoCORE_SyncOperation = NULL;

static mfxSession g_session = NULL;
static mfxFrameSurface1 g_surface;
static uint8_t* g_surface_mem = NULL;
static mfxBitstream g_bs;
static uint8_t* g_bs_mem = NULL;
static int g_width = 0, g_height = 0;
static int g_target_kbps = 0, g_max_kbps = 0, g_fps = 0;
static int g_rc_mode = 0, g_qp = 0, g_target_usage = 0;
static int g_force_idr = 1;

static CRITICAL_SECTION g_cs;
static int g_cs_initialized = 0;

static void ensure_cs(void) {
    if (!g_cs_initialized) {
        InitializeCriticalSection(&g_cs);
        g_cs_initialized = 1;
    }
}

BOOL WINAPI DllMain(HINSTANCE hinstDLL, DWORD fdwReason, LPVOID lpReserved) {
    if (fdwReason == DLL_PROCESS_ATTACH) {
        ensure_cs();
    } else if (fdwReason == DLL_PROCESS_DETACH) {
        if (g_cs_initialized) {
            DeleteCriticalSection(&g_cs);
            g_cs_initialized = 0;
        }
    }
    return TRUE;
}

/* Strict MFX hardware driver candidates (Purged mfx_mft_h264.dll COM server) */
static HMODULE find_intel_mfx_dll(void) {
    const char* candidates[] = {
        "libmfxhw64.dll", "libmfx64.dll", "libmfxhw32.dll", "libmfx32.dll"
    };
    for (int i = 0; i < 4; i++) {
        HMODULE h = LoadLibraryA(candidates[i]);
        if (h) return h;
    }
    return NULL;
}

/* Fast SIMD-vectorized Fused Downscaling BGRA -> NV12 Converter */
static void bgra_to_nv12(const uint8_t* __restrict src_bgra, int src_w, int src_h, int src_pitch,
                         int dst_w, int dst_h, uint8_t* __restrict y_plane, uint8_t* __restrict uv_plane, int pitch) {
    if (src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0) return;
    if (src_pitch <= 0) src_pitch = src_w * 4;

    /* Blazing fast 1:1 native resolution path (Zero coordinate division or scaling math) */
    if (src_w == dst_w && src_h == dst_h) {
        const __m128i y_weights = _mm_setr_epi16(25, 129, 66, 0, 25, 129, 66, 0);
        const __m128i add_128   = _mm_set1_epi32(128);
        const __m128i add_16    = _mm_set1_epi32(16);

        for (int y = 0; y < dst_h; y++) {
            const uint8_t* __restrict src_row = src_bgra + (y * src_pitch);
            uint8_t* __restrict y_out = y_plane + (y * pitch);

            int x = 0;
            for (; x <= dst_w - 4; x += 4) {
                __m128i bgra = _mm_loadu_si128((const __m128i*)(src_row + x * 4));
                __m128i p01 = _mm_unpacklo_epi8(bgra, _mm_setzero_si128());
                __m128i p23 = _mm_unpackhi_epi8(bgra, _mm_setzero_si128());

                p01 = _mm_and_si128(p01, _mm_setr_epi16(-1, -1, -1, 0, -1, -1, -1, 0));
                p23 = _mm_and_si128(p23, _mm_setr_epi16(-1, -1, -1, 0, -1, -1, -1, 0));

                __m128i m01 = _mm_madd_epi16(p01, y_weights);
                __m128i m23 = _mm_madd_epi16(p23, y_weights);

                __m128i lo01 = _mm_shuffle_epi32(m01, _MM_SHUFFLE(2, 0, 2, 0));
                __m128i hi01 = _mm_shuffle_epi32(m01, _MM_SHUFFLE(3, 1, 3, 1));
                __m128i y01 = _mm_add_epi32(lo01, hi01);

                __m128i lo23 = _mm_shuffle_epi32(m23, _MM_SHUFFLE(2, 0, 2, 0));
                __m128i hi23 = _mm_shuffle_epi32(m23, _MM_SHUFFLE(3, 1, 3, 1));
                __m128i y23 = _mm_add_epi32(lo23, hi23);

                __m128i y0123 = _mm_unpacklo_epi64(y01, y23);
                y0123 = _mm_add_epi32(y0123, add_128);
                y0123 = _mm_srai_epi32(y0123, 8);
                y0123 = _mm_add_epi32(y0123, add_16);

                __m128i y16 = _mm_packs_epi32(y0123, y0123);
                __m128i y8  = _mm_packus_epi16(y16, y16);

                *(uint32_t*)(y_out + x) = (uint32_t)_mm_cvtsi128_si32(y8);
            }

            for (; x < dst_w; x++) {
                uint8_t b = src_row[x * 4 + 0];
                uint8_t g = src_row[x * 4 + 1];
                uint8_t r = src_row[x * 4 + 2];
                y_out[x] = (uint8_t)(((66 * r + 129 * g + 25 * b + 128) >> 8) + 16);
            }

            if ((y & 1) == 0) {
                uint8_t* __restrict uv_out = uv_plane + ((y >> 1) * pitch);
                #pragma loop(ivdep)
                for (int x = 0; x < dst_w; x += 2) {
                    uint8_t b = src_row[x * 4 + 0];
                    uint8_t g = src_row[x * 4 + 1];
                    uint8_t r = src_row[x * 4 + 2];
                    uint8_t u = (uint8_t)(((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128);
                    uint8_t v = (uint8_t)(((112 * r - 94 * g - 18 * b + 128) >> 8) + 128);
                    uv_out[x] = u;
                    uv_out[x + 1] = v;
                }
            }
        }
        return;
    }

    /* Scaling path with precalculated x-coordinate lookup on stack to eliminate heap allocation */
    int local_map[4096];
    int* sx_map = (dst_w <= 4096) ? local_map : (int*)malloc(dst_w * sizeof(int));
    if (!sx_map) return;
    for (int x = 0; x < dst_w; x++) {
        int sx = (int)(((long long)x * src_w) / dst_w);
        if (sx >= src_w) sx = src_w - 1;
        sx_map[x] = sx;
    }

    for (int y = 0; y < dst_h; y++) {
        int sy = (int)(((long long)y * src_h) / dst_h);
        if (sy >= src_h) sy = src_h - 1;
        const uint8_t* __restrict src_row = src_bgra + (sy * src_pitch);
        uint8_t* __restrict y_out = y_plane + (y * pitch);

        #pragma loop(ivdep)
        for (int x = 0; x < dst_w; x++) {
            int sx = sx_map[x];
            uint8_t b = src_row[sx * 4 + 0];
            uint8_t g = src_row[sx * 4 + 1];
            uint8_t r = src_row[sx * 4 + 2];
            y_out[x] = (uint8_t)(((66 * r + 129 * g + 25 * b + 128) >> 8) + 16);
        }

        if ((y & 1) == 0) {
            int sy_even = ((int)(((long long)y * src_h) / dst_h)) & ~1;
            if (sy_even >= src_h) sy_even = (src_h - 1) & ~1;
            const uint8_t* __restrict src_row_uv = src_bgra + (sy_even * src_pitch);
            uint8_t* __restrict uv_out = uv_plane + ((y >> 1) * pitch);

            #pragma loop(ivdep)
            for (int x = 0; x < dst_w; x += 2) {
                int sx_even = sx_map[x] & ~1;
                if (sx_even >= src_w) sx_even = (src_w - 1) & ~1;
                uint8_t b = src_row_uv[sx_even * 4 + 0];
                uint8_t g = src_row_uv[sx_even * 4 + 1];
                uint8_t r = src_row_uv[sx_even * 4 + 2];
                uint8_t u = (uint8_t)(((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128);
                uint8_t v = (uint8_t)(((112 * r - 94 * g - 18 * b + 128) >> 8) + 128);
                uv_out[x] = u;
                uv_out[x + 1] = v;
            }
        }
    }
    if (sx_map != local_map) free(sx_map);
}

__declspec(dllexport) void qsv_bgra_to_nv12(const uint8_t* src_bgra, int src_w, int src_h, int src_pitch,
                                           int dst_w, int dst_h, uint8_t* y_plane, uint8_t* uv_plane, int pitch) {
    bgra_to_nv12(src_bgra, src_w, src_h, src_pitch, dst_w, dst_h, y_plane, uv_plane, pitch);
}

/* Fast SIMD/vectorized BGRA 32-bit pixel downscaler */
__declspec(dllexport) void bgra_downscale_bgra(const uint8_t* src, int src_w, int src_h, int src_pitch,
                                                uint8_t* dst, int dst_w, int dst_h, int dst_pitch) {
    if (!src || !dst || src_w <= 0 || src_h <= 0 || dst_w <= 0 || dst_h <= 0) return;
    if (src_pitch <= 0) src_pitch = src_w * 4;
    if (dst_pitch <= 0) dst_pitch = dst_w * 4;

    for (int y = 0; y < dst_h; y++) {
        int sy = (int)(((long long)y * src_h) / dst_h);
        if (sy >= src_h) sy = src_h - 1;
        const uint32_t* __restrict src_row = (const uint32_t*)(src + (sy * src_pitch));
        uint32_t* __restrict dst_row = (uint32_t*)(dst + (y * dst_pitch));

        #pragma loop(ivdep)
        for (int x = 0; x < dst_w; x++) {
            int sx = (int)(((long long)x * src_w) / dst_w);
            if (sx >= src_w) sx = src_w - 1;
            dst_row[x] = src_row[sx];
        }
    }
}

__declspec(dllexport) void qsv_force_idr(void) {
    ensure_cs();
    EnterCriticalSection(&g_cs);
    g_force_idr = 1;
    LeaveCriticalSection(&g_cs);
}

__declspec(dllexport) void qsv_shutdown(void) {
    ensure_cs();
    EnterCriticalSection(&g_cs);
    if (g_session) {
        if (f_MFXVideoENCODE_Close) f_MFXVideoENCODE_Close(g_session);
        if (f_MFXClose) f_MFXClose(g_session);
        g_session = NULL;
    }
    if (g_surface_mem) { _aligned_free(g_surface_mem); g_surface_mem = NULL; }
    if (g_bs_mem) { free(g_bs_mem); g_bs_mem = NULL; }
    if (g_hMfx) { FreeLibrary(g_hMfx); g_hMfx = NULL; }
    g_width = 0;
    g_height = 0;
    g_target_kbps = 0;
    g_max_kbps = 0;
    g_fps = 0;
    g_rc_mode = 0;
    g_qp = 0;
    g_target_usage = 0;
    LeaveCriticalSection(&g_cs);
}

__declspec(dllexport) int qsv_init(int width, int height, int target_kbps, int max_kbps, int fps, int rc_mode, int qp, int target_usage) {
    ensure_cs();
    EnterCriticalSection(&g_cs);

    /* Strict ceiling safeguard against 16-bit unsigned integer overflow (mfxU16 max 65,535) */
    if (target_kbps < 100) target_kbps = 100;
    if (target_kbps > 60000) target_kbps = 60000;
    if (max_kbps < target_kbps) max_kbps = target_kbps;
    if (max_kbps > 60000) max_kbps = 60000;

    /* Dynamic reconfiguration: if running with identical geometry, rate control, target usage, and parameters, no-op */
    if (g_session != NULL) {
        if (width == g_width && height == g_height && target_kbps == g_target_kbps &&
            max_kbps == g_max_kbps && fps == g_fps && rc_mode == g_rc_mode &&
            qp == g_qp && target_usage == g_target_usage) {
            LeaveCriticalSection(&g_cs);
            return 0;
        }
        /* MFX Full Re-Init on parameter change: cleanly close MFXVideoENCODE and rebuild pipeline */
        if (f_MFXVideoENCODE_Close) f_MFXVideoENCODE_Close(g_session);
        if (f_MFXClose) f_MFXClose(g_session);
        g_session = NULL;
        if (g_surface_mem) { _aligned_free(g_surface_mem); g_surface_mem = NULL; }
        if (g_bs_mem) { free(g_bs_mem); g_bs_mem = NULL; }
        if (g_hMfx) { FreeLibrary(g_hMfx); g_hMfx = NULL; }
    }

    g_hMfx = find_intel_mfx_dll();
    if (!g_hMfx) {
        LeaveCriticalSection(&g_cs);
        return -1;
    }

    f_MFXInit = (t_MFXInit)GetProcAddress(g_hMfx, "MFXInit");
    f_MFXClose = (t_MFXClose)GetProcAddress(g_hMfx, "MFXClose");
    f_MFXVideoENCODE_Query = (t_MFXVideoENCODE_Query)GetProcAddress(g_hMfx, "MFXVideoENCODE_Query");
    f_MFXVideoENCODE_Init = (t_MFXVideoENCODE_Init)GetProcAddress(g_hMfx, "MFXVideoENCODE_Init");
    f_MFXVideoENCODE_Close = (t_MFXVideoENCODE_Close)GetProcAddress(g_hMfx, "MFXVideoENCODE_Close");
    f_MFXVideoENCODE_EncodeFrameAsync = (t_MFXVideoENCODE_EncodeFrameAsync)GetProcAddress(g_hMfx, "MFXVideoENCODE_EncodeFrameAsync");
    f_MFXVideoCORE_SyncOperation = (t_MFXVideoCORE_SyncOperation)GetProcAddress(g_hMfx, "MFXVideoCORE_SyncOperation");

    if (!f_MFXInit || !f_MFXVideoENCODE_Init || !f_MFXVideoENCODE_EncodeFrameAsync || !f_MFXVideoCORE_SyncOperation) {
        FreeLibrary(g_hMfx);
        g_hMfx = NULL;
        LeaveCriticalSection(&g_cs);
        return -2;
    }

    mfxVersion ver;
    ver.Major = 1;
    ver.Minor = 0;
    if (f_MFXInit(MFX_IMPL_HARDWARE_ANY, &ver, &g_session) != MFX_ERR_NONE) {
        FreeLibrary(g_hMfx);
        g_hMfx = NULL;
        LeaveCriticalSection(&g_cs);
        return -3;
    }

    g_width = width;
    g_height = height;
    g_target_kbps = target_kbps;
    g_max_kbps = max_kbps;
    g_fps = fps;
    g_rc_mode = rc_mode;
    g_qp = qp;
    g_target_usage = target_usage;
    g_force_idr = 1; /* First frame must be an IDR keyframe with SPS/PPS */

    mfxVideoParam par;
    memset(&par, 0, sizeof(par));
    par.mfx.CodecId = MFX_CODEC_AVC;
    if (target_usage >= 1 && target_usage <= 7) {
        par.mfx.TargetUsage = (mfxU16)target_usage;
    } else {
        par.mfx.TargetUsage = MFX_TARGETUSAGE_BALANCED; // TU 4 default
    }

    if (rc_mode == 1) { // CQP (Constant QP)
        par.mfx.RateControlMethod = MFX_RATECONTROL_CQP;
        int safe_qp = (qp >= 1 && qp <= 51) ? qp : 21;
        par.mfx.QPI = (mfxU16)safe_qp;
        par.mfx.QPP = (mfxU16)safe_qp;
        par.mfx.QPB = (mfxU16)safe_qp;
    } else { // VBR (default)
        par.mfx.RateControlMethod = MFX_RATECONTROL_VBR;
        par.mfx.TargetKbps = (mfxU16)target_kbps;
        par.mfx.MaxKbps = (mfxU16)max_kbps;
        int buf_size = max_kbps / 8 / 2;
        if (buf_size < 16) buf_size = 16;
        if (buf_size > 60000) buf_size = 60000;
        par.mfx.BufferSizeInKB = (mfxU16)buf_size;
    }
    par.mfx.GopPicSize = (mfxU16)(fps * 2);
    par.mfx.GopRefDist = 1; // Strict 0 B-frames
    par.mfx.NumRefFrame = 1; // Strict 1 reference frame to eliminate multi-frame buffering backlog
    par.mfx.IdrInterval = 1;

    par.mfx.FrameInfo.FourCC = MFX_FOURCC_NV12;
    par.mfx.FrameInfo.ChromaFormat = MFX_CHROMAFORMAT_YUV420;
    par.mfx.FrameInfo.PicStruct = MFX_PICSTRUCT_PROGRESSIVE;
    par.mfx.FrameInfo.Width = (mfxU16)((width + 15) & ~15);
    par.mfx.FrameInfo.Height = (mfxU16)((height + 15) & ~15);
    par.mfx.FrameInfo.CropX = 0;
    par.mfx.FrameInfo.CropY = 0;
    par.mfx.FrameInfo.CropW = (mfxU16)width;
    par.mfx.FrameInfo.CropH = (mfxU16)height;
    par.mfx.FrameInfo.FrameRateExtN = (mfxU32)fps;
    par.mfx.FrameInfo.FrameRateExtD = 1;

    par.IOPattern = MFX_IOPATTERN_IN_SYSTEM_MEMORY;
    par.AsyncDepth = 1;

    if (f_MFXVideoENCODE_Query) {
        f_MFXVideoENCODE_Query(g_session, &par, &par);
    }
    /* Strictly enforce AsyncDepth = 1 and NumRefFrame = 1 after Query to eliminate multi-frame ASIC backlog */
    par.AsyncDepth = 1;
    par.mfx.NumRefFrame = 1;

    mfxStatus sts = f_MFXVideoENCODE_Init(g_session, &par);
    if (sts != MFX_ERR_NONE && sts != MFX_WRN_IN_EXECUTION) {
        f_MFXClose(g_session);
        g_session = NULL;
        FreeLibrary(g_hMfx);
        g_hMfx = NULL;
        LeaveCriticalSection(&g_cs);
        return (int)sts;
    }

    int pitch = (par.mfx.FrameInfo.Width + 31) & ~31;
    int surf_h = (par.mfx.FrameInfo.Height + 31) & ~31;
    int y_size = pitch * surf_h;
    int uv_size = pitch * (surf_h / 2);

    g_surface_mem = (uint8_t*)_aligned_malloc(y_size + uv_size, 32);
    memset(&g_surface, 0, sizeof(g_surface));
    memcpy(&g_surface.Info, &par.mfx.FrameInfo, sizeof(mfxFrameInfo));
    g_surface.Data.Pitch = (mfxU16)pitch;
    g_surface.Data.Y = g_surface_mem;
    g_surface.Data.UV = g_surface_mem + y_size;

    /* Expand bitstream buffer to 4 MB to comfortably hold 60 Mbps 1440p IDR keyframes */
    int bs_size = 4 * 1024 * 1024;
    g_bs_mem = (uint8_t*)malloc(bs_size);
    memset(&g_bs, 0, sizeof(g_bs));
    g_bs.Data = g_bs_mem;
    g_bs.MaxLength = (mfxU32)bs_size;

    LeaveCriticalSection(&g_cs);
    return 0;
}

__declspec(dllexport) int qsv_encode_frame(const uint8_t* src_bgra, int src_w, int src_h, int src_pitch, uint8_t* out_buf, int max_out, int* out_size) {
    ensure_cs();
    EnterCriticalSection(&g_cs);
    if (!g_session || !src_bgra || !out_buf || !out_size || !g_surface_mem) {
        if (out_size) *out_size = 0;
        LeaveCriticalSection(&g_cs);
        return -1;
    }

    bgra_to_nv12(src_bgra, src_w, src_h, src_pitch, g_width, g_height, g_surface.Data.Y, g_surface.Data.UV, g_surface.Data.Pitch);

    g_bs.DataOffset = 0;
    g_bs.DataLength = 0;

    mfxEncodeCtrl ctrl;
    mfxEncodeCtrl* pCtrl = NULL;
    if (g_force_idr) {
        memset(&ctrl, 0, sizeof(ctrl));
        ctrl.FrameType = MFX_FRAMETYPE_I | MFX_FRAMETYPE_IDR | MFX_FRAMETYPE_REF;
        pCtrl = &ctrl;
        g_force_idr = 0;
    }

    mfxSyncPoint syncp = NULL;
    mfxStatus sts = f_MFXVideoENCODE_EncodeFrameAsync(g_session, pCtrl, &g_surface, &g_bs, &syncp);
    int ret = 0;
    if ((sts == MFX_ERR_NONE || sts == MFX_WRN_IN_EXECUTION) && syncp) {
        mfxStatus sync_sts = f_MFXVideoCORE_SyncOperation(g_session, syncp, 1000);
        if (sync_sts == MFX_ERR_NONE && g_bs.DataLength > 0 && (int)g_bs.DataLength <= max_out) {
            memcpy(out_buf, g_bs.Data + g_bs.DataOffset, g_bs.DataLength);
            *out_size = (int)g_bs.DataLength;
            g_bs.DataLength = 0;
            g_bs.DataOffset = 0;
            LeaveCriticalSection(&g_cs);
            return 0;
        }
    }
    *out_size = 0;
    g_bs.DataLength = 0;
    g_bs.DataOffset = 0;
    ret = (sts == MFX_ERR_MORE_DATA) ? 1 : (sts == MFX_ERR_NONE ? 0 : -2);
    LeaveCriticalSection(&g_cs);
    return ret;
}

#define MFX_HANDLE_D3D11_DEVICE 0x00000008

__declspec(dllexport) int qsv_set_d3d11_device(void* pD3D11Device) {
    ensure_cs();
    EnterCriticalSection(&g_cs);
    if (!g_session || !pD3D11Device) {
        LeaveCriticalSection(&g_cs);
        return -1;
    }
    typedef mfxStatus (*t_MFXVideoCORE_SetHandle)(mfxSession, int, void*);
    t_MFXVideoCORE_SetHandle f_SetHandle = (t_MFXVideoCORE_SetHandle)GetProcAddress(g_hMfx, "MFXVideoCORE_SetHandle");
    int res = -2;
    if (f_SetHandle) {
        res = (int)f_SetHandle(g_session, MFX_HANDLE_D3D11_DEVICE, pD3D11Device);
    }
    LeaveCriticalSection(&g_cs);
    return res;
}

