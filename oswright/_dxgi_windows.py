"""
Dirty rectangles straight from the Windows compositor.

The Desktop Duplication API already knows exactly which pixels changed since the
last frame -- the compositor has to know, in order to present efficiently -- and
exposes it through `IDXGIOutputDuplication::GetFrameDirtyRects`. Computing the
same thing by hashing tiles of a captured frame is redundant work.

The bigger win is what this makes possible: `AcquireNextFrame` reports whether
anything changed *without transferring any pixels*. A screen capture costs about
33 ms; asking the compositor "did anything move?" costs well under a
millisecond. Most observations in a real agent session are of an idle screen, so
the expensive capture can be skipped entirely rather than performed and then
discarded.

Note on what does NOT work here: capturing only the dirty sub-regions instead of
the full frame. `mss` has a fixed per-grab cost of roughly 16 ms regardless of
region size, so four small grabs cost twice as much as one full-screen grab. The
capture stays whole-frame; only the *analysis* is regional.

Everything degrades gracefully: if Desktop Duplication is unavailable -- no
WDDM driver, a session with no console, secure desktop, a protected-content
window -- callers fall back to tile hashing with no behavioural change.
"""

import ctypes
import ctypes.wintypes
import logging
import platform
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# HRESULTs the duplication API returns in normal operation.
DXGI_ERROR_WAIT_TIMEOUT = -2005270489        # 0x887A0027, nothing changed
DXGI_ERROR_ACCESS_LOST = -2005270490         # 0x887A0026, desktop switched
DXGI_ERROR_INVALID_CALL = -2005270527        # 0x887A0001, frame not released
DXGI_ERROR_UNSUPPORTED = -2005270524         # 0x887A0004

# D3D11 constants used by the staging-texture capture path.
D3D11_USAGE_STAGING = 3
D3D11_CPU_ACCESS_READ = 0x20000
D3D11_MAP_READ = 1
# DXGI_FORMAT_B8G8R8A8_UNORM and its sRGB variant: the desktop is BGRA.
_SUPPORTED_FORMATS = (87, 91)

_available = False
_import_error: Optional[str] = None

if _IS_WINDOWS:
    try:
        import comtypes
        from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

        _available = True
    except ImportError as e:  # pragma: no cover - optional dependency
        _import_error = str(e)


if _available:

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class DXGI_OUTDUPL_POINTER_POSITION(ctypes.Structure):
        _fields_ = [("Position", POINT), ("Visible", ctypes.c_int)]

    class DXGI_OUTDUPL_FRAME_INFO(ctypes.Structure):
        _fields_ = [
            ("LastPresentTime", ctypes.c_longlong),
            ("LastMouseUpdateTime", ctypes.c_longlong),
            ("AccumulatedFrames", ctypes.c_uint),
            ("RectsCoalesced", ctypes.c_int),
            ("ProtectedContentMaskedOut", ctypes.c_int),
            ("PointerPosition", DXGI_OUTDUPL_POINTER_POSITION),
            ("TotalMetadataBufferSize", ctypes.c_uint),
            ("PointerShapeBufferSize", ctypes.c_uint),
        ]

    class DXGI_RATIONAL(ctypes.Structure):
        _fields_ = [("Numerator", ctypes.c_uint), ("Denominator", ctypes.c_uint)]

    class DXGI_MODE_DESC(ctypes.Structure):
        _fields_ = [
            ("Width", ctypes.c_uint),
            ("Height", ctypes.c_uint),
            ("RefreshRate", DXGI_RATIONAL),
            ("Format", ctypes.c_uint),
            ("ScanlineOrdering", ctypes.c_uint),
            ("Scaling", ctypes.c_uint),
        ]

    class DXGI_OUTDUPL_DESC(ctypes.Structure):
        _fields_ = [
            ("ModeDesc", DXGI_MODE_DESC),
            ("Rotation", ctypes.c_uint),
            ("DesktopImageInSystemMemory", ctypes.c_int),
        ]

    class DXGI_MAPPED_RECT(ctypes.Structure):
        _fields_ = [("Pitch", ctypes.c_int), ("pBits", ctypes.POINTER(ctypes.c_ubyte))]

    class DXGI_SAMPLE_DESC(ctypes.Structure):
        _fields_ = [("Count", ctypes.c_uint), ("Quality", ctypes.c_uint)]

    class D3D11_TEXTURE2D_DESC(ctypes.Structure):
        _fields_ = [
            ("Width", ctypes.c_uint),
            ("Height", ctypes.c_uint),
            ("MipLevels", ctypes.c_uint),
            ("ArraySize", ctypes.c_uint),
            ("Format", ctypes.c_uint),
            ("SampleDesc", DXGI_SAMPLE_DESC),
            ("Usage", ctypes.c_uint),
            ("BindFlags", ctypes.c_uint),
            ("CPUAccessFlags", ctypes.c_uint),
            ("MiscFlags", ctypes.c_uint),
        ]

    class D3D11_MAPPED_SUBRESOURCE(ctypes.Structure):
        _fields_ = [
            ("pData", ctypes.c_void_p),
            ("RowPitch", ctypes.c_uint),
            ("DepthPitch", ctypes.c_uint),
        ]

    def _reserved(count: int, start: int = 0) -> list:
        """
        Placeholder vtable slots.

        Only a handful of D3D11 methods are needed, but COM dispatch is by
        vtable offset, so every preceding method must still occupy its slot.
        Generating them beats hand-writing forty-odd entries and getting one
        of them silently wrong.
        """
        return [COMMETHOD([], None, f"_reserved_{start + i}") for i in range(count)]

    class DXGI_OUTPUT_DESC(ctypes.Structure):
        _fields_ = [
            ("DeviceName", ctypes.c_wchar * 32),
            ("DesktopCoordinates", RECT),
            ("AttachedToDesktop", ctypes.c_int),
            ("Rotation", ctypes.c_uint),
            ("Monitor", ctypes.c_void_p),
        ]

    # --- COM interfaces. Method order defines the vtable, so it must match the
    # --- SDK headers exactly; only the methods actually used are declared, with
    # --- earlier ones present purely as placeholders to keep the offsets right.
    # --- Declared in dependency order, since each returns the next.

    class IDXGIObject(IUnknown):
        _iid_ = GUID("{aec22fb8-76f3-4639-9be0-28eb43a67a2e}")
        _methods_ = [
            COMMETHOD([], HRESULT, "SetPrivateData",
                      (["in"], ctypes.POINTER(GUID), "Name"),
                      (["in"], ctypes.c_uint, "DataSize"),
                      (["in"], ctypes.c_void_p, "pData")),
            COMMETHOD([], HRESULT, "SetPrivateDataInterface",
                      (["in"], ctypes.POINTER(GUID), "Name"),
                      (["in"], ctypes.POINTER(IUnknown), "pUnknown")),
            COMMETHOD([], HRESULT, "GetPrivateData",
                      (["in"], ctypes.POINTER(GUID), "Name"),
                      (["in"], ctypes.POINTER(ctypes.c_uint), "pDataSize"),
                      (["in"], ctypes.c_void_p, "pData")),
            COMMETHOD([], HRESULT, "GetParent",
                      (["in"], ctypes.POINTER(GUID), "riid"),
                      (["out"], ctypes.POINTER(ctypes.c_void_p), "ppParent")),
        ]

    class IDXGIOutputDuplication(IDXGIObject):
        _iid_ = GUID("{191cfac3-a341-470d-b26e-a864f428319c}")
        _methods_ = [
            COMMETHOD([], None, "GetDesc",
                      (["in"], ctypes.POINTER(DXGI_OUTDUPL_DESC), "pDesc")),
            COMMETHOD([], HRESULT, "AcquireNextFrame",
                      (["in"], ctypes.c_uint, "TimeoutInMilliseconds"),
                      (["out"], ctypes.POINTER(DXGI_OUTDUPL_FRAME_INFO), "pFrameInfo"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(IUnknown)), "ppDesktopResource")),
            COMMETHOD([], HRESULT, "GetFrameDirtyRects",
                      (["in"], ctypes.c_uint, "DirtyRectsBufferSize"),
                      (["in"], ctypes.POINTER(RECT), "pDirtyRectsBuffer"),
                      (["in"], ctypes.POINTER(ctypes.c_uint), "pDirtyRectsBufferSizeRequired")),
            COMMETHOD([], HRESULT, "GetFrameMoveRects",
                      (["in"], ctypes.c_uint, "MoveRectsBufferSize"),
                      (["in"], ctypes.c_void_p, "pMoveRectBuffer"),
                      (["in"], ctypes.POINTER(ctypes.c_uint), "pMoveRectsBufferSizeRequired")),
            COMMETHOD([], HRESULT, "GetFramePointerShape",
                      (["in"], ctypes.c_uint, "PointerShapeBufferSize"),
                      (["in"], ctypes.c_void_p, "pPointerShapeBuffer"),
                      (["in"], ctypes.POINTER(ctypes.c_uint), "pPointerShapeBufferSizeRequired"),
                      (["in"], ctypes.c_void_p, "pPointerShapeInfo")),
            COMMETHOD([], HRESULT, "MapDesktopSurface",
                      (["in"], ctypes.POINTER(DXGI_MAPPED_RECT), "pLockedRect")),
            COMMETHOD([], HRESULT, "UnMapDesktopSurface"),
            COMMETHOD([], HRESULT, "ReleaseFrame"),
        ]

    class IDXGIOutput(IDXGIObject):
        _iid_ = GUID("{ae02eedb-c735-4690-8d52-5a8dc20213aa}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetDesc",
                      (["in"], ctypes.POINTER(DXGI_OUTPUT_DESC), "pDesc")),
            COMMETHOD([], HRESULT, "GetDisplayModeList"),
            COMMETHOD([], HRESULT, "FindClosestMatchingMode"),
            COMMETHOD([], HRESULT, "WaitForVBlank"),
            COMMETHOD([], HRESULT, "TakeOwnership"),
            COMMETHOD([], None, "ReleaseOwnership"),
            COMMETHOD([], HRESULT, "GetGammaControlCapabilities"),
            COMMETHOD([], HRESULT, "SetGammaControl"),
            COMMETHOD([], HRESULT, "GetGammaControl"),
            COMMETHOD([], HRESULT, "SetDisplaySurface"),
            COMMETHOD([], HRESULT, "GetDisplaySurfaceData"),
            COMMETHOD([], HRESULT, "GetFrameStatistics"),
        ]

    class IDXGIOutput1(IDXGIOutput):
        _iid_ = GUID("{00cddea8-939b-4b83-a340-a685226666cc}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetDisplayModeList1"),
            COMMETHOD([], HRESULT, "FindClosestMatchingMode1"),
            COMMETHOD([], HRESULT, "GetDisplaySurfaceData1"),
            COMMETHOD([], HRESULT, "DuplicateOutput",
                      (["in"], ctypes.POINTER(IUnknown), "pDevice"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(IDXGIOutputDuplication)),
                       "ppOutputDuplication")),
        ]

    class IDXGIAdapter(IDXGIObject):
        _iid_ = GUID("{2411e7e1-12ac-4ccf-bd14-9798e8534dc9}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumOutputs",
                      (["in"], ctypes.c_uint, "Output"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(IDXGIOutput)), "ppOutput")),
            COMMETHOD([], HRESULT, "GetDesc"),
            COMMETHOD([], HRESULT, "CheckInterfaceSupport"),
        ]

    class IDXGIDevice(IDXGIObject):
        _iid_ = GUID("{54ec77fa-1377-44e6-8c32-88fd5f44c84c}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetAdapter",
                      (["out"], ctypes.POINTER(ctypes.POINTER(IDXGIAdapter)), "pAdapter")),
            COMMETHOD([], HRESULT, "CreateSurface"),
            COMMETHOD([], HRESULT, "QueryResourceResidency"),
            COMMETHOD([], HRESULT, "SetGPUThreadPriority",
                      (["in"], ctypes.c_int, "Priority")),
            COMMETHOD([], HRESULT, "GetGPUThreadPriority",
                      (["out"], ctypes.POINTER(ctypes.c_int), "pPriority")),
        ]

    # --- D3D11, needed only to copy the desktop texture somewhere readable ---

    class ID3D11Texture2D(IUnknown):
        # ID3D11DeviceChild (4) + ID3D11Resource (4) precede GetDesc.
        _iid_ = GUID("{6f15aaf2-d208-4e89-9ab4-489535d34f9c}")
        _methods_ = _reserved(8) + [
            COMMETHOD([], None, "GetDesc",
                      (["in"], ctypes.POINTER(D3D11_TEXTURE2D_DESC), "pDesc")),
        ]

    class ID3D11Device(IUnknown):
        _iid_ = GUID("{db6f6ddb-ac77-4e88-8253-819df9bbf140}")
        _methods_ = _reserved(2) + [  # CreateBuffer, CreateTexture1D
            COMMETHOD([], HRESULT, "CreateTexture2D",
                      (["in"], ctypes.POINTER(D3D11_TEXTURE2D_DESC), "pDesc"),
                      (["in"], ctypes.c_void_p, "pInitialData"),
                      (["out"], ctypes.POINTER(ctypes.POINTER(ID3D11Texture2D)),
                       "ppTexture2D")),
        ]

    class ID3D11DeviceContext(IUnknown):
        # ID3D11DeviceChild occupies the first 4 slots. Within the context's own
        # vtable Map is 7, Unmap is 8 and CopyResource is 40.
        _iid_ = GUID("{c0bfa96c-e089-44fb-8eaf-26f8796190da}")
        _methods_ = (
            _reserved(4 + 7)
            + [
                COMMETHOD([], HRESULT, "Map",
                          (["in"], ctypes.POINTER(IUnknown), "pResource"),
                          (["in"], ctypes.c_uint, "Subresource"),
                          (["in"], ctypes.c_uint, "MapType"),
                          (["in"], ctypes.c_uint, "MapFlags"),
                          (["in"], ctypes.POINTER(D3D11_MAPPED_SUBRESOURCE),
                           "pMappedResource")),
                COMMETHOD([], None, "Unmap",
                          (["in"], ctypes.POINTER(IUnknown), "pResource"),
                          (["in"], ctypes.c_uint, "Subresource")),
            ]
            + _reserved(31, start=100)  # slots 9..39
            + [
                COMMETHOD([], None, "CopyResource",
                          (["in"], ctypes.POINTER(IUnknown), "pDstResource"),
                          (["in"], ctypes.POINTER(IUnknown), "pSrcResource")),
            ]
        )

    # Without explicit argtypes ctypes assumes C ints and truncates every
    # pointer on 64-bit, which shows up as an access violation rather than an
    # error return.
    _d3d11 = ctypes.WinDLL("d3d11")
    _d3d11.D3D11CreateDevice.restype = HRESULT
    _d3d11.D3D11CreateDevice.argtypes = [
        ctypes.c_void_p,                              # pAdapter
        ctypes.c_uint,                                # DriverType
        ctypes.c_void_p,                              # Software
        ctypes.c_uint,                                # Flags
        ctypes.c_void_p,                              # pFeatureLevels
        ctypes.c_uint,                                # FeatureLevels
        ctypes.c_uint,                                # SDKVersion
        ctypes.POINTER(ctypes.POINTER(IUnknown)),     # ppDevice
        ctypes.POINTER(ctypes.c_uint),                # pFeatureLevel
        ctypes.POINTER(ctypes.POINTER(IUnknown)),     # ppImmediateContext
    ]


def is_available() -> bool:
    """True if the Desktop Duplication bindings could be loaded."""
    return _available


if _available:
    # comtypes signals a failed HRESULT with COMError, which is not an OSError.
    # DXGI_ERROR_WAIT_TIMEOUT arrives this way on every idle poll, so getting
    # this wrong turns the normal "nothing changed" case into a hard failure.
    _COM_ERRORS = (comtypes.COMError, OSError)
else:  # pragma: no cover - non-Windows
    _COM_ERRORS = (OSError,)


def _hresult_of(exc) -> Optional[int]:
    """Extract the HRESULT from a COMError or OSError, if there is one."""
    for attr in ("hresult", "winerror", "errno"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


class DxgiDirtySource:
    """
    Reports which screen regions changed, according to the compositor.

    Not thread-safe: a duplication object belongs to the thread that created it.
    Create one per thread, or guard access.
    """

    # A frame can report a great many small rectangles. Past this point the
    # detail stops being useful and the caller is better served by one merged
    # region, so the buffer is bounded rather than grown without limit.
    MAX_RECTS = 512

    def __init__(self, output_index: int = 0):
        self._output_index = output_index
        self._dup = None
        self._device = None
        self._d3d_device = None
        self._context = None
        self._staging = None
        self._staging_size = None
        self._last_resource = None
        self._desktop_origin = (0, 0)
        self._holding_frame = False
        self._failed = False
        self._failure_reason: Optional[str] = None
        self._capture_failed = False

    # --- lifecycle ---

    @property
    def failure_reason(self) -> Optional[str]:
        return self._failure_reason

    def _fail(self, reason: str):
        if not self._failed:
            logger.info("Desktop Duplication unavailable (%s); using tile hashing", reason)
        self._failed = True
        self._failure_reason = reason
        self._release_frame()
        self._dup = None
        return None

    def _ensure(self) -> bool:
        """Create the duplication object if needed. Returns False if unusable."""
        if self._failed:
            return False
        if self._dup is not None:
            return True
        if not _available:
            self._fail(_import_error or "comtypes unavailable")
            return False

        try:
            self._create()
        except OSError as e:
            self._fail(f"{type(e).__name__}: {e}")
            return False
        except Exception as e:  # pragma: no cover - driver dependent
            self._fail(f"{type(e).__name__}: {e}")
            return False
        return self._dup is not None

    def _create(self):
        device = ctypes.POINTER(IUnknown)()
        context = ctypes.POINTER(IUnknown)()
        feature_level = ctypes.c_uint()

        # D3D_DRIVER_TYPE_HARDWARE = 1, D3D11_SDK_VERSION = 7. Nothing is
        # rendered here; the device exists only to own the duplication.
        _d3d11.D3D11CreateDevice(
            None, 1, None, 0, None, 0, 7,
            ctypes.byref(device), ctypes.byref(feature_level), ctypes.byref(context),
        )
        if not device:
            raise OSError("D3D11CreateDevice returned a null device")

        self._device = device
        self._d3d_device = device.QueryInterface(ID3D11Device)
        self._context = context.QueryInterface(ID3D11DeviceContext) if context else None

        # COM interfaces are unrelated C++ types; moving between them requires
        # QueryInterface, not a pointer cast.
        dxgi_device = device.QueryInterface(IDXGIDevice)
        adapter = dxgi_device.GetAdapter()
        output = adapter.EnumOutputs(self._output_index)

        desc = DXGI_OUTPUT_DESC()
        output.GetDesc(ctypes.byref(desc))
        self._desktop_origin = (
            desc.DesktopCoordinates.left,
            desc.DesktopCoordinates.top,
        )

        output1 = output.QueryInterface(IDXGIOutput1)
        self._dup = output1.DuplicateOutput(device)

    def _release_frame(self):
        if self._dup is not None and self._holding_frame:
            try:
                self._dup.ReleaseFrame()
            except Exception:
                pass
        self._holding_frame = False
        self._last_resource = None

    def close(self):
        """Release the duplication object."""
        self._release_frame()
        self._dup = None
        self._device = None
        self._d3d_device = None
        self._context = None
        self._staging = None
        self._staging_size = None

    def capture(self):
        """
        Read the desktop image from the frame this source is already holding.

        Returns a PIL Image, or None when unavailable -- no frame is held, the
        GPU path is unsupported, or anything failed. Callers fall back to a
        normal screen capture.

        The frame was already acquired by `poll()`, so this avoids a second,
        independent grab of the same pixels through a different API.

        Note that a frame only exists if the compositor actually presented one.
        On a completely idle screen `poll()` returns "nothing changed" without
        acquiring anything, and this returns None -- which is harmless, because
        an unchanged screen does not need to be re-read.
        """
        if self._capture_failed or not self._holding_frame or self._last_resource is None:
            return None
        if self._context is None or self._d3d_device is None:
            return None

        try:
            return self._capture_via_staging()
        except Exception as e:  # pragma: no cover - driver dependent
            logger.info(
                "DXGI capture unavailable (%s: %s); using the normal capture path",
                type(e).__name__, e,
            )
            self._capture_failed = True
            return None

    def _ensure_staging(self, width: int, height: int, fmt: int):
        """A CPU-readable texture to copy the desktop into, created once."""
        if self._staging is not None and self._staging_size == (width, height, fmt):
            return self._staging

        desc = D3D11_TEXTURE2D_DESC()
        desc.Width = width
        desc.Height = height
        desc.MipLevels = 1
        desc.ArraySize = 1
        desc.Format = fmt
        desc.SampleDesc.Count = 1
        desc.SampleDesc.Quality = 0
        desc.Usage = D3D11_USAGE_STAGING
        desc.BindFlags = 0
        desc.CPUAccessFlags = D3D11_CPU_ACCESS_READ
        desc.MiscFlags = 0

        self._staging = self._d3d_device.CreateTexture2D(ctypes.byref(desc), None)
        self._staging_size = (width, height, fmt)
        return self._staging

    def _capture_via_staging(self):
        from PIL import Image

        desktop = self._last_resource.QueryInterface(ID3D11Texture2D)
        desc = D3D11_TEXTURE2D_DESC()
        desktop.GetDesc(ctypes.byref(desc))

        if desc.Format not in _SUPPORTED_FORMATS:
            raise ValueError(f"unsupported desktop format {desc.Format}")

        staging = self._ensure_staging(desc.Width, desc.Height, desc.Format)

        # GPU-side copy into memory the CPU is allowed to read.
        self._context.CopyResource(staging, desktop)

        mapped = D3D11_MAPPED_SUBRESOURCE()
        self._context.Map(staging, 0, D3D11_MAP_READ, 0, ctypes.byref(mapped))
        try:
            # Rows are padded to RowPitch, which is usually wider than the
            # image, so the buffer cannot be handed to PIL as-is.
            size = mapped.RowPitch * desc.Height
            raw = ctypes.string_at(mapped.pData, size)
            image = Image.frombuffer(
                "RGB", (desc.Width, desc.Height), raw, "raw", "BGRX", mapped.RowPitch, 1
            )
            # frombuffer keeps a reference to `raw`; copy so the image outlives
            # the mapping.
            return image.copy()
        finally:
            self._context.Unmap(staging, 0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    # --- polling ---

    def poll(self, timeout_ms: int = 0):
        """
        Ask the compositor what changed since the last poll.

        Returns:
            None  -- the compositor could not answer; fall back to hashing.
            []    -- definitively nothing changed.
            [...] -- (left, top, right, bottom) tuples in desktop coordinates.

        No pixels are transferred, which is the point: an unchanged screen is
        established for well under a millisecond instead of a ~33 ms capture.
        """
        if not self._ensure():
            return None

        self._release_frame()

        try:
            info, resource = self._dup.AcquireNextFrame(timeout_ms)
        except _COM_ERRORS as e:
            code = _hresult_of(e)
            if code == DXGI_ERROR_WAIT_TIMEOUT:
                return []  # authoritative: the compositor presented nothing new
            if code in (DXGI_ERROR_ACCESS_LOST, DXGI_ERROR_INVALID_CALL):
                # Desktop switched (UAC prompt, lock screen, resolution change).
                # Rebuild on the next call rather than giving up permanently.
                logger.debug("Duplication access lost; recreating")
                self.close()
                return None
            return self._fail(f"AcquireNextFrame: 0x{(code or 0) & 0xFFFFFFFF:08X} {e}")
        except Exception as e:  # pragma: no cover - driver dependent
            return self._fail(f"AcquireNextFrame: {type(e).__name__}: {e}")

        self._holding_frame = True
        self._last_resource = resource

        # A frame with no metadata is a cursor-only update: the desktop image is
        # unchanged, so there is nothing to re-read.
        if info.TotalMetadataBufferSize == 0:
            return []

        try:
            rects = self._dirty_rects(info.TotalMetadataBufferSize)
        except Exception as e:  # pragma: no cover - driver dependent
            return self._fail(f"GetFrameDirtyRects: {type(e).__name__}: {e}")

        ox, oy = self._desktop_origin
        return [(r.left + ox, r.top + oy, r.right + ox, r.bottom + oy) for r in rects]

    def _dirty_rects(self, metadata_size: int) -> list:
        count = min(self.MAX_RECTS, max(1, metadata_size // ctypes.sizeof(RECT)))
        buffer = (RECT * count)()
        required = ctypes.c_uint()

        self._dup.GetFrameDirtyRects(
            ctypes.sizeof(buffer), buffer, ctypes.byref(required)
        )

        returned = min(count, required.value // ctypes.sizeof(RECT))
        return [buffer[i] for i in range(returned)]
