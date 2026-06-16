import pyautogui

def full_print_screen(self):
    # This captures the entire desktop monitor
    screenshot = pyautogui.screenshot()
    ts = int(time.time())
    screenshot.save(f"full_screen_{ts}.png")
    print("Desktop Screenshot Saved!")