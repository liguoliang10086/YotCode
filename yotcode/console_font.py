"""Windows 经典控制台（conhost）字号调整、最大化与缓冲区同步。

终端程序的转义序列改不了字形大小，但 conhost 提供了
SetCurrentConsoleFontEx 可以调小字号；
ShowWindow(SW_MAXIMIZE) 可以最大化窗口；
SetConsoleScreenBufferSize 可以让缓冲区匹配窗口，消除滚动条和底部空白。

只在经典 cmd.exe 窗口生效：
  - Windows Terminal（有 WT_SESSION）自己管理字号/窗口/缓冲区，直接跳过；
  - 输出被重定向（管道/--print）时句柄无效，静默返回。

目标字号来自 config.font_size（可由 .env 里的 YOTCODE_FONT_SIZE 或
settings.json 的 ui.fontSize 指定），没配置时用 DEFAULT_TARGET。
字号是设定值：每次启动都会设成目标值，往大往小都生效。
"""
from __future__ import annotations

import ctypes
import os
import time
from typing import Any

# 终端字号：经典 conhost 的默认字号偏大，消息正文显得很粗。
# 调到 13 后正文大小适中，整屏信息密度接近 Claude Code CLI。
DEFAULT_TARGET = 13

# 允许的字号范围，防止 .env 里写错值把终端弄成看不清 / 装不下。
FONT_SIZE_MIN = 8
FONT_SIZE_MAX = 40


def resolve_font_size(value: object | None) -> int | None:
    """把用户给的字号解析成合法值；无法解析时返回 None（用默认值）。"""
    if value is None:
        return None
    try:
        size = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return max(FONT_SIZE_MIN, min(size, FONT_SIZE_MAX))


class _COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class _SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class _CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [
        ("dwSize", _COORD),
        ("dwCursorPosition", _COORD),
        ("wAttributes", ctypes.c_ushort),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


class _CONSOLE_SCREEN_BUFFER_INFOEX(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("dwFlags", ctypes.c_ulong),
        ("ColorTable", ctypes.c_ulong * 16),
        ("srWindow", _SMALL_RECT),
        ("dwMaximumWindowSize", _COORD),
    ]


def _set_console_background(handle: int, kernel32: Any,
                            rgb: tuple[int, int, int]) -> None:
    """把控制台默认背景色改成应用背景色，让预留的最后一Row看不出断层。"""
    red, green, blue = rgb
    color = red | (green << 8) | (blue << 16)
    info = _CONSOLE_SCREEN_BUFFER_INFOEX()
    info.cbSize = ctypes.sizeof(_CONSOLE_SCREEN_BUFFER_INFOEX)
    if not kernel32.GetConsoleScreenBufferInfoEx(handle, ctypes.byref(info)):
        return
    if info.ColorTable[0] == color:
        return
    info.ColorTable[0] = color
    kernel32.SetConsoleScreenBufferInfoEx(handle, ctypes.byref(info))


def _window_rect(handle: int, kernel32: Any) -> tuple[int, int, int, int] | None:
    """返回当前可见窗口矩形 (left, top, right, bottom)。"""
    info = _CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        return None
    rect = (info.srWindow.Left, info.srWindow.Top,
            info.srWindow.Right, info.srWindow.Bottom)
    if rect[2] < rect[0] or rect[3] < rect[1]:
        return None
    return rect


def _wait_window_stable(handle: int, kernel32: Any, *, tries: int = 40,
                        delay: float = 0.1, stable_checks: int = 6
                        ) -> tuple[int, int, int, int] | None:
    """轮询等待窗口矩形稳定（最大化/改字号是异步生效的）。

    连续 6 次读到相同尺寸才认为稳定，避免拿旧矩形做同步。之前只等 3 次
    ×50ms，字号变化常常还没生效，Textual 拿到的是旧的小尺寸，右边和底部
    就不会被重绘，留下灰块和黑带。
    """
    last_rect: tuple[int, int, int, int] | None = None
    last_size: tuple[int, int] | None = None
    stable = 0
    for _ in range(tries):
        rect = _window_rect(handle, kernel32)
        size = None if rect is None else (
            rect[2] - rect[0] + 1, rect[3] - rect[1] + 1
        )
        if size is not None and size == last_size:
            stable += 1
            if stable >= stable_checks:
                return rect
        else:
            stable = 0
            last_size = size
        last_rect = rect
        time.sleep(delay)
    return last_rect


def _disable_quick_edit() -> None:
    """关闭 conhost 快速编辑，避免点击/拖选留下灰色高亮块。"""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
    if not handle or handle == -1:
        return
    mode = ctypes.c_ulong()
    if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
        return
    enable_quick_edit = 0x0040
    new_mode = mode.value & ~enable_quick_edit
    if new_mode != mode.value:
        kernel32.SetConsoleMode(handle, new_mode)


def console_view_rows() -> int:
    """返回控制台当前可见的行数。"""
    if os.name != "nt":
        return 0
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = kernel32.GetStdHandle(-11)
    if not handle or handle == -1:
        return 0
    rect = _window_rect(handle, kernel32)
    if rect is None:
        return 0
    return rect[3] - rect[1] + 1


def console_view_size() -> tuple[int, int]:
    """返回控制台当前可见区的 (列, 行)。

    拿不到时返回 (0, 0)。
    """
    if os.name != "nt":
        return 0, 0
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)
        if not handle or handle == -1:
            return 0, 0
        rect = _window_rect(handle, kernel32)
        if rect is None:
            return 0, 0
        left, top, right, bottom = rect
        return right - left + 1, bottom - top + 1
    except Exception:
        return 0, 0


def sync_console_buffer() -> tuple[int, int] | None:
    """把控制台缓冲区尺寸同步到当前窗口可见区，返回同步后的 (列, 行)。

    为什么需要它：conhost 的屏幕缓冲区**不会**自动跟随窗口大小。
    用户把窗口拉大时缓冲区没变，Textual 的 driver 收不到
    WINDOW_BUFFER_SIZE_EVENT，于是界面停在旧尺寸上——底部就会错位：
    输入框和状态栏叠在一起、或者被挤到屏幕外面。

    这里主动把缓冲区改成可见区大小：缓冲区一变，事件随之发出，
    Textual 就能正常重排。缓冲区已经等于可见区时什么都不做，
    免得白白触发一次全屏重绘。

    失败（管道、重定向、非 Windows）返回 None。
    """
    if os.name != "nt":
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)
        if not handle or handle == -1:
            return None
        rect = _window_rect(handle, kernel32)
        if rect is None:
            return None
        left, top, right, bottom = rect
        view_cols = right - left + 1
        view_rows = bottom - top + 1
        if view_cols <= 0 or view_rows <= 0:
            return None

        # 已经是可见区大小就不用动了
        info = _CONSOLE_SCREEN_BUFFER_INFO()
        if kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            if (info.dwSize.X, info.dwSize.Y) == (view_cols, view_rows):
                return view_cols, view_rows

        # 缓冲区不能比窗口小，先把窗口复位到 (0,0) 再改缓冲区
        if left or top:
            visible = _SMALL_RECT(0, 0, view_cols - 1, view_rows - 1)
            kernel32.SetConsoleWindowInfo(handle, True, ctypes.byref(visible))
        if not kernel32.SetConsoleScreenBufferSize(handle, _COORD(view_cols, view_rows)):
            return None
        return view_cols, view_rows
    except Exception:
        return None


def read_console_screen() -> list[str]:
    """把当前控制台屏幕的每一行读成字符串（去掉行尾空白）。

    用来验证"界面到底画成了什么样"：经典 conhost 会出现残影、
    重复行，这类问题光看代码看不出来，必须把屏幕内容读出来核对。

    读不到（管道、重定向、非 Windows）返回空列表。
    """
    if os.name != "nt":
        return []
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)
        if not handle or handle == -1:
            return []
        info = _CONSOLE_SCREEN_BUFFER_INFO()
        if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
            return []
        width = info.dwSize.X
        height = info.dwSize.Y
        if width <= 0 or height <= 0:
            return []
        buffer = ctypes.create_unicode_buffer(width)
        read = ctypes.c_ulong()
        lines: list[str] = []
        for y in range(height):
            coord = _COORD(0, y)
            ok = kernel32.ReadConsoleOutputCharacterW(
                handle, buffer, ctypes.c_ushort(width), coord, ctypes.byref(read)
            )
            if not ok:
                lines.append("")
                continue
            lines.append(buffer.value[: read.value].rstrip())
        return lines
    except Exception:
        return []


def bind_console_handles() -> bool:
    """把三个标准句柄绑到真正的控制台设备（CONIN$ / CONOUT$）。

    进程被管道、NUL 之类接管时，Textual 的 driver 拿不到控制台，
    只能退回默认的 80x25 布局，界面就会和实际窗口对不上。
    这里把句柄重新指回控制台设备。

    已经有真控制台时什么都不做，返回 True。
    """
    if os.name != "nt":
        return False
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        GENERIC_READ = 0x80000000
        GENERIC_WRITE = 0x40000000
        OPEN_EXISTING = 3
        bound = False
        for std_handle, device in (
            (-10, "CONIN$"),
            (-11, "CONOUT$"),
            (-12, "CONOUT$"),
        ):
            handle = kernel32.CreateFileW(
                device, GENERIC_WRITE | GENERIC_READ, 3, None, OPEN_EXISTING, 0, None
            )
            if handle and handle != -1:
                kernel32.SetStdHandle(std_handle, handle)
                bound = True
        return bound
    except Exception:
        return False


def prepare_console(target: int | None = None) -> None:
    """按正确顺序准备经典 cmd 控制台：最大化 → 调字号 → 关快速编辑 → 同步缓冲区。

    target 是目标字号（像素高）。None 时用 DEFAULT_TARGET。

    字号是「设定值」而不是「只减不增的上限」：早期版本只在当前字号大于目标时
    才下调，于是控制台一旦被调小就再也调不回来——用户后来想调大字号，改配置
    完全没反应。现在每次都把字号设成目标值，往大往小都能生效。

    缓冲区尺寸必须用窗口矩形的 right+1 / bottom+1 覆盖完整可视区，但 Textual
    的布局尺寸要取真实可见行列数。窗口滚动过时 left/top 可能不为 0，只按行列数
    收缩会截掉底部。

    另外必须给控制台预留最后一行：conhost 在写入屏幕缓冲区最后一行时会把整个
    缓冲区上滚一行，表现为界面不断上移、输入文字掉到输入框外面、底部堆出
    重复状态栏。所以 COLUMNS/LINES 用“可见行数 - 1”，最后一行留给控制台，
    并把控制台背景色改成应用背景色，让这条预留行看不出断层。
    """
    if os.name != "nt":
        return
    if os.environ.get("WT_SESSION"):
        return
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        if not handle or handle == -1:
            return

        # 关闭 conhost 快速编辑：否则点击/拖选会在 Logo、输入框右侧留下灰色块。
        _disable_quick_edit()

        # 1) 最大化窗口
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE

        # 2) 设置字号（可大可小），失败不影响后续
        target = resolve_font_size(target) or DEFAULT_TARGET

        class _FONT_INFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", ctypes.c_ulong),
                ("nFont", ctypes.c_ulong),
                ("dwFontSize", _COORD),
                ("FontFamily", ctypes.c_uint),
                ("FontWeight", ctypes.c_uint),
                ("FaceName", ctypes.c_wchar * 32),
            ]

        font = _FONT_INFO()
        font.cbSize = ctypes.sizeof(_FONT_INFO)
        if kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(font)):
            if font.dwFontSize.Y != target:
                font.dwFontSize.Y = target
                kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(font))

        # 改字号后，ShowWindow(SW_MAXIMIZE) 对已经最大化的窗口常常不再生效。
        # 先还原再最大化，强制 conhost 按新字号重新计算满屏行列数。
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            time.sleep(0.1)
            user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE

        # 3) 等待最大化 + 改字号真正稳定后，同步缓冲区并复位可视区。
        #    字号变化可能比窗口最大化晚很多，所以这里循环复查一次。
        for _ in range(2):
            rect = _wait_window_stable(handle, kernel32)
            if rect is None:
                return
            left, top, right, bottom = rect
            view_cols = right - left + 1
            view_rows = bottom - top + 1
            # 只有窗口曾滚动过才复位左上角；复位时保持当前宽高，
            # 绝不能把已经最大化的窗口收缩成旧尺寸。
            if left or top:
                visible = _SMALL_RECT(0, 0, view_cols - 1, view_rows - 1)
                kernel32.SetConsoleWindowInfo(handle, True, ctypes.byref(visible))
            kernel32.SetConsoleScreenBufferSize(
                handle, _COORD(view_cols, view_rows)
            )
            # 关键：不要设置 COLUMNS / LINES 环境变量。
            # Textual 的 WindowsDriver 会以 WINDOW_BUFFER_SIZE_EVENT 报上来的
            # 缓冲区尺寸为准；这里若把 LINES 设成 rows-1，driver 初始拿到
            # rows-1，但 conhost 随后又发来 rows 的 resize，两者不一致会让
            # app._size 与 screen/driver 各用一套尺寸，差量渲染错位、底部
            # 堆积重影行。让 driver 与缓冲区用同一个尺寸即可。
            os.environ.pop("COLUMNS", None)
            os.environ.pop("LINES", None)
            _set_console_background(handle, kernel32, (0x1E, 0x1D, 0x1B))

            time.sleep(0.2)
            current = _window_rect(handle, kernel32)
            if current is not None and (current[2], current[3]) == (right, bottom):
                break
    except Exception:
        pass  # 任何失败都不能影响程序启动
