#!/usr/bin/env python3
"""干净测试 ST-Link WRITEMEM_16BIT(0x48) 对 F1 半字编程是否有效"""
import sys, os, time, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stm32_stlink_programmer import STM32Programmer, CMD_DEBUG

FB = 0x40022000
KEYR=0x04; SR=0x0C; CR=0x10; AR=0x14
WRITEMEM_16BIT = 0x48

p = STM32Programmer(); p.connect(); s = p.stlink

def rd(o): return s.read_reg32(FB+o)
def wr(o,v): s.write_reg32(FB+o,v)

def write16(addr, half):  # half: 2字节
    cmd = bytearray(16)
    cmd[0] = CMD_DEBUG
    cmd[1] = WRITEMEM_16BIT
    struct.pack_into('<I', cmd, 2, addr)
    struct.pack_into('<H', cmd, 6, 2)   # 长度=2字节
    s.dev.write(s._ep_out, cmd, timeout=1000)
    s.dev.write(s._ep_out, half, timeout=1000)

def flash_wait():
    for _ in range(1000):
        if not (rd(SR) & 0x01): return
        time.sleep(0.0005)

# 擦除页0
wr(KEYR, 0x45670123); wr(KEYR, 0xCDEF89AB)
wr(CR, 0x02); wr(AR, 0x08000000); wr(CR, 0x42)
flash_wait(); wr(CR, 0)
print('擦除后:', s.read_mem32(0x08000000, 8).hex())

# 用 0x48 逐半字写 8 字节: 1234 5678 9ABC DEF0
data = bytes.fromhex('341278569abcf0de')
try:
    wr(CR, 0x01)  # PG
    for i in range(0, len(data), 2):
        write16(0x08000000 + i, data[i:i+2])
        flash_wait()
    wr(CR, 0)
    print('SR=0x%08X' % rd(SR))
    print('读回:', s.read_mem32(0x08000000, 8).hex())
    print('期望:', data.hex())
    print('结果:', '✓ 成功' if s.read_mem32(0x08000000, 8) == data else '✗ 失败')
except Exception as e:
    print('0x48 命令异常(可能此固件不支持16BIT):', e)
p.close()
