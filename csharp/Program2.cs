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
    // ============ STM32 Flash 编程 ============
    class STM32Programmer
    {
        const uint FLASH_BASE_F1 = 0x40022000;
        const uint FLASH_BASE_F4 = 0x40023C00;
        const uint FLASH_KEYR = 0x04;
        const uint FLASH_SR = 0x0C;
        const uint FLASH_CR = 0x10;
        const uint DHCSR = 0xE000EDF0;
        const uint AIRCR = 0xE000ED0C;
        const uint DBGMCU_IDCODE = 0xE0042000;

        static readonly Dictionary<int, string> CHIP_IDS = new Dictionary<int, string>
        {
            {0x410,"STM32F1 Medium-density"},{0x411,"STM32F2/F4xx"},
            {0x412,"STM32F1 Low-density"},{0x413,"STM32F40x/41x"},
            {0x414,"STM32F1 High-density"},{0x415,"STM32L4xx"},
            {0x416,"STM32L1xx"},{0x418,"STM32F1 Connectivity"},
            {0x419,"STM32F42x/43x"},{0x420,"STM32F1 VL Medium"},
            {0x421,"STM32F446"},{0x423,"STM32F401xB/C"},
            {0x425,"STM32L0xx"},{0x428,"STM32F1 VL High"},
            {0x430,"STM32F1 XL"},{0x431,"STM32F411"},
            {0x422,"STM32F302xB/C/F303xB/C"},{0x432,"STM32F37x"},
            {0x433,"STM32F401xD/E"},{0x438,"STM32F303x4/F334/F328"},
            {0x439,"STM32F301/F302x6x8/F318"},{0x446,"STM32F302xE/F303xE"},
            {0x434,"STM32F469/479"},{0x440,"STM32F05x"},
            {0x441,"STM32F412"},{0x442,"STM32F09x"},
            {0x444,"STM32F03x"},{0x445,"STM32F04x"},
            {0x448,"STM32F07x"},{0x449,"STM32F74x/75x"},
            {0x450,"STM32H7xx"},{0x451,"STM32F76x/77x"},
            {0x460,"STM32G0xx"},{0x468,"STM32G4xx"},
        };

        STLink stlink = new STLink();
        public STM32Programmer(string serial = null, int? index = null)
        {
            stlink.Serial = serial;
            stlink.Index = index;
        }
        int chipId = 0;
        uint flashBase = FLASH_BASE_F1;
        bool isF4 = false, isH7 = false, isF7 = false;
        int pageSize = 1024;
        uint psize = 2; // F4/F7编程并行度: 0=8bit,1=16bit,2=32bit,3=64bit(需Vpp)

        public void Connect(string forceChip = null)
        {
            stlink.Open();
            Console.Write("  进入SWD...");
            stlink.EnterSwd();
            Console.Write("  停止CPU...");
            stlink.WriteReg32(DHCSR, 0xA05F0003);
            Thread.Sleep(50);
            // connect-under-reset: 复位后立即halt，抢占运行中的固件，确保DBGMCU可读
            try
            {
                stlink.WriteReg32(0xE000EDFC, 0x00000001); // DEMCR VC_CORERESET
                stlink.WriteReg32(0xE000ED0C, 0x05FA0004); // AIRCR SYSRESETREQ
                Thread.Sleep(100);
                stlink.WriteReg32(DHCSR, 0xA05F0003);      // 再次halt
                Thread.Sleep(50);
                stlink.WriteReg32(0xE000EDFC, 0x00000000); // 清除 VC_CORERESET
            }
            catch { }
            // 读ID: 多地址 + 重试
            uint raw = 0; chipId = 0;
            for (int t = 0; t < 5 && chipId == 0; t++)
            {
                foreach (uint a in new uint[] { DBGMCU_IDCODE, 0x5C001000, 0x40015800 })
                {
                    raw = stlink.ReadReg32(a);
                    int cid = (int)(raw & 0xFFF);
                    if (cid != 0 && cid != 0xFFF) { chipId = cid; break; }
                }
                if (chipId == 0) Thread.Sleep(100);
            }
            // 手动指定芯片系列（兜底）
            if (!string.IsNullOrEmpty(forceChip))
            {
                var map = new Dictionary<string, int> { {"h7",0x450},{"f7",0x451},{"f4",0x413},{"f1",0x410},{"f0",0x440} };
                if (map.TryGetValue(forceChip.ToLower(), out int fc))
                {
                    chipId = fc;
                    Console.WriteLine($"[!] 手动指定芯片系列: {forceChip.ToUpper()}");
                }
            }
            string name = CHIP_IDS.ContainsKey(chipId) ? CHIP_IDS[chipId] : "Unknown";
            Console.WriteLine($"[OK] 芯片: {name} (ID: 0x{chipId:X3}, REV: 0x{raw >> 16:X4})");
            if (chipId == 0)
                Console.WriteLine("[!] 警告: 无法读取芯片ID。若烧录失败请用 --chip f7/f4/h7/f1 手动指定系列");
            int[] h7 = { 0x450, 0x480, 0x483 };
            int[] f7 = { 0x449, 0x451, 0x452 };
            int[] f4 = { 0x411, 0x413, 0x419, 0x421, 0x423, 0x431, 0x433, 0x434, 0x441, 0x458, 0x463 };
            int[] f1big = { 0x414, 0x418, 0x428, 0x430 };
            if (Array.IndexOf(h7, chipId) >= 0)
            {
                flashBase = 0x52002000; isH7 = true; pageSize = 131072;
                Console.WriteLine("    系列: STM32H7, Flash Word=256-bit, 扇区=128KB");
            }
            else if (Array.IndexOf(f7, chipId) >= 0)
            {
                flashBase = FLASH_BASE_F4; isF7 = true; pageSize = 32768;
                Console.WriteLine("    系列: STM32F7, 32-bit 编程, 扇区=32KB/128KB/256KB");
            }
            else if (Array.IndexOf(f4, chipId) >= 0) { flashBase = FLASH_BASE_F4; isF4 = true; pageSize = 16384; }
            else if (Array.IndexOf(f1big, chipId) >= 0) { pageSize = 2048; }
            else { pageSize = 1024; }
            double v = stlink.GetVoltage();
            if (v > 0) Console.WriteLine($"  目标电压: {v:F2}V");
            // F4/F7 编程并行度受供电电压限制:
            //   >=2.7V: 32bit(2)  2.1~2.7V: 16bit(1)  1.8~2.1V: 8bit(0)
            if (isF4 || isF7)
            {
                psize = 2; // 默认32-bit, 与官方工具一致(ST-Link电压读数常偏低不可信)
                int bits = 8 << (int)psize;
                Console.WriteLine($"    编程并行度: {bits}-bit (PSIZE={psize})");
            }
        }

        void FlashUnlock()
        {
            if (isH7)
            {
                uint cr = stlink.ReadReg32(flashBase + 0x0C);
                if ((cr & 0x01) != 0)
                {
                    stlink.WriteReg32(flashBase + 0x04, 0x45670123);
                    stlink.WriteReg32(flashBase + 0x04, 0xCDEF89AB);
                    cr = stlink.ReadReg32(flashBase + 0x0C);
                    if ((cr & 0x01) != 0) throw new STM32Error("H7 Flash Bank1 解锁失败");
                }
            }
            else if (isF4 || isF7)
            {
                // F4/F7: 检查LOCK, 若锁定则解锁
                // 关键(F7): 解锁必须在连接后第一时间, 前面不能有FLASH_CR写操作污染状态机
                uint cr = stlink.ReadReg32(flashBase + FLASH_CR);
                if ((cr & 0x80000000) != 0)
                {
                    stlink.WriteReg32(flashBase + FLASH_KEYR, 0x45670123);
                    stlink.WriteReg32(flashBase + FLASH_KEYR, 0xCDEF89AB);
                    cr = stlink.ReadReg32(flashBase + FLASH_CR);
                    if ((cr & 0x80000000) != 0)
                        throw new STM32Error($"FLASH 解锁失败 CR=0x{cr:X8} (F7需连接后立即解锁)");
                }
            }
            else
            {
                // F0/F1: LOCK = bit7。复位后默认锁定(CR=0x80)，必须解锁否则擦除/编程静默失效。
                uint cr = stlink.ReadReg32(flashBase + FLASH_CR);
                if ((cr & 0x80) != 0)
                {
                    stlink.WriteReg32(flashBase + FLASH_KEYR, 0x45670123);
                    stlink.WriteReg32(flashBase + FLASH_KEYR, 0xCDEF89AB);
                    cr = stlink.ReadReg32(flashBase + FLASH_CR);
                    if ((cr & 0x80) != 0)
                        throw new STM32Error($"F1 FLASH 解锁失败 CR=0x{cr:X8}");
                }
            }
        }

        void FlashLock()
        {
            if (isH7)
            {
                uint cr = stlink.ReadReg32(flashBase + 0x0C);
                stlink.WriteReg32(flashBase + 0x0C, cr | 0x01);
            }
            else if (isF4 || isF7)
            {
                // F4/F7 LOCK = bit31
                uint cr = stlink.ReadReg32(flashBase + FLASH_CR);
                stlink.WriteReg32(flashBase + FLASH_CR, cr | 0x80000000);
            }
            else
            {
                // F1 LOCK = bit7
                uint cr = stlink.ReadReg32(flashBase + FLASH_CR);
                stlink.WriteReg32(flashBase + FLASH_CR, cr | 0x80);
            }
        }

        void FlashWait(double timeout = 10.0)
        {
            var t0 = DateTime.Now;
            if (isH7)
            {
                uint sr;
                while (true)
                {
                    sr = stlink.ReadReg32(flashBase + 0x10);
                    if ((sr & 0x05) == 0) break;
                    if ((DateTime.Now - t0).TotalSeconds > timeout) throw new STM32Error($"H7 Flash 超时 SR=0x{sr:X8}");
                    Thread.Sleep(1);
                }
                const uint FATAL = 0x000E0000;
                if ((sr & 0x0FFF0000) != 0) stlink.WriteReg32(flashBase + 0x14, 0x0FFF0000);
                if ((sr & FATAL) != 0) throw new STM32Error($"H7 Flash 致命错误 SR=0x{sr:X8}");
            }
            else if (isF4 || isF7)
            {
                // F4/F7 SR: bit16=BSY, bit1=OPERR, bit4=WRPERR, bit5=PGAERR, bit6=PGPERR, bit7=ERSERR
                uint sr;
                while (true)
                {
                    sr = stlink.ReadReg32(flashBase + FLASH_SR);
                    if ((sr & (1u << 16)) == 0) break; // BSY=bit16
                    if ((DateTime.Now - t0).TotalSeconds > timeout) throw new STM32Error($"Flash 操作超时 SR=0x{sr:X8}");
                    Thread.Sleep(1);
                }
                uint err = sr & 0xF2;
                if (err != 0)
                {
                    stlink.WriteReg32(flashBase + FLASH_SR, err); // 写1清除
                    var names = new System.Collections.Generic.List<string>();
                    if ((sr & 0x02) != 0) names.Add("OPERR");
                    if ((sr & 0x10) != 0) names.Add("WRPERR");
                    if ((sr & 0x20) != 0) names.Add("PGAERR");
                    if ((sr & 0x40) != 0) names.Add("PGPERR");
                    if ((sr & 0x80) != 0) names.Add("ERSERR");
                    throw new STM32Error($"Flash 错误 SR=0x{sr:X8} ({string.Join("|", names)})");
                }
            }
            else
            {
                uint sr;
                while (true)
                {
                    sr = stlink.ReadReg32(flashBase + FLASH_SR);
                    if ((sr & 0x01) == 0) break; // F1 BSY=bit0
                    if ((DateTime.Now - t0).TotalSeconds > timeout) throw new STM32Error("Flash 操作超时");
                    Thread.Sleep(10);
                }
                if ((sr & 0x04) != 0) throw new STM32Error($"Flash 编程错误 SR=0x{sr:X8}");
                if ((sr & 0x10) != 0) throw new STM32Error($"Flash 写保护错误 SR=0x{sr:X8}");
            }
        }

        public void ErasePages(uint startAddr, int size)
        {
            if (isH7) EraseSectorsH7(startAddr, size);
            else if (isF7) EraseSectorsF7(startAddr, size);
            else if (isF4) EraseSectorsF4(startAddr, size);
            else ErasePagesF1(startAddr, size);
        }

        void EraseSectorsH7(uint startAddr, int size)
        {
            const uint sectorSize = 131072;
            stlink.WriteReg32(flashBase + 0x14, 0x0FEF0000);
            uint wpsn = stlink.ReadReg32(flashBase + 0x38);
            if (wpsn != 0xFF)
            {
                Console.WriteLine($"  [!] 检测到写保护 WPSN=0x{wpsn:X2}, 正在解除...");
                stlink.WriteReg32(flashBase + 0x3C, 0xFF);
            }
            uint flashStart = 0x08000000;
            int firstSector = (int)((startAddr - flashStart) / sectorSize);
            int lastSector = (int)((startAddr + (uint)size - 1 - flashStart) / sectorSize);
            int n = lastSector - firstSector + 1;
            Console.WriteLine($"[*] 擦除 {n} 个扇区 (128KB/扇区, 扇区{firstSector}-{lastSector})...");
            FlashUnlock();
            stlink.WriteReg32(flashBase + 0x14, 0x0FEF0000);
            for (int i = firstSector; i <= lastSector; i++)
            {
                // H7 CR1 正确 bit 定义: PG=bit1, SER=bit2, START=bit7, PSIZE=bit4:5, SNB=bit8:10
                // 擦除必须用 SER=bit2(之前误用bit1=PG导致擦除不生效)。PSIZE=2(32-bit,与官方一致)
                const uint SER = (1u << 2), START = (1u << 7), PSIZE = (2u << 4);
                if (i < 8)
                {
                    uint crVal = SER | ((uint)i << 8) | PSIZE | START;
                    stlink.WriteReg32(flashBase + 0x0C, crVal);
                }
                else
                {
                    uint s = (uint)(i - 8);
                    uint crVal = SER | (s << 8) | PSIZE | START;
                    stlink.WriteReg32(flashBase + 0x10C, crVal);
                }
                FlashWait(30);
                int pct = (i - firstSector + 1) * 100 / n;
                Console.Write($"\r  擦除: {pct}%");
            }
            Console.WriteLine();
            FlashLock();
            Console.WriteLine("[OK] 擦除完成");
        }

        void ErasePagesF1(uint startAddr, int size)
        {
            int nPages = (size + pageSize - 1) / pageSize;
            Console.WriteLine($"[*] 擦除 {nPages} 页 (页大小={pageSize})...");
            FlashUnlock();
            for (int i = 0; i < nPages; i++)
            {
                uint pageAddr = startAddr + (uint)(i * pageSize);
                stlink.WriteReg32(flashBase + FLASH_CR, 0x02);
                stlink.WriteReg32(flashBase + 0x14, pageAddr);
                stlink.WriteReg32(flashBase + FLASH_CR, 0x42);
                FlashWait(5);
                int pct = (i + 1) * 100 / nPages;
                Console.Write($"\r  擦除: {pct}%");
            }
            Console.WriteLine();
            FlashLock();
            Console.WriteLine("[OK] 擦除完成");
        }

        void EraseSectorsF4(uint startAddr, int size)
        {
            // F4 单bank(<=1MB): 4x16K+1x64K+7x128K(12扇区)
            // F4 双bank(2MB,如F427/F429/F437/F439): x2(24扇区), bank2 SNB从16起(SNB[4]选bank)
            int[] bank1 = { 16384, 16384, 16384, 16384, 65536, 131072, 131072, 131072, 131072, 131072, 131072, 131072 };
            var sectors = new List<int>(); sectors.AddRange(bank1); sectors.AddRange(bank1); // 支持到2MB
            uint flashStart = 0x08000000;
            int offset = (int)(startAddr - flashStart);
            int end = offset + size;
            int cur = 0;
            var toErase = new List<int>(); // 存 SNB 编码
            for (int i = 0; i < sectors.Count; i++)
            {
                if (cur < end && cur + sectors[i] > offset)
                    toErase.Add(i < 12 ? i : (i - 12 + 16)); // bank2 SNB从16起
                cur += sectors[i];
            }
            Console.WriteLine($"[*] 擦除 {toErase.Count} 个扇区...");
            FlashUnlock();
            stlink.WriteReg32(flashBase + FLASH_SR, 0xF2); // 清SR残留错误
            FlashWait(30);
            for (int idx = 0; idx < toErase.Count; idx++)
            {
                uint snb = (uint)toErase[idx];
                // 标准两步: 先配置 SER|SNB|PSIZE, 再单独置 STRT
                uint cr = (1u << 1) | (snb << 3) | (psize << 8);
                stlink.WriteReg32(flashBase + FLASH_CR, cr);
                stlink.WriteReg32(flashBase + FLASH_CR, cr | (1u << 16));
                FlashWait(30);
                int pct = (idx + 1) * 100 / toErase.Count;
                Console.Write($"\r  擦除: {pct}%");
            }
            Console.WriteLine();
            stlink.WriteReg32(flashBase + FLASH_CR, 0); // 清SER
            FlashLock();
            Console.WriteLine("[OK] 擦除完成");
        }

        void EraseSectorsF7(uint startAddr, int size)
        {
            // STM32F7 扇区布局: 4x32KB + 1x128KB + 7x256KB (单bank, 最多2MB)
            int[] sectors = { 32768, 32768, 32768, 32768, 131072, 262144, 262144, 262144, 262144, 262144, 262144, 262144 };
            uint flashStart = 0x08000000;
            int offset = (int)(startAddr - flashStart);
            int end = offset + size;
            int cur = 0;
            var toErase = new List<int>();
            for (int i = 0; i < sectors.Length; i++)
            {
                if (cur < end && cur + sectors[i] > offset) toErase.Add(i);
                cur += sectors[i];
            }
            Console.WriteLine($"[*] 擦除 {toErase.Count} 个扇区 (F7)...");
            FlashUnlock();
            stlink.WriteReg32(flashBase + FLASH_SR, 0xF2); // 清SR残留错误
            FlashWait(30);
            for (int idx = 0; idx < toErase.Count; idx++)
            {
                int sn = toErase[idx];
                // 标准两步: 先配置 SER+SNB+PSIZE, 再单独置 STRT
                uint cr = (1u << 1) | ((uint)sn << 3) | (psize << 8);
                stlink.WriteReg32(flashBase + FLASH_CR, cr);
                stlink.WriteReg32(flashBase + FLASH_CR, cr | (1u << 16)); // +STRT
                FlashWait(30);
                int pct = (idx + 1) * 100 / toErase.Count;
                Console.Write($"\r  擦除: {pct}%");
            }
            Console.WriteLine();
            stlink.WriteReg32(flashBase + FLASH_CR, 0); // 清SER
            FlashLock();
            Console.WriteLine("[OK] 擦除完成");
        }

        public void WriteFlash(uint addr, byte[] data)
        {
            Console.WriteLine($"[*] 写入 {data.Length} 字节...");
            FlashUnlock();
            if (isH7) WriteFlashH7(addr, data);
            else if (isF7 || isF4) WriteFlashF4(addr, data);
            else WriteFlashF1(addr, data);
            FlashLock();
            Console.WriteLine("[OK] 写入完成");
        }

        void WriteFlashH7(uint addr, byte[] data)
        {
            uint FB = flashBase;
            const uint CR1 = 0x0C, SR1 = 0x10, CCR1 = 0x14;
            int total = data.Length;
            stlink.WriteReg32(FB + CCR1, 0x0FFF0000);
            uint cr = stlink.ReadReg32(FB + CR1);
            if ((cr & 0x01) != 0) throw new STM32Error($"H7: Flash仍锁定 CR=0x{cr:X8}");
            // PG=1 | PSIZE=2(32-bit，与官方工具一致；之前用3=64-bit会触发ECC错误)
            stlink.WriteReg32(FB + CR1, (1u << 1) | (2u << 4));
            cr = stlink.ReadReg32(FB + CR1);
            if ((cr & 0x02) == 0) throw new STM32Error($"H7: PG位设置失败 CR=0x{cr:X8}");

            const uint FATAL = 0x000E0000;
            int written = 0, block = 32, lastPct = -1;
            while (written < total)
            {
                int len = Math.Min(block, total - written);
                var chunk = new byte[32];
                for (int i = 0; i < 32; i++) chunk[i] = 0xFF;
                Array.Copy(data, written, chunk, 0, len);
                uint waddr = addr + (uint)written;
                bool ok = false;
                uint sr = 0;
                for (int attempt = 0; attempt < 4; attempt++)
                {
                    stlink.WriteMem32(waddr, chunk);
                    var t0 = DateTime.Now;
                    while (true)
                    {
                        sr = stlink.ReadReg32(FB + SR1);
                        if ((sr & 0x05) == 0) break;
                        if ((DateTime.Now - t0).TotalSeconds > 2) throw new STM32Error($"H7 Flash 超时 SR=0x{sr:X8}");
                    }
                    if ((sr & 0x0FFF0000) != 0) stlink.WriteReg32(FB + CCR1, 0x0FFF0000);
                    var rb = stlink.ReadMem32(waddr, 32);
                    bool match = true;
                    for (int i = 0; i < 32; i++) if (rb[i] != chunk[i]) { match = false; break; }
                    if (match) { ok = true; break; }
                    stlink.WriteReg32(FB + CR1, (1u << 1) | (2u << 4));
                }
                if (!ok) throw new STM32Error($"H7: word @ 0x{waddr:X8} 写入失败 (重试4次) SR=0x{sr:X8}");
                written += len;
                int pct = written * 100 / total;
                if (pct != lastPct) { Console.Write($"\r  写入: {pct}%"); lastPct = pct; }
            }
            Console.WriteLine();
        }

        void WriteFlashF4(uint addr, byte[] data)
        {
            int total = data.Length;
            stlink.WriteReg32(flashBase + FLASH_SR, 0xF2); // 清SR残留错误
            // PG=1 | PSIZE
            stlink.WriteReg32(flashBase + FLASH_CR, (1u << 0) | (psize << 8));
            bool use8 = (psize == 0);
            int align = 1 << (int)psize; // 0->1, 2->4
            int written = 0, block = 64, lastPct = -1;
            while (written < total)
            {
                int len = Math.Min(block, total - written);
                int padded = (align > 1 && len % align != 0) ? len + (align - len % align) : len;
                var chunk = new byte[padded];
                for (int i = 0; i < padded; i++) chunk[i] = 0xFF;
                Array.Copy(data, written, chunk, 0, len);
                if (use8) stlink.WriteMem8(addr + (uint)written, chunk);
                else stlink.WriteMem32(addr + (uint)written, chunk);
                FlashWait(2);
                written += len;
                int pct = written * 100 / total;
                if (pct != lastPct) { Console.Write($"\r  写入: {pct}%"); lastPct = pct; }
            }
            Console.WriteLine();
        }

        void WriteFlashF1(uint addr, byte[] data)
        {
            // F0/F1: 半字(16-bit) 编程。必须用 WRITEMEM_16BIT(0x48)，
            // 8/32-bit 总线写无法触发 F1 Flash 编程。逐半字写，每个半字写后等 BSY 清零。
            // 长度补齐到偶数(半字对齐)，尾部补 0xFF
            if (data.Length % 2 != 0)
            {
                var d2 = new byte[data.Length + 1];
                Array.Copy(data, d2, data.Length);
                d2[data.Length] = 0xFF;
                data = d2;
            }
            int total = data.Length;
            stlink.WriteReg32(flashBase + FLASH_CR, 0x01); // PG
            int written = 0, lastPct = -1;
            try
            {
                while (written < total)
                {
                    var half = new byte[2] { data[written], data[written + 1] };
                    stlink.WriteMem16(addr + (uint)written, half);
                    FlashWait(1);
                    uint sr = stlink.ReadReg32(flashBase + FLASH_SR);
                    if ((sr & ((1u << 2) | (1u << 4))) != 0)
                    {
                        stlink.WriteReg32(flashBase + FLASH_SR, sr);
                        string err = ((sr & (1u << 2)) != 0 ? "PGERR " : "") + ((sr & (1u << 4)) != 0 ? "WRPRTERR" : "");
                        throw new STM32Error($"F1 编程错误 @ 0x{addr + (uint)written:X8} SR=0x{sr:X8} ({err})");
                    }
                    written += 2;
                    int pct = written * 100 / total;
                    if (pct != lastPct) { Console.Write($"\r  写入: {pct}%"); lastPct = pct; }
                }
            }
            finally
            {
                stlink.WriteReg32(flashBase + FLASH_CR, 0x00); // 清PG
            }
            Console.WriteLine();
        }

        public void Verify(uint addr, byte[] data)
        {
            int total = data.Length;
            Console.WriteLine("[*] 校验...");
            stlink.WriteReg32(DHCSR, 0xA05F0003);
            Thread.Sleep(50);
            int verified = 0, block = 256, lastPct = -1;
            while (verified < total)
            {
                int sz = Math.Min(block, total - verified);
                int readSz = (sz % 4 == 0) ? sz : sz + (4 - sz % 4);
                var mem = stlink.ReadMem32(addr + (uint)verified, readSz);
                if (!MemEq(mem, data, verified, sz))
                {
                    for (int r = 0; r < 2; r++)
                    {
                        Thread.Sleep(10);
                        mem = stlink.ReadMem32(addr + (uint)verified, readSz);
                        if (MemEq(mem, data, verified, sz)) break;
                    }
                }
                if (!MemEq(mem, data, verified, sz))
                {
                    for (int bi = 0; bi < sz; bi++)
                    {
                        if (bi >= mem.Length || mem[bi] != data[verified + bi])
                        {
                            Console.WriteLine($"\n  校验失败 @ 0x{addr + (uint)verified + (uint)bi:X8} (offset={verified + bi})");
                            int s = Math.Max(0, bi - 0);
                            var rb = new System.Text.StringBuilder();
                            var eb = new System.Text.StringBuilder();
                            for (int k = s; k < Math.Min(s + 16, sz); k++)
                            {
                                rb.Append(k < mem.Length ? mem[k].ToString("X2") : "??");
                                eb.Append(data[verified + k].ToString("X2"));
                            }
                            Console.WriteLine($"  读到: {rb}");
                            Console.WriteLine($"  期望: {eb}");
                            // 全FF判断
                            bool allFF = true;
                            for (int k = bi; k < Math.Min(bi + 16, sz); k++) if (k >= mem.Length || mem[k] != 0xFF) { allFF = false; break; }
                            Console.WriteLine(allFF ? "  诊断: 读到全FF -> 该处未写入(Flash空)" : "  诊断: 数据不符 -> 写入错乱或错位");
                            break;
                        }
                    }
                    throw new STM32Error($"校验失败 @ 0x{addr + (uint)verified:X8}");
                }
                verified += sz;
                int pct = verified * 100 / total;
                if (pct != lastPct) { Console.Write($"\r  校验: {pct}%"); lastPct = pct; }
            }
            Console.WriteLine();
            Console.WriteLine("[OK] 校验通过");
        }

        static bool MemEq(byte[] mem, byte[] data, int dataOff, int sz)
        {
            if (mem.Length < sz) return false;
            for (int i = 0; i < sz; i++) if (mem[i] != data[dataOff + i]) return false;
            return true;
        }

        public byte[] ReadFlash(uint addr, int size)
        {
            var result = new List<byte>();
            int block = 256, read = 0;
            while (read < size)
            {
                int sz = Math.Min(block, size - read);
                if (sz % 4 != 0) sz += 4 - sz % 4;
                result.AddRange(stlink.ReadMem32(addr + (uint)read, sz));
                read += sz;
            }
            return result.GetRange(0, size).ToArray();
        }

        public void ResetRun()
        {
            // 注意：部分 bootloader 只在上电复位(POR)时才跳 APP，软件复位会留在 boot；
            // 若复位后仍停在 boot，请断电重启。
            stlink.WriteReg32(AIRCR, 0x05FA0004);
            Thread.Sleep(100);
            stlink.Run();
            Console.WriteLine("[OK] 目标已复位运行");
        }

        public void FlashFirmware(string filepath, uint? address, bool verify, bool runAfter, string forceChip = null)
        {
            byte[] data;
            uint flashAddr;
            string ext = Path.GetExtension(filepath).ToLower();
            if (ext == ".hex")
            {
                var (hexAddr, d) = IntelHexParser.Parse(filepath);
                data = d;
                flashAddr = address ?? hexAddr;
            }
            else
            {
                data = File.ReadAllBytes(filepath);
                flashAddr = address ?? 0x08000000;
            }
            Console.WriteLine($"[*] 加载固件: {filepath}");
            Console.WriteLine($"  大小: {data.Length} 字节 ({data.Length / 1024.0:F1} KB)");
            Console.WriteLine($"  地址: 0x{flashAddr:X8}");
            Connect(forceChip);
            ErasePages(flashAddr, data.Length);
            WriteFlash(flashAddr, data);
            if (verify) Verify(flashAddr, data);
            if (runAfter) ResetRun();
            Console.WriteLine("\n[★] 烧录完成!");
        }

        public void Close() => stlink.Close();

        // ============ Main 入口 ============
        static int Main(string[] args)
        {
            Console.OutputEncoding = System.Text.Encoding.UTF8;
            string firmware = null, output = null;
            uint? address = null;
            int size = 256;
            bool noVerify = false, noRun = false, info = false, erase = false, read = false;
            string chip = null;
            string serial = null;
            int? device = null;
            bool listDev = false;

            for (int i = 0; i < args.Length; i++)
            {
                switch (args[i])
                {
                    case "-f": case "--file": firmware = args[++i]; break;
                    case "-a": case "--address": address = ParseUInt(args[++i]); break;
                    case "-s": case "--size": size = (int)ParseUInt(args[++i]); break;
                    case "-o": case "--output": output = args[++i]; break;
                    case "--no-verify": noVerify = true; break;
                    case "--no-run": noRun = true; break;
                    case "--chip": chip = args[++i]; break;
                    case "-i": case "--info": info = true; break;
                    case "-e": case "--erase": erase = true; break;
                    case "-r": case "--read": read = true; break;
                    case "-d": case "--device": device = (int)ParseUInt(args[++i]); break;
                    case "--serial": serial = args[++i]; break;
                    case "-l": case "--list": listDev = true; break;
                    case "-h": case "--help": PrintHelp(); return 0;
                }
            }

            // 列出所有 ST-Link 设备
            if (listDev)
            {
                using (var ctx = new UsbContext())
                {
                    var ds = STLink.ListDevices(ctx);
                    if (ds.Count == 0) Console.WriteLine("未找到 ST-Link 设备");
                    else
                    {
                        Console.WriteLine($"检测到 {ds.Count} 个 ST-Link 设备:");
                        for (int k = 0; k < ds.Count; k++)
                            Console.WriteLine($"  {k + 1}. ST-Link {ds[k].name} (PID: 0x{ds[k].pid:X4}) 序列号: {(string.IsNullOrEmpty(ds[k].serial) ? "(无)" : ds[k].serial)}");
                    }
                }
                return 0;
            }

            // 无参数：自动烧录 exe 同目录下的 firmware.hex
            bool autoMode = false;
            if (firmware == null && !info && !erase && !read)
            {
                string exeDir = AppContext.BaseDirectory;
                string autoHex = Path.Combine(exeDir, "firmware.hex");
                string autoBin = Path.Combine(exeDir, "firmware.bin");
                if (File.Exists(autoHex)) { firmware = autoHex; autoMode = true; }
                else if (File.Exists(autoBin)) { firmware = autoBin; autoMode = true; }
                if (autoMode)
                    Console.WriteLine($"[*] 自动模式: 烧录同目录固件 {Path.GetFileName(firmware)}");
            }

            var prog = new STM32Programmer(serial, device);
            int rc = 0;
            try
            {
                if (firmware != null)
                {
                    if (!File.Exists(firmware)) throw new STM32Error($"文件不存在: {firmware}");
                    prog.FlashFirmware(firmware, address, !noVerify, !noRun, chip);
                }
                else if (info) { prog.Connect(chip); }
                else if (erase) { prog.Connect(chip); prog.ErasePages(0x08000000, 0x100000); }
                else if (read)
                {
                    prog.Connect(chip);
                    uint addr = address ?? 0x08000000;
                    var data = prog.ReadFlash(addr, size);
                    if (output != null) { File.WriteAllBytes(output, data); Console.WriteLine($"[OK] 已保存到 {output}"); }
                    else
                    {
                        for (int i = 0; i < data.Length; i += 16)
                        {
                            var sb = new System.Text.StringBuilder();
                            for (int j = i; j < Math.Min(i + 16, data.Length); j++) sb.Append($"{data[j]:X2} ");
                            Console.WriteLine($"  {addr + (uint)i:X8}: {sb}");
                        }
                    }
                }
                else { PrintHelp(); }
            }
            catch (STM32Error e)
            {
                Console.Error.WriteLine($"\n[X] 错误: {e.Message}");
                rc = 1;
            }
            catch (Exception e)
            {
                Console.Error.WriteLine($"\n[X] 异常: {e.Message}");
                rc = 1;
            }
            finally { prog.Close(); }
            if (autoMode)
            {
                Console.WriteLine("\n按任意键退出...");
                try { Console.ReadKey(); } catch { }
            }
            return rc;
        }

        static uint ParseUInt(string s)
        {
            s = s.Trim();
            if (s.StartsWith("0x") || s.StartsWith("0X"))
                return Convert.ToUInt32(s.Substring(2), 16);
            return Convert.ToUInt32(s);
        }

        static void PrintHelp()
        {
            Console.WriteLine("STM32 ST-Link SWD 烧录工具 (C# 版)");
            Console.WriteLine("用法: stm32_stlink [选项]");
            Console.WriteLine("  -f, --file <路径>     烧录固件 (.hex/.bin)");
            Console.WriteLine("  -a, --address <地址>  起始地址 (默认 0x08000000)");
            Console.WriteLine("  --no-verify           跳过校验");
            Console.WriteLine("  --no-run              烧录后不复位运行");
            Console.WriteLine("  -i, --info            读取芯片信息");
            Console.WriteLine("  -e, --erase           全片擦除");
            Console.WriteLine("  -r, --read            读取Flash (-a 地址 -s 大小 [-o 文件])");
            Console.WriteLine("  -l, --list            列出所有 ST-Link 设备");
            Console.WriteLine("  -d, --device <编号>   多个ST-Link时按编号选择(从1开始)");
            Console.WriteLine("  --serial <序列号>     多个ST-Link时按序列号选择(支持部分匹配)");
            Console.WriteLine("  -h, --help            显示帮助");
            Console.WriteLine("\n示例: stm32_stlink -f firmware.hex");
        }
    }
}
