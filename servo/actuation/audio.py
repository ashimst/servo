"""The default speaker's volume and mute state, through Windows Core Audio (comtypes).

Media keys toggle and step blindly: "unmute" pressed on unmuted speakers mutes them, and a
volume key press can't be checked. Core Audio sets and reads the state, so every change
is verified by reading it back.
"""

from __future__ import annotations

import sys
from ctypes import POINTER, HRESULT, c_float, c_uint, c_void_p, byref
from ctypes.wintypes import BOOL, DWORD
from typing import Any


class AudioError(RuntimeError):
    pass


def _endpoint() -> Any:
    if sys.platform != "win32":
        raise AudioError("Volume control needs Windows.")
    import comtypes
    from comtypes import COMMETHOD, GUID, IUnknown

    class IAudioEndpointVolume(IUnknown):
        _iid_ = GUID("{5CDF2C82-841E-4546-9722-0CF74078229A}")
        _methods_ = [
            COMMETHOD([], HRESULT, "RegisterControlChangeNotify", (["in"], c_void_p, "p")),
            COMMETHOD([], HRESULT, "UnregisterControlChangeNotify", (["in"], c_void_p, "p")),
            COMMETHOD([], HRESULT, "GetChannelCount", (["out"], POINTER(c_uint), "n")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevel", (["in"], c_float, "db"), (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "SetMasterVolumeLevelScalar", (["in"], c_float, "level"), (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevel", (["out"], POINTER(c_float), "db")),
            COMMETHOD([], HRESULT, "GetMasterVolumeLevelScalar", (["out"], POINTER(c_float), "level")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevel", (["in"], c_uint, "ch"), (["in"], c_float, "db"),
                      (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "SetChannelVolumeLevelScalar", (["in"], c_uint, "ch"), (["in"], c_float, "lvl"),
                      (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevel", (["in"], c_uint, "ch"), (["out"], POINTER(c_float), "db")),
            COMMETHOD([], HRESULT, "GetChannelVolumeLevelScalar", (["in"], c_uint, "ch"),
                      (["out"], POINTER(c_float), "lvl")),
            COMMETHOD([], HRESULT, "SetMute", (["in"], BOOL, "mute"), (["in"], c_void_p, "ctx")),
            COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "mute")),
        ]

    class IMMDevice(IUnknown):
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"), (["in"], DWORD, "ctx"),
                      (["in"], c_void_p, "params"), (["out"], POINTER(POINTER(IUnknown)), "out")),
        ]

    class IMMDeviceEnumerator(IUnknown):
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], DWORD, "flow"), (["in"], DWORD, "mask"),
                      (["out"], POINTER(c_void_p), "out")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], DWORD, "flow"), (["in"], DWORD, "role"),
                      (["out"], POINTER(POINTER(IMMDevice)), "out")),
        ]

    comtypes.CoInitialize()
    enum = comtypes.CoCreateInstance(GUID("{BCDE0395-E52F-467C-8E3D-C4579291692E}"), IMMDeviceEnumerator,
                                     comtypes.CLSCTX_ALL)
    try:
        device = enum.GetDefaultAudioEndpoint(0, 1)   # eRender, eMultimedia
    except Exception as e:  # noqa: BLE001
        raise AudioError(f"No speaker to control: {e}") from e
    unk = device.Activate(byref(IAudioEndpointVolume._iid_), comtypes.CLSCTX_ALL, None)
    return unk.QueryInterface(IAudioEndpointVolume)


def get_volume() -> int:
    """Master volume, 0-100."""
    return round(_endpoint().GetMasterVolumeLevelScalar() * 100)


def set_volume(percent: int) -> int:
    """Set the master volume and return what it reads afterwards."""
    ep = _endpoint()
    ep.SetMasterVolumeLevelScalar(max(0, min(100, int(percent))) / 100.0, None)
    return round(ep.GetMasterVolumeLevelScalar() * 100)


def is_muted() -> bool:
    return bool(_endpoint().GetMute())


def set_muted(muted: bool) -> bool:
    """Mute or unmute and return the state read back."""
    ep = _endpoint()
    ep.SetMute(bool(muted), None)
    return bool(ep.GetMute())
