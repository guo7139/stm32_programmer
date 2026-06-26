#!/usr/bin/env python3
"""验证 H7 正确擦除位(SER=bit2)+32bit写入"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stm32_stlink_programmer import STM32Programmer

p = STM32Programmer(); p.connect(); s = p.stlink
FB = p.flash_base
KEYR1, CR1, SR1, CCR1 = 0x04, 0x0C, 0x10, 0x14
def rd(o): return s.read_reg32(FB+o)
def wr(o,v): s.write_reg32(FB+o,v)

if rd(CR1) & 0x01:
    wr(KEYR1, 0x45670123); wr(KEYR1, 0xCDEF89AB)
print(f"解锁后 CR1=0x{rd(CR1):08X}")

# 擦除扇区0：正确位 SER=bit2 | SNB(bit8:10)=0 | PSIZE=2(bit4:5) | START=bit7
wr(CCR1, 0x0FFF0000)
PSIZE = (2 << 4)  # 32-bit
cr = (1<<2) | (0<<8) | PSIZE | (1<<7)   # SER | SNB0 | PSIZE | START
print(f"写擦除 CR1=0x{cr:08X}")
wr(CR1, cr)
t0=time.time()
while rd(SR1) & 0x05:
    if time.time()-t0>5: break
    time.sleep(0.002)
wr(CCR1, 0x0FFF0000)
fb0 = s.read_mem32(0x08000000,32)
print(f"擦除后 SR1=0x{rd(SR1):08X}")
print(f"擦除后 FLASH[0]={fb0.hex()}")
print("擦除结果:", "✓ 全FF" if fb0==b'\xFF'*32 else "✗ 未擦干净")

# 写一个word：PG=1 | PSIZE
word = bytes(range(32))
wr(CR1, (1<<1) | PSIZE)  # PG | PSIZE
print(f"\n设PG CR1=0x{rd(CR1):08X}, 待写 {word.hex()}")
s.write_mem32(0x08000000, word)
t0=time.time()
while rd(SR1) & 0x05:
    if time.time()-t0>2: break
    time.sleep(0.002)
sr=rd(SR1)
rb=s.read_mem32(0x08000000,32)
print(f"SR1=0x{sr:08X}")
print(f"读回: {rb.hex()}")
print(f"期望: {word.hex()}")
print("写入结果:", "✓ 成功" if rb==word else "✗ 失败")
wr(CR1, 0)
p.close()
