#!/usr/bin/env python3
"""H7 单 word 编程诊断：观察写入 256-bit word 的完整过程"""
import sys, os, time, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stm32_stlink_programmer import STM32Programmer

p = STM32Programmer(); p.connect(); s = p.stlink
FB = p.flash_base  # 0x52002000
KEYR1, CR1, SR1, CCR1 = 0x04, 0x0C, 0x10, 0x14

def rd(o): return s.read_reg32(FB+o)
def wr(o,v): s.write_reg32(FB+o,v)

print(f"FB=0x{FB:08X}")
print(f"CR1=0x{rd(CR1):08X} SR1=0x{rd(SR1):08X}")

# 解锁
if rd(CR1) & 0x01:
    wr(KEYR1, 0x45670123); wr(KEYR1, 0xCDEF89AB)
print(f"解锁后 CR1=0x{rd(CR1):08X}")

# 擦除扇区0
wr(CCR1, 0x0FFF0000)
print(f"擦除前 FLASH[0]={s.read_mem32(0x08000000,32).hex()}")
wr(CR1, (1<<1) | (0<<8) | (1<<7))  # SER | SNB=0 | START
while rd(SR1) & 0x05: time.sleep(0.002)
wr(CCR1, 0x0FFF0000)
print(f"擦除后 SR1=0x{rd(SR1):08X} FLASH[0]={s.read_mem32(0x08000000,32).hex()}")

# 准备一个 word 数据
word = bytes(range(32))  # 00 01 02 ... 1F
print(f"\n待写 word: {word.hex()}")

# PG=1, PSIZE=3(64bit)
wr(CR1, (1<<1) | (3<<4))
print(f"设PG后 CR1=0x{rd(CR1):08X}")

# 方式A: 一次性 write_mem32 写32字节
print("\n=== 方式A: 一次write_mem32(32B) ===")
s.write_mem32(0x08000000, word)
t0=time.time()
while rd(SR1) & 0x05:
    if time.time()-t0>2: break
    time.sleep(0.002)
sr=rd(SR1)
print(f"SR1=0x{sr:08X}")
print(f"读回: {s.read_mem32(0x08000000,32).hex()}")
print(f"期望: {word.hex()}")
print("结果:", "✓" if s.read_mem32(0x08000000,32)==word else "✗")

# 第二个word用方式B: 8次4字节写
wr(CCR1, 0x0FFF0000)
word2 = bytes(range(32,64))
print(f"\n=== 方式B: 8x write_mem32(4B) @0x08000020 ===")
print(f"待写: {word2.hex()}")
wr(CR1, (1<<1) | (3<<4))
for i in range(0,32,4):
    s.write_mem32(0x08000020+i, word2[i:i+4])
t0=time.time()
while rd(SR1) & 0x05:
    if time.time()-t0>2: break
    time.sleep(0.002)
sr=rd(SR1)
print(f"SR1=0x{sr:08X}")
print(f"读回: {s.read_mem32(0x08000020,32).hex()}")
print(f"期望: {word2.hex()}")
print("结果:", "✓" if s.read_mem32(0x08000020,32)==word2 else "✗")

wr(CR1, 0)
p.close()
