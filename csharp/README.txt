STM32网络固件烧录工具（C# / WinForms / .NET 6）

启动：双击 publish\STM32网络固件烧录工具.exe
服务器：http://192.168.60.241:9100
配置：EXE同目录 stm32_programmer_config.json

配置文件只保存非敏感信息：
username、model_code、part_no、purpose、program、status、aircraft_no、eo_no。
绝不保存密码、api_token、用户ID或权限内容。
登录成功后保存用户名；固件查询成功后保存当前选择；下次启动按业务值自动恢复。

主要功能：
1. 每次启动强制登录，api_token仅保存在进程内存。
2. 按api_limit.program/status过滤类型与状态。
3. 类型支持BootLoader/App/Parameter；状态1显示“局方批准”。
4. api_limit.after_sales严格为true时显示航空器编号和EO单号。
5. 无芯片选项时隐藏芯片标签和下拉框，并自动重排界面。
6. 网络固件下载、实际字节数与MD5严格校验。
7. ST-Link连接、擦除、写入、校验、复位运行；失败可在过程窗重试。
8. 全片擦除及失败重试。
9. 每次服务器固件烧录尝试上报烧录记录。

命令行自检：
publish\STM32网络固件烧录工具.exe --self-test
退出码0表示程序集、基础权限模型和配置读写安全测试正常。
