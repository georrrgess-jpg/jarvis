"""Working on a Google Doc that's open in the browser with the keyboard, when the Google link can't do it.

Google Docs has a menu search (Alt+/) that can run any menu command by name, so renaming is
"Alt+/, Rename, Enter, type the name, Enter". Typing goes in at the end of the document (Ctrl+End).
"""

from __future__ import annotations

import time

VK_CONTROL, VK_MENU, VK_RETURN, VK_ESCAPE, VK_END, VK_DELETE = 0x11, 0x12, 0x0D, 0x1B, 0x23, 0x2E
VK_OEM_2 = 0xBF  # the "/" key
VK_A = ord("A")


def rename(desktop, window, name: str, settle: float = 0.6) -> None:
    desktop.bring_to_front(window)
    desktop.press([VK_ESCAPE], window=window)
    desktop.press([VK_MENU, VK_OEM_2], window=window)  # Alt+/ : Docs' "search the menus"
    time.sleep(settle)
    desktop.type_text("Rename", window=window)
    time.sleep(settle)
    desktop.press([VK_RETURN], window=window)  # File > Rename: the title box gets focus, its text selected
    time.sleep(settle)
    desktop.press([VK_CONTROL, VK_A], window=window)
    desktop.type_text(name, window=window)
    desktop.press([VK_RETURN], window=window)
    time.sleep(settle)


def type_at_end(desktop, window, text: str, settle: float = 0.3) -> None:
    desktop.bring_to_front(window)
    desktop.press([VK_ESCAPE], window=window)
    time.sleep(settle)
    desktop.press([VK_CONTROL, VK_END], window=window)
    time.sleep(0.1)
    desktop.type_text(text, window=window)


def clear(desktop, window, settle: float = 0.3) -> None:
    desktop.bring_to_front(window)
    desktop.press([VK_ESCAPE], window=window)
    time.sleep(settle)
    desktop.press([VK_CONTROL, VK_A], window=window)
    desktop.press([VK_DELETE], window=window)
