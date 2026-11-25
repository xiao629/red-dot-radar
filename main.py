# -*- coding: utf-8 -*-
"""
小地圖紅點雷達偵測器
Python 3.11
僅擷取使用者自行框選的螢幕區域，以 HSV 顏色遮罩偵測紅色標記。
"""
from __future__ import annotations

import queue
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox

import cv2
import mss
import numpy as np

try:
    import winsound
except ImportError:
    winsound = None


APP_TITLE = "小地圖紅點雷達偵測器"


class RegionSelector:
    """全螢幕半透明框選工具，回傳螢幕座標 (left, top, width, height)。"""
    def __init__(self, parent: tk.Tk, callback):
        self.parent = parent
        self.callback = callback
        self.start_x = self.start_y = 0
        self.rect = None

        self.win = tk.Toplevel(parent)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.28)
        sw = self.win.winfo_screenwidth()
        sh = self.win.winfo_screenheight()
        self.win.geometry(f"{sw}x{sh}+0+0")
        self.canvas = tk.Canvas(self.win, bg="#202020", cursor="crosshair",
                                highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(
            24, 24, anchor="nw",
            text="請按住滑鼠左鍵拖曳框選小地圖，放開即可完成；按 Esc 取消",
            fill="white", font=("Microsoft JhengHei", 16, "bold")
        )
        self.canvas.bind("<ButtonPress-1>", self.on_down)
        self.canvas.bind("<B1-Motion>", self.on_move)
        self.canvas.bind("<ButtonRelease-1>", self.on_up)
        self.win.bind("<Escape>", lambda _e: self.cancel())
        self.win.focus_force()

    def on_down(self, event):
        self.start_x, self.start_y = event.x_root, event.y_root
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(
            event.x, event.y, event.x, event.y,
            outline="#00ff88", width=3, fill=""
        )

    def on_move(self, event):
        if self.rect:
            x1 = self.start_x - self.win.winfo_rootx()
            y1 = self.start_y - self.win.winfo_rooty()
            x2 = event.x_root - self.win.winfo_rootx()
            y2 = event.y_root - self.win.winfo_rooty()
            self.canvas.coords(self.rect, x1, y1, x2, y2)

    def on_up(self, event):
        x1, y1 = self.start_x, self.start_y
        x2, y2 = event.x_root, event.y_root
        left, top = min(x1, x2), min(y1, y2)
        width, height = abs(x2 - x1), abs(y2 - y1)
        self.win.destroy()
        if width < 20 or height < 20:
            self.parent.deiconify()
            self.parent.lift()
            messagebox.showwarning("選取範圍太小", "請重新框選完整的小地圖區域。")
            return
        self.callback({"left": left, "top": top, "width": width, "height": height})

    def cancel(self):
        self.win.destroy()


class RadarApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title(APP_TITLE)
        self.root.geometry("510x610")
        self.root.minsize(480, 570)
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.region = None
        self.running = False
        self.stop_event = threading.Event()
        self.worker = None
        self.events = queue.Queue()
        self.last_alarm = 0.0
        self.was_detected = False
        self.total_alerts = 0
        self.current_count = 0

        self.min_area = tk.IntVar(value=5)
        self.max_area = tk.IntVar(value=220)
        self.repeat_alarm = tk.BooleanVar(value=False)
        self.sound_enabled = tk.BooleanVar(value=True)
        self.cooldown = tk.DoubleVar(value=2.0)

        self._build_ui()
        self.root.after(100, self._process_events)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="小地圖紅點雷達", font=("Microsoft JhengHei", 19, "bold")).pack(anchor="w")
        ttk.Label(
            outer, text="框選小地圖後，持續偵測紅色玩家標記並播放警報。",
            font=("Microsoft JhengHei", 9)
        ).pack(anchor="w", pady=(2, 12))

        region_box = ttk.LabelFrame(outer, text=" 1. 偵測範圍 ", padding=10)
        region_box.pack(fill="x", pady=5)
        self.region_label = ttk.Label(region_box, text="尚未選取小地圖", foreground="#a33")
        self.region_label.pack(anchor="w", pady=(0, 8))
        row = ttk.Frame(region_box)
        row.pack(fill="x")
        ttk.Button(row, text="滑鼠框選小地圖", command=self.select_region).pack(side="left")
        ttk.Button(row, text="重新選取", command=self.select_region).pack(side="left", padx=8)

        detect_box = ttk.LabelFrame(outer, text=" 2. 偵測設定 ", padding=10)
        detect_box.pack(fill="x", pady=5)

        ttk.Label(detect_box, text="最小紅點面積（排除雜訊）").pack(anchor="w")
        ttk.Scale(detect_box, from_=1, to=35, variable=self.min_area,
                  orient="horizontal").pack(fill="x")
        self.min_area_label = ttk.Label(detect_box, text="5 px")
        self.min_area_label.pack(anchor="e")
        self.min_area.trace_add("write", lambda *_: self.min_area_label.config(
            text=f"{self.min_area.get()} px"))

        ttk.Checkbutton(detect_box, text="偵測到紅點時播放警報音",
                        variable=self.sound_enabled).pack(anchor="w", pady=(6, 2))
        ttk.Checkbutton(detect_box, text="紅點持續存在時，間隔播放警報",
                        variable=self.repeat_alarm).pack(anchor="w", pady=2)

        cooldown_row = ttk.Frame(detect_box)
        cooldown_row.pack(fill="x", pady=(5, 0))
        ttk.Label(cooldown_row, text="重複警報間隔").pack(side="left")
        ttk.Spinbox(cooldown_row, from_=1, to=30, increment=1,
                    textvariable=self.cooldown, width=5).pack(side="left", padx=6)
        ttk.Label(cooldown_row, text="秒").pack(side="left")

        status_box = ttk.LabelFrame(outer, text=" 3. 即時狀態 ", padding=10)
        status_box.pack(fill="x", pady=5)
        self.status_label = ttk.Label(status_box, text="尚未開始監控", font=("Microsoft JhengHei", 11, "bold"))
        self.status_label.pack(anchor="w")
        self.count_label = ttk.Label(status_box, text="目前紅點數量：0")
        self.count_label.pack(anchor="w", pady=(5, 0))
        self.alert_label = ttk.Label(status_box, text="累計警報：0")
        self.alert_label.pack(anchor="w")

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=12)
        self.start_button = ttk.Button(controls, text="開始監控", command=self.start_monitoring)
        self.start_button.pack(side="left", fill="x", expand=True)
        self.stop_button = ttk.Button(controls, text="停止監控", command=self.stop_monitoring,
                                      state="disabled")
        self.stop_button.pack(side="left", fill="x", expand=True, padx=(8, 0))
        ttk.Button(outer, text="測試警報音", command=self.play_alarm).pack(fill="x")

        note = (
            "使用提示：請先讓遊戲保持視窗模式，框選小地圖本體（盡量不要包含外框及其他 UI）。\n"
            "若漏偵測或誤報，可調整「最小紅點面積」。黃色角色不符合紅色 HSV 範圍，會自然排除。\n"
            "本程式只讀取螢幕影像，不會操作遊戲或讀取遊戲記憶體。"
        )
        ttk.Label(outer, text=note, wraplength=460, foreground="#555",
                  font=("Microsoft JhengHei", 8)).pack(anchor="w", pady=(8, 0))

    def select_region(self):
        if self.running:
            messagebox.showinfo("請先停止", "請先停止監控，再重新選取小地圖。")
            return
        self.root.withdraw()
        self.root.after(250, lambda: RegionSelector(self.root, self._region_selected))

    def _region_selected(self, region):
        self.region = region
        self.region_label.config(
            text=f"已選取：X={region['left']} Y={region['top']} "
                 f"W={region['width']} H={region['height']}",
            foreground="green"
        )
        self.root.deiconify()
        self.root.lift()

    def start_monitoring(self):
        if not self.region:
            messagebox.showwarning("尚未選取", "請先按「滑鼠框選小地圖」。")
            return
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.was_detected = False
        self.last_alarm = 0.0
        self.status_label.config(text="監控中：等待紅點出現…", foreground="green")
        self.start_button.config(state="disabled")
        self.stop_button.config(state="normal")
        # 在 UI 執行緒讀取 Tk 變數，再將純 Python 值交給背景執行緒。
        self.monitor_min_area = int(self.min_area.get())
        self.monitor_max_area = int(self.max_area.get())
        self.monitor_repeat_alarm = bool(self.repeat_alarm.get())
        self.monitor_cooldown = max(1.0, float(self.cooldown.get()))
        self.monitor_sound_enabled = bool(self.sound_enabled.get())
        self.worker = threading.Thread(target=self._monitor_loop, daemon=True)
        self.worker.start()

    def stop_monitoring(self):
        self.running = False
        self.stop_event.set()
        self.status_label.config(text="已停止監控", foreground="black")
        self.start_button.config(state="normal")
        self.stop_button.config(state="disabled")

    @staticmethod
    def detect_red_points(frame, min_area, max_area):
        """HSV 雙區間紅色偵測；黃色角色、藍色圖示不會進入紅色遮罩。"""
        hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
        lower_red_1 = np.array([0, 100, 100], dtype=np.uint8)
        upper_red_1 = np.array([12, 255, 255], dtype=np.uint8)
        lower_red_2 = np.array([168, 100, 100], dtype=np.uint8)
        upper_red_2 = np.array([180, 255, 255], dtype=np.uint8)
        mask1 = cv2.inRange(hsv, lower_red_1, upper_red_1)
        mask2 = cv2.inRange(hsv, lower_red_2, upper_red_2)
        mask = cv2.bitwise_or(mask1, mask2)
        kernel = np.ones((3, 3), np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        points = []
        for contour in contours:
            area = cv2.contourArea(contour)
            if min_area <= area <= max_area:
                perimeter = cv2.arcLength(contour, True)
                if perimeter <= 0:
                    continue
                circularity = 4.0 * np.pi * area / (perimeter * perimeter)
                # 紅點通常近似圓形；稍微放寬以容許縮放與抗鋸齒。
                if circularity >= 0.28:
                    moments = cv2.moments(contour)
                    if moments["m00"]:
                        cx = int(moments["m10"] / moments["m00"])
                        cy = int(moments["m01"] / moments["m00"])
                        points.append((cx, cy))
        return points

    def _monitor_loop(self):
        try:
            with mss.mss() as sct:
                while not self.stop_event.is_set():
                    shot = np.array(sct.grab(self.region))
                    frame = cv2.cvtColor(shot, cv2.COLOR_BGRA2BGR)
                    points = self.detect_red_points(
                        frame, self.monitor_min_area, self.monitor_max_area
                    )
                    count = len(points)
                    now = time.monotonic()

                    if count > 0:
                        should_alert = (not self.was_detected) or (
                            self.monitor_repeat_alarm and now - self.last_alarm >= self.monitor_cooldown
                        )
                        if should_alert:
                            self.last_alarm = now
                            self.events.put(("alert", count))
                        self.was_detected = True
                    else:
                        self.was_detected = False

                    self.events.put(("count", count))
                    # 約 12 次/秒，避免不必要的 CPU 使用。
                    self.stop_event.wait(0.08)
        except Exception as exc:
            self.events.put(("error", str(exc)))
        finally:
            self.events.put(("stopped", None))

    def _process_events(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "count":
                    self.current_count = value
                    self.count_label.config(text=f"目前紅點數量：{value}")
                    if self.running and value == 0:
                        self.status_label.config(text="監控中：目前沒有紅點", foreground="green")
                elif kind == "alert":
                    self.total_alerts += 1
                    self.alert_label.config(text=f"累計警報：{self.total_alerts}")
                    self.status_label.config(text=f"⚠ 偵測到 {value} 個紅點！", foreground="red")
                    if self.monitor_sound_enabled:
                        self.play_alarm()
                elif kind == "error":
                    self.stop_event.set()
                    self.running = False
                    self.start_button.config(state="normal")
                    self.stop_button.config(state="disabled")
                    self.status_label.config(text="偵測發生錯誤", foreground="red")
                    messagebox.showerror("偵測錯誤", value)
                elif kind == "stopped":
                    if self.running and self.stop_event.is_set():
                        self.running = False
                        self.start_button.config(state="normal")
                        self.stop_button.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._process_events)

    @staticmethod
    def play_alarm():
        def _beep():
            try:
                if winsound:
                    winsound.Beep(1500, 180)
                    time.sleep(0.06)
                    winsound.Beep(1900, 220)
                else:
                    print("\a", end="", flush=True)
            except RuntimeError:
                pass
        threading.Thread(target=_beep, daemon=True).start()

    def on_close(self):
        self.stop_event.set()
        self.running = False
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    RadarApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
