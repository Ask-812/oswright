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
                      (["in"], ctypes.c_void_p, "pDesc")),
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
                      (["in"], ctypes.c_void_p, "pLockedRect")),
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
        self._desktop_origin = (0, 0)
        self._holding_frame = False
        self._failed = False
        self._failure_reason: Optional[str] = None

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

    def close(self):
        """Release the duplication object."""
        self._release_frame()
        self._dup = None
        self._device = None

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
            info, _resource = self._dup.AcquireNextFrame(timeout_ms)
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
