# 第三方组件与素材

项目应用源码沿用根目录 MIT 许可；下面的独立组件保留各自许可，不适用项目 MIT。

- 原始鲸鲸项目：Banan1cee/whale_gril，https://github.com/Banan1cee/whale_gril ，原 LICENSE 随仓库保留。
- 便携 Node/Python 运行时来自上游公开 runtime-3.50.zip，GitHub 附件 SHA-256：b5b6ba5fc126773a096eae888c0c89038afe4cc18f49e10bc7fd1d96df036992。Node 为 v22.14.0；Node LICENSE 随完整包 third_party/ 分发，官方源码 https://github.com/nodejs/node/tree/v22.14.0 。Python 许可及 Python 包 dist-info 中的许可随运行时保留，Python 官方 https://www.python.org/ 。
- FFmpeg/FFprobe 8.1.2 full_build，Gyan Doshi 构建，GPLv3，属于独立命令行程序。完整包 third_party/ 内附 COPYING.GPLv3、FFmpeg n8.1.2 官方源码 ZIP；构建来源和第三方库清单 https://www.gyan.dev/ffmpeg/builds/ ，对应官方源码 https://github.com/FFmpeg/FFmpeg/tree/n8.1.2 。具体二进制构建参数见 runtime/ffmpeg/BUILD_INFO.txt；外部库保留自身许可，修改和再分发时应核对完整对应源码要求。
- OCR：RapidOCR、ONNX Runtime、OpenCV、NumPy、Pillow 及模型，使用随包 dist-info/license 和 rapidocr/model 目录原有许可。模型资源没有转换为本项目自有许可。包版本清单由发行构建记录，资源文件随完整包分发。
- 项目现有鲸鲸图标在 assets/ 与 web/assets/，保留原素材来源语境；不是个人微信头像或聊天截图。

本仓库不携带运行时大文件；它们放在 Release 完整包。完整包保留第三方许可文本及源代码获取依据。
