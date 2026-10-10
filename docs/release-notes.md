本版补齐普通用户可直接打开的 Windows 程序。

## 推荐下载：安装版

下载 `windows-link-manager-2.0.1-windows-x64-setup.exe`，双击安装，完成后从开始菜单打开 **Windows Link Manager**。默认安装在当前用户目录，无需管理员权限。

**不需要安装 Python，不需要运行命令，使用时不需要联网安装依赖。** 需要 Windows 10（1809 或更新版本）/Windows 11 x64。

## 便携版

下载 `windows-link-manager-2.0.1-windows-x64.zip`，解压后双击 `WindowsLinkManager.exe`。保留同目录的 `_internal` 文件夹，迁移时复制整个目录。

首次打开显示可编辑的空列表，可通过“新增”配置链接。程序支持 Junction、目录和文件符号链接、硬链接，以及项目分组、搜索、浅色/深色外观、操作预览、备份和恢复历史。

## 验证与开源

发布前运行 Windows 测试，实际编译安装器、安装到隔离目录，再运行打包后的 GUI。GUI 测试将 PATH 中的 Python 路径移除，检查界面渲染及首次启动；卸载测试检查用户配置和历史保留。

项目采用 GPL-3.0-only。`source.zip` 是开发用项目源码；Qt/PySide 的四个 `tar.xz` 附件是所含依赖的对应源码，普通用户无需下载。许可证和版权告知随程序保留。

完整用法、符号链接权限要求及恢复边界见 [README](https://github.com/yuri368/windows-link-manager#readme)。默认策略保护已有普通目标；“永久删除后创建”没有备份且无法撤销，请核对操作预览。
