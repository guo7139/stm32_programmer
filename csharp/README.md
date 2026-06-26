# STM32 ST-Link SWD 烧录工具 — C# 版

Python 版 `stm32_stlink_programmer.py` 的 C# 移植，功能完全一致，编译为 Windows exe，无需 Python 环境。

## 文件说明

| 文件 | 说明 |
|------|------|
| Program.cs | IntelHexParser + STLink USB 通信层 |
| Program2.cs | STM32Programmer (Flash 编程/擦除/校验) + Main 入口 |
| stm32cs.csproj | 工程文件（依赖 LibUsbDotNet 3.0.102-alpha） |
| stm32_stlink.exe | 单文件自包含版（约 61MB，无需装 .NET 运行时，开箱即用） |
| stm32_stlink_trim.exe | 裁剪版（约 11MB，体积小，同样自包含开箱即用，推荐） |

## 运行环境

- Windows x64
- ST-Link 需用 Zadig 替换为 WinUSB 驱动（与 Python 版一致，见上级目录 README）
- 两个 exe 都是自包含，目标机**无需安装 .NET 运行时**

## 已验证芯片

- STM32H743VGT6（ID 0x450）— 完整烧录校验通过
- STM32F76x/77x（ID 0x451）— 完整烧录校验通过
- F74x/75x(0x449)、F72x/73x(0x452) — 已支持
- F4 全系：F40x/41x(0x413)、F42x/43x(0x419 含 F427/F429)、F446(0x421)、F411(0x431) 等 — 已支持
  - 含 2MB 双 bank（F427/F429/F437/F439），bank2 扇区 SNB 从 16 起编码

## 当前限制

- **STM32F1 当前不支持通过本 C# / Python ST-Link 直写方案稳定烧录。**
  - 原因是 F1 Flash 编程要求真正的 16-bit 半字写，而当前 ST-Link V2 通用 USB 写内存路径不具备稳定的半字编程能力。
  - 后续若要支持，需要单独实现 F1 专用 SRAM Flash Loader。

## 双击自动模式（推荐给非命令行用户）

把 exe 和 `firmware.hex`（或 `firmware.bin`）放在**同一目录**，直接双击 exe 即可：
- 程序自动查找同目录的 firmware.hex / firmware.bin 并烧录
- 烧录完成后暂停显示"按任意键退出"，方便看结果（双击不会一闪而过）

无需任何命令行参数。

## 使用方法

```cmd
stm32_stlink.exe -f firmware.hex            :: 烧录并自动复位运行
stm32_stlink.exe -f firmware.bin -a 0x08000000
stm32_stlink.exe -f firmware.hex --no-verify :: 跳过校验
stm32_stlink.exe -f firmware.hex --no-run    :: 烧录后不运行
stm32_stlink.exe -i                          :: 读取芯片信息
stm32_stlink.exe -e                          :: 全片擦除
stm32_stlink.exe -r -a 0x08000000 -s 256     :: 读Flash
stm32_stlink.exe -f fw.hex --chip f7         :: 手动指定芯片系列(ID读不到时)
```

`--chip` 可选值：h7 / f7 / f4 / f1 / f0。仅当芯片 ID 自动识别失败时用作兜底。

## 重新编译

需要 .NET 6 SDK。NuGet 官方源若不通，用华为云镜像（已配在 NuGet.config）。

```cmd
:: 自包含单文件版（61MB）
dotnet publish -c Release -r win-x64 --self-contained true ^
  -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true

:: 裁剪版（11MB，推荐）
dotnet publish -c Release -r win-x64 --self-contained true ^
  -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true ^
  -p:PublishTrimmed=true -p:TrimMode=link -o publish_trim
```

NuGet.config 内容（华为云镜像）：
```xml
<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <packageSources>
    <clear />
    <add key="huawei" value="https://mirrors.huaweicloud.com/repository/nuget/v3/index.json" />
  </packageSources>
</configuration>
```

## 移植说明

- USB 通信用 LibUsbDotNet 3.x（对应 Python 的 pyusb）
- 端点固定 EP_OUT=0x02 (V2) / 0x01 (V2-1/V3)，EP_IN=0x81
- **不调用 SetConfiguration**（Windows WinUSB 下会重置端点导致超时），仅 ClaimInterface(0)
- H7 per-word 回读重试、WRPERR/STRBERR 处理、Flash 寄存器映射均与 Python 版逐行对应
- 所有 STM32H7 踩坑修复（见上级目录 README）已完整移植
- 所有 STM32F7 踩坑修复已完整移植，与 Python 版逐行对应：
  - EnterSwd 集成 NRST 硬件复位（DriveNrst）+ debug 电源域上电（PowerUpDebug 写 DP CTRL/STAT）
  - FlashWait 区分 F4/F7（BSY=bit16）与 F1（BSY=bit0）
  - FLASH_KEYR 连接后第一时间解锁（F7 状态机敏感）
  - FlashLock 区分 F4/F7（LOCK=bit31）与 F1（LOCK=bit7）
  - F7 擦除两步法 + 擦除/写入前清 SR
  - 芯片 ID 多地址轮询重试 + --chip 手动兜底
  - F4 与 F7 共用擦除/写入/解锁逻辑（寄存器布局相同），EraseSectorsF4 支持 2MB 双 bank + 正确 SNB 编码
