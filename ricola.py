"""Screenshot a multiple-choice question and show the answer number.

On Windows, press F8 to turn capture on, and F8 again to turn it off.
On macOS, press Control-Option-S. The F8 key is play/pause there, so the
script never receives it.
While capture is on, right-click solves the question on that screen.
The choice number (1, 2, 3, 4, counting from the top) appears as small
black text at the top center of that monitor.

Windows:
  py -3.13 -m venv .venv
  .venv\\Scripts\\python.exe -m pip install -r requirements.txt
  copy .env.example .env
  # put your Anthropic key in .env
  .venv\\Scripts\\python.exe ricola.py

macOS:
  python3 -m venv .venv
  .venv/bin/python -m pip install -r requirements.txt
  cp .env.example .env
  # put your Anthropic key in .env
  .venv/bin/python ricola.py

  Allow the terminal, or Python, under:
    System Settings → Privacy & Security → Accessibility
    System Settings → Privacy & Security → Screen Recording
  Quit and start again after changing those.

Esc hides the number. Close the console window to quit.
"""

from __future__ import annotations

import base64
import ctypes
import io
import os
import queue
import sys
import threading
from pathlib import Path


def enable_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


enable_dpi_awareness()

import mss
import tkinter as tk
from anthropic import Anthropic
from PIL import Image
from pynput import keyboard, mouse

SCRIPT_DIR = Path(__file__).resolve().parent
MODEL_DEFAULT = "claude-sonnet-5-5"
EFFORT_DEFAULT = "high"
OVERLAY_FONT = ("Helvetica", 8, "normal") if sys.platform == "darwin" else ("Segoe UI", 8, "normal")
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
# Hardware keycode for the S key on Apple keyboards. Control-Option-S is used
# because F8 is the play/pause media key and never arrives as F8.
MAC_S_KEYCODE = 1
MODIFIER_KEYS = {
    keyboard.Key.ctrl: "ctrl",
    keyboard.Key.ctrl_l: "ctrl",
    keyboard.Key.ctrl_r: "ctrl",
    keyboard.Key.alt: "alt",
    keyboard.Key.alt_l: "alt",
    keyboard.Key.alt_r: "alt",
    keyboard.Key.alt_gr: "alt",
}

SYSTEM_PROMPT = """You answer the multiple-choice question in the main page of a screenshot.

Ignore other windows, chat panels, and any answer already written on screen. Count the choices from top to bottom. The top choice is 1, the next is 2, then 3, then 4. Use that visual order even if the choices are also labeled with letters or radio buttons.

Reply with only the number of the correct choice. No words, punctuation, or explanation.
If you cannot find a multiple-choice question, reply with ?"""


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def parse_choice(text: str) -> str:
    for char in text or "":
        if char.isdigit() and char != "0":
            return char
    return "?"


def overlay_origin(mon: dict, width: int, _height: int) -> tuple[int, int]:
    return (
        int(mon["left"]) + max(0, (int(mon.get("width", 0)) - width) // 2),
        int(mon["top"]) + int(mon.get("height", 0) * 0.95),
    )


def monitor_at(sct: mss.MSS, x: int, y: int) -> dict:
    for mon in sct.monitors[1:]:
        if mon["left"] <= x < mon["left"] + mon["width"] and mon["top"] <= y < mon["top"] + mon["height"]:
            return mon
    return sct.monitors[1]


def capture(x: int, y: int) -> tuple[Image.Image, dict]:
    with mss.MSS() as sct:
        mon = monitor_at(sct, x, y)
        raw = sct.grab(mon)
        image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    image = shrink(image, 1920)
    return image, mon


def shrink(image: Image.Image, longest: int) -> Image.Image:
    width, height = image.size
    scale = longest / max(width, height)
    if scale >= 1:
        return image
    size = (max(1, int(width * scale)), max(1, int(height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def to_jpeg_b64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return base64.standard_b64encode(buffer.getvalue()).decode("ascii")


def claude_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    key = os.environ.get("CLAUDE_API_KEY", "").strip()
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    return key


def configure_overlay(root: tk.Tk, label: tk.Label) -> None:
    """Borderless, click-through window. White is the Windows color key."""
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    if sys.platform == "darwin":
        root.configure(bg="systemTransparent")
        label.configure(bg="systemTransparent")
        try:
            root.attributes("-transparent", True)
        except tk.TclError:
            pass
        try:
            root.tk.call(
                "::tk::unsupported::MacWindowStyle",
                "style",
                root._w,
                "help",
                "noActivates",
            )
        except tk.TclError:
            pass
        return
    root.configure(bg="white")
    label.configure(bg="white")
    try:
        root.attributes("-transparentcolor", "white")
    except tk.TclError:
        pass


def set_console_title(title: str) -> None:
    if sys.platform == "win32":
        ctypes.windll.kernel32.SetConsoleTitleW(title)
        return
    if sys.platform == "darwin":
        sys.stdout.write(f"\033]0;{title}\007")
        sys.stdout.flush()


def _mac_view_is(view, target: int) -> bool:
    import objc

    if view is None:
        return False
    if objc.pyobjc_id(view) == target:
        return True
    try:
        children = view.subviews()
    except Exception:
        return False
    return any(_mac_view_is(child, target) for child in children)


def mac_ns_window(tk_window: tk.Tk):
    """Find the Cocoa window for a Tk window without casting a raw pointer."""
    import objc
    from AppKit import NSApp

    if NSApp is None:
        return None
    target = int(tk_window.winfo_id())
    windows = list(NSApp.windows())
    for window in windows:
        if objc.pyobjc_id(window) == target or _mac_view_is(window.contentView(), target):
            return window
    for window in windows:
        try:
            if str(window.title()) == tk_window.title():
                return window
        except Exception:
            continue
    if len(windows) == 1:
        return windows[0]
    return None


def prepare_macos() -> None:
    """Ask for the two permissions a global click and a screen grab need."""
    try:
        import HIServices
        import Quartz
    except ImportError:
        print("On macOS, install the Mac frameworks:")
        print("  .venv/bin/python -m pip install -r requirements.txt")
        sys.exit(1)

    trusted = bool(HIServices.AXIsProcessTrusted())
    try:
        screen = bool(Quartz.CGPreflightScreenCaptureAccess())
    except Exception:
        screen = True
    if trusted and screen:
        return

    print("macOS is blocking input or screen capture.")
    if not trusted:
        try:
            HIServices.AXIsProcessTrustedWithOptions({HIServices.kAXTrustedCheckOptionPrompt: True})
        except Exception:
            pass
        print("Allow this app under System Settings → Privacy & Security → Accessibility.")
    if not screen:
        try:
            Quartz.CGRequestScreenCaptureAccess()
        except Exception:
            pass
        print("Allow this app under System Settings → Privacy & Security → Screen Recording.")
    print("Quit and start ricola again after changing those.")


class Ricola:
    def __init__(self) -> None:
        self.ui: queue.Queue = queue.Queue()
        self.busy = False
        self.armed = threading.Event()
        self.client: Anthropic | None = None

        self.root = tk.Tk()
        self.root.title("ricola")
        self.root.withdraw()

        self.label = tk.Label(
            self.root,
            text="",
            fg="black",
            bg="white",
            font=OVERLAY_FONT,
            padx=2,
            pady=0,
            bd=0,
            highlightthickness=0,
        )
        self.label.pack()
        configure_overlay(self.root, self.label)
        self.clickthrough_ready = False
        self._held: set[str] = set()
        self._toggle_down = False
        self._right_down = None
        self._right_up = None
        self._event_location = None
        if sys.platform == "darwin":
            import Quartz

            self._right_down = Quartz.kCGEventRightMouseDown
            self._right_up = Quartz.kCGEventRightMouseUp
            self._event_location = Quartz.CGEventGetLocation

        self.root.after(30, self.poll)
        keyboard.Listener(on_press=self.on_press, on_release=self.on_release).start()
        listener_options = {}
        if sys.platform == "win32":
            listener_options["win32_event_filter"] = self.filter_mouse
        elif sys.platform == "darwin":
            listener_options["darwin_intercept"] = self.intercept_mouse
        self.mouse_listener = mouse.Listener(on_click=self.on_click, **listener_options)
        self.mouse_listener.start()

    def on_press(self, key) -> None:
        modifier = MODIFIER_KEYS.get(key)
        if modifier:
            self._held.add(modifier)
            return
        if key == keyboard.Key.f8 or self.mac_toggle(key):
            if not self._toggle_down:
                self._toggle_down = True
                self.ui.put(("toggle", None))
            return
        if key == keyboard.Key.esc and self.armed.is_set():
            self.ui.put(("hide", None))

    def on_release(self, key) -> None:
        modifier = MODIFIER_KEYS.get(key)
        if modifier:
            self._held.discard(modifier)
            return
        if key == keyboard.Key.f8 or getattr(key, "vk", None) == MAC_S_KEYCODE:
            self._toggle_down = False

    def mac_toggle(self, key) -> bool:
        if sys.platform != "darwin":
            return False
        if "ctrl" not in self._held or "alt" not in self._held:
            return False
        if getattr(key, "vk", None) == MAC_S_KEYCODE:
            return True
        return getattr(key, "char", None) in ("s", "S")

    def filter_mouse(self, msg, data):
        if not self.armed.is_set() or msg not in (WM_RBUTTONDOWN, WM_RBUTTONUP):
            return True
        if msg == WM_RBUTTONDOWN:
            self.ui.put(("trigger", (int(data.pt.x), int(data.pt.y))))
        self.mouse_listener.suppress_event()
        return True

    def intercept_mouse(self, event_type, event):
        """Swallow an armed right-click on macOS. Other events pass through."""
        if (
            self._event_location is not None
            and self.armed.is_set()
            and event_type in (self._right_down, self._right_up)
        ):
            if event_type == self._right_down:
                x, y = self._event_location(event)
                self.ui.put(("trigger", (int(x), int(y))))
            return None
        return event

    def on_click(self, x, y, button, pressed):
        return None

    def poll(self) -> None:
        try:
            while True:
                kind, payload = self.ui.get_nowait()
                if kind == "toggle":
                    self.toggle()
                elif kind == "trigger":
                    self.begin(payload[0], payload[1])
                elif kind == "hide":
                    self.root.withdraw()
                elif kind == "show":
                    self.show(payload["text"], payload["mon"])
                elif kind == "idle":
                    self.busy = False
        except queue.Empty:
            pass
        self.root.after(30, self.poll)

    def toggle(self) -> None:
        if self.armed.is_set():
            self.armed.clear()
            self.root.withdraw()
            print("Capture off.", flush=True)
            return
        self.armed.set()
        print("Capture on. Right-click the question.", flush=True)

    def begin(self, x: int, y: int) -> None:
        if not self.armed.is_set():
            return
        if self.busy:
            return
        self.busy = True
        self.root.withdraw()
        self.root.update_idletasks()
        # Let the overlay leave the screen before the grab.
        self.root.after(60, lambda: threading.Thread(target=self.work, args=(x, y), daemon=True).start())

    def work(self, x: int, y: int) -> None:
        mon = None
        try:
            if not self.armed.is_set():
                return
            image, mon = capture(x, y)
            if not self.armed.is_set():
                return
            self.ui.put(("show", {"text": ".", "mon": mon}))
            answer = self.ask(image)
            if not self.armed.is_set():
                return
            self.ui.put(("show", {"text": answer, "mon": mon}))
        except Exception:
            self.ui.put(("show", {"text": "!", "mon": mon}))
        finally:
            self.ui.put(("idle", None))

    def ask(self, image: Image.Image) -> str:
        if self.client is None:
            self.client = Anthropic(timeout=45.0)
        model = os.environ.get("CLAUDE_MODEL", MODEL_DEFAULT) or MODEL_DEFAULT
        effort = os.environ.get("CLAUDE_EFFORT", EFFORT_DEFAULT) or EFFORT_DEFAULT
        response = self.client.messages.create(
            model=model,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            output_config={"effort": effort},
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": to_jpeg_b64(image),
                            },
                        },
                        {
                            "type": "text",
                            "text": "Which choice is correct? Reply with the choice number only.",
                        },
                    ],
                }
            ],
        )
        text = "".join(block.text for block in response.content if getattr(block, "type", None) == "text")
        return parse_choice(text)

    def show(self, text: str, mon: dict | None) -> None:
        if not self.armed.is_set():
            return
        if mon is None:
            mon = {"left": 0, "top": 0}
        self.label.configure(text=text)
        self.root.update_idletasks()
        width = max(self.label.winfo_reqwidth(), 6)
        height = max(self.label.winfo_reqheight(), 6)
        x, y = overlay_origin(mon, width, height)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        self.apply_overlay_style()

    def apply_overlay_style(self) -> None:
        if sys.platform == "darwin":
            self.apply_mac_overlay()
        elif sys.platform == "win32":
            self.apply_win_overlay()

    def apply_mac_overlay(self) -> None:
        self.root.update_idletasks()
        try:
            from AppKit import (
                NSColor,
                NSScreenSaverWindowLevel,
                NSWindowCollectionBehaviorCanJoinAllSpaces,
                NSWindowCollectionBehaviorFullScreenAuxiliary,
                NSWindowCollectionBehaviorStationary,
            )
        except ImportError:
            return
        window = mac_ns_window(self.root)
        if window is None:
            return
        window.setIgnoresMouseEvents_(True)
        window.setLevel_(NSScreenSaverWindowLevel)
        window.setOpaque_(False)
        window.setHasShadow_(False)
        window.setHidesOnDeactivate_(False)
        window.setBackgroundColor_(NSColor.clearColor())
        window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
        )

    def apply_win_overlay(self) -> None:
        self.root.update_idletasks()
        child = self.root.winfo_id()
        hwnd = ctypes.windll.user32.GetParent(child) or child
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x00080000
        WS_EX_TRANSPARENT = 0x00000020
        WS_EX_TOOLWINDOW = 0x00000080
        WS_EX_NOACTIVATE = 0x08000000
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd,
            GWL_EXSTYLE,
            style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE,
        )
        HWND_TOPMOST = -1
        SWP_NOSIZE = 0x0001
        SWP_NOMOVE = 0x0002
        SWP_NOACTIVATE = 0x0010
        SWP_SHOWWINDOW = 0x0040
        ctypes.windll.user32.SetWindowPos(
            hwnd,
            HWND_TOPMOST,
            0,
            0,
            0,
            0,
            SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
        )
        LWA_COLORKEY = 0x00000001
        ctypes.windll.user32.SetLayeredWindowAttributes(hwnd, 0x00FFFFFF, 0, LWA_COLORKEY)

    def run(self) -> None:
        self.root.mainloop()


def run_demo() -> None:
    """Show a sample 2 and save a screenshot, without calling the API."""
    _image, mon = capture(0, 0)

    root = tk.Tk()
    root.title("ricola")
    label = tk.Label(
        root,
        text="2",
        fg="black",
        bg="white",
        font=OVERLAY_FONT,
        padx=2,
        pady=0,
        bd=0,
        highlightthickness=0,
    )
    label.pack()
    configure_overlay(root, label)
    root.update_idletasks()
    width = max(label.winfo_reqwidth(), 8)
    height = max(label.winfo_reqheight(), 8)
    x, y = overlay_origin(mon, width, height)
    root.geometry(f"{width}x{height}+{x}+{y}")
    holder = Ricola.__new__(Ricola)
    holder.root = root
    holder.apply_overlay_style()
    root.after(8000, root.destroy)
    root.mainloop()


def main() -> None:
    load_dotenv(SCRIPT_DIR / ".env")
    demo = "--demo" in sys.argv
    if not demo:
        key = claude_key()
        if not key or "your-key" in key:
            print("Add your Claude key to .env")
            print("Copy .env.example to .env and set ANTHROPIC_API_KEY.")
            sys.exit(1)
    if sys.platform == "darwin":
        prepare_macos()
    if demo:
        run_demo()
        return
    set_console_title("ricola")
    if sys.platform == "darwin":
        print("Running. Press Control-Option-S to turn capture on.", flush=True)
        print("Press it again to turn capture off. This window stays open.", flush=True)
    else:
        print("Running. Press F8 to turn capture on.", flush=True)
        print("Press F8 again to turn capture off. This window stays open.", flush=True)
    Ricola().run()


if __name__ == "__main__":
    main()
