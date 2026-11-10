"""Find the address phones should use to reach the hub (plan step 5.3).

A computer often has several network adapters: Wi-Fi, Ethernet (sometimes two, one of them
unplugged but still holding an address), VPN adapters (OpenVPN/TAP, WireGuard, Tailscale),
virtual ones (Hyper-V "vEthernet", VirtualBox, VMware, Docker/WSL). Only a real Wi-Fi or
Ethernet adapter that is connected, and best one with a router (default gateway), is what a
phone on the same Wi-Fi can reach.

On Windows the adapters are read with GetAdaptersAddresses (type, state, gateway: a few ms).
Elsewhere a best-effort list from the socket module is used.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import os
import socket
import subprocess
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger("house_cinema.server")

Kind = Literal["ethernet", "wifi", "vpn", "virtual", "loopback", "other"]

_VPN_WORDS = ("tap-windows", "openvpn", "wireguard", "tailscale", "zerotier", "anyconnect", "wintun",
              "hamachi", "fortinet", "forticlient", "globalprotect", "pangp", "nordlynx", "proton", "vpn",
              "tunnel", "juniper", "pulse secure", "sonicwall", "cloudflare warp", "radmin")
_VIRTUAL_WORDS = ("hyper-v", "vethernet", "virtualbox", "vmware", "docker", "wsl", "virtual", "loopback adapter",
                  "npcap", "bluetooth", "kernel debug", "teredo", "isatap", "6to4", "pseudo-interface")
_WIFI_WORDS = ("wi-fi", "wifi", "wireless", "wlan", "802.11")
IF_TYPE_ETHERNET, IF_TYPE_WIFI, IF_TYPE_LOOPBACK, IF_TYPE_PPP, IF_TYPE_TUNNEL = 6, 71, 24, 23, 131


@dataclass(frozen=True)
class Adapter:
    ip: str
    name: str
    description: str = ""
    kind: Kind = "other"
    up: bool = True
    gateway: bool = False
    """The adapter has a default gateway (a router): typical of the home network."""


@dataclass
class Addresses:
    best: list[Adapter] = field(default_factory=list)
    """Addresses to show for phones, best first (usually one)."""
    others: list[Adapter] = field(default_factory=list)
    """Also of this computer but not for phones (VPN, virtual, disconnected)."""
    warnings: list[str] = field(default_factory=list)

    def urls(self, port: int) -> list[str]:
        return [f"http://{a.ip}:{port}/" for a in self.best]


def classify(name: str, description: str, if_type: int | None = None) -> Kind:
    text = f"{name} {description}".lower()
    if if_type == IF_TYPE_LOOPBACK or "loopback pseudo" in text:
        return "loopback"
    if if_type in (IF_TYPE_PPP, IF_TYPE_TUNNEL) or any(w in text for w in _VPN_WORDS):
        return "vpn"
    if any(w in text for w in _VIRTUAL_WORDS):
        return "virtual"
    if if_type == IF_TYPE_WIFI or any(w in text for w in _WIFI_WORDS):
        return "wifi"
    if if_type == IF_TYPE_ETHERNET or "ethernet" in text:
        return "ethernet"
    return "other"


def _private_rank(ip: str) -> int:
    a = ipaddress.ip_address(ip)
    if a in ipaddress.ip_network("192.168.0.0/16"):
        return 0
    if a in ipaddress.ip_network("10.0.0.0/8"):
        return 1
    if a in ipaddress.ip_network("172.16.0.0/12"):
        return 2
    return 3


def choose(adapters: list[Adapter]) -> Addresses:
    """Pick the address(es) for phones. Pure: tested with made-up adapter lists."""
    result = Addresses()
    usable, others = [], []
    for a in adapters:
        try:
            ip = ipaddress.ip_address(a.ip)
        except ValueError:
            continue
        if ip.version != 4 or ip.is_loopback or a.kind == "loopback":
            continue
        if ip.is_link_local or not a.up or a.kind in ("vpn", "virtual"):
            others.append(a)  # 169.254.x.x = no DHCP answer: not a working network
        else:
            usable.append(a)
    order = {"ethernet": 0, "wifi": 1, "other": 2}
    usable.sort(key=lambda a: (not a.gateway, order.get(a.kind, 3), _private_rank(a.ip), a.ip))
    with_gateway = [a for a in usable if a.gateway]
    if with_gateway:
        result.best = [with_gateway[0]]
        # Another real network with a router (Wi-Fi and Ethernet both on): show it too.
        result.best += [a for a in with_gateway[1:] if a.kind in ("ethernet", "wifi") and a.ip != result.best[0].ip]
        others += [a for a in usable if not a.gateway]
    elif usable:
        result.best = usable
        result.warnings.append("Không thấy router (default gateway) trên mạng Wi-Fi/Ethernet: điện thoại chỉ kết "
                               "nối được nếu cùng mạng trực tiếp với máy chủ.")
    else:
        vpn = [a for a in others if a.up and a.kind == "vpn" and not ipaddress.ip_address(a.ip).is_link_local]
        if vpn:
            result.warnings.append("Chỉ có mạng VPN, không có Wi-Fi/Ethernet: chỉ thiết bị trong cùng VPN mới vào "
                                   "được hub.")
            result.best = vpn
            others = [a for a in others if a not in vpn]
        else:
            result.warnings.append("Không tìm thấy mạng LAN (Wi-Fi hoặc Ethernet) đang kết nối: điện thoại sẽ không "
                                   "vào được hub. Kiểm tra Wi-Fi hoặc cáp mạng của máy chủ.")
    result.others = sorted(others, key=lambda a: (not a.up, a.kind, a.ip))
    return result


# ---------------------------------------------------------------------- reading the adapters


def _windows_adapters() -> list[Adapter]:
    import ctypes
    from ctypes import wintypes

    class SocketAddress(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class Unicast(ctypes.Structure):
        pass

    Unicast._fields_ = [("Length", wintypes.ULONG), ("Flags", wintypes.DWORD), ("Next", ctypes.POINTER(Unicast)),
                        ("Address", SocketAddress), ("PrefixOrigin", ctypes.c_int), ("SuffixOrigin", ctypes.c_int),
                        ("DadState", ctypes.c_int), ("ValidLifetime", wintypes.ULONG),
                        ("PreferredLifetime", wintypes.ULONG), ("LeaseLifetime", wintypes.ULONG),
                        ("OnLinkPrefixLength", ctypes.c_uint8)]

    class Gateway(ctypes.Structure):
        pass

    Gateway._fields_ = [("Length", wintypes.ULONG), ("Reserved", wintypes.DWORD), ("Next", ctypes.POINTER(Gateway)),
                        ("Address", SocketAddress)]

    class AdapterAddresses(ctypes.Structure):
        pass

    AdapterAddresses._fields_ = [
        ("Length", wintypes.ULONG), ("IfIndex", wintypes.DWORD), ("Next", ctypes.POINTER(AdapterAddresses)),
        ("AdapterName", ctypes.c_char_p), ("FirstUnicastAddress", ctypes.POINTER(Unicast)),
        ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
        ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_wchar_p),
        ("Description", ctypes.c_wchar_p), ("FriendlyName", ctypes.c_wchar_p),
        ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", wintypes.ULONG),
        ("Flags", wintypes.ULONG), ("Mtu", wintypes.ULONG), ("IfType", wintypes.DWORD), ("OperStatus", ctypes.c_int),
        ("Ipv6IfIndex", wintypes.DWORD), ("ZoneIndices", wintypes.ULONG * 16), ("FirstPrefix", ctypes.c_void_p),
        ("TransmitLinkSpeed", ctypes.c_uint64), ("ReceiveLinkSpeed", ctypes.c_uint64),
        ("FirstWinsServerAddress", ctypes.c_void_p), ("FirstGatewayAddress", ctypes.POINTER(Gateway)),
    ]

    iphlpapi = ctypes.WinDLL("iphlpapi")
    flags = 0x2 | 0x4 | 0x8 | 0x80  # skip anycast, multicast, DNS servers; include gateways
    size = wintypes.ULONG(16 * 1024)
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        ret = iphlpapi.GetAdaptersAddresses(socket.AF_INET, flags, None, buf, ctypes.byref(size))
        if ret == 0:
            break
        if ret != 111:  # ERROR_BUFFER_OVERFLOW: try again with the size asked for
            raise OSError(ret, "GetAdaptersAddresses failed")
    else:
        raise OSError("GetAdaptersAddresses: buffer keeps growing")

    def ipv4(addr: SocketAddress) -> str | None:
        if not addr.lpSockaddr or addr.iSockaddrLength < 8:
            return None
        raw = ctypes.string_at(addr.lpSockaddr, addr.iSockaddrLength)
        if int.from_bytes(raw[:2], "little") != socket.AF_INET:
            return None
        return socket.inet_ntoa(raw[4:8])

    found: list[Adapter] = []
    node = ctypes.cast(buf, ctypes.POINTER(AdapterAddresses))
    while node:
        a = node.contents
        name, desc = a.FriendlyName or "", a.Description or ""
        kind = classify(name, desc, a.IfType)
        has_gateway = False
        gw = a.FirstGatewayAddress
        while gw:
            if ipv4(gw.contents.Address) not in (None, "0.0.0.0"):
                has_gateway = True
            gw = gw.contents.Next
        uni = a.FirstUnicastAddress
        while uni:
            ip = ipv4(uni.contents.Address)
            if ip:
                found.append(Adapter(ip=ip, name=name, description=desc, kind=kind, up=a.OperStatus == 1,
                                     gateway=has_gateway))
            uni = uni.contents.Next
        node = a.Next
    return found


def _generic_adapters() -> list[Adapter]:
    found: dict[str, Adapter] = {}
    route_ip = None
    try:  # the address used for the default route (UDP connect sends no packet)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            route_ip = s.getsockname()[0]
    except OSError:
        pass
    try:
        names = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        names = []
    for ip in {*names, *([route_ip] if route_ip else [])}:
        found[ip] = Adapter(ip=ip, name="", kind="other", gateway=ip == route_ip)
    return list(found.values())


def list_adapters() -> list[Adapter]:
    if os.name == "nt":
        try:
            return _windows_adapters()
        except Exception as exc:  # never stop the hub for this
            logger.warning("Cannot list network adapters (%s); using a simpler method", exc)
    return _generic_adapters()


def find_addresses() -> Addresses:
    return choose(list_adapters())


def public_profile_names(timeout: float = 5.0) -> list[str]:
    """Windows network connections set to the *Public* profile (the firewall then blocks
    phones). Empty when unknown. Takes ~0.5 s: call it in the background."""
    if os.name != "nt":
        return []
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-NetConnectionProfile | Select-Object InterfaceAlias, NetworkCategory | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            stdin=subprocess.DEVNULL).stdout
        data = json.loads(out or "[]")
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return []
    rows = data if isinstance(data, list) else [data]
    # NetworkCategory: 0 = Public, 1 = Private, 2 = Domain.
    return [str(r.get("InterfaceAlias")) for r in rows if isinstance(r, dict) and r.get("NetworkCategory") in (0, "Public")]


def public_profile_warnings(found: Addresses, names: list[str] | None = None) -> list[str]:
    """A warning for each address-to-use whose Windows network is set to *Public*."""
    names = public_profile_names() if names is None else names
    return [f"Windows đang coi mạng “{a.name}” là mạng Public nên firewall có thể chặn điện thoại. Đổi sang "
            f"Private: Settings › Network & Internet › {a.name} › Network profile type › Private (xem README)."
            for a in found.best if a.name and a.name in names]


def startup_text(found: Addresses, port: int, *, qr: bool = True) -> str:
    """What the terminal shows at start: the address(es) for phones and a QR code ("" when
    there is no address: the warnings are logged instead, never a useless 127.0.0.1)."""
    lines = []
    urls = found.urls(port)
    if urls:
        lines.append("Mở hub trên điện thoại / máy khác cùng mạng Wi-Fi:")
        for a, url in zip(found.best, urls):
            lines.append(f"    {url}    ({KIND_LABEL.get(a.kind, a.kind)}{f': {a.name}' if a.name else ''})")
        if qr:
            lines.append("Hoặc quét mã QR này bằng camera điện thoại:")
            lines.append(qr_text(urls[0]).rstrip("\n"))
    return "\n".join(lines)


KIND_LABEL = {"ethernet": "Ethernet", "wifi": "Wi-Fi", "vpn": "VPN", "virtual": "mạng ảo", "other": "mạng"}


# ---------------------------------------------------------------------- QR code


def qr_text(url: str) -> str:
    """The QR code of ``url`` drawn with block characters, for the terminal."""
    import io

    import segno

    out = io.StringIO()
    segno.make(url, error="m", micro=False).terminal(out=out, compact=True, border=2)
    return out.getvalue()


def qr_svg_data_uri(url: str) -> str:
    import segno

    return segno.make(url, error="m", micro=False).svg_data_uri(scale=6, border=2, dark="#000", light="#fff")
