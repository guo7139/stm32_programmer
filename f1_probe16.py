#!/usr/bin/env python3
"""探测F1半字编程所需的ST-Link写命令码"""
import sys, os, time, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stm32_stlink_programmer import STM32Programmer, CMD_DEBUG

FB = 0x40022000
KEYR=0x04; SR=0x0C; CR=0x10; AR=0x14
p = STM32Programmer(); p.connect(); s = p.stlink

def rd(o): return s.read_reg32(FB+o)
def wr(o,v): s.write_reg32(FB+o,v)
def erase0():
    wr(KEYR, 0x45670123); wr(KEYR, 0xCDEF89AB)
    wr(CR, 0x02); wr(AR, 0x08000000); wr(CR, 0x42)
    while rd(SR) & 0x01: time.sleep(0.001)
    wr(CR, 0)

def try_cmd(cmd1, payload=b'\xA5\x5A'):
    erase0()
    wr(CR, 0x01)  # PG
    cmd = bytearray(16)
    cmd[0] = CMD_DEBUG
    cmd[1] = cmd1
    struct.pack_into('<I', cmd, 2, 0x08000000)
    struct.pack_into('<H', cmd, 6, len(payload))
    try:
        s.dev.write(s._ep_out, cmd, timeout=1000)
        s.dev.write(s._ep_out, payload, timeout=1000)
        time.sleep(0.02)
        sr = rd(SR)
        mem = s.read_mem32(0x08000000, 4)
        return True, sr, mem.hex()
    except Exception as e:
        return False, str(e), ''
    finally:
        wr(CR, 0)

print('初始:', s.read_mem32(0x08000000, 4).hex())
for c in [0x08, 0x0D, 0x47, 0x48, 0x0A, 0x0B]:
    ok, a, b = try_cmd(c)
    if ok:
        print(f'cmd=0x{c:02X} SR=0x{a:08X} mem={b}')
    else:
        print(f'cmd=0x{c:02X} EXC={a}')
p.close()
