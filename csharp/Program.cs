using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Threading;
using LibUsbDotNet;
using LibUsbDotNet.LibUsb;
using LibUsbDotNet.Main;

namespace Stm32Prog
{
    class STM32Error : Exception
    {
        public STM32Error(string msg) : base(msg) { }
    }

    // ============ Intel HEX 解析 ============
    static class IntelHexParser
    {
        public static (uint addr, byte[] data) Parse(string filepath)
        {
            var segments = new Dictionary<uint, byte[]>();
            uint baseAddr = 0;
            foreach (var rawLine in File.ReadAllLines(filepath))
            {
                var line = rawLine.Trim();
                if (!line.StartsWith(":")) continue;
                var hex = line.Substring(1);
                var raw = new byte[hex.Length / 2];
                for (int i = 0; i < raw.Length; i++)
                    raw[i] = Convert.ToByte(hex.Substring(i * 2, 2), 16);
                int length = raw[0];
                int recType = raw[3];
                uint offset = (uint)((raw[1] << 8) | raw[2]);
                var data = new byte[length];
                Array.Copy(raw, 4, data, 0, length);
                if (recType == 0x00)
                    segments[baseAddr + offset] = data;
                else if (recType == 0x01)
                    break;
                else if (recType == 0x02)
                    baseAddr = (uint)(((data[0] << 8) | data[1]) << 4);
                else if (recType == 0x04)
                    baseAddr = (uint)(((data[0] << 8) | data[1]) << 16);
            }
            if (segments.Count == 0)
                throw new STM32Error("HEX 文件为空或格式错误");
            uint minAddr = segments.Keys.Min();
            uint maxAddr = segments.Max(kv => kv.Key + (uint)kv.Value.Length);
            var result = new byte[maxAddr - minAddr];
            foreach (var kv in segments)
                Array.Copy(kv.Value, 0, result, (int)(kv.Key - minAddr), kv.Value.Length);
            return (minAddr, result);
        }
    }

    // ============ ST-Link USB 通信 ============
    class STLink
    {
        const int STLINK_VID = 0x0483;
        const int V2_PID = 0x3748, V21_PID = 0x374B, V3_PID = 0x374F;
        static readonly int[] PIDS = { V2_PID, V21_PID, V3_PID };

        const byte CMD_GET_VERSION = 0xF1, CMD_DEBUG = 0xF2, CMD_DFU = 0xF3;
        const byte CMD_GET_MODE = 0xF5, CMD_GET_VOLTAGE = 0xF7;
        const byte MODE_DFU = 0x00, MODE_MASS = 0x01, MODE_DEBUG = 0x02;
        const byte DFU_EXIT = 0x07;
        const byte DBG_ENTER = 0x30, DBG_EXIT = 0x21;
        const byte DBG_RESETSYS = 0x03, DBG_READMEM32 = 0x07, DBG_WRITEMEM32 = 0x08;
        const byte DBG_WRITEMEM16 = 0x48;  // 16-bit 总线写内存，F1 半字编程必需
        const byte DBG_RUNCORE = 0x09, DBG_HALTCORE = 0x02, DBG_ENTER_SWD = 0xA3;

        UsbContext _ctx;
        IUsbDevice _dev;
        UsbEndpointWriter _writer;
        UsbEndpointReader _reader;
        byte _epOut = 0x02, _epIn = 0x81;

        public void Open()
        {
            _ctx = new UsbContext();
            var all = _ctx.List();
            foreach (var pid in PIDS)
            {
                _dev = all.FirstOrDefault(d => d.VendorId == STLINK_VID && d.ProductId == pid);
                if (_dev != null) break;
            }
            if (_dev == null)
                throw new STM32Error("未找到 ST-Link 设备");

            _dev.Open();
            int pidNow = _dev.ProductId;
            string name = pidNow == V2_PID ? "V2" : pidNow == V21_PID ? "V2-1" : pidNow == V3_PID ? "V3" : "?";
            Console.WriteLine($"[OK] 找到 ST-Link {name} (PID: 0x{pidNow:X4})");
            if (pidNow != V2_PID) _epOut = 0x01;

            // 不调用 SetConfiguration（Windows WinUSB 下会重置端点状态导致超时）
            // 仅 claim interface 0（WinUSB 必需）
            (_dev as IUsbDevice)?.ClaimInterface(0);

            _writer = _dev.OpenEndpointWriter((WriteEndpointID)_epOut);
            _reader = _dev.OpenEndpointReader((ReadEndpointID)_epIn);

            // 清空残留
            try { var junk = new byte[64]; _reader.Read(junk, 50, out _); } catch { }

            Console.Write("  验证通信...");
            try
            {
                var buf = new byte[16];
                buf[0] = CMD_GET_VERSION;
                WriteRaw(buf, 1000);
                var res = ReadRaw(64, 1000);
                int ver = (res[0] << 8) | res[1];
                int sv = (ver >> 12) & 0x0F;
                int jv = (ver >> 6) & 0x3F;
                Console.WriteLine($" OK (FW: V{sv}, JTAG: v{jv})");
            }
            catch (Exception e)
            {
                Console.WriteLine($" 失败: {e.Message}");
                throw new STM32Error($"ST-Link通信失败: {e.Message}. 请拔插ST-Link后重试。");
            }
        }

        public void Close()
        {
            try { (_dev as IUsbDevice)?.ReleaseInterface(0); } catch { }
            try { _dev?.Close(); } catch { }
            try { _ctx?.Dispose(); } catch { }
            _dev = null;
        }

        void WriteRaw(byte[] data, int timeout)
        {
            Error ec = _writer.Write(data, timeout, out int _);
            if (ec != Error.Success)
                throw new STM32Error($"USB写入错误: {ec}");
        }

        byte[] ReadRaw(int len, int timeout)
        {
            var buf = new byte[len];
            Error ec = _reader.Read(buf, timeout, out int got);
            if (ec != Error.Success)
                throw new STM32Error($"USB读取错误: {ec}");
            if (got < len)
            {
                var trimmed = new byte[got];
                Array.Copy(buf, trimmed, got);
                return trimmed;
            }
            return buf;
        }

        // 发送16字节命令，始终读64字节后截取
        byte[] Cmd(byte[] data, int rxLen = 64, int timeout = 1000)
        {
            var buf = new byte[16];
            Array.Copy(data, buf, Math.Min(data.Length, 16));
            WriteRaw(buf, timeout);
            if (rxLen > 0)
            {
                var res = ReadRaw(64, timeout);
                var outb = new byte[rxLen];
                Array.Copy(res, outb, Math.Min(rxLen, res.Length));
                return outb;
            }
            return new byte[0];
        }

        public int GetMode()
        {
            var res = Cmd(new byte[] { CMD_GET_MODE }, 2);
            return res[0];
        }

        public double GetVoltage()
        {
            var res = Cmd(new byte[] { CMD_GET_VOLTAGE }, 8);
            uint a0 = BitConverter.ToUInt32(res, 0);
            uint a1 = BitConverter.ToUInt32(res, 4);
            return a0 != 0 ? 2.0 * a1 * 1.2 / a0 : 0.0;
        }

        public void DriveNrst(int level)
        {
            try { Cmd(new byte[] { CMD_DEBUG, 0x3C, (byte)(level & 1) }, 2); } catch { }
        }

        public (byte status, uint val) ReadDap(ushort ap, ushort addr)
        {
            var cmd = new byte[16];
            cmd[0] = CMD_DEBUG; cmd[1] = 0x45;
            BitConverter.GetBytes(ap).CopyTo(cmd, 2);
            BitConverter.GetBytes(addr).CopyTo(cmd, 4);
            WriteRaw(cmd, 1000);
            var r = ReadRaw(8, 1000);
            return (r[0], BitConverter.ToUInt32(r, 4));
        }

        public byte WriteDap(ushort ap, ushort addr, uint val)
        {
            var cmd = new byte[16];
            cmd[0] = CMD_DEBUG; cmd[1] = 0x46;
            BitConverter.GetBytes(ap).CopyTo(cmd, 2);
            BitConverter.GetBytes(addr).CopyTo(cmd, 4);
            BitConverter.GetBytes(val).CopyTo(cmd, 6);
            WriteRaw(cmd, 1000);
            var r = ReadRaw(2, 1000);
            return r[0];
        }

        public bool PowerUpDebug()
        {
            try
            {
                for (int i = 0; i < 5; i++)
                {
                    WriteDap(0xFFFF, 0x04, (1u << 28) | (1u << 30));
                    Thread.Sleep(20);
                    var (st, v) = ReadDap(0xFFFF, 0x04);
                    if (((v >> 29) & 1) != 0) return true;
                }
                return false;
            }
            catch { return false; }
        }

        byte DoEnterSwd()
        {
            var res = Cmd(new byte[] { CMD_DEBUG, DBG_ENTER, DBG_ENTER_SWD }, 2);
            return res[0];
        }

        public void EnterSwd()
        {
            int mode = GetMode();
            Console.Write($"mode={mode}...");
            if (mode == MODE_DFU)
            {
                Console.Write("dfu_exit...");
                var buf = new byte[16];
                buf[0] = CMD_DFU; buf[1] = DFU_EXIT;
                WriteRaw(buf, 1000);
                Thread.Sleep(300);
            }
            Console.Write("enter_swd...");
            byte last = 0;
            for (int attempt = 0; attempt < 5; attempt++)
            {
                last = DoEnterSwd();
                if (last == 0x80) break;
                Console.Write($"[0x{last:X2}retry]...");
                try { Cmd(new byte[] { CMD_DEBUG, DBG_EXIT }, 2); } catch { }
                Thread.Sleep(200);
            }
            // 若失败, connect-under-reset (NRST复位唤醒)
            if (last != 0x80)
            {
                Console.Write("[NRST复位]...");
                DriveNrst(0);
                Thread.Sleep(200);
                DriveNrst(1);
                Thread.Sleep(10);
                last = DoEnterSwd();
                if (last != 0x80)
                    throw new STM32Error($"进入SWD失败: status=0x{last:X2}");
            }
            // debug电源域上电(F7/M7关键: 否则AHB-AP读内存全0)
            if (!PowerUpDebug())
                Console.Write("[debug域上电失败]");
            Console.WriteLine(" OK");
        }

        public void Run() => Cmd(new byte[] { CMD_DEBUG, DBG_RUNCORE }, 2);

        public byte[] ReadMem32(uint addr, int size)
        {
            var cmd = new byte[16];
            cmd[0] = CMD_DEBUG; cmd[1] = DBG_READMEM32;
            BitConverter.GetBytes(addr).CopyTo(cmd, 2);
            BitConverter.GetBytes((ushort)size).CopyTo(cmd, 6);
            WriteRaw(cmd, 1000);
            return ReadRaw(size, 1000);
        }

        public void WriteMem32(uint addr, byte[] data)
        {
            var cmd = new byte[16];
            cmd[0] = CMD_DEBUG; cmd[1] = DBG_WRITEMEM32;
            BitConverter.GetBytes(addr).CopyTo(cmd, 2);
            BitConverter.GetBytes((ushort)data.Length).CopyTo(cmd, 6);
            WriteRaw(cmd, 1000);
            WriteRaw(data, 1000);
        }

        public void WriteMem8(uint addr, byte[] data)
        {
            var cmd = new byte[16];
            cmd[0] = CMD_DEBUG; cmd[1] = 0x0D;
            BitConverter.GetBytes(addr).CopyTo(cmd, 2);
            BitConverter.GetBytes((ushort)data.Length).CopyTo(cmd, 6);
            WriteRaw(cmd, 1000);
            WriteRaw(data, 1000);
        }

        // 16-bit 总线写内存 (WRITEMEM_16BIT=0x48)。STM32F0/F1 Flash 半字编程必需，
        // 8-bit/32-bit 总线写无法触发 F1 编程。
        public void WriteMem16(uint addr, byte[] data)
        {
            var cmd = new byte[16];
            cmd[0] = CMD_DEBUG; cmd[1] = DBG_WRITEMEM16;
            BitConverter.GetBytes(addr).CopyTo(cmd, 2);
            BitConverter.GetBytes((ushort)data.Length).CopyTo(cmd, 6);
            WriteRaw(cmd, 1000);
            WriteRaw(data, 1000);
        }

        public uint ReadReg32(uint addr)
        {
            var d = ReadMem32(addr, 4);
            return BitConverter.ToUInt32(d, 0);
        }

        public void WriteReg32(uint addr, uint val)
        {
            WriteMem32(addr, BitConverter.GetBytes(val));
        }
    }
    // Program 类在 Program2.cs 续写
}
