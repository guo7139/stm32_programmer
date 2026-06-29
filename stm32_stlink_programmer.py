#!/usr/bin/env python3
"""
STM32 ST-Link SWD Programmer
通过 USB 直接与 ST-Link V2 通信，实现 SWD 协议烧录 STM32
"""

import sys, os, struct, time, argparse
from pathlib import Path

# ============ libusb 后端 ============
_usb_backend = None
try:
    import libusb_package, importlib.resources, glob
    import usb.backend.libusb1
    _dll_dir = str(importlib.resources.files("libusb_package"))
    _dlls = glob.glob(os.path.join(_dll_dir, "**", "libusb-1.0*"), recursive=True)
    if _dlls:
        _usb_backend = usb.backend.libusb1.get_backend(find_library=lambda x: _dlls[0])
except Exception:
    pass
if not _usb_backend:
    try:
        import usb.backend.libusb1
        _p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libusb-1.0.dll")
        if os.path.exists(_p):
            _usb_backend = usb.backend.libusb1.get_backend(find_library=lambda x: _p)
    except Exception:
        pass

import usb.core, usb.util

# ============ 常量 ============
STLINK_VID = 0x0483
STLINK_V2_PID = 0x3748
STLINK_V21_PID = 0x374B
STLINK_V3_PID = 0x374F
STLINK_PIDS = [STLINK_V2_PID, STLINK_V21_PID, STLINK_V3_PID]

CMD_GET_VERSION = 0xF1
CMD_DEBUG = 0xF2
CMD_DFU = 0xF3
CMD_GET_MODE = 0xF5
CMD_GET_VOLTAGE = 0xF7

MODE_DFU = 0x00
MODE_MASS = 0x01
MODE_DEBUG = 0x02
DFU_EXIT = 0x07

DBG_ENTER = 0x30  # APIV2 (JTAG >= v22)
DBG_EXIT = 0x21
DBG_READCOREID = 0x22
DBG_RESETSYS = 0x03
DBG_READMEM32 = 0x07
DBG_WRITEMEM32 = 0x08
DBG_WRITEMEM16 = 0x48  # 16-bit 总线写内存，F1 半字编程必需
DBG_RUNCORE = 0x09
DBG_HALTCORE = 0x02
DBG_ENTER_SWD = 0xA3

FLASH_KEY1 = 0x45670123
FLASH_KEY2 = 0xCDEF89AB
DEFAULT_FLASH_START = 0x08000000

CHIP_IDS = {
    0x410: "STM32F1 Medium-density", 0x411: "STM32F2/F4xx",
    0x412: "STM32F1 Low-density", 0x413: "STM32F40x/41x",
    0x414: "STM32F1 High-density", 0x415: "STM32L4xx",
    0x416: "STM32L1xx", 0x418: "STM32F1 Connectivity",
    0x419: "STM32F42x/43x", 0x420: "STM32F1 VL Medium",
    0x421: "STM32F446", 0x423: "STM32F401xB/C",
    0x425: "STM32L0xx", 0x428: "STM32F1 VL High",
    0x430: "STM32F1 XL", 0x431: "STM32F411",
    0x422: "STM32F302xB/C/F303xB/C", 0x432: "STM32F37x",
    0x433: "STM32F401xD/E", 0x438: "STM32F303x4/F334/F328",
    0x439: "STM32F301/F302x6x8/F318", 0x446: "STM32F302xE/F303xE",
    0x434: "STM32F469/479", 0x440: "STM32F05x",
    0x441: "STM32F412", 0x442: "STM32F09x",
    0x444: "STM32F03x", 0x445: "STM32F04x",
    0x448: "STM32F07x", 0x449: "STM32F74x/75x",
    0x450: "STM32H7xx", 0x451: "STM32F76x/77x",
    0x460: "STM32G0xx", 0x468: "STM32G4xx",
}


class STM32Error(Exception):
    pass


class IntelHexParser:
    @staticmethod
    def parse(filepath):
        segments = {}
        base_addr = 0
        with open(filepath, 'r') as f:
            for line in f:
                line = line.strip()
                if not line.startswith(':'):
                    continue
                raw = bytes.fromhex(line[1:])
                length, rec_type = raw[0], raw[3]
                offset = (raw[1] << 8) | raw[2]
                data = raw[4:4+length]
                if rec_type == 0x00:
                    segments[base_addr + offset] = data
                elif rec_type == 0x01:
                    break
                elif rec_type == 0x02:
                    base_addr = ((data[0] << 8) | data[1]) << 4
                elif rec_type == 0x04:
                    base_addr = ((data[0] << 8) | data[1]) << 16
        if not segments:
            raise ValueError("HEX 文件为空或格式错误")
        min_addr = min(segments.keys())
        max_addr = max(a + len(d) for a, d in segments.items())
        result = bytearray(max_addr - min_addr)
        for a, d in segments.items():
            o = a - min_addr
            result[o:o+len(d)] = d
        return min_addr, bytes(result)


class STLink:
    """ST-Link USB 通信 - 与 stlink_debug.py 完全一致的通信方式"""

    def __init__(self, serial=None, index=None):
        self.dev = None
        self._ep_out = 0x02  # V2默认
        self._ep_in = 0x81
        self.serial = serial    # 按序列号选择设备
        self.index = index      # 按编号选择设备(从1开始)

    @staticmethod
    def list_devices():
        """枚举所有已连接的 ST-Link 设备，返回 [(dev, pid, name, serial), ...]"""
        devs = []
        for pid in STLINK_PIDS:
            found = usb.core.find(find_all=True, idVendor=STLINK_VID,
                                  idProduct=pid, backend=_usb_backend)
            for d in (found or []):
                name = {STLINK_V2_PID: "V2", STLINK_V21_PID: "V2-1",
                        STLINK_V3_PID: "V3"}.get(pid, "?")
                try:
                    sn = usb.util.get_string(d, d.iSerialNumber) or ""
                except Exception:
                    sn = ""
                devs.append((d, pid, name, sn))
        return devs

    def open(self):
        devs = STLink.list_devices()
        if not devs:
            raise STM32Error("未找到 ST-Link 设备")

        chosen = None
        if self.serial:
            # 按序列号匹配（支持部分匹配）
            for d, pid, name, sn in devs:
                if sn and (sn == self.serial or self.serial in sn):
                    chosen = (d, pid, name, sn); break
            if chosen is None:
                raise STM32Error(f"未找到序列号匹配 '{self.serial}' 的 ST-Link 设备")
        elif self.index is not None:
            if self.index < 1 or self.index > len(devs):
                raise STM32Error(f"设备编号 {self.index} 超出范围 (共 {len(devs)} 个)")
            chosen = devs[self.index - 1]
        elif len(devs) == 1:
            chosen = devs[0]
        else:
            # 多个设备且未指定 → 交互式选择
            print(f"[*] 检测到 {len(devs)} 个 ST-Link 设备:")
            for i, (d, pid, name, sn) in enumerate(devs, 1):
                print(f"    {i}. ST-Link {name} (PID: 0x{pid:04X}) 序列号: {sn or '(无)'}")
            while True:
                try:
                    sel = input(f"  请选择要使用的设备 [1-{len(devs)}]: ").strip()
                except EOFError:
                    raise STM32Error("检测到多个 ST-Link，请用 --device N 或 --serial SN 指定")
                if sel.isdigit() and 1 <= int(sel) <= len(devs):
                    chosen = devs[int(sel) - 1]; break
                print("  输入无效，请重新输入")

        self.dev, pid, name, sn = chosen
        print(f"[✓] 使用 ST-Link {name} (PID: 0x{pid:04X})" + (f" 序列号: {sn}" if sn else ""))

        if pid != STLINK_V2_PID:
            self._ep_out = 0x01

        # 与诊断脚本完全一致的初始化流程
        try:
            if self.dev.is_kernel_driver_active(0):
                self.dev.detach_kernel_driver(0)
        except Exception:
            pass

        # claim interface（关键！Windows WinUSB 需要）
        try:
            usb.util.claim_interface(self.dev, 0)
        except Exception:
            pass

        # 清空残留数据
        try:
            self.dev.read(self._ep_in, 64, timeout=50)
        except Exception:
            pass

        # 验证通信（与诊断脚本相同的 GET_VERSION）
        print("  验证通信...", end="", flush=True)
        try:
            buf = bytearray(16)
            buf[0] = CMD_GET_VERSION
            self.dev.write(self._ep_out, buf, timeout=1000)
            res = self.dev.read(self._ep_in, 64, timeout=1000)
            ver = (res[0] << 8) | res[1]
            sv = (ver >> 12) & 0x0F
            jv = (ver >> 6) & 0x3F
            print(f" OK (FW: V{sv}, JTAG: v{jv})")
        except Exception as e:
            print(f" 失败: {e}")
            raise STM32Error(f"ST-Link通信失败: {e}. 请拔插ST-Link后重试。")

    def close(self):
        if self.dev:
            usb.util.dispose_resources(self.dev)
            self.dev = None

    def _cmd(self, data, rx_len=64, timeout=1000):
        """发送16字节命令，读取响应"""
        buf = bytearray(16)
        for i, b in enumerate(data):
            buf[i] = b
        self.dev.write(self._ep_out, buf, timeout=timeout)
        if rx_len > 0:
            # 始终读64字节（ST-Link固定包大小），避免残留数据
            res = bytes(self.dev.read(self._ep_in, 64, timeout=timeout))
            return res[:rx_len]
        return b''

    def _write_bulk(self, data, timeout=3000):
        """写入大块数据"""
        self.dev.write(self._ep_out, data, timeout=timeout)

    def get_version(self):
        res = self._cmd([CMD_GET_VERSION], rx_len=6)
        ver = (res[0] << 8) | res[1]
        return (ver >> 12) & 0x0F, (ver >> 6) & 0x3F, ver & 0x3F

    def get_mode(self):
        res = self._cmd([CMD_GET_MODE], rx_len=2)
        return res[0]

    def get_voltage(self):
        res = self._cmd([CMD_GET_VOLTAGE], rx_len=8)
        a0 = struct.unpack_from('<I', res, 0)[0]
        a1 = struct.unpack_from('<I', res, 4)[0]
        return 2.0 * a1 * 1.2 / a0 if a0 else 0.0

    def leave_mode(self):
        print("get_mode...", end="", flush=True)
        mode = self.get_mode()
        print(f"mode={mode}...", end="", flush=True)
        if mode == MODE_DFU:
            print("exit_dfu...", end="", flush=True)
            # DFU_EXIT: 发送命令后不读响应，但需要等设备切换模式
            buf = bytearray(16)
            buf[0] = CMD_DFU
            buf[1] = DFU_EXIT
            self.dev.write(self._ep_out, buf, timeout=1000)
            time.sleep(0.5)
            # 清空可能的残留数据
            try:
                self.dev.read(self._ep_in, 64, timeout=100)
            except Exception:
                pass
            print("ok...", end="", flush=True)
        elif mode == MODE_DEBUG:
            print("exit_debug...", end="", flush=True)
            self._cmd([CMD_DEBUG, DBG_EXIT], rx_len=2)
            print("ok...", end="", flush=True)
        elif mode == MODE_MASS:
            pass  # mass storage 模式不需要退出

    def drive_nrst(self, level):
        """控制 NRST 引脚: 0=低(复位), 1=高(释放)"""
        try:
            self._cmd([CMD_DEBUG, 0x3C, level & 1], rx_len=2)
        except Exception:
            pass

    def read_dap(self, ap, addr):
        """READ_DAP_REG (0x45): 读 DP/AP 寄存器, ap=0xFFFF为DP"""
        cmd = bytearray(16)
        cmd[0] = CMD_DEBUG
        cmd[1] = 0x45
        struct.pack_into('<H', cmd, 2, ap & 0xFFFF)
        struct.pack_into('<H', cmd, 4, addr & 0xFFFF)
        self.dev.write(self._ep_out, cmd, timeout=1000)
        r = bytes(self.dev.read(self._ep_in, 8, timeout=1000))
        return r[0], struct.unpack('<I', r[4:8])[0]

    def write_dap(self, ap, addr, val):
        """WRITE_DAP_REG (0x46): 写 DP/AP 寄存器"""
        cmd = bytearray(16)
        cmd[0] = CMD_DEBUG
        cmd[1] = 0x46
        struct.pack_into('<H', cmd, 2, ap & 0xFFFF)
        struct.pack_into('<H', cmd, 4, addr & 0xFFFF)
        struct.pack_into('<I', cmd, 6, val)
        self.dev.write(self._ep_out, cmd, timeout=1000)
        return bytes(self.dev.read(self._ep_in, 2, timeout=1000))[0]

    def power_up_debug(self):
        """给 debug 电源域上电 (CDBGPWRUPREQ|CSYSPWRUPREQ)，确保AHB-AP可访问内存"""
        try:
            for _ in range(5):
                self.write_dap(0xFFFF, 0x04, (1 << 28) | (1 << 30))
                time.sleep(0.02)
                st, v = self.read_dap(0xFFFF, 0x04)
                if (v >> 29) & 1:  # CDBGPWRUPACK
                    return True
            return False
        except Exception:
            return False

    def _do_enter_swd(self):
        """发送 APIV2 进入SWD命令，返回status"""
        res = self._cmd([CMD_DEBUG, DBG_ENTER, DBG_ENTER_SWD], rx_len=2)
        return res[0]

    def enter_swd(self):
        # 1. 检查当前模式
        mode = self.get_mode()
        print(f"mode={mode}...", end="", flush=True)

        # 2. 如果在DFU模式，先退出（DFU_EXIT 不读响应）
        if mode == MODE_DFU:
            print("dfu_exit...", end="", flush=True)
            buf = bytearray(16)
            buf[0] = CMD_DFU
            buf[1] = DFU_EXIT
            self.dev.write(self._ep_out, buf, timeout=1000)
            time.sleep(0.3)

        # 3. 进入 SWD (APIV2)，失败重试
        print("enter_swd...", end="", flush=True)
        last = 0
        for attempt in range(5):
            last = self._do_enter_swd()
            if last == 0x80:
                break
            print(f"[0x{last:02X}retry]...", end="", flush=True)
            try:
                self._cmd([CMD_DEBUG, DBG_EXIT], rx_len=2)
            except Exception:
                pass
            time.sleep(0.2)

        # 4. 若简单进入失败，尝试 connect-under-reset (NRST复位唤醒)
        if last != 0x80:
            print("[NRST复位]...", end="", flush=True)
            self.drive_nrst(0)        # 拉低复位
            time.sleep(0.2)
            self.drive_nrst(1)        # 释放
            time.sleep(0.01)          # 趁芯片刚醒
            last = self._do_enter_swd()
            if last != 0x80:
                raise STM32Error(f"进入SWD失败: status=0x{last:02X}")

        # 5. 给 debug 电源域上电，确保 AHB-AP 可访问内存（F7/M7关键）
        if not self.power_up_debug():
            print("[debug域上电失败]", end="", flush=True)
        print(" OK")

    def halt(self):
        self._cmd([CMD_DEBUG, DBG_HALTCORE], rx_len=2)

    def run(self):
        self._cmd([CMD_DEBUG, DBG_RUNCORE], rx_len=2)

    def reset(self):
        self._cmd([CMD_DEBUG, DBG_RESETSYS], rx_len=2)
        time.sleep(0.1)

    def read_mem32(self, addr, size):
        cmd = bytearray(16)
        cmd[0] = CMD_DEBUG
        cmd[1] = DBG_READMEM32
        struct.pack_into('<I', cmd, 2, addr)
        struct.pack_into('<H', cmd, 6, size)
        self.dev.write(self._ep_out, cmd, timeout=1000)
        return bytes(self.dev.read(self._ep_in, size, timeout=1000))

    def write_mem32(self, addr, data):
        cmd = bytearray(16)
        cmd[0] = CMD_DEBUG
        cmd[1] = DBG_WRITEMEM32
        struct.pack_into('<I', cmd, 2, addr)
        struct.pack_into('<H', cmd, 6, len(data))
        self.dev.write(self._ep_out, cmd, timeout=1000)
        self._write_bulk(data, timeout=1000)

    def write_mem8(self, addr, data):
        """8-bit 总线写内存 (WRITEMEM_8BIT=0x0D)，单次<=64字节。用于低电压8-bit Flash编程"""
        cmd = bytearray(16)
        cmd[0] = CMD_DEBUG
        cmd[1] = 0x0D
        struct.pack_into('<I', cmd, 2, addr)
        struct.pack_into('<H', cmd, 6, len(data))
        self.dev.write(self._ep_out, cmd, timeout=1000)
        self._write_bulk(data, timeout=1000)

    def write_mem16(self, addr, data):
        """16-bit 总线写内存 (WRITEMEM_16BIT=0x48)。STM32F0/F1 Flash 半字编程必需，
        8-bit/32-bit 总线写无法触发 F1 编程。addr 和长度需 2 字节对齐，单次<=64字节。"""
        cmd = bytearray(16)
        cmd[0] = CMD_DEBUG
        cmd[1] = DBG_WRITEMEM16
        struct.pack_into('<I', cmd, 2, addr)
        struct.pack_into('<H', cmd, 6, len(data))
        self.dev.write(self._ep_out, cmd, timeout=1000)
        self._write_bulk(data, timeout=1000)

    def read_reg32(self, addr):
        d = self.read_mem32(addr, 4)
        return struct.unpack_from('<I', d, 0)[0]

    def write_reg32(self, addr, val):
        self.write_mem32(addr, struct.pack('<I', val))


# ============ STM32 Flash 编程 ============
class STM32Programmer:
    FLASH_BASE_F1 = 0x40022000
    FLASH_BASE_F4 = 0x40023C00
    FLASH_KEYR = 0x04
    FLASH_SR = 0x0C
    FLASH_CR = 0x10
    DHCSR = 0xE000EDF0
    AIRCR = 0xE000ED0C
    DBGMCU_IDCODE = 0xE0042000

    def __init__(self, serial=None, index=None):
        self.stlink = STLink(serial=serial, index=index)
        self.chip_id = None
        self.flash_base = self.FLASH_BASE_F1
        self.is_f4 = False
        self.is_f7 = False
        self.psize = 2  # F4/F7编程并行度: 0=8bit,1=16bit,2=32bit
        self.is_h7 = False
        self.page_size = 1024
        self.flash_word_size = 2  # 默认F1半字

    def connect(self, force_chip=None):
        self.stlink.open()
        print("  进入SWD...", end="", flush=True)
        self.stlink.enter_swd()
        print("  停止CPU...", end="", flush=True)
        # halt CPU
        self.stlink.write_reg32(self.DHCSR, 0xA05F0003)
        time.sleep(0.05)
        # connect-under-reset: 设置 DEMCR.VC_CORERESET=1 (复位后立即halt)，再系统复位
        # 确保抢占运行中的固件，让芯片停在复位向量处，DBGMCU 可靠读取
        try:
            self.stlink.write_reg32(0xE000EDFC, 0x00000001)  # DEMCR VC_CORERESET
            self.stlink.write_reg32(0xE000ED0C, 0x05FA0004)  # AIRCR SYSRESETREQ
            time.sleep(0.1)
            self.stlink.write_reg32(self.DHCSR, 0xA05F0003)  # 再次确保 halt
            time.sleep(0.05)
            self.stlink.write_reg32(0xE000EDFC, 0x00000000)  # 清除 VC_CORERESET
        except Exception:
            pass
        # read chip id (多地址 + 重试，刚进调试时DBGMCU可能未就绪)
        raw = 0
        self.chip_id = 0
        for _ in range(5):
            for addr in (self.DBGMCU_IDCODE, 0x5C001000, 0x40015800):
                raw = self.stlink.read_reg32(addr)
                cid = raw & 0xFFF
                if cid != 0 and cid != 0xFFF:
                    self.chip_id = cid
                    break
            if self.chip_id:
                break
            time.sleep(0.1)
        # 手动指定芯片系列（兜底）
        if force_chip:
            fc = force_chip.lower()
            forced = {'h7': 0x450, 'f7': 0x451, 'f4': 0x413,
                      'f1': 0x410, 'f0': 0x440}.get(fc)
            if forced:
                self.chip_id = forced
                print(f"[!] 手动指定芯片系列: {fc.upper()}")
        name = CHIP_IDS.get(self.chip_id, "Unknown")
        print(f"[✓] 芯片: {name} (ID: 0x{self.chip_id:03X}, REV: 0x{raw>>16:04X})")
        if self.chip_id == 0:
            print("[!] 警告: 无法读取芯片ID。若烧录失败请用 --chip f7/f4/h7/f1 手动指定系列")
        # 判断系列
        if self.chip_id in (0x450, 0x480, 0x483):
            # STM32H7xx
            self.flash_base = 0x52002000
            self.is_h7 = True
            self.is_f4 = False
            self.page_size = 131072  # 128KB 扇区
            self.flash_word_size = 32  # 256-bit flash word
            print(f"    系列: STM32H7, Flash Word=256-bit, 扇区=128KB")
        elif self.chip_id in (0x449, 0x451, 0x452):
            # STM32F7xx (F74x/75x, F76x/77x, F72x/73x)
            self.flash_base = self.FLASH_BASE_F4
            self.is_f7 = True
            self.page_size = 32768
            print(f"    系列: STM32F7, 32-bit 编程, 扇区=32KB/128KB/256KB")
        elif self.chip_id in (0x411, 0x413, 0x419, 0x421, 0x423, 0x431,
                            0x433, 0x434, 0x441, 0x458, 0x463):
            self.flash_base = self.FLASH_BASE_F4
            self.is_f4 = True
            self.page_size = 16384
        elif self.chip_id in (0x414, 0x418, 0x428, 0x430):
            self.page_size = 2048
        else:
            self.page_size = 1024
        # 读电压
        v = self.stlink.get_voltage()
        if v > 0:
            print(f"  目标电压: {v:.2f}V")
        # F4/F7 编程并行度受供电电压限制:
        #   >=2.7V: 32bit(2)  2.1~2.7V: 16bit(1)  1.8~2.1V: 8bit(0)
        if self.is_f4 or self.is_f7:
            # 默认32-bit编程(PSIZE=2)，与官方工具一致。
            # ST-Link测量电压常偏低不可信，且32-bit总线写最稳定。
            # 如确需低电压降级，可在此改 self.psize
            self.psize = 2  # 32-bit
            bits = 8 << self.psize
            print(f"    编程并行度: {bits}-bit (PSIZE={self.psize})")

    def flash_unlock(self):
        if self.is_h7:
            # H7: KEYR1 at offset 0x04, CR1 at 0x0C
            # 检查是否已解锁
            cr = self.stlink.read_reg32(self.flash_base + 0x0C)
            if cr & 0x01:  # LOCK bit
                self.stlink.write_reg32(self.flash_base + 0x04, 0x45670123)
                self.stlink.write_reg32(self.flash_base + 0x04, 0xCDEF89AB)
                cr = self.stlink.read_reg32(self.flash_base + 0x0C)
                if cr & 0x01:
                    raise STM32Error("H7 Flash Bank1 解锁失败")
            # 清除所有错误标志
            self.stlink.write_reg32(self.flash_base + 0x14, 0x0FEF0000)
        elif self.is_f4 or self.is_f7:
            # F4/F7: 检查LOCK, 若锁定则解锁
            # 关键(F7): 解锁必须在连接后第一时间, 前面不能有FLASH_CR写操作污染状态机
            cr = self.stlink.read_reg32(self.flash_base + self.FLASH_CR)
            if cr & 0x80000000:  # LOCK bit31
                self.stlink.write_reg32(self.flash_base + self.FLASH_KEYR, 0x45670123)
                self.stlink.write_reg32(self.flash_base + self.FLASH_KEYR, 0xCDEF89AB)
                cr = self.stlink.read_reg32(self.flash_base + self.FLASH_CR)
                if cr & 0x80000000:
                    raise STM32Error(f"FLASH 解锁失败 CR=0x{cr:08X} (F7需连接后立即解锁,检查时序)")
        else:
            # F0/F1: LOCK = bit7。复位后默认锁定(CR=0x80)，必须解锁否则擦除/编程静默失效。
            cr = self.stlink.read_reg32(self.flash_base + self.FLASH_CR)
            if cr & 0x80:  # LOCK bit7
                self.stlink.write_reg32(self.flash_base + self.FLASH_KEYR, 0x45670123)
                self.stlink.write_reg32(self.flash_base + self.FLASH_KEYR, 0xCDEF89AB)
                cr = self.stlink.read_reg32(self.flash_base + self.FLASH_CR)
                if cr & 0x80:
                    raise STM32Error(f"F1 FLASH 解锁失败 CR=0x{cr:08X}")

    def flash_lock(self):
        if self.is_h7:
            cr = self.stlink.read_reg32(self.flash_base + 0x0C)
            self.stlink.write_reg32(self.flash_base + 0x0C, cr | 0x01)
        elif self.is_f4 or self.is_f7:
            # F4/F7 LOCK = bit31
            cr = self.stlink.read_reg32(self.flash_base + self.FLASH_CR)
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, cr | 0x80000000)
        else:
            # F1 LOCK = bit7
            cr = self.stlink.read_reg32(self.flash_base + self.FLASH_CR)
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, cr | 0x80)

    def flash_wait(self, timeout=10.0):
        t0 = time.time()
        if self.is_h7:
            # H7: SR1 at offset 0x10, BSY=bit0 QW=bit2
            while True:
                sr = self.stlink.read_reg32(self.flash_base + 0x10)
                if not (sr & 0x05):  # BSY=0 and QW=0
                    break
                if time.time() - t0 > timeout:
                    raise STM32Error(f"H7 Flash 超时 SR=0x{sr:08X}")
                time.sleep(0.001)
            # H7 SR1 错误位:
            #   bit16=WRPERR, bit17=PGSERR, bit18=STRBERR, bit19=INCERR
            #   bit21=RDPERR, bit22=RDSERR, bit23=SNECCERR, bit24=DBECCERR
            # 通过ST-Link调试器编程时 WRPERR(bit16) 和 ECC错误(bit23/24) 会误触发，
            # 实测数据写入正确，故忽略这些，只对致命错误报错
            FATAL = 0x000E0000  # PGSERR | STRBERR | INCERR
            if sr & 0x0FFF0000:
                # 清除所有错误标志
                self.stlink.write_reg32(self.flash_base + 0x14, 0x0FFF0000)
            if sr & FATAL:
                raise STM32Error(f"H7 Flash 致命错误 SR=0x{sr:08X}")
        elif self.is_f4 or self.is_f7:
            # F4/F7 SR: bit16=BSY, bit0=EOP, bit1=OPERR
            #   bit4=WRPERR, bit5=PGAERR, bit6=PGPERR, bit7=ERSERR
            while True:
                sr = self.stlink.read_reg32(self.flash_base + self.FLASH_SR)
                if not (sr & (1 << 16)):  # BSY=bit16
                    break
                if time.time() - t0 > timeout:
                    raise STM32Error(f"Flash 操作超时 SR=0x{sr:08X}")
                time.sleep(0.001)
            # 错误位检查 (bit1,4,5,6,7)
            err = sr & 0xF2
            if err:
                # 清除错误标志(写1清除)
                self.stlink.write_reg32(self.flash_base + self.FLASH_SR, err)
                names = []
                if sr & 0x02: names.append("OPERR")
                if sr & 0x10: names.append("WRPERR")
                if sr & 0x20: names.append("PGAERR")
                if sr & 0x40: names.append("PGPERR")
                if sr & 0x80: names.append("ERSERR")
                raise STM32Error(f"Flash 错误 SR=0x{sr:08X} ({'|'.join(names)})")
        else:
            while True:
                sr = self.stlink.read_reg32(self.flash_base + self.FLASH_SR)
                if not (sr & 0x01):  # F1 BSY bit
                    break
                if time.time() - t0 > timeout:
                    raise STM32Error("Flash 操作超时")
                time.sleep(0.01)
            if sr & 0x04:  # PGERR
                raise STM32Error(f"Flash 编程错误 SR=0x{sr:08X}")
            if sr & 0x10:  # WRPRTERR
                raise STM32Error(f"Flash 写保护错误 SR=0x{sr:08X}")

    def mass_erase(self):
        print("[*] 全片擦除...")
        self.flash_unlock()
        if self.is_f4:
            # MER bit
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, (1<<2))
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, (1<<2)|(1<<16))
        else:
            # MER + STRT
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, 0x04)
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, 0x44)
        self.flash_wait(timeout=30)
        self.flash_lock()
        print("[✓] 擦除完成")

    def erase_pages(self, start_addr, size):
        """擦除覆盖指定范围的页/扇区"""
        if self.is_h7:
            self._erase_sectors_h7(start_addr, size)
        elif self.is_f7:
            self._erase_sectors_f7(start_addr, size)
        elif self.is_f4:
            self._erase_sectors_f4(start_addr, size)
        else:
            self._erase_pages_f1(start_addr, size)

    def _erase_sectors_h7(self, start_addr, size):
        """H7 扇区擦除（128KB/扇区）"""
        sector_size = 131072  # 128KB
        # 清除SR错误标志
        self.stlink.write_reg32(self.flash_base + 0x14, 0x0FEF0000)
        # 检查写保护状态 (WPSN_CUR1 at offset 0x38, 各bit=0表示对应扇区被保护)
        wpsn = self.stlink.read_reg32(self.flash_base + 0x38)
        if wpsn != 0xFF:  # 不是全部解保护
            print(f"  [!] 检测到写保护 WPSN=0x{wpsn:02X}, 正在解除...")
            # 写入 WPSN_PRG1 = 0xFF (解除所有扇区保护)
            self.stlink.write_reg32(self.flash_base + 0x3C, 0xFF)
            # 需要 Option byte reload - 写 OPTCR.OPTSTART
            # OPTCR at base + 0x18, OPTSTART = bit1
            # 但这需要 option unlock... 先跳过，直接试
        flash_start = 0x08000000
        first_sector = (start_addr - flash_start) // sector_size
        last_sector = (start_addr + size - 1 - flash_start) // sector_size
        n = last_sector - first_sector + 1
        print(f"[*] 擦除 {n} 个扇区 (128KB/扇区, 扇区{first_sector}-{last_sector})...")
        self.flash_unlock()
        # 清除错误标志
        self.stlink.write_reg32(self.flash_base + 0x14, 0x0FEF0000)
        for i in range(first_sector, last_sector + 1):
            # H7 CR1 正确 bit 定义: PG=bit1, SER=bit2, START=bit7,
            #   PSIZE=bit4:5, SNB=bit8:10。擦除必须用 SER=bit2(之前误用bit1=PG导致擦除不生效)
            # PSIZE=2 (32-bit, 与官方工具一致)
            SER = (1 << 2); START = (1 << 7); PSIZE = (2 << 4)
            if i < 8:
                cr_val = SER | (i << 8) | PSIZE | START
                self.stlink.write_reg32(self.flash_base + 0x0C, cr_val)
            else:
                # Bank 2: base + 0x100 偏移
                s = i - 8
                cr_val = SER | (s << 8) | PSIZE | START
                self.stlink.write_reg32(self.flash_base + 0x10C, cr_val)
            self.flash_wait(timeout=30)
            pct = (i - first_sector + 1) * 100 // n
            print(f"\r  擦除: {pct}%", end="", flush=True)
        print()
        self.flash_lock()
        print("[✓] 擦除完成")

    def _erase_pages_f1(self, start_addr, size):
        n_pages = (size + self.page_size - 1) // self.page_size
        print(f"[*] 擦除 {n_pages} 页 (页大小={self.page_size})...")
        self.flash_unlock()
        for i in range(n_pages):
            page_addr = start_addr + i * self.page_size
            # PER bit
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, 0x02)
            # 写入页地址
            self.stlink.write_reg32(self.flash_base + 0x14, page_addr)
            # STRT
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, 0x42)
            self.flash_wait(timeout=5)
            pct = (i + 1) * 100 // n_pages
            print(f"\r  擦除: {pct}%", end="", flush=True)
        print()
        self.flash_lock()
        print("[✓] 擦除完成")

    def _erase_sectors_f7(self, start_addr, size):
        # STM32F7 扇区布局: 4x32KB + 1x128KB + 7x256KB (单bank, 最多2MB)
        sectors = [32768]*4 + [131072] + [262144]*7
        flash_start = 0x08000000
        offset = start_addr - flash_start
        end = offset + size
        cur = 0
        to_erase = []
        for i, sz in enumerate(sectors):
            if cur < end and cur + sz > offset:
                to_erase.append(i)
            cur += sz
        print(f"[*] 擦除 {len(to_erase)} 个扇区 (F7)...")
        self.flash_unlock()
        # 清除SR残留错误标志(写1清除 bit1,4,5,6,7)
        self.stlink.write_reg32(self.flash_base + self.FLASH_SR, 0xF2)
        # 等待空闲
        self.flash_wait(timeout=30)
        for idx, sn in enumerate(to_erase):
            # 标准F7扇区擦除: 先配置 SER+SNB+PSIZE, 再单独置 STRT
            cr = (1 << 1) | (sn << 3) | (self.psize << 8)  # SER|SNB|PSIZE
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, cr)
            cr |= (1 << 16)  # STRT
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, cr)
            self.flash_wait(timeout=30)
            pct = (idx+1)*100//len(to_erase)
            print(f"\r  擦除: {pct}%", end="", flush=True)
        print()
        # 擦除完清 SER 位
        self.stlink.write_reg32(self.flash_base + self.FLASH_CR, 0)
        self.flash_lock()
        print("[✓] 擦除完成")

    def _erase_sectors_f4(self, start_addr, size):
        # F4 单bank(<=1MB): 4x16K + 1x64K + 7x128K (12扇区)
        # F4 双bank(2MB, 如F427/F429/F437/F439): 上述布局 x2 (24扇区)
        #   bank2 扇区的 SNB 编码从 16 开始(SNB[4]位选bank), 即扇区12->SNB16 ... 扇区23->SNB27
        bank1 = [16384]*4 + [65536] + [131072]*7  # 1MB
        sectors = bank1 + bank1  # 支持到2MB; <=1MB的固件自然只用到前几个
        flash_start = 0x08000000
        offset = start_addr - flash_start
        end = offset + size
        cur = 0
        to_erase = []  # 存(扇区索引, 对应SNB编码)
        for i, sz in enumerate(sectors):
            if cur < end and cur + sz > offset:
                snb = i if i < 12 else (i - 12 + 16)  # bank2 SNB 从16起
                to_erase.append(snb)
            cur += sz
        print(f"[*] 擦除 {len(to_erase)} 个扇区...")
        self.flash_unlock()
        # 清SR残留错误标志
        self.stlink.write_reg32(self.flash_base + self.FLASH_SR, 0xF2)
        self.flash_wait(timeout=30)
        for idx, snb in enumerate(to_erase):
            # 标准两步: 先配置 SER|SNB|PSIZE, 再单独置 STRT(bit16)
            cr = (1 << 1) | (snb << 3) | (self.psize << 8)
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, cr)
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, cr | (1 << 16))
            self.flash_wait(timeout=30)
            pct = (idx+1)*100//len(to_erase)
            print(f"\r  擦除: {pct}%", end="", flush=True)
        print()
        self.stlink.write_reg32(self.flash_base + self.FLASH_CR, 0)  # 清SER
        self.flash_lock()
        print("[✓] 擦除完成")

    def write_flash(self, addr, data):
        total = len(data)
        print(f"[*] 写入 {total} 字节...")
        self.flash_unlock()

        if self.is_h7:
            self._write_flash_h7(addr, data)
        elif self.is_f7 or self.is_f4:
            self._write_flash_f4(addr, data)
        else:
            self._write_flash_f1(addr, data)

        self.flash_lock()
        print("[✓] 写入完成")

    def _write_flash_h7(self, addr, data):
        """H7: 256-bit (32字节) flash word 编程，带per-word错误重试"""
        FB = self.flash_base
        CR1, SR1, CCR1 = 0x0C, 0x10, 0x14
        total = len(data)
        # 清除所有错误标志
        self.stlink.write_reg32(FB + CCR1, 0x0FFF0000)
        # 确认解锁
        cr = self.stlink.read_reg32(FB + CR1)
        if cr & 0x01:
            raise STM32Error(f"H7: Flash仍锁定 CR=0x{cr:08X}")
        # PG=1 | PSIZE=2(32-bit，与官方工具一致；之前用3=64-bit会触发ECC错误)
        self.stlink.write_reg32(FB + CR1, (1 << 1) | (2 << 4))
        cr = self.stlink.read_reg32(FB + CR1)
        if not (cr & 0x02):
            raise STM32Error(f"H7: PG位设置失败 CR=0x{cr:08X}")

        def _wait_qw(timeout=2.0):
            t0 = time.time()
            while True:
                sr = self.stlink.read_reg32(FB + SR1)
                if not (sr & 0x05):  # BSY=0 且 QW=0
                    return sr
                if time.time() - t0 > timeout:
                    raise STM32Error(f"H7 Flash 超时 SR=0x{sr:08X}")

        FATAL = 0x000E0000  # PGSERR | STRBERR | INCERR

        written = 0
        block = 32
        last_pct = -1
        while written < total:
            end = min(written + block, total)
            chunk = data[written:end]
            if len(chunk) < 32:
                chunk = chunk + b'\xFF' * (32 - len(chunk))
            waddr = addr + written

            # 写入该 word，遇到错误则清标志重试（最多4次）
            ok = False
            for attempt in range(4):
                self.stlink.write_mem32(waddr, chunk)
                sr = _wait_qw()
                # 清除非致命标志(WRPERR/ECC)
                if sr & 0x0FFF0000:
                    self.stlink.write_reg32(FB + CCR1, 0x0FFF0000)
                # 回读验证该 word
                rb = self.stlink.read_mem32(waddr, 32)
                if rb == chunk:
                    ok = True
                    break
                # 数据不符或致命错误，重新设PG后重试
                self.stlink.write_reg32(FB + CR1, (1 << 1) | (2 << 4))
            if not ok:
                raise STM32Error(f"H7: word @ 0x{waddr:08X} 写入失败 (重试4次) SR=0x{sr:08X}")

            written = end
            pct = min(written, total) * 100 // total
            if pct != last_pct:
                print(f"\r  写入: {pct}%", end="", flush=True)
                last_pct = pct
        print()


    def _write_flash_f4(self, addr, data):
        """F4/F7: 按电压决定的并行度编程。
        关键: ST-Link总线写宽度必须=Flash PSIZE宽度, 否则PGPERR。
        PSIZE=2(32bit)用write_mem32; PSIZE=0(8bit)用write_mem8。"""
        total = len(data)
        # 清除SR残留错误标志
        self.stlink.write_reg32(self.flash_base + self.FLASH_SR, 0xF2)
        # PG=1 | PSIZE(据电压)
        self.stlink.write_reg32(self.flash_base + self.FLASH_CR, (1<<0)|(self.psize<<8))
        use8 = (self.psize == 0)
        align = 1 << self.psize  # 0->1, 2->4
        written = 0
        block = 64  # 单次USB传输上限
        last_pct = -1
        while written < total:
            end = min(written + block, total)
            chunk = data[written:end]
            if align > 1 and len(chunk) % align:
                chunk = chunk + b'\xFF' * (align - len(chunk) % align)
            if use8:
                self.stlink.write_mem8(addr + written, chunk)
            else:
                self.stlink.write_mem32(addr + written, chunk)
            self.flash_wait(timeout=2)
            written = end
            pct = min(written, total) * 100 // total
            if pct != last_pct:
                print(f"\r  写入: {pct}%", end="", flush=True)
                last_pct = pct
        print()

    def _write_flash_f1(self, addr, data):
        """F0/F1: 半字(16-bit) 编程。
        必须用 WRITEMEM_16BIT(0x48)，8/32-bit 总线写无法触发 F1 Flash 编程。
        逐半字写，每个半字写后等待 BSY 清零。"""
        # 长度补齐到偶数（半字对齐），尾部补 0xFF
        if len(data) % 2:
            data = data + b'\xFF'
        total = len(data)
        # 设置 PG 位
        self.stlink.write_reg32(self.flash_base + self.FLASH_CR, 0x01)
        written = 0
        last_pct = -1
        try:
            while written < total:
                self.stlink.write_mem16(addr + written, data[written:written+2])
                self.flash_wait(timeout=1)
                # 检查编程错误（PGERR=bit2, WRPRTERR=bit4）
                sr = self.stlink.read_reg32(self.flash_base + self.FLASH_SR)
                if sr & ((1 << 2) | (1 << 4)):
                    self.stlink.write_reg32(self.flash_base + self.FLASH_SR, sr)
                    raise STM32Error(
                        f"F1 编程错误 @ 0x{addr+written:08X} SR=0x{sr:08X} "
                        f"({'PGERR ' if sr & (1<<2) else ''}{'WRPRTERR' if sr & (1<<4) else ''})")
                written += 2
                pct = min(written, total) * 100 // total
                if pct != last_pct:
                    print(f"\r  写入: {pct}%", end="", flush=True)
                    last_pct = pct
        finally:
            # 清除 PG 位
            self.stlink.write_reg32(self.flash_base + self.FLASH_CR, 0x00)
        print()

    def verify(self, addr, data):
        total = len(data)
        print("[*] 校验...")
        # 校验前确保CPU halt且Flash已锁定，状态稳定
        self.stlink.write_reg32(self.DHCSR, 0xA05F0003)
        time.sleep(0.05)
        verified = 0
        block = 256
        last_pct = -1
        while verified < total:
            sz = min(block, total - verified)
            read_sz = sz if sz % 4 == 0 else sz + (4 - sz % 4)
            # 读取（失败重试2次）
            mem = self.stlink.read_mem32(addr + verified, read_sz)
            if mem[:sz] != data[verified:verified+sz]:
                # 重试：可能是ST-Link读缓冲残留
                for _ in range(2):
                    time.sleep(0.01)
                    mem = self.stlink.read_mem32(addr + verified, read_sz)
                    if mem[:sz] == data[verified:verified+sz]:
                        break
            if mem[:sz] != data[verified:verified+sz]:
                # 逐字节定位
                for bi in range(sz):
                    if bi >= len(mem) or mem[bi] != data[verified+bi]:
                        print(f"\n  校验失败 @ 0x{addr+verified+bi:08X} (offset={verified+bi})")
                        print(f"  读到: {mem[bi:bi+16].hex()}")
                        print(f"  期望: {data[verified+bi:verified+bi+16].hex()}")
                        break
                raise STM32Error(f"校验失败 @ 0x{addr+verified:08X}")
            verified += sz
            pct = verified * 100 // total
            if pct != last_pct:
                print(f"\r  校验: {pct}%", end="", flush=True)
                last_pct = pct
        print()
        print("[✓] 校验通过")

    def read_flash(self, addr, size):
        result = bytearray()
        block = 256
        read = 0
        while read < size:
            sz = min(block, size - read)
            if sz % 4:
                sz += 4 - sz % 4
            result.extend(self.stlink.read_mem32(addr + read, sz))
            read += sz
        return bytes(result[:size])

    def reset_run(self):
        """复位并运行。注意：部分 bootloader 只在上电复位(POR)时才跳 APP，
        软件复位会留在 boot；若复位后仍停在 boot，请断电重启。"""
        self.stlink.write_reg32(self.AIRCR, 0x05FA0004)
        time.sleep(0.1)
        self.stlink.run()
        print("[✓] 目标已复位运行")

    def flash_firmware(self, filepath, address=None, verify=True, run_after=True, force_chip=None):
        ext = Path(filepath).suffix.lower()
        if ext == '.hex':
            hex_addr, data = IntelHexParser.parse(filepath)
            flash_addr = address if address else hex_addr
        elif ext in ('.bin', '.elf'):
            with open(filepath, 'rb') as f:
                data = f.read()
            flash_addr = address if address else 0x08000000
        else:
            raise STM32Error(f"不支持的格式: {ext}")

        print(f"[*] 加载固件: {filepath}")
        print(f"  大小: {len(data)} 字节 ({len(data)/1024:.1f} KB)")
        print(f"  地址: 0x{flash_addr:08X}")

        self.connect(force_chip=force_chip)
        self.erase_pages(flash_addr, len(data))
        self.write_flash(flash_addr, data)
        if verify:
            self.verify(flash_addr, data)
        if run_after:
            self.reset_run()
        print("\n[★] 烧录完成!")

    def close(self):
        self.stlink.close()


def main():
    parser = argparse.ArgumentParser(description='STM32 ST-Link SWD 烧录工具')
    parser.add_argument('-f', '--firmware', help='固件文件 (.hex/.bin)')
    parser.add_argument('-a', '--address', type=lambda x: int(x,0), help='起始地址')
    parser.add_argument('-i', '--info', action='store_true', help='芯片信息')
    parser.add_argument('-e', '--erase', action='store_true', help='全片擦除')
    parser.add_argument('-r', '--read', action='store_true', help='读Flash')
    parser.add_argument('-s', '--size', type=lambda x: int(x,0), default=256, help='读取字节数')
    parser.add_argument('-o', '--output', help='保存到文件')
    parser.add_argument('--no-verify', action='store_true', help='跳过校验')
    parser.add_argument('--no-run', action='store_true', help='不启动')
    parser.add_argument('--chip', help='手动指定芯片系列: h7/f7/f4/f1/f0 (ID读取失败时用)')
    parser.add_argument('-d', '--device', type=int, help='多个ST-Link时按编号选择(从1开始)')
    parser.add_argument('--serial', help='多个ST-Link时按序列号选择(支持部分匹配)')
    parser.add_argument('-l', '--list', action='store_true', help='列出所有ST-Link设备')
    args = parser.parse_args()

    # 列出设备
    if args.list:
        devs = STLink.list_devices()
        if not devs:
            print("未找到 ST-Link 设备")
        else:
            print(f"检测到 {len(devs)} 个 ST-Link 设备:")
            for i, (d, pid, name, sn) in enumerate(devs, 1):
                print(f"  {i}. ST-Link {name} (PID: 0x{pid:04X}) 序列号: {sn or '(无)'}")
        return

    if not any([args.firmware, args.info, args.erase, args.read]):
        parser.print_help()
        return

    prog = STM32Programmer(serial=args.serial, index=args.device)
    try:
        if args.firmware:
            if not os.path.isfile(args.firmware):
                raise STM32Error(f"文件不存在: {args.firmware}")
            prog.flash_firmware(args.firmware, args.address,
                              not args.no_verify, not args.no_run, args.chip)
        elif args.info:
            prog.connect(force_chip=args.chip)
        elif args.erase:
            prog.connect(force_chip=args.chip)
            prog.mass_erase()
        elif args.read:
            prog.connect(force_chip=args.chip)
            addr = args.address or 0x08000000
            data = prog.read_flash(addr, args.size)
            if args.output:
                with open(args.output, 'wb') as f:
                    f.write(data)
                print(f"[✓] 已保存到 {args.output}")
            else:
                for i in range(0, len(data), 16):
                    h = ' '.join(f'{b:02X}' for b in data[i:i+16])
                    print(f"  {addr+i:08X}: {h}")
    except STM32Error as e:
        print(f"\n[✗] 错误: {e}", file=sys.stderr)
        sys.exit(1)
    except usb.core.USBError as e:
        print(f"\n[✗] USB错误: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[!] 中断")
        sys.exit(130)
    finally:
        prog.close()


if __name__ == '__main__':
    main()
