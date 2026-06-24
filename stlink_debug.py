#!/usr/bin/env python3
"""
ST-Link V2 通信诊断工具
用于排查 USB 超时问题
"""
import sys
import time

# 加载 libusb 后端
_usb_backend = None
try:
    import libusb_package
    import importlib.resources
    import usb.backend.libusb1
    import glob, os
    _dll_dir = str(importlib.resources.files("libusb_package"))
    _dll_candidates = glob.glob(os.path.join(_dll_dir, "**", "libusb-1.0*"), recursive=True)
    if _dll_candidates:
        _usb_backend = usb.backend.libusb1.get_backend(find_library=lambda x: _dll_candidates[0])
        print(f"[i] 使用 libusb-package 后端: {_dll_candidates[0]}")
except (ImportError, Exception) as e:
    print(f"[i] libusb-package 加载失败: {e}")

# 尝试用脚本目录的 dll
if not _usb_backend:
    try:
        import usb.backend.libusb1
        script_dir = os.path.dirname(os.path.abspath(__file__))
        dll_path = os.path.join(script_dir, "libusb-1.0.dll")
        if os.path.exists(dll_path):
            _usb_backend = usb.backend.libusb1.get_backend(find_library=lambda x: dll_path)
            print(f"[i] 使用本地 DLL: {dll_path}")
    except Exception as e:
        print(f"[i] 本地 DLL 加载失败: {e}")

if not _usb_backend:
    try:
        import usb.backend.libusb1
        _usb_backend = usb.backend.libusb1.get_backend()
        if _usb_backend:
            print("[i] 使用系统 libusb 后端")
    except Exception:
        pass

if not _usb_backend:
    print("[✗] 无法加载任何 libusb 后端！")
    sys.exit(1)

import usb.core
import usb.util

STLINK_VID = 0x0483
STLINK_V2_PID = 0x3748

print("\n[1] 查找 ST-Link 设备...")
dev = usb.core.find(idVendor=STLINK_VID, idProduct=STLINK_V2_PID, backend=_usb_backend)
if dev is None:
    print("[✗] 未找到 ST-Link V2")
    sys.exit(1)
print(f"[✓] 找到设备: VID=0x{dev.idVendor:04X} PID=0x{dev.idProduct:04X}")
print(f"    Bus={dev.bus} Address={dev.address}")

print("\n[2] 设备描述符信息:")
print(f"    bNumConfigurations: {dev.bNumConfigurations}")
try:
    mfr = usb.util.get_string(dev, dev.iManufacturer)
    prod = usb.util.get_string(dev, dev.iProduct)
    print(f"    Manufacturer: {mfr}")
    print(f"    Product: {prod}")
except:
    print("    (无法读取字符串描述符)")

print("\n[3] 分离内核驱动...")
try:
    if dev.is_kernel_driver_active(0):
        dev.detach_kernel_driver(0)
        print("    已分离内核驱动")
    else:
        print("    无内核驱动")
except (usb.core.USBError, NotImplementedError) as e:
    print(f"    跳过: {e}")

print("\n[4] 设置配置...")
try:
    dev.set_configuration()
    print("    配置成功")
except usb.core.USBError as e:
    print(f"    配置失败(可忽略): {e}")

print("\n[5] 枚举端点:")
try:
    cfg = dev.get_active_configuration()
    print(f"    活动配置: {cfg.bConfigurationValue}")
    for intf in cfg:
        print(f"    接口 {intf.bInterfaceNumber} (alt={intf.bAlternateSetting}):")
        for ep in intf:
            direction = "IN" if usb.util.endpoint_direction(ep.bEndpointAddress) == usb.util.ENDPOINT_IN else "OUT"
            print(f"      EP 0x{ep.bEndpointAddress:02X} ({direction}) MaxPacket={ep.wMaxPacketSize} Type={ep.bmAttributes & 0x3}")
except usb.core.USBError as e:
    print(f"    枚举失败: {e}")

print("\n[6] 测试通信 - 发送 GET_VERSION (0xF1)...")
# ST-Link V2: OUT=0x02, IN=0x81
EP_OUT = 0x02
EP_IN = 0x81

cmd = bytearray(16)
cmd[0] = 0xF1  # STLINK_GET_VERSION

for timeout in [1000, 3000, 5000, 10000]:
    print(f"\n  尝试 timeout={timeout}ms:")
    try:
        print(f"    写入 EP 0x{EP_OUT:02X}... ", end="", flush=True)
        written = dev.write(EP_OUT, cmd, timeout=timeout)
        print(f"OK ({written} bytes)")
        
        print(f"    读取 EP 0x{EP_IN:02X}... ", end="", flush=True)
        data = dev.read(EP_IN, 6, timeout=timeout)
        print(f"OK ({len(data)} bytes)")
        print(f"    响应: {' '.join(f'{b:02X}' for b in data)}")
        
        ver = (data[0] << 8) | data[1]
        stlink_ver = (ver >> 12) & 0x0F
        jtag_ver = (ver >> 6) & 0x3F
        swim_ver = ver & 0x3F
        print(f"    ST-Link版本: V{stlink_ver}, JTAG: v{jtag_ver}, SWIM: v{swim_ver}")
        print("\n[✓] 通信成功！")
        break
    except usb.core.USBError as e:
        print(f"失败: {e}")
else:
    print("\n[✗] 所有超时都失败")
    print("\n[诊断建议]:")
    print("  1. 确认 Zadig 替换的是正确的接口（Interface 0）")
    print("  2. 尝试拔插 ST-Link 后重试")
    print("  3. 在设备管理器中确认驱动显示为 WinUSB 或 libusb-win32")
    print("  4. 如果 ST-Link 的 LED 不亮，可能硬件有问题")

usb.util.dispose_resources(dev)
