"""Screenshot a multiple-choice question and show the answer number.

On Windows, press F8 to turn capture on, and F8 again to turn it off.
On macOS, press the backslash key. F8 is play/pause there, so the script
never receives it.
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
from anthropic import Anthropic
from PIL import Image

# Apple's Tk aborts on macOS 15.6 with "macOS 15 (1507) or later required".
tk = None
if sys.platform != "darwin":
    import tkinter as tk

try:
    from pynput import keyboard, mouse
except (ImportError, OSError) as exc:
    print("The keyboard library will not load.")
    print(exc)
    print("From the ricola folder, run:")
    print("  .venv/bin/python -m pip install -r requirements.txt")
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
MODEL_DEFAULT = "claude-sonnet-5-5"
EFFORT_DEFAULT = "high"
OVERLAY_FONT = ("Helvetica", 8, "normal") if sys.platform == "darwin" else ("Segoe UI", 8, "normal")
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
# Apple ANSI keycode for the key that types a backslash.
MAC_BACKSLASH_KEYCODE = 42

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


def is_backslash(key) -> bool:
    if getattr(key, "char", None) == "\\":
        return True
    return sys.platform == "darwin" and getattr(key, "vk", None) == MAC_BACKSLASH_KEYCODE


def claude_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if key:
        return key
    key = os.environ.get("CLAUDE_API_KEY", "").strip()
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    return key


def configure_overlay(root, label) -> None:
    """Borderless, click-through window. White is the Windows color key."""
    root.overrideredirect(True)
    root.attributes("-topmost", True)
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


class MacAnswer:
    """Small click-through number. Uses Cocoa because Apple's Tk aborts on 15.6."""

    def __init__(self) -> None:
        from AppKit import (
            NSApplication,
            NSApplicationActivationPolicyAccessory,
            NSBackingStoreBuffered,
            NSColor,
            NSFont,
            NSMakeRect,
            NSScreen,
            NSScreenSaverWindowLevel,
            NSTextAlignmentCenter,
            NSTextField,
            NSWindow,
            NSWindowCollectionBehaviorCanJoinAllSpaces,
            NSWindowCollectionBehaviorFullScreenAuxiliary,
            NSWindowCollectionBehaviorStationary,
            NSWindowStyleMaskBorderless,
        )

        self._make_rect = NSMakeRect
        self._screens = NSScreen
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.app = app
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(0, 0, 24, 16),
            NSWindowStyleMaskBorderless,
            NSBackingStoreBuffered,
            False,
        )
        self.window.setLevel_(NSScreenSaverWindowLevel)
        self.window.setOpaque_(False)
        self.window.setBackgroundColor_(NSColor.clearColor())
        self.window.setIgnoresMouseEvents_(True)
        self.window.setHasShadow_(False)
        self.window.setHidesOnDeactivate_(False)
        self.window.setCollectionBehavior_(
            NSWindowCollectionBehaviorCanJoinAllSpaces
            | NSWindowCollectionBehaviorFullScreenAuxiliary
            | NSWindowCollectionBehaviorStationary
        )
        self.label = NSTextField.alloc().initWithFrame_(NSMakeRect(0, 0, 24, 16))
        self.label.setStringValue_("")
        self.label.setBordered_(False)
        self.label.setBezeled_(False)
        self.label.setDrawsBackground_(False)
        self.label.setEditable_(False)
        self.label.setSelectable_(False)
        self.label.setAlignment_(NSTextAlignmentCenter)
        self.label.setTextColor_(NSColor.blackColor())
        self.label.setFont_(NSFont.systemFontOfSize_(13))
        self.window.setContentView_(self.label)

    def hide(self) -> None:
        self.window.orderOut_(None)

    def show(self, text: str, mon: dict | None) -> None:
        if mon is None:
            mon = {"left": 0, "top": 0, "width": 0, "height": 0}
        width = max(18, 11 * max(1, len(text)))
        height = 18
        quartz_x, quartz_y = overlay_origin(mon, width, height)
        screens = self._screens.screens()
        primary_h = float(screens[0].frame().size.height) if screens else 900.0
        cocoa_y = primary_h - (quartz_y + height)
        self.label.setStringValue_(text)
        self.window.setFrame_display_(self._make_rect(quartz_x, cocoa_y, width, height), True)
        self.window.orderFrontRegardless()

    def pump(self, poll) -> None:
        from Foundation import NSTimer

        self._poll = poll
        NSTimer.scheduledTimerWithTimeInterval_repeats_block_(0.03, True, lambda _timer: self._poll())
        self.app.run()


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

        self.root = None
        self.label = None
        self.mac: MacAnswer | None = None
        self.clickthrough_ready = False
        self._toggle_down = False
        self._right_down = None
        self._right_up = None
        self._event_location = None
        self._key_callback = None
        if sys.platform == "darwin":
            import Quartz

            self.mac = MacAnswer()
            self._right_down = Quartz.kCGEventRightMouseDown
            self._right_up = Quartz.kCGEventRightMouseUp
            self._event_location = Quartz.CGEventGetLocation
            threading.Thread(target=self.listen_keys, daemon=True).start()
        else:
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
        if key == keyboard.Key.f8 or is_backslash(key):
            if not self._toggle_down:
                self._toggle_down = True
                self.ui.put(("toggle", None))
            return
        if key == keyboard.Key.esc and self.armed.is_set():
            self.ui.put(("hide", None))

    def on_release(self, key) -> None:
        if key == keyboard.Key.f8 or is_backslash(key):
            self._toggle_down = False

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
                    self.hide_overlay()
                elif kind == "show":
                    self.show(payload["text"], payload["mon"])
                elif kind == "idle":
                    self.busy = False
        except queue.Empty:
            pass
        if self.root is not None:
            self.root.after(30, self.poll)

    def hide_overlay(self) -> None:
        if self.mac is not None:
            self.mac.hide()
        elif self.root is not None:
            self.root.withdraw()

    def toggle(self) -> None:
        if self.armed.is_set():
            self.armed.clear()
            self.hide_overlay()
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
        self.hide_overlay()
        if self.root is not None:
            self.root.update_idletasks()
            # Let the overlay leave the screen before the grab.
            self.root.after(60, lambda: threading.Thread(target=self.work, args=(x, y), daemon=True).start())
            return
        threading.Timer(0.06, lambda: threading.Thread(target=self.work, args=(x, y), daemon=True).start()).start()

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
        if self.mac is not None:
            self.mac.show(text, mon)
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

    def listen_keys(self) -> None:
        """Watch for backslash without the Mac keyboard library that aborts."""
        try:
            import Quartz
            from CoreFoundation import (
                CFMachPortCreateRunLoopSource,
                CFRunLoopAddSource,
                CFRunLoopGetCurrent,
                CFRunLoopRun,
                kCFRunLoopDefaultMode,
            )
        except ImportError as exc:
            print("Could not load the Mac keyboard library.", flush=True)
            print(exc, flush=True)
            return

        def callback(_proxy, event_type, event, _refcon):
            try:
                if event_type == Quartz.kCGEventKeyDown:
                    repeat = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
                    keycode = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                    if not repeat and keycode == MAC_BACKSLASH_KEYCODE:
                        self.ui.put(("toggle", None))
                    elif not repeat and keycode == 53 and self.armed.is_set():
                        self.ui.put(("hide", None))
            except Exception:
                pass
            return event

        tap = Quartz.CGEventTapCreate(
            Quartz.kCGSessionEventTap,
            Quartz.kCGHeadInsertEventTap,
            Quartz.kCGEventTapOptionListenOnly,
            Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown),
            callback,
            None,
        )
        self._key_callback = callback
        if not tap:
            print("Allow Accessibility for Terminal, then start ricola again.", flush=True)
            return
        source = CFMachPortCreateRunLoopSource(None, tap, 0)
        CFRunLoopAddSource(CFRunLoopGetCurrent(), source, kCFRunLoopDefaultMode)
        Quartz.CGEventTapEnable(tap, True)
        CFRunLoopRun()

    def apply_overlay_style(self) -> None:
        if sys.platform == "win32":
            self.apply_win_overlay()

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
        if self.mac is not None:
            self.mac.pump(self.poll)
            return
        self.root.mainloop()


def run_demo() -> None:
    """Show a sample 2 and save a screenshot, without calling the API."""
    _image, mon = capture(0, 0)
    if sys.platform == "darwin":
        mac = MacAnswer()
        mac.show("2", mon)
        threading.Timer(8, lambda: mac.app.terminate_(None)).start()
        mac.pump(lambda: None)
        return

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
        print("Running. Press \\ to turn capture on.", flush=True)
        print("Press \\ again to turn capture off. This window stays open.", flush=True)
    else:
        print("Running. Press F8 to turn capture on.", flush=True)
        print("Press F8 again to turn capture off. This window stays open.", flush=True)
    Ricola().run()


if __name__ == "__main__":
    main()
