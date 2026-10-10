# Third-party notices

Windows Link Manager 的项目代码按 `GPL-3.0-only` 发布。本声明不改变第三方组件各自的许可及版权。

## 发布范围

仓库及源码 ZIP 包含本项目源码、测试、启动器、配置示例和本项目界面截图。源码包不包含 `.venv`、Python 运行时、Qt DLL、Qt 插件、第三方 wheel 或系统字体。启动器在使用者本机通过 pip 安装 `requirements.txt` 声明的依赖。

## Python 与 PySide6

| 组件 | 当前版本 | 授权与来源 |
| --- | --- | --- |
| Python | 支持 3.11–3.14 | 标准库随使用者的 Python 安装提供；[Python 许可](https://docs.python.org/3/license.html)。 |
| PySide6 | 6.11.2 | [Qt for Python](https://doc.qt.io/qtforpython-6/)；[PyPI 发布页](https://pypi.org/project/PySide6/6.11.2/)。 |
| PySide6 Essentials / Addons | 6.11.2 | PySide6 的间接依赖，包含 Qt 库及绑定；[Qt for Python 许可证](https://doc.qt.io/qtforpython-6/licenses.html)。 |
| Shiboken6 | 6.11.2 | PySide6 的间接依赖；[Shiboken 文档](https://doc.qt.io/qtforpython-6/shiboken6/)。 |

上述 PySide6 和 Shiboken6 包的许可元数据声明为 `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`。本项目使用这些组件提供的 GPL v3 相容开源授权路线；不宣称获得商业许可，也不把这些组件重新许可为本项目代码。

程序使用 Qt Core、Qt GUI 和 Qt Widgets。Qt 内部还包含有独立许可证的第三方组件，具体声明以所安装版本的 Qt for Python/Qt 分发包和[官方许可证清单](https://doc.qt.io/qt-6/licenses.html)为准。相关源码可从 [Qt 官方源码下载](https://download.qt.io/official_releases/QtForPython/pyside6/)及 [Qt 源码仓库](https://code.qt.io/)获取。

如另行分发包含 Python、PySide6 或 Qt 的二进制程序，须按实际包含的组件保留许可证、版权告知并满足对应源码等义务。当前源码 ZIP 不包含此类二进制分发。

## 字体与界面素材

程序读取 Windows 已安装的系统字体；源码包中没有字体文件。`docs/images` 为本项目自身界面的演示截图，使用通用示例路径。程序未捆绑第三方产品的截图、商标或图标资产。

## AI 辅助开发

维护者提出需求并决定功能与发布范围，使用 OpenAI Codex 辅助实现、重构和测试。此说明不主张将第三方作品的权利转为本项目的权利，也不主张所有 AI 辅助输出均具有独占版权。
