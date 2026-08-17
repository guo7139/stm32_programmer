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
import getpass
import json
import sys
import os
import tempfile
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request


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

# 用户认证服务器
AUTH_SERVER = "http://192.168.60.241:9100"
AUTH_LOGIN_PATH = "/api/login"
AUTH_TIMEOUT = 10


class AuthenticationError(Exception):
    """用户认证错误"""
    pass


class AuthClient:
    """登录、保存令牌以及自动携带令牌的 HTTP API 客户端。"""

    def __init__(self, server=AUTH_SERVER, timeout=AUTH_TIMEOUT):
        self.server = server.rstrip('/')
        self.timeout = timeout
        self.token_file = self._get_token_file()
        self.api_token = self._load_token()

    @staticmethod
    def _get_token_file():
        if os.name == 'nt':
            base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
        else:
            base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        return base / 'stm32_programmer' / 'auth.json'

    def _load_token(self):
        try:
            data = json.loads(self.token_file.read_text(encoding='utf-8'))
            token = data.get('api_token')
            return token if isinstance(token, str) and token else None
        except (OSError, ValueError, TypeError):
            return None

    def _save_token(self, token):
        self.token_file.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix='auth-', suffix='.tmp', dir=str(self.token_file.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'api_token': token}, f)
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            os.replace(temp_name, self.token_file)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def logout(self):
        self.api_token = None
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass

    def _send(self, path, method='GET', payload=None, require_token=True):
        headers = {'Accept': 'application/json'}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if require_token:
            if not self.api_token:
                raise AuthenticationError('尚未登录，请先使用 --login 登录')
            # 同时提供标准Bearer头和api_token头，兼容服务端常见校验方式。
            headers['Authorization'] = f'Bearer {self.api_token}'
            headers['api_token'] = self.api_token
        req = urllib_request.Request(self.server + path, data=body, headers=headers, method=method)
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode('utf-8')
        except urllib_error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='replace')
            raise AuthenticationError(f'服务器返回 HTTP {e.code}: {detail or e.reason}') from e
        except urllib_error.URLError as e:
            raise AuthenticationError(f'无法连接登录服务器 {self.server}: {e.reason}') from e
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as e:
            raise AuthenticationError('服务器返回的不是有效 JSON') from e

    def login(self, username, password):
        result = self._send(AUTH_LOGIN_PATH, method='POST',
                            payload={'username': username, 'password': password},
                            require_token=False)
        token = result.get('api_token')
        if result.get('ok') is not True or not isinstance(token, str) or not token:
            raise AuthenticationError(result.get('message') or result.get('error') or '用户名或密码错误')
        self.api_token = token
        self._save_token(token)
        return result

    def request(self, path, method='GET', payload=None):
        """供其他业务接口调用：自动附带已保存的api_token。"""
        return self._send(path, method=method, payload=payload, require_token=True)


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

    # DTR/RTS 接线约定（可通过参数反转）：
    #   DTR -> RESET (低电平有效)
    #   RTS -> BOOT0 (高电平进Bootloader)
    # 注意：pyserial 中 setDTR(True) 实际输出低电平（RS232反相逻辑）
    # 所以 dtr=True -> 引脚低电平 -> 复位有效
    #      rts=True -> 引脚低电平 -> BOOT0=0（正常启动）
    # 实际极性取决于具体电路（是否有反相器），通过 invert_dtr/invert_rts 调整

    def __init__(self, port, baudrate=115200, timeout=5.0,
                 reset_pin='dtr', boot0_pin='rts',
                 invert_reset=False, invert_boot0=False):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self.serial = None
        self.supported_cmds = []
        self.bootloader_version = None
        self.chip_id = None
        # DTR/RTS 引脚映射
        self.reset_pin = reset_pin    # 'dtr' or 'rts'
        self.boot0_pin = boot0_pin    # 'rts' or 'dtr'
        self.invert_reset = invert_reset
        self.invert_boot0 = invert_boot0

    def _set_pin(self, pin, active):
        """设置DTR/RTS引脚状态
        
        Args:
            pin: 'dtr' or 'rts'
            active: True=有效（复位/BOOT0高）
        """
        if pin == 'dtr':
            # pyserial: dtr=True -> 引脚电压低（RS232逻辑）
            # 多数电路: DTR低 -> RESET有效，所以 active=True -> setDTR(True)
            self.serial.dtr = active
        else:
            self.serial.rts = active

    def reset_to_bootloader(self):
        """通过DTR/RTS信号线复位芯片并进入Bootloader模式
        
        时序:
          1. BOOT0 = 1（拉高，准备进入Bootloader）
          2. RESET = 0（复位有效）
          3. 延时 100ms
          4. RESET = 1（释放复位）
          5. 延时 50ms（等待Bootloader启动）
        """
        print("[*] 通过 DTR/RTS 自动复位芯片进入 Bootloader...")
        # 如果串口未打开，先打开
        if self.serial is None or not self.serial.is_open:
            self.serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_EVEN,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout
            )

        
        reset_active = not self.invert_reset   # 复位有效电平
        reset_inactive = self.invert_reset
        boot0_high = not self.invert_boot0     # BOOT0=1
        boot0_low = self.invert_boot0

        # Step 1: BOOT0 拉高
        self._set_pin(self.boot0_pin, boot0_high)
        time.sleep(0.01)
        
        # Step 2: 产生复位脉冲（拉低RESET）
        self._set_pin(self.reset_pin, reset_active)
        time.sleep(0.1)
        
        # Step 3: 释放复位
        self._set_pin(self.reset_pin, reset_inactive)
        time.sleep(0.05)
        
        # 复位后串口可能不稳定（USB串口适配器可能重新枚举）
        # 先尝试直接同步，失败则关闭串口等待重连后再次复位
        try:
            self.serial.reset_input_buffer()
            time.sleep(0.1)
            print("[*] 发送同步字节 0x7F...")
            self._sync()
            print("[✓] 同步成功，已连接到 Bootloader")
            return
        except (serial.SerialException, PermissionError, OSError, STM32Error):
            pass

        # 直接同步失败，关闭串口等待重连
        try:
            self.serial.close()
        except Exception:
            pass
        self.serial = None
        print("[*] 串口不稳定，等待重新就绪...")
        time.sleep(2.0)
        self._wait_for_port(timeout=30)

        # 重新打开串口
        self.serial = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.timeout
        )
        time.sleep(0.2)

        # 重新拉高BOOT0并复位（因为关闭串口后引脚状态丢失）
        print("[*] 重新复位芯片进入 Bootloader...")
        self._set_pin(self.boot0_pin, not self.invert_boot0)  # BOOT0=1
        time.sleep(0.01)
        self._set_pin(self.reset_pin, not self.invert_reset)  # RESET有效
        time.sleep(0.1)
        self._set_pin(self.reset_pin, self.invert_reset)      # 释放RESET
        time.sleep(0.05)

        self.serial.reset_input_buffer()
        time.sleep(0.1)
        print("[*] 发送同步字节 0x7F...")
        self._sync()
        print("[✓] 同步成功，已连接到 Bootloader")

    def reset_to_app(self):
        """复位芯片并正常启动（BOOT0=0）"""
        print("[*] 复位芯片，正常启动用户程序...")
        boot0_low = self.invert_boot0
        reset_active = not self.invert_reset
        reset_inactive = self.invert_reset

        self._set_pin(self.boot0_pin, boot0_low)
        time.sleep(0.01)
        self._set_pin(self.reset_pin, reset_active)
        time.sleep(0.1)
        self._set_pin(self.reset_pin, reset_inactive)
        print("[✓] 芯片已复位，正常运行")

    def connect(self, wait_for_port=False, wait_timeout=30):
        """打开串口并同步握手
        
        Args:
            wait_for_port: 如果为True，当串口不存在时等待其出现（用于断电重连场景）
            wait_timeout: 等待串口出现的超时时间（秒）
        """
        if wait_for_port:
            self._wait_for_port(wait_timeout)
        
        # 带重试的串口打开+同步（应对串口刚恢复但驱动未完全就绪的情况）
        max_retries = 5
        for retry in range(max_retries):
            try:
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
                time.sleep(0.2)  # 等待串口稳定


                # 发送同步字节
                print("[*] 发送同步字节 0x7F...")
                self._sync()
                print("[✓] 同步成功，已连接到 Bootloader")
                return
            except (serial.SerialException, PermissionError, OSError) as e:
                if self.serial and self.serial.is_open:
                    try:
                        self.serial.close()
                    except Exception:
                        pass
                    self.serial = None
                if retry < max_retries - 1:
                    print(f"[!] 串口操作失败: {e}")
                    print(f"[*] 等待 2 秒后重试 ({retry+2}/{max_retries})...")
                    time.sleep(2.0)
                else:
                    raise serial.SerialException(f"串口连接失败（已重试{max_retries}次）: {e}")

    def _wait_for_port(self, timeout=30):
        """等待串口设备出现并可用（断电重连场景）"""
        start = time.time()
        # Windows: 检查端口是否能被打开（设备文件存在不代表驱动就绪）
        # Linux: 检查设备文件是否存在
        is_windows = sys.platform.startswith('win') or (os.name == 'nt')
        
        def port_ready():
            """检测串口是否真正可用（不只是设备文件存在）"""
            if is_windows:
                try:
                    s = serial.Serial(self.port, baudrate=self.baudrate, timeout=0.1)
                    s.close()
                    return True
                except (serial.SerialException, PermissionError, OSError):
                    return False
            else:
                return os.path.exists(self.port)
        
        if port_ready():
            return
        print(f"[*] 等待串口 {self.port} 就绪（请给芯片上电）...")
        while time.time() - start < timeout:
            if port_ready():
                time.sleep(0.5)  # 额外等待驱动完全初始化
                print(f"[✓] 串口 {self.port} 已就绪")
                return
            time.sleep(0.3)
        raise STM32Error(f"等待串口 {self.port} 超时（{timeout}秒）")

    def reconnect(self, wait_timeout=30):
        """断电重连后重新建立连接
        
        用于芯片需要断电复位进入Bootloader的场景。
        关闭当前串口，等待设备重新出现，再重新握手。
        """
        if self.serial and self.serial.is_open:
            self.serial.close()
        print("[*] 请断电重连芯片（BOOT0保持拉高），等待重新连接...")
        self._wait_for_port(wait_timeout)
        self.connect()

    def _sync(self):
        """发送同步字节并等待 ACK"""
        # 先清空接收缓冲区（可能有上次残留数据）
        self.serial.reset_input_buffer()
        time.sleep(0.1)

        for attempt in range(10):
            self.serial.write(bytes([SYNC_BYTE]))
            resp = self._read_byte()
            if resp == ACK:
                return
            elif resp == NACK:
                # NACK也算有响应，再发一次通常就ACK了
                continue
            # 无响应，等待后重试（给芯片Bootloader启动时间）
            time.sleep(0.3)
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

    def flash_firmware(self, filepath, address=None, verify=True, go_after=True, wait_for_port=False, auto_reset=False):
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
        if auto_reset:
            self.reset_to_bootloader()
        else:
            self.connect(wait_for_port=wait_for_port)

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
  # 首次登录（密码会隐藏输入）
  python stm32_uart_programmer.py --login --username admin

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
  方式A - 手动模式:
    1. 将 BOOT0 引脚拉高（接 VCC 或跳线帽）
    2. 复位芯片（按复位键或断电重启）
    3. 连接串口（TX→RX, RX→TX, GND→GND）
    4. 运行本工具烧录
    5. 烧录完成后将 BOOT0 恢复低电平，复位即可正常运行

  方式B - 自动模式 (--auto-reset):
    接线: TX→RX, RX→TX, DTR→RESET, RTS→BOOT0, GND→GND
    程序自动控制 BOOT0/RESET 引脚，无需手动操作
    如极性不对可加 --invert-reset / --invert-boot0
""")

    parser.add_argument('-p', '--port',
                        help='串口设备 (如 /dev/ttyUSB0 或 COM3；烧录操作必填)')
    parser.add_argument('--login', action='store_true',
                        help='登录服务器并保存api_token')
    parser.add_argument('--username',
                        help='登录用户名（与--login一起使用）')
    parser.add_argument('--logout', action='store_true',
                        help='删除本机保存的api_token并退出登录')
    parser.add_argument('--wait-port', action='store_true',
                        help='启动时等待串口设备出现（用于断电重连场景）')
    parser.add_argument('--auto-reset', action='store_true',
                        help='通过DTR/RTS自动复位芯片进入Bootloader（需硬件连接DTR→RESET, RTS→BOOT0）')
    parser.add_argument('--reset-pin', default='dtr', choices=['dtr', 'rts'],
                        help='RESET连接的引脚 (默认 dtr)')
    parser.add_argument('--boot0-pin', default='rts', choices=['dtr', 'rts'],
                        help='BOOT0连接的引脚 (默认 rts)')
    parser.add_argument('--invert-reset', action='store_true',
                        help='反转RESET引脚极性')
    parser.add_argument('--invert-boot0', action='store_true',
                        help='反转BOOT0引脚极性')
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
    auth = AuthClient()

    if args.logout:
        auth.logout()
        print("[✓] 已退出登录，本机api_token已删除")
        return

    if args.login:
        username = args.username or input("用户名: ").strip()
        if not username:
            parser.error("用户名不能为空")
        password = getpass.getpass("密码: ")
        if not password:
            parser.error("密码不能为空")
        try:
            user = auth.login(username, password)
        except AuthenticationError as e:
            print(f"[✗] 登录失败: {e}", file=sys.stderr)
            sys.exit(1)
        print(f"[✓] 登录成功: {user.get('name') or user.get('username') or username}")
        return

    # 除登录、退出和帮助外，所有软件功能均要求已登录。
    if not auth.api_token:
        parser.error("请先登录：python stm32_uart_programmer.py --login --username <用户名>")

    if not args.port:
        parser.error("烧录操作需要指定串口 (-p/--port)")
    if not args.erase_only and not args.read and not args.firmware \
            and not args.unprotect_write and not args.unprotect_read:
        parser.error("请指定固件文件 (-f) 或操作模式 (--erase-only/--read/--unprotect-*)")

    programmer = STM32Programmer(
        args.port, args.baudrate,
        reset_pin=args.reset_pin,
        boot0_pin=args.boot0_pin,
        invert_reset=args.invert_reset,
        invert_boot0=args.invert_boot0
    )

    try:
        if args.firmware:
            # 烧录模式
            programmer.flash_firmware(
                filepath=args.firmware,
                wait_for_port=args.wait_port,
                auto_reset=args.auto_reset,
                address=args.address,
                verify=not args.no_verify,
                go_after=not args.no_go
            )
        elif args.erase_only:
            if args.auto_reset:
                programmer.reset_to_bootloader()
            else:
                programmer.connect(wait_for_port=args.wait_port)
            programmer.get_info()
            programmer.erase_flash()
            print("[✓] 全片擦除完成")
        elif args.read:
            if not args.address:
                args.address = DEFAULT_FLASH_START
            if not args.size:
                parser.error("读取模式需要指定大小 (-s)")
            if args.auto_reset:
                programmer.reset_to_bootloader()
            else:
                programmer.connect(wait_for_port=args.wait_port)
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
            if args.auto_reset:
                programmer.reset_to_bootloader()
            else:
                programmer.connect(wait_for_port=args.wait_port)
            programmer.write_unprotect()
        elif args.unprotect_read:
            if args.auto_reset:
                programmer.reset_to_bootloader()
            else:
                programmer.connect(wait_for_port=args.wait_port)
            programmer.readout_unprotect()

    except STM32Error as e:
        print(f"\n[✗] 错误: {e}", file=sys.stderr)
        sys.exit(1)
    except serial.SerialException as e:
        # 串口断开可能是断电重连，尝试等待恢复
        print(f"\n[!] 串口断开: {e}")
        print("[*] 如果是断电复位，请重新上电（保持BOOT0拉高）...")
        try:
            programmer.reconnect(wait_timeout=30)
            print("[✓] 重新连接成功，但需要重新运行烧录命令")
        except Exception:
            pass
        print(f"[✗] 串口错误: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[!] 用户中断")
        sys.exit(130)
    finally:
        programmer.close()


if __name__ == '__main__':
    main()
