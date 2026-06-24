#!/usr/bin/env python3
"""F1擦除诊断: 确认CPU halt状态 + 擦除是否真生效"""
import sys, os, time, struct
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from stm32_stlink_programmer import STM32Programmer

DHCSR=0xE000EDF0
FB=0x40022000  # F1 FLASH base
SR=0x0C; CR=0x10

p = STM32Programmer()
p.connect()
s = p.stlink

print(f"\n=== 连接后状态 ===")
d = s.read_reg32(DHCSR)
print(f"  DHCSR=0x{d:08X} S_HALT(bit17)={(d>>17)&1}")
print(f"  Flash[0]={s.read_mem32(0x08000000,4).hex()}")
print(f"  FLASH_CR=0x{s.read_reg32(FB+CR):08X} (bit7=LOCK)")

print(f"\n=== 再次强制halt ===")
s.write_reg32(DHCSR, 0xA05F0003)
time.sleep(0.05)
d = s.read_reg32(DHCSR)
print(f"  DHCSR=0x{d:08X} S_HALT={(d>>17)&1}")

print(f"\n=== 手动擦除第0页并立即读回 ===")
# 解锁
s.write_reg32(FB+0x04, 0x45670123)
s.write_reg32(FB+0x04, 0xCDEF89AB)
cr = s.read_reg32(FB+CR)
print(f"  解锁后 CR=0x{cr:08X} LOCK(bit7)={(cr>>7)&1}")
# 擦除第0页
s.write_reg32(FB+CR, 0x02)         # PER
s.write_reg32(FB+0x14, 0x08000000) # AR=page addr
s.write_reg32(FB+CR, 0x42)         # PER+STRT
# 等BSY(bit0)
t0=time.time()
while s.read_reg32(FB+SR) & 0x01:
    if time.time()-t0>5: 
        print("  擦除超时!")
        break
sr = s.read_reg32(FB+SR)
print(f"  擦除后 SR=0x{sr:08X}")
m = s.read_mem32(0x08000000, 16)
print(f"  擦除后读Flash[0]: {m.hex()}")
if all(b==0xFF for b in m):
    print("  [OK] 擦除生效(全FF)")
else:
    print("  [!] 擦除未生效! CPU可能在运行干扰")

p.close()
