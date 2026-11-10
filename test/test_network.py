"""Tests for plan step 5.3: the address (and QR code) phones should use."""

from __future__ import annotations

import io
import ipaddress
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from source.modules.server import create_app, network
from source.modules.server.network import Adapter, choose, classify
from source.modules.server.runner import announce_addresses
from source.utils.config import load_settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LIBRARY = PROJECT_ROOT / "test" / "fixtures" / "library"

WIFI = Adapter("192.168.1.20", "Wi-Fi", "Intel(R) Wi-Fi 6 AX201 160MHz", "wifi", True, True)
ETH = Adapter("192.168.1.2", "Ethernet 2", "ASIX USB to Gigabit Ethernet Family Adapter", "ethernet", True, True)
ETH_UNPLUGGED = Adapter("192.168.1.103", "Ethernet", "Intel(R) Ethernet Connection (7) I219-V", "ethernet", False, True)
APIPA = Adapter("169.254.71.249", "Ethernet 3", "Realtek PCIe GbE", "ethernet", True, False)
HYPERV = Adapter("172.24.80.1", "vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter", "virtual", True, False)
VBOX = Adapter("192.168.56.1", "Ethernet 4", "VirtualBox Host-Only Ethernet Adapter", "virtual", True, False)
VPN_FULL = Adapter("10.8.0.6", "OpenVPN TAP-Windows6", "TAP-Windows Adapter V9", "vpn", True, True)  # takes the route
TAILSCALE = Adapter("100.101.102.103", "Tailscale", "Tailscale Tunnel", "vpn", True, False)
LOOP = Adapter("127.0.0.1", "Loopback Pseudo-Interface 1", "Software Loopback Interface 1", "loopback", True, False)


# ======================================================================= adapter kinds


@pytest.mark.parametrize("name,desc,if_type,kind", [
    ("Wi-Fi", "Intel(R) Wi-Fi 6 AX201 160MHz", 71, "wifi"),
    ("WLAN", "Realtek 8821CE Wireless LAN 802.11ac PCI-E NIC", None, "wifi"),
    ("Ethernet", "Intel(R) Ethernet Connection (7) I219-V", 6, "ethernet"),
    ("Ethernet 2", "ASIX USB to Gigabit Ethernet Family Adapter", 6, "ethernet"),
    ("vEthernet (Default Switch)", "Hyper-V Virtual Ethernet Adapter", 6, "virtual"),
    ("vEthernet (WSL)", "Hyper-V Virtual Ethernet Adapter #2", 6, "virtual"),
    ("Ethernet 4", "VirtualBox Host-Only Ethernet Adapter", 6, "virtual"),
    ("VMware Network Adapter VMnet8", "VMware Virtual Ethernet Adapter for VMnet8", 6, "virtual"),
    ("OpenVPN TAP-Windows6", "TAP-Windows Adapter V9", 6, "vpn"),
    ("WireGuard Tunnel", "WireGuard Tunnel", 53, "vpn"),
    ("Tailscale", "Tailscale Tunnel", 53, "vpn"),
    ("ZeroTier One [abc]", "ZeroTier Virtual Port", 6, "vpn"),
    ("Bluetooth Network Connection", "Bluetooth Device (Personal Area Network)", 6, "virtual"),
    ("Loopback Pseudo-Interface 1", "Software Loopback Interface 1", 24, "loopback"),
])
def test_adapter_kinds(name, desc, if_type, kind):
    assert classify(name, desc, if_type) == kind


# ======================================================================= choosing


def test_the_connected_ethernet_wins_over_an_unplugged_one():
    """This hub's own server: two Ethernet cards, the built-in one unplugged but still with a
    fixed address, plus an OpenVPN TAP adapter and loopback."""
    tap = Adapter("169.254.26.239", "OpenVPN TAP-Windows6", "TAP-Windows Adapter V9", "vpn", False, False)
    r = choose([ETH_UNPLUGGED, ETH, tap, LOOP])
    assert r.best == [ETH] and r.warnings == []
    assert ETH_UNPLUGGED in r.others and tap in r.others and LOOP not in r.others


def test_vpn_and_virtual_adapters_are_never_the_address():
    r = choose([VPN_FULL, HYPERV, VBOX, TAILSCALE, WIFI, LOOP])
    assert r.best == [WIFI]
    assert {a.ip for a in r.others} == {VPN_FULL.ip, HYPERV.ip, VBOX.ip, TAILSCALE.ip}


def test_wifi_and_ethernet_both_connected():
    r = choose([WIFI, ETH])
    assert r.best == [ETH, WIFI]  # Ethernet first (faster), Wi-Fi shown too
    office_wifi = Adapter("10.0.0.20", "Wi-Fi", "Intel(R) Wi-Fi 6", "wifi", True, True)
    assert choose([office_wifi, ETH]).best == [ETH, office_wifi]  # by kind, not by address order


def test_link_local_means_no_network():
    r = choose([APIPA, LOOP])
    assert r.best == [] and "Không tìm thấy mạng LAN" in r.warnings[0]


def test_no_network_at_all_warns_and_never_offers_localhost():
    r = choose([LOOP])
    assert r.best == [] and r.urls(8000) == []
    assert "Không tìm thấy mạng LAN" in r.warnings[0]
    out = io.StringIO()
    assert network.startup_text(r, 8000) == ""


def test_only_a_vpn():
    r = choose([TAILSCALE, LOOP])
    assert r.best == [TAILSCALE] and "Chỉ có mạng VPN" in r.warnings[0]


def test_lan_without_router():
    direct = Adapter("192.168.137.1", "Ethernet", "Realtek", "ethernet", True, False)
    r = choose([direct])
    assert r.best == [direct] and "router" in r.warnings[0]


def test_ipv6_and_garbage_are_ignored():
    r = choose([Adapter("fe80::1", "Wi-Fi", "", "wifi"), Adapter("not an ip", "x"), WIFI])
    assert r.best == [WIFI]


def test_this_computer_really():
    found = network.find_addresses()
    for a in found.best + found.others:
        ip = ipaddress.ip_address(a.ip)
        assert ip.version == 4 and not ip.is_loopback
    for a in found.best:
        assert a.up and a.kind not in ("vpn", "virtual")


# ======================================================================= terminal and admin page


def test_startup_prints_the_address_and_a_qr_code(monkeypatch):
    monkeypatch.setattr(network, "list_adapters", lambda: [HYPERV, WIFI, LOOP])
    monkeypatch.setattr(network, "public_profile_names", lambda timeout=5.0: [])
    out = io.StringIO()
    announce_addresses(8000, out=out)
    text = out.getvalue()
    assert "http://192.168.1.20:8000/" in text and "(Wi-Fi: Wi-Fi)" in text
    assert "172.24.80.1" not in text and "127.0.0.1" not in text
    qr = [line for line in text.splitlines() if "█" in line]
    assert len(qr) >= 13 and len({len(line) for line in qr}) == 1  # a square block of text


def test_console_without_unicode_still_gets_the_address(monkeypatch):
    monkeypatch.setattr(network, "list_adapters", lambda: [WIFI])
    monkeypatch.setattr(network, "public_profile_names", lambda timeout=5.0: [])
    raw = io.BytesIO()
    out = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="\n")  # redirected to a file
    announce_addresses(8000, out=out)
    out.flush()
    assert raw.getvalue().decode("cp1252") == "Open on a phone on the same network: http://192.168.1.20:8000/\n"


def test_qr_code_holds_the_url():
    import segno

    url = "http://192.168.1.20:8000/"
    code = segno.make(url, error="m", micro=False)
    assert code.designator.endswith("-M")
    assert network.qr_svg_data_uri(url).startswith("data:image/svg+xml")
    # The same matrix as what the terminal shows (dark modules drawn as spaces).
    rows = network.qr_text(url).splitlines()
    assert len(rows) == (len(code.matrix) + 4 + 1) // 2


def test_startup_without_network_logs_a_warning(monkeypatch, caplog):
    monkeypatch.setattr(network, "list_adapters", lambda: [LOOP])
    monkeypatch.setattr(network, "public_profile_names", lambda timeout=5.0: [])
    out = io.StringIO()
    announce_addresses(8000, out=out)
    assert out.getvalue() == "" and "Không tìm thấy mạng LAN" in caplog.text


def test_public_profile_warning(monkeypatch, caplog):
    found = choose([WIFI])
    w = network.public_profile_warnings(found, names=["Wi-Fi"])
    assert len(w) == 1 and "Public" in w[0] and "Private" in w[0]
    assert network.public_profile_warnings(found, names=["Ethernet"]) == []


def settings(tmp_path):
    return load_settings(environ={"HUB_INDEX_PATH": os.environ["HUB_INDEX_PATH"],
                                  "HUB_CACHE_DIR": os.environ["HUB_CACHE_DIR"]},
                         env_file=tmp_path / "none.env", cli_paths=[str(LIBRARY)])


def test_admin_page_api(tmp_path, monkeypatch):
    monkeypatch.setattr(network, "list_adapters", lambda: [ETH_UNPLUGGED, ETH, HYPERV, LOOP])
    monkeypatch.setattr(network, "public_profile_names", lambda timeout=5.0: ["Ethernet 2"])
    app = create_app(settings(tmp_path))
    with TestClient(app, base_url="http://localhost:8000", client=("127.0.0.1", 5)) as c:
        data = c.get("/api/admin/network").json()
    assert data["port"] == 8000
    assert [a["url"] for a in data["addresses"]] == ["http://192.168.1.2:8000/"]
    assert {a["ip"] for a in data["others"]} == {"192.168.1.103", "172.24.80.1"}
    assert data["qr_svg"].startswith("data:image/svg+xml")
    assert any("Public" in w for w in data["warnings"])
    with TestClient(app, client=("192.168.1.50", 5)) as phone:
        assert phone.get("/api/admin/network").status_code == 403  # server computer only


def test_admin_page_shows_the_qr_code(tmp_path, monkeypatch):
    sync_api = pytest.importorskip("playwright.sync_api")
    import threading
    import time

    import uvicorn

    from source.modules.server import bind_socket

    monkeypatch.setattr(network, "list_adapters", lambda: [WIFI, HYPERV, LOOP])
    monkeypatch.setattr(network, "public_profile_names", lambda timeout=5.0: [])
    app = create_app(settings(tmp_path))
    sock = bind_socket("127.0.0.1", 0)
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, lifespan="on"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    while not server.started:
        time.sleep(0.05)
    port = sock.getsockname()[1]
    try:
        with sync_api.sync_playwright() as p:
            try:
                browser = p.chromium.launch(channel="msedge", headless=True)
            except Exception:
                browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1100, "height": 900})
            page.goto(f"http://127.0.0.1:{port}/admin")
            page.wait_for_selector("#qr:not([hidden])")
            assert page.inner_text("#urls").startswith(f"http://192.168.1.20:{port}/")
            assert page.evaluate("document.querySelector('#qr').naturalWidth") > 100
            assert "172.24.80.1" in page.text_content("#others")  # in the closed "other addresses"
            if os.environ.get("HUB_SCREENSHOTS"):
                # Only this panel: the folder list above shows paths of this computer.
                page.locator("section[aria-labelledby=connect-title]").screenshot(
                    path=str(PROJECT_ROOT / "docs" / "planning" / "img" / "5.3-admin-qr.png"))
            browser.close()
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
