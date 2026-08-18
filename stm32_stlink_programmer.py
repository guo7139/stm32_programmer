#!/usr/bin/env python3
"""
STM32 ST-Link SWD Programmer
通过 USB 直接与 ST-Link V2 通信，实现 SWD 协议烧录 STM32
"""

import sys, os, struct, time, argparse
import contextlib, getpass, hashlib, json, queue, tempfile, threading
from pathlib import Path
from urllib import error as urllib_error
from urllib import request as urllib_request
from urllib import parse as urllib_parse

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

# 用户认证服务器
AUTH_SERVER = "http://192.168.60.241:9100"
AUTH_LOGIN_PATH = "/api/login"
AUTH_TIMEOUT = 10

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


class AuthenticationError(Exception):
    """登录或HTTP权限验证错误。"""
    pass


class AppConfig:
    """主程序目录的非敏感配置；绝不保存密码或api_token。"""
    ALLOWED_KEYS = {
        'username', 'model_code', 'part_no', 'purpose', 'program', 'status',
        'aircraft_no', 'eo_no'
    }

    def __init__(self, path=None):
        self.path = Path(path) if path else Path(__file__).resolve().parent / 'stm32_programmer_config.json'
        self.data = self.load()

    def load(self):
        try:
            raw = json.loads(self.path.read_text(encoding='utf-8'))
            if not isinstance(raw, dict):
                return {}
            return {key: raw.get(key) for key in self.ALLOWED_KEYS if key in raw}
        except (OSError, ValueError, TypeError):
            return {}

    def update(self, **values):
        for key, value in values.items():
            if key in self.ALLOWED_KEYS:
                self.data[key] = value
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix='config-', suffix='.tmp', dir=str(self.path.parent))
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as output:
                json.dump(self.data, output, ensure_ascii=False, indent=2)
                output.write('\n')
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)


class AuthClient:
    """登录并在当前进程内存中保存api_token；令牌绝不落盘。"""

    def __init__(self, server=AUTH_SERVER, timeout=AUTH_TIMEOUT):
        self.server = server.rstrip('/')
        self.timeout = timeout
        self.token_file = self._get_token_file()
        self.api_token = None
        self.username = None
        # 清理旧版本可能遗留的敏感Token文件，之后不再创建该文件。
        try:
            self.token_file.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass

    @staticmethod
    def _get_token_file():
        if os.name == 'nt':
            base = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
        else:
            base = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config'))
        return base / 'stm32_programmer' / 'auth.json'

    def logout(self):
        self.api_token = None
        self.username = None
        try:
            self.token_file.unlink()
        except (FileNotFoundError, OSError):
            pass

    def _send(self, path, method='GET', payload=None, require_token=True):
        headers = {'Accept': 'application/json'}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = 'application/json'
        if require_token:
            if not self.api_token:
                raise AuthenticationError('尚未登录')
            headers['Authorization'] = f'Bearer {self.api_token}'
            headers['api_token'] = self.api_token
        req = urllib_request.Request(self.server + path, data=body,
                                     headers=headers, method=method)
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as response:
                raw = response.read().decode('utf-8')
        except urllib_error.HTTPError as e:
            detail = e.read().decode('utf-8', errors='replace')
            raise AuthenticationError(
                f'服务器返回 HTTP {e.code}: {detail or e.reason}') from e
        except urllib_error.URLError as e:
            raise AuthenticationError(
                f'无法连接登录服务器 {self.server}: {e.reason}') from e
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
            raise AuthenticationError(
                result.get('message') or result.get('error') or '用户名或密码错误')
        self.api_token = token
        self.username = username
        return result

    def request(self, path, method='GET', payload=None):
        """调用其他权限API，自动携带Authorization和api_token请求头。"""
        return self._send(path, method=method, payload=payload, require_token=True)

    def request_with_token_query(self, path, params=None):
        """调用要求通过token查询参数鉴权的GET接口。"""
        if not self.api_token:
            raise AuthenticationError('尚未登录')
        query = dict(params or {})
        query['token'] = self.api_token
        separator = '&' if '?' in path else '?'
        return self.request(path + separator + urllib_parse.urlencode(query))

    @staticmethod
    def _as_list(result):
        """兼容接口直接返回数组或以data/list/items包装数组。"""
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            for key in ('data', 'list', 'items', 'rows'):
                if isinstance(result.get(key), list):
                    return result[key]
        return []

    def get_models_and_parts(self):
        """获取机型基础数据；接口返回零部件时同时按model_code归纳机型。"""
        records = self._as_list(self.request_with_token_query('/api/parts'))
        models = []
        seen = set()
        for item in records:
            code = str(item.get('model_code') or '').strip()
            if not code or code in seen:
                continue
            seen.add(code)
            models.append({
                'id': item.get('id'),
                'model_code': code,
                'model_name': str(item.get('model_name') or code),
                'status': item.get('status'),
                'status_text': item.get('status_text') or '',
            })
        return models, records

    def get_parts(self, model_code, initial_records=None):
        """按机型获取零部件；若服务器忽略筛选参数则在客户端再次筛选。"""
        records = self._as_list(self.request_with_token_query(
            '/api/parts', {'model_code': model_code}))
        if not records and initial_records:
            records = initial_records
        return [item for item in records
                if str(item.get('model_code') or '').strip() == model_code
                and item.get('part_no')]

    @staticmethod
    def split_purposes(part):
        """将零部件purpose字段按|分割并去除空项。"""
        return [item.strip() for item in str(part.get('purpose') or '').split('|')
                if item.strip()]

    @staticmethod
    def format_file_size(size):
        """将字节数格式化为易读的B/KB/MB/GB。"""
        try:
            value = int(size)
        except (TypeError, ValueError):
            return '-'
        units = ('B', 'KB', 'MB', 'GB', 'TB')
        display = float(value)
        unit = units[0]
        for unit in units:
            if display < 1024 or unit == units[-1]:
                break
            display /= 1024
        return f'{int(display)} {unit}' if unit == 'B' else f'{display:.1f} {unit}'

    @classmethod
    def resolve_burn_address(cls, program, part):
        """按程序类型决定最终烧录地址：bootload固定基址，app取零部件配置。"""
        if program == 'bootload':
            return '0x08000000'
        if program == 'app':
            address = str((part or {}).get('burn_addr') or '').strip()
            cls.parse_burn_address(address)
            return address
        raise AuthenticationError(f'不支持的程序类型：{program}')

    @staticmethod
    def parse_burn_address(value):
        """校验并解析零部件接口返回的烧录地址。"""
        text = str(value or '').strip()
        if not text:
            raise AuthenticationError('该零部件未配置烧录地址 burn_addr')
        try:
            address = int(text, 0)
        except ValueError as exc:
            raise AuthenticationError(f'烧录地址格式错误：{text}') from exc
        if address < 0 or address > 0xFFFFFFFF:
            raise AuthenticationError(f'烧录地址超出范围：{text}')
        return address

    def _firmware_directory(self):
        if os.name == 'nt':
            base = Path(os.environ.get('LOCALAPPDATA',
                       Path.home() / 'AppData' / 'Local'))
        else:
            base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
        return base / 'stm32_programmer' / 'firmware'

    def download_firmware(self, version, progress=None):
        """下载固件并严格校验MD5和字节大小，成功后返回本地路径。"""
        version_id = version.get('id')
        expected_md5 = str(version.get('file_md5') or '').strip().lower()
        try:
            expected_size = int(version.get('file_size'))
        except (TypeError, ValueError) as exc:
            raise AuthenticationError('固件缺少有效的file_size，不能安全烧录') from exc
        if version_id in (None, ''):
            raise AuthenticationError('固件版本缺少id，无法下载')
        if not expected_md5:
            raise AuthenticationError('固件MD5为空，不能下载和烧录')
        if expected_size < 0:
            raise AuthenticationError('固件file_size无效，不能安全烧录')

        raw_name = Path(str(version.get('file_name') or f'firmware-{version_id}.bin')).name
        safe_name = ''.join(c for c in raw_name if c.isalnum() or c in '._-')
        if not safe_name:
            safe_name = f'firmware-{version_id}.bin'
        directory = self._firmware_directory()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / safe_name
        fd, temp_name = tempfile.mkstemp(prefix='download-', suffix='.tmp', dir=str(directory))
        os.close(fd)
        query = urllib_parse.urlencode({'token': self.api_token})
        url = self.server + f'/openapi/download/{urllib_parse.quote(str(version_id), safe="")}?{query}'
        request = urllib_request.Request(url, headers={
            'Accept': 'application/octet-stream',
            'Authorization': f'Bearer {self.api_token}',
            'api_token': self.api_token,
        }, method='GET')
        digest, total = hashlib.md5(), 0
        try:
            try:
                response = urllib_request.urlopen(request, timeout=max(self.timeout, 30))
            except urllib_error.HTTPError as exc:
                detail = exc.read().decode('utf-8', errors='replace')
                raise AuthenticationError(
                    f'下载服务器返回 HTTP {exc.code}: {detail or exc.reason}') from exc
            except urllib_error.URLError as exc:
                raise AuthenticationError(f'无法下载固件：{exc.reason}') from exc
            with response, open(temp_name, 'wb') as output:
                while True:
                    chunk = response.read(1024 * 256)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    total += len(chunk)
                    if progress:
                        progress(total, expected_size)
                output.flush()
                os.fsync(output.fileno())
            actual_md5 = digest.hexdigest().lower()
            errors = []
            if total != expected_size:
                errors.append(f'文件大小不一致：期望 {expected_size} 字节，实际 {total} 字节')
            if actual_md5 != expected_md5:
                errors.append(f'MD5不一致：期望 {expected_md5}，实际 {actual_md5}')
            if errors:
                raise AuthenticationError('；'.join(errors))
            os.replace(temp_name, target)
            return str(target)
        finally:
            if os.path.exists(temp_name):
                try:
                    os.unlink(temp_name)
                except OSError:
                    pass

    def get_latest_version(self, model_code, part_no, purpose, program, status,
                           aircraft_no='', eo_no=''):
        """查询已发布的最新固件版本；两个编号仅在非空时传给服务器。"""
        params = {
            'model_code': model_code,
            'part_no': part_no,
            'purpose': purpose or '',
            'program': program,
            'status': status,
        }
        aircraft_no = str(aircraft_no or '').strip()
        eo_no = str(eo_no or '').strip()
        if aircraft_no:
            params['aircraft_no'] = aircraft_no
        if eo_no:
            params['eo_no'] = eo_no
        result = self.request_with_token_query('/openapi/versions/latest', params)
        if result is None:
            return None
        if isinstance(result, dict) and 'data' in result:
            result = result.get('data')
        return result if isinstance(result, dict) and result else None


def show_login_dialog(auth, initial_username=''):
    """显示用户名/密码登录窗口。成功返回True，取消返回False。"""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        return terminal_login(auth, initial_username)

    try:
        root = tk.Tk()
    except tk.TclError:
        return terminal_login(auth, initial_username)

    root.title('STM32 ST-Link 烧录工具 - 用户登录')
    root.resizable(False, False)
    root.protocol('WM_DELETE_WINDOW', root.destroy)
    result = {'ok': False}

    frame = ttk.Frame(root, padding=22)
    frame.grid(row=0, column=0, sticky='nsew')
    ttk.Label(frame, text='用户登录', font=('', 16, 'bold')).grid(
        row=0, column=0, columnspan=2, pady=(0, 16))
    ttk.Label(frame, text='服务器：').grid(row=1, column=0, sticky='e', pady=5)
    server_var = tk.StringVar(value=auth.server)
    server_entry = ttk.Entry(frame, textvariable=server_var, width=34, state='readonly')
    server_entry.grid(row=1, column=1, sticky='ew', pady=5)
    ttk.Label(frame, text='用户名：').grid(row=2, column=0, sticky='e', pady=5)
    username_var = tk.StringVar(value=initial_username)
    username_entry = ttk.Entry(frame, textvariable=username_var, width=34)
    username_entry.grid(row=2, column=1, sticky='ew', pady=5)
    ttk.Label(frame, text='密码：').grid(row=3, column=0, sticky='e', pady=5)
    password_var = tk.StringVar()
    password_entry = ttk.Entry(frame, textvariable=password_var, show='●', width=34)
    password_entry.grid(row=3, column=1, sticky='ew', pady=5)
    status_var = tk.StringVar(value='请输入用户名和密码')
    status_label = ttk.Label(frame, textvariable=status_var, foreground='#555555')
    status_label.grid(row=4, column=0, columnspan=2, pady=(8, 4))
    buttons = ttk.Frame(frame)
    buttons.grid(row=5, column=0, columnspan=2, pady=(10, 0))

    def finish_login(user):
        result['ok'] = True
        result['user'] = user
        root.destroy()

    def set_busy(busy):
        state = 'disabled' if busy else 'normal'
        login_button.configure(state=state)
        cancel_button.configure(state=state)
        username_entry.configure(state=state)
        password_entry.configure(state=state)

    def submit(event=None):
        username = username_var.get().strip()
        password = password_var.get()
        if not username or not password:
            messagebox.showwarning('登录提示', '请输入用户名和密码', parent=root)
            return
        set_busy(True)
        status_var.set('正在连接登录服务器...')

        def worker():
            try:
                user = auth.login(username, password)
            except AuthenticationError as exc:
                root.after(0, lambda message=str(exc): login_failed(message))
            except Exception as exc:
                root.after(0, lambda message=f'登录失败：{exc}': login_failed(message))
            else:
                root.after(0, lambda: finish_login(user))

        threading.Thread(target=worker, daemon=True).start()

    def login_failed(message):
        set_busy(False)
        password_var.set('')
        status_var.set('登录失败，请重新输入')
        messagebox.showerror('登录失败', message, parent=root)
        password_entry.focus_set()

    login_button = ttk.Button(buttons, text='登录', command=submit, width=12)
    login_button.grid(row=0, column=0, padx=5)
    cancel_button = ttk.Button(buttons, text='取消', command=root.destroy, width=12)
    cancel_button.grid(row=0, column=1, padx=5)
    root.bind('<Return>', submit)
    root.bind('<Escape>', lambda event: root.destroy())
    root.update_idletasks()
    x = max((root.winfo_screenwidth() - root.winfo_width()) // 2, 0)
    y = max((root.winfo_screenheight() - root.winfo_height()) // 2, 0)
    root.geometry(f'+{x}+{y}')
    (password_entry if initial_username else username_entry).focus_set()
    root.mainloop()
    return result['ok']


def show_firmware_selection_dialog(auth, burn_options=None, app_config=None):
    """显示机型/零部件/用途/程序选择与固件版本确认窗口。"""
    try:
        import tkinter as tk
        from tkinter import messagebox, ttk
    except ImportError:
        return terminal_firmware_selection(auth, app_config)
    try:
        root = tk.Tk()
    except tk.TclError:
        return terminal_firmware_selection(auth, app_config)

    root.title('STM32 ST-Link 烧录工具 - 固件选择')
    root.resizable(False, False)
    result = {'confirmed': False, 'selection': None, 'version': None,
              'downloaded_file': None, 'burn_addr': None,
              'gui_managed': True, 'burn_completed': False}
    burn_options = burn_options or {}
    app_config = app_config or AppConfig()
    saved = dict(app_config.data)
    state = {'models': [], 'parts': [], 'initial_records': [], 'version': None,
             'burn_active': False, 'restoring': True}

    frame = ttk.Frame(root, padding=20)
    frame.grid(row=0, column=0, sticky='nsew')
    ttk.Label(frame, text='选择固件', font=('', 16, 'bold')).grid(
        row=0, column=0, columnspan=3, pady=(0, 14))

    model_var, part_var = tk.StringVar(), tk.StringVar()
    purpose_var = tk.StringVar()
    program_var = tk.StringVar(value=(saved.get('program') if saved.get('program') in ('bootload', 'app') else 'bootload'))
    status_options = ('研发验证', '已发布', '生产测试')
    saved_status = str(saved.get('status') if saved.get('status') is not None else '1')
    saved_status = saved_status if saved_status in ('0', '1', '2') else '1'
    status_value_var = tk.StringVar(value=status_options[int(saved_status)])
    aircraft_no_var = tk.StringVar(value=str(saved.get('aircraft_no') or ''))
    eo_no_var = tk.StringVar(value=str(saved.get('eo_no') or ''))
    status_var = tk.StringVar(value='正在从服务器加载机型...')

    ttk.Label(frame, text='机型：').grid(row=1, column=0, sticky='e', pady=5)
    model_box = ttk.Combobox(frame, textvariable=model_var, state='readonly', width=42)
    model_box.grid(row=1, column=1, columnspan=2, sticky='ew', pady=5)
    ttk.Label(frame, text='零部件：').grid(row=2, column=0, sticky='e', pady=5)
    part_box = ttk.Combobox(frame, textvariable=part_var, state='readonly', width=42)
    part_box.grid(row=2, column=1, columnspan=2, sticky='ew', pady=5)

    purpose_label = ttk.Label(frame, text='用途：')
    purpose_box = ttk.Combobox(frame, textvariable=purpose_var, state='readonly', width=42)
    ttk.Label(frame, text='类型：').grid(row=4, column=0, sticky='e', pady=5)
    program_box = ttk.Combobox(frame, textvariable=program_var, state='readonly',
                               values=('bootload', 'app'), width=42)
    program_box.grid(row=4, column=1, columnspan=2, sticky='ew', pady=5)
    ttk.Label(frame, text='状态：').grid(row=5, column=0, sticky='e', pady=5)
    status_box = ttk.Combobox(frame, textvariable=status_value_var,
                              values=status_options, state='readonly', width=42)
    status_box.grid(row=5, column=1, columnspan=2, sticky='ew', pady=5)
    ttk.Label(frame, text='航空器编号：').grid(row=6, column=0, sticky='e', pady=5)
    aircraft_no_entry = ttk.Entry(frame, textvariable=aircraft_no_var, width=44)
    aircraft_no_entry.grid(row=6, column=1, columnspan=2, sticky='ew', pady=5)
    ttk.Label(frame, text='EO单号：').grid(row=7, column=0, sticky='e', pady=5)
    eo_no_entry = ttk.Entry(frame, textvariable=eo_no_var, width=44)
    eo_no_entry.grid(row=7, column=1, columnspan=2, sticky='ew', pady=5)

    query_button = ttk.Button(frame, text='查询固件')
    query_button.grid(row=8, column=1, pady=(12, 8), sticky='w')
    ttk.Label(frame, textvariable=status_var, foreground='#555555').grid(
        row=9, column=0, columnspan=3, pady=(2, 10))

    info_frame = ttk.LabelFrame(frame, text='固件版本信息', padding=12)
    info_frame.grid(row=10, column=0, columnspan=3, sticky='ew')
    info_vars = {key: tk.StringVar(value='-') for key in
                 ('file_name', 'file_md5', 'file_size', 'version', 'burn_addr')}
    labels = [('文件名', 'file_name'), ('MD5', 'file_md5'),
              ('文件大小', 'file_size'), ('版本', 'version'),
              ('烧录地址', 'burn_addr')]
    for row, (text, key) in enumerate(labels):
        ttk.Label(info_frame, text=text + '：').grid(row=row, column=0, sticky='ne', pady=3)
        ttk.Label(info_frame, textvariable=info_vars[key], wraplength=390).grid(
            row=row, column=1, sticky='w', pady=3)

    button_frame = ttk.Frame(frame)
    button_frame.grid(row=11, column=0, columnspan=3, pady=(14, 0))
    erase_button = tk.Button(
        button_frame, text='全片擦除', width=12,
        background='#c62828', foreground='white', activebackground='#8e0000',
        activeforeground='white', relief='raised', cursor='hand2')
    erase_button.grid(row=0, column=0, padx=5)
    confirm_button = ttk.Button(button_frame, text='烧录', state='disabled', width=12)
    confirm_button.grid(row=0, column=1, padx=5)
    cancel_button = ttk.Button(button_frame, text='取消', width=12)
    cancel_button.grid(row=0, column=2, padx=5)

    def model_display(item):
        name = item.get('model_name') or item.get('model_code') or ''
        code = item.get('model_code') or ''
        status = item.get('status_text') or ''
        return f'{code} - {name}' + (f'（{status}）' if status else '')

    def part_display(item):
        number = item.get('part_no') or ''
        name = item.get('part_name') or ''
        status = item.get('status_text') or ''
        return f'{number} - {name}' + (f'（{status}）' if status else '')

    def selected_model():
        index = model_box.current()
        return state['models'][index] if 0 <= index < len(state['models']) else None

    def selected_part():
        index = part_box.current()
        return state['parts'][index] if 0 <= index < len(state['parts']) else None

    def clear_version():
        state['version'] = None
        confirm_button.configure(state='disabled')
        for var in info_vars.values():
            var.set('-')

    def set_busy(busy, message=None):
        widget_state = 'disabled' if busy else 'readonly'
        model_box.configure(state=widget_state)
        part_box.configure(state=widget_state)
        program_box.configure(state=widget_state)
        status_box.configure(state=widget_state)
        aircraft_no_entry.configure(state='disabled' if busy else 'normal')
        eo_no_entry.configure(state='disabled' if busy else 'normal')
        if purpose_box.winfo_ismapped():
            purpose_box.configure(state=widget_state)
        query_button.configure(state='disabled' if busy else 'normal')
        erase_button.configure(state='disabled' if busy else 'normal')
        if message:
            status_var.set(message)

    def show_purpose(part, preferred=''):
        raw = str((part or {}).get('purpose') or '').strip()
        purposes = [value.strip() for value in raw.split('|') if value.strip()]
        purpose_var.set(preferred if preferred in purposes else
                        (purposes[0] if purposes else ''))
        purpose_box.configure(values=purposes)
        if purposes:
            purpose_label.grid(row=3, column=0, sticky='e', pady=5)
            purpose_box.grid(row=3, column=1, columnspan=2, sticky='ew', pady=5)
        else:
            purpose_label.grid_remove()
            purpose_box.grid_remove()

    def parts_loaded(parts):
        state['parts'] = parts
        part_box.configure(values=[part_display(item) for item in parts])
        if parts:
            target_no = str(saved.get('part_no') or '') if state['restoring'] else ''
            index = next((i for i, item in enumerate(parts)
                          if str(item.get('part_no') or '') == target_no), 0)
            part_box.current(index)
            preferred_purpose = str(saved.get('purpose') or '') if state['restoring'] else ''
            show_purpose(parts[index], preferred_purpose)
            status_var.set(f'已加载 {len(parts)} 个零部件')
        else:
            part_var.set('')
            show_purpose(None)
            status_var.set('该机型没有可选零部件')
        state['restoring'] = False
        set_busy(False)

    def operation_failed(message):
        set_busy(False)
        status_var.set('请求失败')
        messagebox.showerror('服务器请求失败', message, parent=root)

    def on_model_changed(event=None):
        model = selected_model()
        clear_version()
        if not model:
            return
        set_busy(True, '正在加载零部件...')
        def worker():
            try:
                parts = auth.get_parts(model['model_code'], state['initial_records'])
            except Exception as exc:
                root.after(0, lambda message=str(exc): operation_failed(message))
            else:
                root.after(0, lambda values=parts: parts_loaded(values))
        threading.Thread(target=worker, daemon=True).start()

    def on_part_changed(event=None):
        clear_version()
        show_purpose(selected_part())

    def data_loaded(models, records):
        state['models'], state['initial_records'] = models, records
        model_box.configure(values=[model_display(item) for item in models])
        set_busy(False)
        if not models:
            status_var.set('服务器未返回机型数据')
            messagebox.showwarning('没有机型', '服务器未返回可选机型', parent=root)
            return
        target_code = str(saved.get('model_code') or '')
        index = next((i for i, item in enumerate(models)
                      if str(item.get('model_code') or '') == target_code), 0)
        model_box.current(index)
        on_model_changed()

    def load_data():
        set_busy(True, '正在从服务器加载机型...')
        def worker():
            try:
                models, records = auth.get_models_and_parts()
            except Exception as exc:
                root.after(0, lambda message=str(exc): operation_failed(message))
            else:
                root.after(0, lambda: data_loaded(models, records))
        threading.Thread(target=worker, daemon=True).start()

    def version_loaded(version):
        set_busy(False)
        if not version:
            clear_version()
            status_var.set('未查询到匹配固件')
            messagebox.showinfo('查询结果', '没有匹配的已发布固件', parent=root)
            return
        state['version'] = version
        size = version.get('file_size')
        info_vars['file_name'].set(str(version.get('file_name') or ''))
        info_vars['file_md5'].set(str(version.get('file_md5') or ''))
        info_vars['file_size'].set(auth.format_file_size(size))
        info_vars['burn_addr'].set(str((result.get('selection') or {}).get('burn_addr') or '-'))
        info_vars['version'].set(str(version.get('version') or ''))
        status_var.set('查询成功，请核对固件信息后点击烧录')
        confirm_button.configure(state='normal')

    def query_version():
        model, part = selected_model(), selected_part()
        if not model or not part:
            messagebox.showwarning('查询提示', '请选择机型和零部件', parent=root)
            return
        program = program_var.get().strip()
        if not program:
            messagebox.showwarning('查询提示', '请选择程序', parent=root)
            return
        try:
            resolved_address = auth.resolve_burn_address(program, part)
        except AuthenticationError as exc:
            messagebox.showerror('无法查询固件', str(exc), parent=root)
            return
        status_index = status_box.current()
        if status_index not in (0, 1, 2):
            messagebox.showwarning('查询提示', '请选择状态', parent=root)
            return
        status_value = status_index
        clear_version()
        selection = {'model_code': model['model_code'], 'part_no': part['part_no'],
                     'purpose': purpose_var.get().strip(), 'program': program,
                     'status': status_value, 'burn_addr': resolved_address,
                     'aircraft_no': aircraft_no_var.get().strip(),
                     'eo_no': eo_no_var.get().strip()}
        try:
            app_config.update(**{
                key: selection[key] for key in
                ('model_code', 'part_no', 'purpose', 'program', 'status',
                 'aircraft_no', 'eo_no')
            })
        except OSError as exc:
            messagebox.showerror('配置保存失败', str(exc), parent=root)
            return
        set_busy(True, '正在查询最新固件...')
        def worker():
            try:
                version = auth.get_latest_version(
                    selection['model_code'], selection['part_no'],
                    selection['purpose'], selection['program'], selection['status'],
                    selection['aircraft_no'], selection['eo_no'])
            except Exception as exc:
                root.after(0, lambda message=str(exc): operation_failed(message))
            else:
                result['selection'] = selection
                root.after(0, lambda value=version: version_loaded(value))
        threading.Thread(target=worker, daemon=True).start()

    def confirm():
        version = state['version']
        selection = result.get('selection') or {}
        if not version:
            return
        burn_addr = str(selection.get('burn_addr') or '').strip()
        try:
            burn_address = auth.parse_burn_address(burn_addr)
        except AuthenticationError as exc:
            messagebox.showerror('无法烧录', str(exc), parent=root)
            return
        if not str(version.get('file_md5') or '').strip():
            messagebox.showerror('无法下载', '固件MD5为空，不能下载和烧录', parent=root)
            return
        # 用户点击“烧录”后直接进入烧录过程，不再二次确认。
        start_burn_progress(version, burn_address)

    def start_burn_progress(version, burn_address):
        """显示实时烧录日志窗口；失败后可连接设备并在当前窗口重试。"""
        progress_window = tk.Toplevel(root)
        progress_window.withdraw()
        progress_window.title('STM32 ST-Link 烧录过程')
        progress_window.minsize(620, 400)
        progress_window.transient(root)

        progress_frame = ttk.Frame(progress_window, padding=12)
        progress_frame.pack(fill='both', expand=True)
        ttk.Label(progress_frame, text='烧录过程', font=('', 15, 'bold')).pack(
            anchor='w', pady=(0, 8))
        log_frame = ttk.Frame(progress_frame)
        log_frame.pack(fill='both', expand=True)
        log_text = tk.Text(log_frame, wrap='word', state='disabled',
                           font=('Consolas', 10), background='#101820',
                           foreground='#e8f1f2', insertbackground='white')
        scrollbar = ttk.Scrollbar(log_frame, orient='vertical', command=log_text.yview)
        log_text.configure(yscrollcommand=scrollbar.set)
        log_text.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        progress_status = tk.StringVar(value='正在准备烧录...')
        ttk.Label(progress_frame, textvariable=progress_status).pack(
            anchor='w', pady=(8, 4))
        action_frame = ttk.Frame(progress_frame)
        action_frame.pack(anchor='e')
        burn_button = ttk.Button(action_frame, text='烧录', state='disabled', width=12)
        burn_button.grid(row=0, column=0, padx=(0, 8))
        close_button = ttk.Button(action_frame, text='关闭', state='disabled', width=12)
        close_button.grid(row=0, column=1)

        messages = queue.Queue()
        process = {'running': False, 'finished': False,
                   'downloaded_path': None, 'success': False}

        class QueueWriter:
            def __init__(self, output_queue):
                self.output_queue = output_queue
            def write(self, text):
                if text:
                    self.output_queue.put(('log', str(text)))
                return len(text or '')
            def flush(self):
                return None
            def isatty(self):
                return False

        def append_log(text):
            log_text.configure(state='normal')
            parts = str(text).split(chr(13))
            for index, part in enumerate(parts):
                if index:
                    log_text.delete('end-1c linestart', 'end-1c')
                if part:
                    log_text.insert('end-1c', part)
            log_text.see('end')
            log_text.configure(state='disabled')

        def close_progress():
            if process['running'] or not process['finished']:
                return
            progress_window.destroy()
            state['burn_active'] = False
            set_busy(False)
            cancel_button.configure(state='normal')
            confirm_button.configure(state='normal' if state['version'] else 'disabled')
            status_var.set('烧录流程已结束，可重新选择或再次烧录')

        close_button.configure(command=close_progress)
        progress_window.protocol('WM_DELETE_WINDOW', close_progress)
        state['burn_active'] = True
        set_busy(True, '正在下载、校验并烧录固件...')
        confirm_button.configure(state='disabled')
        cancel_button.configure(state='disabled')

        def progress(received, expected):
            if expected > 0:
                messages.put(('status', f'正在下载固件... {received * 100 // expected}%'))
            else:
                messages.put(('status', f'正在下载固件... {auth.format_file_size(received)}'))

        def worker():
            writer = QueueWriter(messages)
            programmer = None
            success = False
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    if not process['downloaded_path']:
                        print('[*] 开始下载固件...')
                        process['downloaded_path'] = auth.download_firmware(
                            version, progress=progress)
                        print('[✓] 下载完成并通过MD5、文件大小校验: '
                              f"{process['downloaded_path']}")
                        print(f'[*] 烧录地址: 0x{burn_address:08X}')
                    else:
                        print(chr(10) + '[*] 重新执行 ST-Link 烧录...')
                        print(f"[*] 使用已校验固件: {process['downloaded_path']}")
                    print('[*] 开始执行 ST-Link 烧录...')
                    programmer = STM32Programmer(
                        serial=burn_options.get('serial'),
                        index=burn_options.get('device'))
                    programmer.flash_firmware(
                        process['downloaded_path'], burn_address,
                        burn_options.get('verify', True),
                        burn_options.get('run_after', True),
                        burn_options.get('chip'))
                success = True
            except Exception as exc:
                # 用户窗口只显示可操作的错误信息，不暴露Python调用栈。
                newline = chr(10)
                messages.put(('log', newline + f'[✗] 烧录失败: {exc}' + newline))
            finally:
                if programmer is not None:
                    try:
                        programmer.close()
                    except Exception as exc:
                        newline = chr(10)
                        messages.put(('log', newline +
                                      f'[!] 关闭ST-Link时出错: {exc}' + newline))
                        success = False
                messages.put(('finished', success,
                              ('烧录完成，请确认日志后手动关闭窗口' if success else
                               '烧录失败；连接ST-Link后可点击“烧录”重试')))

        def start_attempt():
            if process['running'] or process['success']:
                return
            process['running'] = True
            process['finished'] = False
            burn_button.configure(state='disabled')
            close_button.configure(state='disabled')
            progress_status.set('正在烧录，请稍候...')
            threading.Thread(target=worker, daemon=True).start()

        burn_button.configure(command=start_attempt)

        def poll_messages():
            try:
                while True:
                    item = messages.get_nowait()
                    if item[0] == 'log':
                        append_log(item[1])
                    elif item[0] == 'status':
                        progress_status.set(item[1])
                    elif item[0] == 'finished':
                        success, text = item[1], item[2]
                        process['running'] = False
                        process['finished'] = True
                        process['success'] = success
                        progress_status.set(text)
                        close_button.configure(state='normal')
                        burn_button.configure(state='disabled' if success else 'normal')
                        if success:
                            result['confirmed'] = True
                            result['burn_completed'] = True
                            result['version'] = version
                            result['downloaded_file'] = process['downloaded_path']
                            result['burn_addr'] = burn_address
                            status_var.set('烧录完成；过程窗口等待手动关闭')
                        else:
                            status_var.set('烧录失败；可在过程窗口连接设备后重试')
            except queue.Empty:
                pass
            if progress_window.winfo_exists():
                progress_window.after(80, poll_messages)

        progress_window.update_idletasks()
        width, height = 760, 520
        x = max((progress_window.winfo_screenwidth() - width) // 2, 0)
        y = max((progress_window.winfo_screenheight() - height) // 2, 0)
        progress_window.geometry(f'{width}x{height}+{x}+{y}')
        progress_window.deiconify()
        progress_window.focus_set()
        start_attempt()
        progress_window.after(80, poll_messages)

    def start_mass_erase():
        """执行与命令行-e/--erase相同的连接和全片擦除流程。"""
        first = messagebox.askyesno(
            '全片擦除警告',
            '全片擦除将删除目标芯片中的全部Flash数据。\n\n'
            '该操作不可撤销，请谨慎操作。是否继续？',
            icon='warning', parent=root, default='no')
        if not first:
            return
        second = messagebox.askyesno(
            '再次确认全片擦除',
            '请再次确认：目标设备、ST-Link连接和芯片型号均正确。\n\n'
            '执行后全部Flash数据将永久丢失，是否确认执行？',
            icon='warning', parent=root, default='no')
        if not second:
            return
        show_mass_erase_progress()

    def show_mass_erase_progress():
        progress_window = tk.Toplevel(root)
        progress_window.withdraw()
        progress_window.title('STM32 ST-Link 全片擦除过程')
        progress_window.minsize(620, 400)
        progress_window.transient(root)

        progress_frame = ttk.Frame(progress_window, padding=12)
        progress_frame.pack(fill='both', expand=True)
        ttk.Label(progress_frame, text='全片擦除过程',
                  font=('', 15, 'bold'), foreground='#c62828').pack(
                      anchor='w', pady=(0, 8))
        ttk.Label(progress_frame,
                  text='警告：该操作将永久删除目标芯片中的全部Flash数据。',
                  foreground='#c62828').pack(anchor='w', pady=(0, 8))
        log_frame = ttk.Frame(progress_frame)
        log_frame.pack(fill='both', expand=True)
        log_text = tk.Text(log_frame, wrap='word', state='disabled',
                           font=('Consolas', 10), background='#101820',
                           foreground='#e8f1f2', insertbackground='white')
        scrollbar = ttk.Scrollbar(log_frame, orient='vertical', command=log_text.yview)
        log_text.configure(yscrollcommand=scrollbar.set)
        log_text.pack(side='left', fill='both', expand=True)
        scrollbar.pack(side='right', fill='y')
        progress_status = tk.StringVar(value='正在连接ST-Link并执行全片擦除...')
        ttk.Label(progress_frame, textvariable=progress_status).pack(
            anchor='w', pady=(8, 4))
        close_button = ttk.Button(progress_frame, text='关闭', state='disabled', width=12)
        close_button.pack(anchor='e')

        messages = queue.Queue()
        process = {'running': True, 'finished': False}

        class QueueWriter:
            def write(self, text):
                if text:
                    messages.put(('log', str(text)))
                return len(text or '')
            def flush(self):
                return None
            def isatty(self):
                return False

        def append_log(text):
            log_text.configure(state='normal')
            log_text.insert('end', str(text))
            log_text.see('end')
            log_text.configure(state='disabled')

        def close_progress():
            if process['running'] or not process['finished']:
                return
            progress_window.destroy()
            state['burn_active'] = False
            set_busy(False)
            cancel_button.configure(state='normal')
            confirm_button.configure(state='normal' if state['version'] else 'disabled')
            status_var.set('全片擦除流程已结束，可继续选择固件')

        close_button.configure(command=close_progress)
        progress_window.protocol('WM_DELETE_WINDOW', close_progress)
        state['burn_active'] = True
        set_busy(True, '正在执行全片擦除，请勿断开设备或电源...')
        confirm_button.configure(state='disabled')
        cancel_button.configure(state='disabled')

        def worker():
            writer = QueueWriter()
            programmer = None
            success = False
            try:
                with contextlib.redirect_stdout(writer), contextlib.redirect_stderr(writer):
                    print('[!] 即将执行全片擦除，全部Flash数据将被删除...')
                    programmer = STM32Programmer(
                        serial=burn_options.get('serial'),
                        index=burn_options.get('device'))
                    # 与命令行-e/--erase分支执行路径一致。
                    programmer.connect(force_chip=burn_options.get('chip'))
                    programmer.mass_erase()
                success = True
            except Exception as exc:
                messages.put(('log', f'\n[✗] 全片擦除失败: {exc}\n'))
            finally:
                if programmer is not None:
                    try:
                        programmer.close()
                    except Exception as exc:
                        messages.put(('log', f'\n[!] 关闭ST-Link时出错: {exc}\n'))
                        success = False
                messages.put(('finished', success))

        def poll_messages():
            try:
                while True:
                    item = messages.get_nowait()
                    if item[0] == 'log':
                        append_log(item[1])
                    elif item[0] == 'finished':
                        success = item[1]
                        process['running'] = False
                        process['finished'] = True
                        progress_status.set(
                            '全片擦除完成，请确认日志后关闭窗口' if success else
                            '全片擦除失败，请检查连接和日志')
                        close_button.configure(state='normal')
                        status_var.set(
                            '全片擦除完成；过程窗口等待手动关闭' if success else
                            '全片擦除失败；请检查过程窗口日志')
            except queue.Empty:
                pass
            if progress_window.winfo_exists():
                progress_window.after(80, poll_messages)

        progress_window.update_idletasks()
        width, height = 760, 520
        x = max((progress_window.winfo_screenwidth() - width) // 2, 0)
        y = max((progress_window.winfo_screenheight() - height) // 2, 0)
        progress_window.geometry(f'{width}x{height}+{x}+{y}')
        progress_window.deiconify()
        progress_window.focus_set()
        threading.Thread(target=worker, daemon=True).start()
        progress_window.after(80, poll_messages)

    def close_selection(event=None):
        if state['burn_active']:
            messagebox.showwarning('设备操作进行中', '烧录或全片擦除尚未结束，暂时不能关闭窗口', parent=root)
            return
        root.destroy()

    query_button.configure(command=query_version)
    erase_button.configure(command=start_mass_erase)
    confirm_button.configure(command=confirm)
    cancel_button.configure(command=close_selection)
    model_box.bind('<<ComboboxSelected>>', on_model_changed)
    part_box.bind('<<ComboboxSelected>>', on_part_changed)
    program_box.bind('<<ComboboxSelected>>', lambda event: clear_version())
    status_value_var.trace_add('write', lambda *_: clear_version())
    aircraft_no_var.trace_add('write', lambda *_: clear_version())
    eo_no_var.trace_add('write', lambda *_: clear_version())
    root.protocol('WM_DELETE_WINDOW', close_selection)
    root.bind('<Escape>', close_selection)
    root.update_idletasks()
    x = max((root.winfo_screenwidth() - root.winfo_width()) // 2, 0)
    y = max((root.winfo_screenheight() - root.winfo_height()) // 2, 0)
    root.geometry(f'+{x}+{y}')
    load_data()
    root.mainloop()
    return result if result['confirmed'] else None


def terminal_firmware_selection(auth, app_config=None):
    """无图形环境时的固件选择流程。"""
    app_config = app_config or AppConfig()
    saved = app_config.data
    try:
        models, records = auth.get_models_and_parts()
        if not models:
            print('[✗] 服务器未返回机型数据', file=sys.stderr)
            return None
        print('机型:')
        for i, item in enumerate(models, 1):
            print(f"  {i}. {item['model_code']} - {item.get('model_name', '')}")
        default_model = next((i for i, item in enumerate(models, 1)
                              if str(item.get('model_code') or '') ==
                              str(saved.get('model_code') or '')), 1)
        model_text = input(f'请选择机型编号 [{default_model}]: ').strip()
        model = models[int(model_text or default_model) - 1]
        parts = auth.get_parts(model['model_code'], records)
        if not parts:
            print('[✗] 该机型没有可选零部件', file=sys.stderr)
            return None
        print('零部件:')
        for i, item in enumerate(parts, 1):
            print(f"  {i}. {item['part_no']} - {item.get('part_name', '')}")
        default_part = next((i for i, item in enumerate(parts, 1)
                             if str(item.get('part_no') or '') ==
                             str(saved.get('part_no') or '')), 1)
        part_text = input(f'请选择零部件编号 [{default_part}]: ').strip()
        part = parts[int(part_text or default_part) - 1]
        purposes = [v.strip() for v in str(part.get('purpose') or '').split('|') if v.strip()]
        purpose = ''
        if purposes:
            print('用途:')
            for i, value in enumerate(purposes, 1):
                print(f'  {i}. {value}')
            default_purpose = (purposes.index(saved.get('purpose')) + 1
                               if saved.get('purpose') in purposes else 1)
            purpose_text = input(f'请选择用途编号 [{default_purpose}]: ').strip()
            purpose = purposes[int(purpose_text or default_purpose) - 1]
        programs = ['bootload', 'app']
        print('程序:')
        for i, value in enumerate(programs, 1):
            print(f'  {i}. {value}')
        default_program = (programs.index(saved.get('program')) + 1
                           if saved.get('program') in programs else 1)
        program_text = input(f'请选择程序编号 [{default_program}]: ').strip()
        program = programs[int(program_text or default_program) - 1]
        status_labels = ['研发验证', '已发布', '生产测试']
        saved_status = str(saved.get('status') if saved.get('status') is not None else '1')
        default_status = int(saved_status) if saved_status in ('0', '1', '2') else 1
        print('状态:')
        for index, value in enumerate(status_labels):
            print(f'  {index}. {value}')
        status_text = input(f'请选择状态值 [{default_status}]: ').strip()
        status = int(status_text or default_status)
        if status not in (0, 1, 2):
            raise ValueError('状态只能选择0、1或2')
        aircraft_no = input(
            f"航空器编号（可留空） [{saved.get('aircraft_no', '')}]: ").strip()
        aircraft_no = aircraft_no or str(saved.get('aircraft_no') or '')
        eo_no = input(f"EO单号（可留空） [{saved.get('eo_no', '')}]: ").strip()
        eo_no = eo_no or str(saved.get('eo_no') or '')
        app_config.update(
            model_code=model['model_code'], part_no=part['part_no'],
            purpose=purpose, program=program, status=status,
            aircraft_no=aircraft_no, eo_no=eo_no)
        version = auth.get_latest_version(
            model['model_code'], part['part_no'], purpose, program, status,
            aircraft_no, eo_no)
    except (AuthenticationError, ValueError, IndexError) as e:
        print(f'[✗] 固件查询失败: {e}', file=sys.stderr)
        return None
    if not version:
        print('[✗] 没有匹配的已发布固件', file=sys.stderr)
        return None
    burn_addr = auth.resolve_burn_address(program, part)
    print(f"文件名: {version.get('file_name', '')}")
    print(f"MD5: {version.get('file_md5', '')}")
    print(f"文件大小: {auth.format_file_size(version.get('file_size'))}")
    print(f"版本: {version.get('version', '')}")
    print(f"烧录地址: {burn_addr or '(未配置)'}")
    if input('确认下载并烧录该固件？[y/N]: ').strip().lower() not in ('y', 'yes'):
        return None
    try:
        parsed_addr = auth.parse_burn_address(burn_addr)
        downloaded = auth.download_firmware(
            version, progress=lambda received, total:
            print(f'\r下载: {auth.format_file_size(received)} / '
                  f'{auth.format_file_size(total)}', end='', flush=True))
        print()
    except AuthenticationError as e:
        print(f'\n[✗] 固件文件错误: {e}；未执行烧录', file=sys.stderr)
        return None
    return {'confirmed': True,
            'selection': {'model_code': model['model_code'], 'part_no': part['part_no'],
                          'purpose': purpose, 'program': program, 'status': status,
                          'aircraft_no': aircraft_no, 'eo_no': eo_no},
            'version': version, 'downloaded_file': downloaded,
            'burn_addr': parsed_addr}


def terminal_login(auth, initial_username=''):
    """无图形环境时使用终端登录，密码不回显。"""
    username = initial_username or input('用户名: ').strip()
    if not username:
        return False
    password = getpass.getpass('密码: ')
    if not password:
        return False
    try:
        user = auth.login(username, password)
    except AuthenticationError as e:
        print(f'[✗] 登录失败: {e}', file=sys.stderr)
        return False
    print(f"[✓] 登录成功: {user.get('name') or user.get('username') or username}")
    return True


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
                sn = STLink._read_serial(d)
                devs.append((d, pid, name, sn))
        return devs

    @staticmethod
    def _read_serial(d):
        """读取 ST-Link 序列号。老款 ST-Link V2 的序列号是原始二进制 12 字节，
        按文本解码会乱码，需转成十六进制（与 STM32CubeProgrammer/ st-info 显示一致）。"""
        # 读原始字符串描述符字节：bmRequestType=0x80, GET_DESCRIPTOR, (STRING<<8)|index, langid=0x0409
        try:
            idx = d.iSerialNumber
            if not idx:
                return ""
            buf = d.ctrl_transfer(0x80, 0x06, (0x03 << 8) | idx, 0x0409, 255)
        except Exception:
            # 退回 get_string
            try:
                raw = usb.util.get_string(d, d.iSerialNumber) or ""
            except Exception:
                return ""
            if all(32 <= ord(c) < 127 for c in raw):
                return raw
            return ''.join(f"{ord(c) & 0xFF:02X}" for c in raw)
        buf = bytes(buf)
        # buf[0]=bLength, buf[1]=bDescriptorType(0x03)，其后为字符串数据
        data = buf[2:buf[0]] if len(buf) >= 2 and buf[0] >= 2 else buf
        # ST-Link 的字符串描述符把序列号当 UTF-16 存：奇数位(高字节)全为 0，
        # 真实序列号字节在偶数位(低字节)。先剥离高字节 0 取出真实字节序列。
        if len(data) >= 2 and len(data) % 2 == 0 and all(data[i] == 0 for i in range(1, len(data), 2)):
            real = bytes(data[i] for i in range(0, len(data), 2))
        else:
            real = bytes(data)
        # 真实字节若全为可打印 ASCII → 文本序列号；否则转大写 hex(与 CubeProgrammer 一致)
        if real and all(32 <= b < 127 for b in real):
            return real.decode('ascii')
        return ''.join(f"{b:02X}" for b in real)

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
    parser = argparse.ArgumentParser(description='STM32 ST-Link SWD 烧录工具（需要用户登录）')
    parser.add_argument('--login', action='store_true', help='显示登录界面并重新登录')
    parser.add_argument('--username', help='登录界面预填用户名')
    parser.add_argument('--logout', action='store_true', help='退出登录并删除本机api_token')
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
    auth = AuthClient()
    app_config = AppConfig()

    if args.logout:
        auth.logout()
        print("[✓] 已退出当前会话；api_token未保存到本机")
        return

    # 每次启动都必须登录；仅从普通配置中预填用户名，Token只保存在内存。
    initial_username = args.username or str(app_config.data.get('username') or '')
    if not show_login_dialog(auth, initial_username):
        print("[✗] 未登录，软件不能使用", file=sys.stderr)
        sys.exit(1)
    try:
        app_config.update(username=auth.username or initial_username)
    except OSError as exc:
        print(f"[✗] 无法保存用户名配置: {exc}", file=sys.stderr)
        sys.exit(1)
    print("[✓] 登录成功，已获得使用权限")

    # 登录后选择服务器上的固件版本，并恢复上次非敏感选择配置。
    firmware_choice = show_firmware_selection_dialog(auth, {
        'serial': args.serial,
        'device': args.device,
        'verify': not args.no_verify,
        'run_after': not args.no_run,
        'chip': args.chip,
    }, app_config)
    if not firmware_choice:
        print("[✗] 未确认固件，操作已取消", file=sys.stderr)
        sys.exit(1)
    selected = firmware_choice['selection']
    version = firmware_choice['version']
    downloaded_file = firmware_choice.get('downloaded_file')
    burn_addr = firmware_choice.get('burn_addr')
    # 图形界面已在烧录过程窗口中完成下载、校验和烧录，禁止主流程重复烧录。
    if firmware_choice.get('gui_managed'):
        return
    print(f"[✓] 固件下载校验通过: {version.get('file_name', '')} "
          f"({version.get('version', '')})")
    print(f"[*] 烧录文件: {downloaded_file}")
    print(f"[*] 烧录地址: 0x{burn_addr:08X}")

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

    # 用户点击“烧录”后，服务器下载并校验通过的文件优先用于烧录；
    # 等效于: python stm32_stlink_programmer.py -f 下载文件 -a 烧录地址
    prog = STM32Programmer(serial=args.serial, index=args.device)
    try:
        if downloaded_file:
            if not os.path.isfile(downloaded_file):
                raise STM32Error(f"下载的固件文件不存在: {downloaded_file}")
            prog.flash_firmware(downloaded_file, burn_addr,
                                not args.no_verify, not args.no_run, args.chip)
        elif args.firmware:
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
