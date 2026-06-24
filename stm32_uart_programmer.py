#!/usr/bin/env python3
"""
STM32 UART Bootloader Programmer
基于 ST AN3155 协议实现的串口烧录工具
替代 STM32CubeProgrammer，支持 bin/hex 固件烧录

作者: 亿航OS AI助理
协议参考: AN3155 - USART protocol used in the STM32 bootloader
"""

import serial
import struct
import time
import argparse
import sys
import os
from pathlib import Path


# ============ 协议常量 ============
ACK = 0x79
NACK = 0x1F
SYNC_BYTE = 0x7F

# Bootloader 命令
CMD_GET = 0x00
CMD_GET_VERSION = 0x01
CMD_GET_ID = 0x02
CMD_READ_MEMORY = 0x11
CMD_GO = 0x21
CMD_WRITE_MEMORY = 0x31
CMD_ERASE = 0x43
CMD_EXTENDED_ERASE = 0x44
CMD_WRITE_PROTECT = 0x63
CMD_WRITE_UNPROTECT = 0x73
CMD_READOUT_PROTECT = 0x82
CMD_READOUT_UNPROTECT = 0x92

# 默认 Flash 起始地址
DEFAULT_FLASH_START = 0x08000000

# 每次写入的最大字节数
WRITE_BLOCK_SIZE = 256


class STM32Error(Exception):
    """STM32 烧录错误"""
    pass


class IntelHexParser:
    """Intel HEX 文件解析器"""

    def __init__(self):
        self.segments = {}  # {address: data_bytes}

    def parse(self, filepath):
        """解析 .hex 文件，返回 (start_address, binary_data)"""
        base_address = 0
        min_addr = None
        max_addr = 0
        data_dict = {}

        with open(filepath, 'r') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line or line[0] != ':':
                    continue

                # 解析记录
                raw = bytes.fromhex(line[1:])
                byte_count = raw[0]
                address = (raw[1] << 8) | raw[2]
                record_type = raw[3]
                data = raw[4:4 + byte_count]
                checksum = raw[-1]

                # 校验
                calc_sum = sum(raw[:-1]) & 0xFF
                if (calc_sum + checksum) & 0xFF != 0:
                    raise STM32Error(f"HEX文件校验失败 (行 {line_num})")

                if record_type == 0x00:  # Data Record
                    full_addr = base_address + address
                    for i, byte in enumerate(data):
                        data_dict[full_addr + i] = byte
                    if min_addr is None or full_addr < min_addr:
                        min_addr = full_addr
                    end_addr = full_addr + len(data)
                    if end_addr > max_addr:
                        max_addr = end_addr

                elif record_type == 0x01:  # End of File
                    break

                elif record_type == 0x02:  # Extended Segment Address
                    base_address = ((data[0] << 8) | data[1]) << 4

                elif record_type == 0x03:  # Start Segment Address
                    pass  # 忽略

                elif record_type == 0x04:  # Extended Linear Address
                    base_address = ((data[0] << 8) | data[1]) << 16

                elif record_type == 0x05:  # Start Linear Address
                    pass  # 忽略

        if min_addr is None:
            raise STM32Error("HEX文件中没有数据")

        # 组装为连续二进制
        size = max_addr - min_addr
        binary = bytearray(b'\xFF' * size)
        for addr, byte in data_dict.items():
            binary[addr - min_addr] = byte

        return min_addr, bytes(binary)


class STM32Programmer:
    """STM32 UART Bootloader 烧录器"""

    def __init__(self, port, baudrate=115200, timeout=5.0):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial = None
        self.supported_cmds = []
        self.bootloader_version = None
        self.chip_id = None

    def connect(self):
        """打开串口并同步握手"""
        print(f"[*] 打开串口 {self.port} @ {self.baudrate} baud...")
        self.serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,  # AN3155 要求偶校验
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout
        )
        self.serial.reset_input_buffer()
        self.serial.reset_output_buffer()

        # 发送同步字节
        print("[*] 发送同步字节 0x7F...")
        self._sync()
        print("[✓] 同步成功，已连接到 Bootloader")

    def _sync(self):
        """发送同步字节并等待 ACK"""
        for attempt in range(3):
            self.serial.write(bytes([SYNC_BYTE]))
            resp = self._read_byte()
            if resp == ACK:
                return
            elif resp == NACK:
                # 可能已经同步过了，再试
                continue
            time.sleep(0.1)
        raise STM32Error("同步失败：未收到 ACK。请确认：\n"
                         "  1. BOOT0 引脚已拉高\n"
                         "  2. 芯片已复位\n"
                         "  3. 串口连接正确（TX/RX 交叉）\n"
                         "  4. 波特率匹配")

    def _read_byte(self):
        """读取单个字节"""
        data = self.serial.read(1)
        if len(data) == 0:
            return None
        return data[0]

    def _wait_ack(self):
        """等待 ACK 响应"""
        resp = self._read_byte()
        if resp == ACK:
            return True
        elif resp == NACK:
            return False
        elif resp is None:
            raise STM32Error("等待响应超时")
        else:
            raise STM32Error(f"未知响应: 0x{resp:02X}")

    def _send_cmd(self, cmd):
        """发送命令（命令 + 补码）"""
        self.serial.write(bytes([cmd, cmd ^ 0xFF]))
        if not self._wait_ack():
            raise STM32Error(f"命令 0x{cmd:02X} 被拒绝 (NACK)")

    def _send_address(self, address):
        """发送4字节地址 + 校验和"""
        addr_bytes = struct.pack('>I', address)
        checksum = 0
        for b in addr_bytes:
            checksum ^= b
        self.serial.write(addr_bytes + bytes([checksum]))
        if not self._wait_ack():
            raise STM32Error(f"地址 0x{address:08X} 被拒绝")

    def _send_data_with_checksum(self, data):
        """发送数据块：[N-1] + data + checksum"""
        n = len(data)
        checksum = (n - 1)
        for b in data:
            checksum ^= b
        self.serial.write(bytes([n - 1]) + data + bytes([checksum & 0xFF]))
        if not self._wait_ack():
            raise STM32Error("数据写入被拒绝")

    def get_info(self):
        """获取 Bootloader 信息"""
        # GET 命令
        print("[*] 获取 Bootloader 信息...")
        self._send_cmd(CMD_GET)
        n = self._read_byte()  # 后续字节数
        self.bootloader_version = self._read_byte()
        self.supported_cmds = []
        for _ in range(n):
            self.supported_cmds.append(self._read_byte())
        self._wait_ack()  # 结束 ACK

        print(f"    Bootloader 版本: {self.bootloader_version >> 4}.{self.bootloader_version & 0xF}")
        print(f"    支持的命令: {[f'0x{c:02X}' for c in self.supported_cmds]}")

        # GET ID 命令
        self._send_cmd(CMD_GET_ID)
        n = self._read_byte()  # PID 字节数 (通常为1，表示2字节)
        pid_bytes = self.serial.read(n + 1)
        self._wait_ack()
        self.chip_id = int.from_bytes(pid_bytes, 'big')
        print(f"    芯片 PID: 0x{self.chip_id:04X}")

        return {
            'version': self.bootloader_version,
            'commands': self.supported_cmds,
            'chip_id': self.chip_id
        }

    def erase_flash(self, pages=None):
        """
        擦除 Flash
        pages=None: 全片擦除
        pages=[0,1,2...]: 擦除指定页
        """
        use_extended = CMD_EXTENDED_ERASE in self.supported_cmds

        if use_extended:
            print("[*] 执行扩展擦除...")
            self._send_cmd(CMD_EXTENDED_ERASE)

            if pages is None:
                # 全片擦除: 发送 0xFFFF + 校验
                self.serial.write(bytes([0xFF, 0xFF, 0x00]))
                print("    全片擦除中（可能需要数秒）...")
                # 全片擦除耗时较长
                old_timeout = self.serial.timeout
                self.serial.timeout = 30
                if not self._wait_ack():
                    self.serial.timeout = old_timeout
                    raise STM32Error("全片擦除失败")
                self.serial.timeout = old_timeout
            else:
                # 按页擦除
                n_pages = len(pages) - 1
                data = struct.pack('>H', n_pages)
                for page in pages:
                    data += struct.pack('>H', page)
                checksum = 0
                for b in data:
                    checksum ^= b
                self.serial.write(data + bytes([checksum]))
                old_timeout = self.serial.timeout
                self.serial.timeout = 15
                if not self._wait_ack():
                    self.serial.timeout = old_timeout
                    raise STM32Error("页擦除失败")
                self.serial.timeout = old_timeout
        else:
            print("[*] 执行标准擦除...")
            self._send_cmd(CMD_ERASE)

            if pages is None:
                # 全片擦除
                self.serial.write(bytes([0xFF, 0x00]))
                old_timeout = self.serial.timeout
                self.serial.timeout = 30
                if not self._wait_ack():
                    self.serial.timeout = old_timeout
                    raise STM32Error("全片擦除失败")
                self.serial.timeout = old_timeout
            else:
                # 按页擦除
                n = len(pages) - 1
                data = bytes([n]) + bytes(pages)
                checksum = 0
                for b in data:
                    checksum ^= b
                self.serial.write(data + bytes([checksum]))
                if not self._wait_ack():
                    raise STM32Error("页擦除失败")

        print("[✓] 擦除完成")

    def write_memory(self, address, data):
        """写入数据到指定地址（自动分块）"""
        total = len(data)
        written = 0

        print(f"[*] 写入 {total} 字节到 0x{address:08X}...")

        while written < total:
            chunk_size = min(WRITE_BLOCK_SIZE, total - written)
            chunk = data[written:written + chunk_size]

            # 补齐到4字节对齐（填充0xFF）
            if len(chunk) % 4 != 0:
                chunk = chunk + b'\xFF' * (4 - len(chunk) % 4)
                chunk_size = len(chunk)

            self._send_cmd(CMD_WRITE_MEMORY)
            self._send_address(address + written)
            self._send_data_with_checksum(chunk)

            written += chunk_size

            # 进度显示
            progress = written * 100 // total
            bar_len = 40
            filled = bar_len * written // total
            bar = '█' * filled + '░' * (bar_len - filled)
            print(f"\r    [{bar}] {progress}% ({written}/{total})", end='', flush=True)

        print()  # 换行
        print("[✓] 写入完成")

    def read_memory(self, address, size):
        """从指定地址读取数据"""
        data = bytearray()
        read = 0

        print(f"[*] 从 0x{address:08X} 读取 {size} 字节...")

        while read < size:
            chunk_size = min(256, size - read)
            self._send_cmd(CMD_READ_MEMORY)
            self._send_address(address + read)

            # 发送读取长度 (N-1) + 校验
            n = chunk_size - 1
            self.serial.write(bytes([n, n ^ 0xFF]))
            if not self._wait_ack():
                raise STM32Error(f"读取地址 0x{address + read:08X} 失败")

            chunk = self.serial.read(chunk_size)
            if len(chunk) != chunk_size:
                raise STM32Error("读取数据不完整")
            data.extend(chunk)
            read += chunk_size

        print(f"[✓] 读取完成 ({len(data)} 字节)")
        return bytes(data)

    def verify(self, address, data):
        """校验 Flash 内容"""
        print("[*] 校验固件...")
        flash_data = self.read_memory(address, len(data))

        if flash_data == data:
            print("[✓] 校验通过！固件一致")
            return True
        else:
            # 找出第一个不同的位置
            for i in range(len(data)):
                if i >= len(flash_data) or flash_data[i] != data[i]:
                    print(f"[✗] 校验失败！偏移 0x{i:X}: "
                          f"期望 0x{data[i]:02X}, 实际 0x{flash_data[i]:02X}")
                    return False
            return False

    def go(self, address):
        """跳转执行（从指定地址启动用户程序）"""
        print(f"[*] 跳转到 0x{address:08X} 执行...")
        self._send_cmd(CMD_GO)
        self._send_address(address)
        print("[✓] 已跳转，用户程序开始执行")

    def write_unprotect(self):
        """解除写保护"""
        print("[*] 解除写保护...")
        self._send_cmd(CMD_WRITE_UNPROTECT)
        if not self._wait_ack():
            raise STM32Error("解除写保护失败")
        print("[✓] 写保护已解除（芯片将自动复位）")
        time.sleep(0.5)
        # 复位后需要重新同步
        self._sync()

    def readout_unprotect(self):
        """解除读保护"""
        print("[*] 解除读保护...")
        self._send_cmd(CMD_READOUT_UNPROTECT)
        old_timeout = self.serial.timeout
        self.serial.timeout = 30
        if not self._wait_ack():
            self.serial.timeout = old_timeout
            raise STM32Error("解除读保护失败")
        self.serial.timeout = old_timeout
        print("[✓] 读保护已解除（芯片将自动复位，Flash 已被擦除）")
        time.sleep(0.5)
        self._sync()

    def close(self):
        """关闭串口"""
        if self.serial and self.serial.is_open:
            self.serial.close()
            print("[*] 串口已关闭")

    def flash_firmware(self, filepath, address=None, verify=True, go_after=True):
        """
        一键烧录固件（主流程）
        filepath: .bin 或 .hex 文件路径
        address: 烧录起始地址（bin文件必须指定，hex文件自动解析）
        verify: 烧录后是否校验
        go_after: 烧录后是否跳转执行
        """
        # 1. 加载固件
        ext = Path(filepath).suffix.lower()
        if ext == '.hex':
            print(f"[*] 解析 Intel HEX 文件: {filepath}")
            parser = IntelHexParser()
            flash_addr, firmware_data = parser.parse(filepath)
            if address is not None:
                flash_addr = address
            print(f"    起始地址: 0x{flash_addr:08X}")
            print(f"    固件大小: {len(firmware_data)} 字节 ({len(firmware_data)/1024:.1f} KB)")
        elif ext == '.bin':
            if address is None:
                flash_addr = DEFAULT_FLASH_START
            else:
                flash_addr = address
            print(f"[*] 加载 BIN 文件: {filepath}")
            with open(filepath, 'rb') as f:
                firmware_data = f.read()
            print(f"    起始地址: 0x{flash_addr:08X}")
            print(f"    固件大小: {len(firmware_data)} 字节 ({len(firmware_data)/1024:.1f} KB)")
        else:
            raise STM32Error(f"不支持的文件格式: {ext}（支持 .bin 和 .hex）")

        # 2. 连接
        self.connect()

        # 3. 获取芯片信息
        self.get_info()

        # 4. 擦除
        self.erase_flash()

        # 5. 写入
        self.write_memory(flash_addr, firmware_data)

        # 6. 校验
        if verify:
            if CMD_READ_MEMORY in self.supported_cmds:
                if not self.verify(flash_addr, firmware_data):
                    raise STM32Error("固件校验失败！")
            else:
                print("[!] 芯片不支持 Read Memory 命令，跳过校验")

        # 7. 跳转执行
        if go_after:
            self.go(flash_addr)

        print("\n" + "=" * 50)
        print("  🎉 烧录完成！")
        print("=" * 50)


def main():
    parser = argparse.ArgumentParser(
        description='STM32 UART Bootloader 烧录工具 (AN3155协议)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  # 烧录 bin 文件（默认地址 0x08000000）
  python stm32_uart_programmer.py -p /dev/ttyUSB0 -f firmware.bin

  # 烧录 hex 文件（地址从文件解析）
  python stm32_uart_programmer.py -p COM3 -f firmware.hex

  # 指定起始地址和波特率
  python stm32_uart_programmer.py -p /dev/ttyUSB0 -f app.bin -a 0x08004000 -b 57600

  # 只擦除不烧录
  python stm32_uart_programmer.py -p /dev/ttyUSB0 --erase-only

  # 烧录后不跳转执行
  python stm32_uart_programmer.py -p /dev/ttyUSB0 -f firmware.bin --no-go

  # 读取 Flash 内容
  python stm32_uart_programmer.py -p /dev/ttyUSB0 --read -a 0x08000000 -s 1024 -o dump.bin

烧录前准备:
  1. 将 BOOT0 引脚拉高（接 VCC 或跳线帽）
  2. 复位芯片（按复位键或断电重启）
  3. 连接串口（TX→RX, RX→TX, GND→GND）
  4. 运行本工具烧录
  5. 烧录完成后将 BOOT0 恢复低电平，复位即可正常运行
""")

    parser.add_argument('-p', '--port', required=True,
                        help='串口设备 (如 /dev/ttyUSB0 或 COM3)')
    parser.add_argument('-b', '--baudrate', type=int, default=115200,
                        help='波特率 (默认 115200)')
    parser.add_argument('-f', '--firmware',
                        help='固件文件路径 (.bin 或 .hex)')
    parser.add_argument('-a', '--address', type=lambda x: int(x, 0),
                        help='Flash 起始地址 (默认 0x08000000)')
    parser.add_argument('--no-verify', action='store_true',
                        help='跳过烧录后校验')
    parser.add_argument('--no-go', action='store_true',
                        help='烧录后不跳转执行')
    parser.add_argument('--erase-only', action='store_true',
                        help='只执行全片擦除')
    parser.add_argument('--read', action='store_true',
                        help='读取 Flash 内容')
    parser.add_argument('-s', '--size', type=lambda x: int(x, 0),
                        help='读取大小（字节）')
    parser.add_argument('-o', '--output',
                        help='读取内容保存到文件')
    parser.add_argument('--unprotect-write', action='store_true',
                        help='解除写保护')
    parser.add_argument('--unprotect-read', action='store_true',
                        help='解除读保护（会擦除全片！）')

    args = parser.parse_args()

    # 参数校验
    if not args.erase_only and not args.read and not args.firmware \
            and not args.unprotect_write and not args.unprotect_read:
        parser.error("请指定固件文件 (-f) 或操作模式 (--erase-only/--read/--unprotect-*)")

    programmer = STM32Programmer(args.port, args.baudrate)

    try:
        if args.firmware:
            # 烧录模式
            programmer.flash_firmware(
                filepath=args.firmware,
                address=args.address,
                verify=not args.no_verify,
                go_after=not args.no_go
            )
        elif args.erase_only:
            programmer.connect()
            programmer.get_info()
            programmer.erase_flash()
            print("[✓] 全片擦除完成")
        elif args.read:
            if not args.address:
                args.address = DEFAULT_FLASH_START
            if not args.size:
                parser.error("读取模式需要指定大小 (-s)")
            programmer.connect()
            programmer.get_info()
            data = programmer.read_memory(args.address, args.size)
            if args.output:
                with open(args.output, 'wb') as f:
                    f.write(data)
                print(f"[✓] 已保存到 {args.output}")
            else:
                # 十六进制打印
                for i in range(0, len(data), 16):
                    hex_str = ' '.join(f'{b:02X}' for b in data[i:i+16])
                    ascii_str = ''.join(chr(b) if 32 <= b < 127 else '.' for b in data[i:i+16])
                    print(f"  {args.address + i:08X}: {hex_str:<48s} {ascii_str}")
        elif args.unprotect_write:
            programmer.connect()
            programmer.write_unprotect()
        elif args.unprotect_read:
            programmer.connect()
            programmer.readout_unprotect()

    except STM32Error as e:
        print(f"\n[✗] 错误: {e}", file=sys.stderr)
        sys.exit(1)
    except serial.SerialException as e:
        print(f"\n[✗] 串口错误: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[!] 用户中断")
        sys.exit(130)
    finally:
        programmer.close()


if __name__ == '__main__':
    main()
