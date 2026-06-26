# STM32 UART Bootloader 烧录工具

基于 ST AN3155 协议实现的串口烧录工具，可完全替代 STM32CubeProgrammer 的串口烧录功能。

## 功能特性

- ✅ 支持 .bin 和 .hex (Intel HEX) 固件格式
- ✅ UART Bootloader 协议完整实现（AN3155）
- ✅ 支持标准擦除和扩展擦除（全片/按页/按扇区）
- ✅ 支持读取芯片信息（Bootloader版本、芯片ID）
- ✅ 支持读取Flash内存
- ✅ 支持写保护/读保护 解除
- ✅ 烧录后自动校验
- ✅ 烧录完成后可自动跳转执行
- ✅ 进度条显示
- ✅ 纯 Python 实现，跨平台（Windows/Linux/macOS）

## 安装依赖

```bash
pip install pyserial
```

## 硬件连接

1. 将 STM32 的 BOOT0 引脚拉高（3.3V）
2. 连接 UART：TX→STM32_RX, RX→STM32_TX, GND→GND
3. 复位芯片（按下RESET或断电重连）
4. 芯片进入 System Bootloader 模式

## 使用方法

### 基本烧录（最常用）
```bash
# 烧录 bin 文件
python stm32_uart_programmer.py -p /dev/ttyUSB0 -f firmware.bin

# 烧录 hex 文件（自动识别地址）
python stm32_uart_programmer.py -p /dev/ttyUSB0 -f firmware.hex

# Windows
python stm32_uart_programmer.py -p COM3 -f firmware.bin
```

### 指定选项
```bash
# 指定波特率和起始地址
python stm32_uart_programmer.py -p /dev/ttyUSB0 -f firmware.bin -b 115200 -a 0x08000000

# 烧录后不跳转执行（默认会自动跳转）
python stm32_uart_programmer.py -p /dev/ttyUSB0 -f firmware.bin --no-go

# 跳过校验（加快速度）
python stm32_uart_programmer.py -p /dev/ttyUSB0 -f firmware.bin --no-verify

# 只擦除不写入
python stm32_uart_programmer.py -p /dev/ttyUSB0 -e

# 按扇区擦除（0-3扇区）
python stm32_uart_programmer.py -p /dev/ttyUSB0 -e --sectors 0 1 2 3
```

### 读取操作
```bash
# 读取芯片信息
python stm32_uart_programmer.py -p /dev/ttyUSB0 -i

# 读取Flash内容（256字节）
python stm32_uart_programmer.py -p /dev/ttyUSB0 -r -a 0x08000000 -s 256

# 读取并保存到文件
python stm32_uart_programmer.py -p /dev/ttyUSB0 -r -a 0x08000000 -s 1024 -o dump.bin
```

### 保护操作
```bash
# 解除写保护
python stm32_uart_programmer.py -p /dev/ttyUSB0 --unprotect-write

# 解除读保护
python stm32_uart_programmer.py -p /dev/ttyUSB0 --unprotect-read
```

## 支持的芯片

理论上支持所有带 UART Bootloader 的 STM32 系列：
- STM32F0xx, STM32F1xx, STM32F2xx, STM32F3xx, STM32F4xx, STM32F7xx
- STM32L0xx, STM32L1xx, STM32L4xx, STM32L5xx
- STM32G0xx, STM32G4xx
- STM32H7xx
- STM32WBxx, STM32WLxx

## 常见问题

**Q: 连接失败？**
- 确认 BOOT0=1 且芯片已复位
- 检查串口接线（TX/RX是否交叉）
- 尝试降低波特率（-b 9600）

**Q: 擦除失败？**
- 可能有写保护，先执行 `--unprotect-write`

**Q: 校验失败？**
- 检查固件文件是否完整
- 确认烧录地址正确

## 协议参考

- [AN3155](https://www.st.com/resource/en/application_note/an3155-usart-protocol-used-in-the-stm32-bootloader-stmicroelectronics.pdf) - USART protocol used in the STM32 bootloader
- [AN2606](https://www.st.com/resource/en/application_note/an2606-stm32-microcontroller-system-memory-boot-mode-stmicroelectronics.pdf) - STM32 system memory boot mode


---

# STM32 ST-Link SWD 烧录工具

基于 pyusb 直连 ST-Link，通过 SWD 协议实现固件烧录，无需 STM32CubeProgrammer。
文件：`stm32_stlink_programmer.py`

## 功能特性

- pyusb 直接与 ST-Link V2/V2-1/V3 通信，完全自主可控
- SWD 协议进入调试、halt/run、读写内存
- 自动识别芯片型号（多地址探测 DBGMCU_IDCODE）
- 支持 .bin / .hex 固件格式
- 按页/扇区擦除、写入、回读校验、复位运行
- 适配 STM32F1/F4/H7 不同的 Flash 控制器
- 进度条显示

## 安装依赖

```bash
pip install pyusb libusb-package
```

- pyusb==1.2.1
- libusb-package（Windows 上提供 libusb-1.0.dll 后端）

## Windows 驱动准备（重要）

ST-Link 默认使用 ST 官方驱动，pyusb 能枚举但无法通信，必须用 Zadig 替换为 WinUSB：

1. 下载 Zadig（https://zadig.akeo.ie/）
2. Options -> List All Devices
3. 下拉选中 STM32 STLink
4. 目标驱动选 WinUSB，点 Replace Driver
5. 完成后拔插 ST-Link（否则驱动不生效）

注意：换成 WinUSB 后，Keil/CubeIDE/CubeProgrammer 等 ST 官方工具将无法识别 ST-Link。
恢复方法：设备管理器 -> ST-Link -> 卸载设备（勾选删除驱动）-> 拔插 -> Windows 自动装回原驱动。

Linux 下用 99-stlink.rules 实现免提权：把规则文件复制到 /etc/udev/rules.d/ 后重新加载 udev 规则即可。

## 硬件连接

ST-Link 与目标板 SWD 接口对接：
- SWCLK -> SWCLK
- SWDIO -> SWDIO
- GND -> GND
- 3.3V -> 3.3V（或目标板独立供电）

## 使用方法

```bash
# 烧录并自动复位运行（默认）
python stm32_stlink_programmer.py -f firmware.hex

# 烧录 bin，指定地址
python stm32_stlink_programmer.py -f firmware.bin -a 0x08000000

# 烧录后不复位运行
python stm32_stlink_programmer.py -f firmware.hex --no-run

# 跳过校验
python stm32_stlink_programmer.py -f firmware.hex --no-verify
```

## 已验证芯片

- STM32H743VGT6（LQFP-100，ID 0x450）— 完整烧录校验通过
- STM32F1 Medium-density（ID 0x410，如 EH22G PCU 板）— 完整烧录校验通过
- STM32F76x/77x（ID 0x451）— 完整烧录校验通过
- STM32F74x/75x（0x449）、STM32F72x/73x（0x452）— 已加支持（同 F7 逻辑）
- STM32F40x/41x（0x413）、F42x/43x（0x419，含 F427/F429）、F446（0x421）、F411（0x431）等 F4 全系 — 已支持（F4 逻辑，含 2MB 双 bank）

## STM32F1 适配要点（踩坑记录）

F1（如 ID 0x410）已完整支持烧录。调通过程踩了两个隐蔽坑：

1. **Flash 编程必须用 16-bit 半字写命令**：F1 的 Flash 编程要求真正的半字（16-bit）总线写。ST-Link 通用的 8-bit(WRITEMEM_8BIT=0x0D)/32-bit(WRITEMEM_32BIT=0x08) 写内存命令都**无法触发** F1 的 Flash 编程——表现为“设了 PG 位、SR 显示 EOP、但内容仍是全 FF”。必须使用 **WRITEMEM_16BIT=0x48** 命令逐半字写入。注意 0x47 是 16-bit 读命令，发数据当写命令用会污染端点导致后续超时。
2. **F1 解锁的 LOCK 位是 bit7（不是 F4/F7 的 bit31）**：之前 flash_unlock 的 else 分支按 F4/F7 写（检查 bit31），F1 复位后 CR=0x80（bit31=0）条件不成立，导致 **F1 从未真正解锁**。Flash 锁定时擦除/编程命令被硬件静默忽略且不报错，于是出现“擦除/写入都显示 100%，校验却读到残留旧数据”。必须为 F1 单独判断 LOCK=bit7 并写 KEYR 解锁。
3. 编程流程：解锁 → 设 PG(bit0) → 逐半字 write_mem16，每个半字后等 BSY(bit0) 清零并检查 PGERR(bit2)/WRPRTERR(bit4) → 清 PG 位。
4. 警示：“校验通过”不等于“写入成功”——若目标芯片之前被官方工具烧过相同固件，未真正写入时校验会被旧数据蒙混通过。务必先确认擦除后读回是全 FF。


## STM32H7 适配要点（踩坑记录）

H7 的 Flash 控制器与 F1/F4 完全不同，移植时注意：

| 项目 | F1/F0 | F4 | H7 |
|------|-------|-----|--------|
| Flash 基址 | 0x40022000 | 0x40023C00 | 0x52002000 |
| 编程单位 | 16-bit 半字 | 32-bit | 256-bit (32字节) flash word |
| 擦除单位 | 页(1~2KB) | 扇区(不等) | 128KB 扇区 |
| DBGMCU_IDCODE | 0xE0042000 | 0xE0042000 | 0x5C001000 |

关键寄存器（H7 Bank1）：
- FLASH_KEYR1 = base + 0x04（解锁 KEY1=0x45670123, KEY2=0xCDEF89AB）
- FLASH_CR1   = base + 0x0C（bit0=LOCK, bit1=PG, bit2=SER, bit7=START, bit4:5=PSIZE, bit8:10=SNB）
- FLASH_SR1   = base + 0x10（bit0=BSY, bit2=QW, bit16=WRPERR, bit17=PGSERR, bit18=STRBERR, bit19=INCERR）
- FLASH_CCR1  = base + 0x14（写 1 清除对应错误标志）

坑点：

1. 每次必须写满 256-bit（32字节）flash word，不足用 0xFF 补齐，否则触发 INCERR。
2. **PSIZE=2（32-bit），与官方工具一致**。编程前设 CR1 = PG | (2<<4)。曾误用 PSIZE=3(64-bit)，在 2.1V 供电下会触发 ECC 错误导致写入失败。
3. **扇区擦除必须用 SER=bit2（不是 bit1）**：H7 CR1 的 bit1 是 PG、bit2 才是 SER。曾误把 (1<<1) 当 SER，等于设了 PG 而非扇区擦除请求，导致**擦除根本没生效**（读回仍是旧数据）。往未擦净的 flash word 上写会触发 DBECCERR(bit25)。擦除请求 CR1 = SER(bit2) | (SNB<<8) | PSIZE | START(bit7)。
4. WRPERR(bit16) 误报：通过 ST-Link 调试器编程时，擦除和编程操作都会误置 WRPERR，但数据实际写入正确（已逐字节验证）。因此 H7 只对致命错误（PGSERR/STRBERR/INCERR）报错，忽略 WRPERR 和 ECC 错误位，每次操作后清 CCR1。
5. 校验时序：写入完成锁定 Flash 后，校验前要重新 halt CPU + 短延时让 Flash 状态稳定，否则首个 word 可能读到脏数据。读取失败自动重试。
6. STRBERR(bit18 写突发错误) 偶发：老款 ST-Link V2 固件（如 JTAG v46）写 256-bit word 时，会偶发把单次 word 写入拆成多次总线访问，触发 STRBERR，表现为烧录到中途（如 86%）随机报错。解决方案：每个 word 写入后立即回读比对，不符就清错误标志、重设 PG 位后重写该 word，最多重试 4 次。等于把校验前移到写入环节，既容错又保证数据完整。

## STM32F7 适配要点（踩坑记录）

F7 的 Flash 寄存器与 F4 相同（基址 0x40023C00），仅扇区布局不同。但 F7 通过老款 ST-Link 编程时有几个极隐蔽的坑，调试了很久：

F7 扇区布局：4×32KB + 1×128KB + 7×256KB（单 bank，最大 2MB）。

关键寄存器（基址 0x40023C00）：
- FLASH_ACR  = +0x00
- FLASH_KEYR = +0x04（KEY1=0x45670123, KEY2=0xCDEF89AB）
- FLASH_SR   = +0x0C（**bit16=BSY**, bit1=OPERR, bit4=WRPERR, bit5=PGAERR, bit6=PGPERR, bit7=ERSERR）
- FLASH_CR   = +0x10（bit0=PG, bit1=SER, bit3:6=SNB, bit8:9=PSIZE, bit16=STRT, **bit31=LOCK**）
- FLASH_OPTCR= +0x14（bit8:15=RDP）

坑点（按踩坑顺序）：

1. **FLASH_SR 的 BSY 是 bit16，不是 bit0**。F1 的 BSY 在 bit0，照搬到 F4/F7 会导致 flash_wait 根本没等待操作完成（bit0 在 F7 是 EOP），写入立即报 PGPERR/ERSERR。

2. **FLASH_CR 的 LOCK 是 bit31**（F1 是 bit7）。flash_lock 写错位无害但不规范。

3. **FLASH_KEYR 解锁状态机极度敏感（最大的坑）**：必须在「连接（含 NRST 复位 halt + debug 域上电）后第一时间」解锁，解锁动作前面绝不能有任何对 FLASH_CR 的读写操作，否则状态机被污染，KEYR 写入永久无效（CR 的 LOCK bit31 恒为 1，解不开）。验证方法：OPTKEYR（option 解锁，机制相同）能成功解锁，证明 KEY 写入通道正常，纯粹是 FLASH_KEYR 的时序要求。这解释了为什么诊断脚本里做了一堆前置读写后再解锁总是失败，而「复位后立即解锁」一次就成。

4. **PSIZE 固定用 32-bit（PSIZE=2）**，与官方工具一致。ST-Link 测量的目标电压读数常偏低（实测显示 2.10V 但官方 32-bit 照样烧成功），不可据此降级并行度。注意 ST-Link 写内存的总线宽度必须与 PSIZE 宽度匹配，否则 PGPERR——所以低电压若真要 8-bit，需用 WRITEMEM_8BIT(0x0D) 命令，但实践中直接 32-bit 即可。

5. **擦除用标准两步法**：先写 CR=SER|SNB|PSIZE，再单独写 CR 置 STRT(bit16)。擦除和写入前都先清 SR 残留错误标志（写 0xF2）。

6. **debug 电源域必须上电**：见下方 ST-Link 通信要点第 5 条。这是 F7（及所有 M7 核）能读写内存的前提，否则 AHB-AP 访问全返回 0。


## STM32F4 适配要点

F4 与 F7 的 Flash 寄存器布局相同（基址 0x40023C00，BSY=bit16，LOCK=bit31，编程逻辑一致），共用同一套擦除/写入/解锁代码。差异仅在扇区布局：

- **单 bank（≤1MB，如 F405/F407/F415/F417/F446/F411）**：4×16KB + 1×64KB + 7×128KB = 12 扇区
- **双 bank（2MB，如 F427/F429/F437/F439）**：上述布局 ×2 = 24 扇区

双 bank 的坑点：
- **bank2 的扇区号 SNB 编码从 16 开始**，不是连续的 12-23。即扇区 12→SNB16、扇区 13→SNB17 …… 扇区 23→SNB27（SNB[4] 位用于选 bank）。代码里 `snb = i if i < 12 else (i - 12 + 16)` 处理。
- 烧录 ≤1MB 的固件只会用到前 12 个扇区，双 bank 编码不影响；超过 1MB 才进入 bank2。

PSIZE 同 F7 固定 32-bit。擦除用两步法（先配 SER+SNB+PSIZE 再单独置 STRT），擦除/写入前清 SR。

> 注：F427 等 2MB 双 bank 的代码已实现但尚未在真实硬件上验证（H743、F76x/77x 已实测通过）。STM32F1 不在此列，见上方“当前限制”。


## ST-Link 通信要点

1. 进入 SWD：ST-Link V2 上电默认 DFU 模式（mode=0），需先发 DFU_EXIT（[0xF3,0x07]，不读响应），等待后再发 APIV2 进入命令 [0xF2, 0x30, 0xA3]（JTAG >= v22 用 0x30，旧版 0x20）。
2. 不要调用 set_configuration()：Windows WinUSB 下会重置端点状态导致超时。
3. 显式 claim_interface(dev, 0)：Windows WinUSB 必需。
4. _cmd() 始终读 64 字节（端点 MaxPacketSize）后截取，避免残留数据影响后续命令。
5. **debug 电源域上电（M7/F7 关键）**：进入 SWD 后，AHB-AP 访问内存前必须给 debug 域上电，否则读 CPUID/Flash 全返回 0。方法：用 WRITE_DAP_REG（命令 0x46）写 DP CTRL/STAT（DP 寄存器地址 0x04）置位 CDBGPWRUPREQ(bit28)|CSYSPWRUPREQ(bit30)，读回确认 CDBGPWRUPACK(bit29)=1。可用 READ_DAP_REG（0x45）读 DP/AP 寄存器，ap=0xFFFF 表示 DP。
6. **connect-under-reset（抢占运行中固件）**：简单 enter_swd 失败或读不到芯片时，用硬件 NRST 复位：DRIVE_NRST 命令 [0xF2, 0x3C, level]（level 0=低/复位, 1=高/释放），拉低→等待→释放→趁芯片刚复位立即重新 enter_swd + debug 上电 + halt。这样能停在复位向量、读到芯片 ID。
7. **CoreID 可读但内存全 0**：说明 SWD 链路通（DP 层 OK）但 debug 域没上电（见第 5 条）。读 CoreID 用 [0xF2, 0x22]，正常返回如 0x5BA02477（M7）。
8. **芯片 ID 读取**：halt 后多地址轮询 + 重试（DBGMCU_IDCODE 在 0xE0042000，H7 在 0x5C001000）。读不到时可用 --chip 参数手动指定系列兜底。

## 协议参考

- ST-Link USB 协议（参考 stlink-org/stlink、OpenOCD 源码）
- STM32H743 参考手册 RM0433（Embedded Flash memory 章节）
