# Third-party notices

Windows Link Manager 的项目代码按 `GPL-3.0-only` 发布。本声明不改变第三方组件各自的许可及版权。

## 发布范围

仓库及项目源码 ZIP 包含本项目源码、测试、启动器、配置示例、打包脚本和本项目界面截图。项目源码包不包含 `.venv`、Python 运行时、Qt DLL、第三方 wheel 或系统字体。

Windows 安装器及便携 ZIP 包含运行本程序所需的 Python、PySide6、Shiboken6、Qt 库和 Qt 插件。程序文件中保留项目 `LICENSE`、本声明，以及 `licenses` 下的 Python 许可、上游版权与许可证文本。源码运行所需的依赖由启动器安装；二进制版本无需联网安装依赖。

## Python 与 PySide6

| 组件 | 当前版本 | 授权与来源 |
| --- | --- | --- |
| Python | 源码支持 3.11–3.14；二进制构建使用 3.13 | [Python 许可](https://docs.python.org/3/license.html)；二进制包保留实际构建运行时的 `LICENSE.txt`。 |
| PySide6 | 6.11.2 | [Qt for Python](https://doc.qt.io/qtforpython-6/)；[PyPI 发布页](https://pypi.org/project/PySide6/6.11.2/)。 |
| PySide6 Essentials / Addons | 6.11.2 | PySide6 的间接依赖，包含 Qt 库及绑定；[Qt for Python 许可证](https://doc.qt.io/qtforpython-6/licenses.html)。 |
| Shiboken6 | 6.11.2 | PySide6 的间接依赖；[Shiboken 文档](https://doc.qt.io/qtforpython-6/shiboken6/)。 |

上述 PySide6 和 Shiboken6 包的许可元数据声明为 `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`。本项目使用这些组件提供的 GPL v3 相容开源授权路线；不宣称获得商业许可，也不把这些组件重新许可为本项目代码。

程序使用 Qt Core、Qt GUI 和 Qt Widgets。Qt 内部还包含有独立许可证的第三方组件，具体声明以所安装版本的 Qt for Python/Qt 分发包和[官方许可证清单](https://doc.qt.io/qt-6/licenses.html)为准。相关源码可从 [Qt 官方源码下载](https://download.qt.io/official_releases/QtForPython/pyside6/)及 [Qt 源码仓库](https://code.qt.io/)获取。

## 二进制包的依赖源码

本项目在同一 [GitHub Release](https://github.com/yuri368/windows-link-manager/releases) 提供项目源码和以下 **6.11.2** 官方上游源码附件，供获取源码及重建使用：

- `qtbase-everywhere-src-6.11.2.tar.xz`：Qt Core、GUI、Widgets 及其所含插件和第三方组件。
- `qtsvg-everywhere-src-6.11.2.tar.xz`：SVG 插件所依赖的 Qt SVG。
- `qttranslations-everywhere-src-6.11.2.tar.xz`：Qt 翻译资源。
- `pyside-setup-everywhere-src-6.11.2.tar.xz`：PySide6、Shiboken6 的源码和构建说明。

上述 Qt/PySide 组件采用其提供的 GPL v3 兼容开源授权。本项目未修改这些上游源码。Qt/PySide 包中可能同时附有商业许可选项的文本；保留该文本不代表本项目取得或使用商业授权。Qt 内部第三方组件各自保留原有许可及版权，随程序保留其声明和所声明的许可证文件。

二进制采用目录分发，Qt DLL 位于 `_internal` 下。获取本项目源码后，可按 README 和发布构建脚本修改、重新构建。Qt/PySide 的上游构建说明分别见 [Qt Windows 构建](https://doc.qt.io/qt-6/windows-building.html)和 [Qt for Python 构建](https://doc.qt.io/qtforpython-6/building_from_source/index.html)。

PyInstaller 6.22.3 用于封装运行环境，遵循其 [GPL 2.0 附分发例外](https://pyinstaller.org/en/stable/license.html)；安装器由 Inno Setup 6 生成，遵循其 [许可](https://jrsoftware.org/files/is/license.txt)。这些打包工具不改变本项目及所含依赖的许可证。

## 字体与界面素材

程序读取 Windows 已安装的系统字体；源码包中没有字体文件。`docs/images` 为本项目自身界面的演示截图，使用通用示例路径。程序未捆绑第三方产品的截图、商标或图标资产。

## AI 辅助开发

维护者提出需求并决定功能与发布范围，使用 OpenAI Codex 辅助实现、重构和测试。此说明不主张将第三方作品的权利转为本项目的权利，也不主张所有 AI 辅助输出均具有独占版权。
