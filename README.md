<br>
<br>
<br>
<br>
<p align="center">
  <img src="./data/uk._92li.cftm.CloudflaredTunnelManager.svg" alt="Logo" width="96" height="96"/>
</p>

<h1 align="center">Cloudflared-Helper-Desktop</h1>

<p align="center">
一个基于 Python + GTK4 的现代化桌面应用，用于管理多条 Cloudflare Tunnel 客户端连接，</p>
<p align="center"><a href="https://github.com/lingyicute/Cloudflared-Helper">Cloudflared-Helper</a> 的 GUI 继任者。
</p>
<br>
<br>
<p align="center">
  <a href="https://chd.92li.uk/">🌐 Official Website</a> •
  <a href="https://github.com/lingyicute/Cloudflared-Helper-Desktop/releases">📦 Download</a> •
  <a href="https://github.com/lingyicute/Cloudflared-Helper-Desktop/issues">🐛 Report Bug</a>
</p>
<br>
<br>
<br>

## 功能

- **多隧道并发**：保存任意数量的隧道，可同时连接多条；每条隧道独立的状态指示、启停按钮与实时日志。
- **剪贴板快速连接**：窗口获得焦点时读取剪贴板，发现 `*.trycloudflare.com` 快速隧道链接就弹窗询问；点「是」直接唤出新建隧道对话框，主机名和名称（`yyyymmdd hh:mm:ss 快速隧道`）都已填好，**只需要填一个本地端口号**，回车即可保存。
- **cloudflared 版本管理**
  - 自动识别系统/架构（`amd64` / `arm64` / `arm` / `386`，Linux 与 macOS）
  - 从 GitHub Releases 拉取版本列表，一键下载安装，显示下载进度，可取消
  - 多版本并存，随时在“最新已安装 / 系统 PATH / 指定版本”之间切换
  - 自动 `chmod +x`，探测并展示当前生效的版本
- **智能校验**：端口范围检查、低于 1024 端口的权限提示、本地端口冲突检测。
- **贴心细节**：程序启动时自动连接、复制等效命令行、失败时的日志快捷跳转、退出前的断开确认。

<br>

## 为什么没有「协议类型」选项

`cloudflared access` 的 `ssh` / `rdp` / `smb` 三个子命令并不是不同的协议实现，它们只是 `tcp` 的
[别名](https://github.com/cloudflare/cloudflared/blob/master/cmd/cloudflared/access/cmd.go)：四个子命令共用同一个 Action 和同一组 flag，源码中没有任何地方读取子命令名；`ssh://`、`rdp://`、`smb://` 这类 scheme
也只用来推导缺省的 `--url`，对转发行为没有任何影响。

因此本应用没有「协议类型」设置，统一执行：

```bash
cloudflared access tcp --hostname <隧道主机名> --url <监听地址>:<端口>
```

> [!Note]
> `access tcp` 这个子命令名从 cloudflared `2020.6.1` 起就有了（`2020.5.1` 及更早只有 `access ssh`，
> 当时还没有 `tcp`）。应用内下载的版本来自官方 Releases 的最新 30 个，全部满足；只有手动把二进制指向
> 2020 年之前的系统 PATH cloudflared 时，才需要改用 `access ssh`。然而，Cloudflare 官方仅支持一年以内的 cloudflared 版本，出于安全性上的考虑，梨不建议您使用如此古老的 cloudflared。

<br>

## 剪贴板快速连接

从别处复制一条 `cloudflared tunnel --url ...` 输出的快速隧道链接，切回本应用窗口即可：

1. 窗口获得焦点后读取剪贴板，识别出 `*.trycloudflare.com` 链接就弹出「是否要连接这个快速隧道？」，链接居中展示。
2. 点「是」唤出新建隧道对话框：主机名填好链接的主机名，名称也填好了 `yyyymmdd hh:mm:ss 快速隧道`，光标直接停在端口框上（默认值已全选）。
3. 敲入本地端口号，回车即保存 —— 全程不用碰鼠标。

几条刻意的约束，都是为了不招人烦：

- **只认「整条剪贴板就是一个链接」**：允许协议头、端口、路径和一层包裹的引号 / 尖括号，但不会在一大段聊天记录里搜链接；`x.trycloudflare.com.evil.com` 这类后缀伪装也不会命中。
- **同一条链接一个会话只问一次**：点过「否」之后再来回切窗口不会反复弹窗。
- **已经建过相同主机名的隧道就不再问**（快速隧道的子域是随机且唯一的）。
- **在预填的新建对话框里点了“取消”不会吞掉链接**：同一条链接下次切回窗口时还会再问一次。
- **窗口内部换焦点不算「获得焦点」**：只有整个窗口拿到键盘焦点才会读剪贴板，并延迟 150 ms，避免 Wayland 下刚获得焦点就读不到剪贴板、以及 alt-tab 掠过的抖动。

不想用可以在主菜单里关掉「剪贴板快速连接」，对应配置项 `clipboard_quick_tunnel`。

<br>

## 配置

- 配置保存在 `~/.config/cloudflared-tunnel-manager/config.json`
- 二进制保存在 `~/.local/share/cloudflared-tunnel-manager/versions/<版本>/`

<br>

## 推荐运行方式：Flatpak

> [!IMPORTANT]
> **推荐使用 Flatpak 运行本软件。** 
> 
> 只要是软件，就有可能存在漏洞。攻击者，尤其是国家级定向攻击者，可能会攻击本软件，进而尝试在用户的电脑上执行代码。
>
> Flatpak 会将本软件及其启动的子进程放入沙箱，并按权限限制它们能够接触的主机资源。它不能替代及时更新系统，软硬件加固等安全措施，也不能保证完全阻止攻击；但在漏洞被利用时，它有助于限制影响范围，作为**纵深防御**的一层。因此，除非确有需要，请优先选择 Flatpak，而不是便携包或直接从源码运行。
>
> 如需更多安全性建议，请参阅 [ComputeSec](https://github.com/lingyicute/ComputeSec)。

从 [Releases](https://github.com/lingyicute/Cloudflared-Helper-Desktop/releases) 下载与架构相符的 `.flatpak` 文件后安装并启动：

```bash
# 以 x86_64 包为例；aarch64 请替换为对应文件名
flatpak install --user ./cloudflared-tunnel-manager-x86_64.flatpak
flatpak run uk._92li.cftm.CloudflaredTunnelManager
```

<br>

## 从源码运行（不推荐作为日常运行方式）

依赖：Python ≥ 3.10、GTK 4、libadwaita ≥ 1.4、PyGObject。

```bash
# Debian / Ubuntu 24.04+
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
# Fedora
sudo dnf install python3-gobject gtk4 libadwaita
# Arch
sudo pacman -S python-gobject gtk4 libadwaita
```

启动：
```bash
python3 main.py
```

<br>

## 本地回归测试

安装上述 GTK / PyGObject 依赖后，使用能导入 `gi` 的 Python（Debian / Ubuntu
通常是 `/usr/bin/python3`）。无显示测试不会访问网络或启动真实 cloudflared：

```bash
python3 tests/test_quick_tunnel.py
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

GTK 测试需要 `xvfb` / `xauth`，已有端到端测试还需要 `xdotool` / `xclip`：

```bash
GTK_A11Y=none G_DEBUG=fatal-warnings xvfb-run -a python3 tests/ui_regressions.py -v
xvfb-run -a python3 tests/e2e_quick_tunnel.py
xvfb-run -a python3 tests/e2e_tunnel_dialog.py
```

Pull request 会运行检查。

<br>

## 构建（GitHub Actions）

`.github/workflows/build.yaml` 包含四个作业：

| 作业 | 说明 |
| --- | --- |
| `check` | 字节编译 + 单元测试 + doctest + 在 Xvfb 中导入全部模块，并跑两个端到端测试（剪贴板快速连接、隧道对话框） |
| `pyinstaller` | 在 `ubuntu-24.04` / `ubuntu-24.04-arm` 上打包便携 tar.gz（x86_64、aarch64） |
| `flatpak` | 使用 GNOME 50 运行时构建 `.flatpak` 包 |
| `release` | 自动创建 GitHub Release 并上传所有产物 |

本地构建 Flatpak：

```bash
flatpak-builder --user --install --force-clean build-dir flatpak/uk._92li.cftm.CloudflaredTunnelManager.json
flatpak run uk._92li.cftm.CloudflaredTunnelManager
```

本地构建 PyInstaller 便携包（Linux）：

```bash
sudo apt install python3-venv python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 libgirepository-1.0-1 gobject-introspection librsvg2-common adwaita-icon-theme binutils
python3 -m venv --system-site-packages .venv
.venv/bin/pip install "pyinstaller>=6.13"
.venv/bin/pyinstaller --noconfirm --clean cloudflared-tunnel-manager.spec

# 自检：验证产物内的 GTK4 / libadwaita typelib 是否完整（无需显示器）
dist/cloudflared-tunnel-manager/cloudflared-tunnel-manager --self-test
```
> [!Tip]
> **为什么必须用 spec 文件？** PyInstaller 内置的 `gi` hooks 默认收集 **GTK 3.0**
> 的 typelib；构建机上只有 GTK 4，hook 会静默跳过，导致产物里没有
> `Gtk-4.0.typelib`，运行时报 `ValueError: Namespace Gtk not available`。
> 正确的版本（`Gtk 4.0` / `Gdk 4.0` / `Adw 1`）只能通过
> `Analysis(hooksconfig={"gi": {"module-versions": ...}})` 指定，
> 命令行参数无法表达，所以构建配置放在 `cloudflared-tunnel-manager.spec` 中。

<br>

## 目录结构

```
main.py                      入口
cftm/app.py                  Adw.Application、主窗口、隧道列表
cftm/tunnel.py               隧道模型 + Gio.Subprocess 进程管理
cftm/binary_manager.py       cloudflared 版本管理
cftm/versions_page.py        版本管理页面
cftm/dialogs.py              编辑对话框、日志窗口
cftm/quick_tunnel.py         剪贴板快速隧道：焦点监听 + 链接识别 + 确认弹窗
cftm/config.py               JSON 配置
data/                        desktop / metainfo / 图标
flatpak/                     Flatpak 清单
tests/                       单元测试（无需显示）+ UI 端到端测试（Xvfb）
.github/workflows/build.yaml CI / 发布流程
index.html                   项目主页
```

<br>

## 🗂️ License

Cloudflared-Helper-Desktop is released under the GNU Affero General Public License v3.0 (AGPLv3).

Copyright (C) 2024-2026 lingyicute.

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see https://www.gnu.org/licenses/.
