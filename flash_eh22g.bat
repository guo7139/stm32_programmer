@echo off
REM ============================================================
REM  EH22G PCU 板 (STM32F1) BOOT+APP 一键烧录
REM  BOOT -> 0x08000000   APP -> 0x08002000
REM  自动查找当前目录下的 *BOOT*.hex 和 *APP*.bin
REM ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo ============================================================
echo   EH22G PCU 一键烧录 (BOOT + APP)
echo ============================================================
echo.

REM --- 查找 BOOT 固件 (*BOOT*.hex) ---
set "BOOT_FILE="
for %%f in (*BOOT*.hex) do set "BOOT_FILE=%%f"
if "!BOOT_FILE!"=="" (
    echo [X] 未找到 BOOT 固件 ^(*BOOT*.hex^)
    goto :end
)

REM --- 查找 APP 固件 (*APP*.bin) ---
set "APP_FILE="
for %%f in (*APP*.bin) do set "APP_FILE=%%f"
if "!APP_FILE!"=="" (
    echo [X] 未找到 APP 固件 ^(*APP*.bin^)
    goto :end
)

echo 找到固件:
echo   BOOT: !BOOT_FILE!  -^> 0x08000000
echo   APP : !APP_FILE!  -^> 0x08002000
echo.

REM --- 第1步: 烧录 BOOT ---
echo [步骤 1/2] 烧录 BOOT 到 0x08000000 ...
echo ------------------------------------------------------------
python stm32_stlink_programmer.py -f "!BOOT_FILE!"
if errorlevel 1 (
    echo.
    echo [X] BOOT 烧录失败, 终止.
    goto :end
)
echo.

REM --- 第2步: 烧录 APP ---
echo [步骤 2/2] 烧录 APP 到 0x08002000 ...
echo ------------------------------------------------------------
python stm32_stlink_programmer.py -f "!APP_FILE!" -a 0x08002000
if errorlevel 1 (
    echo.
    echo [X] APP 烧录失败.
    goto :end
)

echo.
echo ============================================================
echo   [★] BOOT + APP 全部烧录完成!
echo ============================================================

:end
echo.
pause
