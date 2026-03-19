#!/usr/bin/env python3
# Copyright (c) 2025 Kaj Maney / liquidGuru (liquidguru.com) — CC BY-NC 4.0
"""
idGuru — Desktop Launcher
Place in same folder as idguru.py and double-click to run.
Works on Windows and macOS.
"""

import sys, os, threading, webbrowser, subprocess, time, platform
import tkinter as tk
from tkinter import messagebox
from pathlib import Path
import urllib.request, urllib.error

HERE    = Path(__file__).parent
INDEXER = HERE / "idguru.py"
# Also check old filename for backwards compatibility
if not INDEXER.exists():
    INDEXER = HERE / "underwater_indexer.py"
PYTHON  = sys.executable
URL     = "http://localhost:5000"
IS_MAC  = platform.system() == "Darwin"
IS_WIN  = platform.system() == "Windows"

# On macOS, subprocess needs CREATE_NO_WINDOW equivalent (just omit it)
POPEN_FLAGS = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WIN else {}


class Launcher:
    def __init__(self):
        self.server_proc = None
        self.running = False
        self.build_ui()
        self.root.after(300, self.start_server)

    def build_ui(self):
        self.root = tk.Tk()
        self.root.title("idGuru")
        self.root.geometry("440x260")
        self.root.resizable(False, False)
        self.root.configure(bg="#050f1f")
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

        # macOS: bring window to front
        if IS_MAC:
            self.root.lift()
            self.root.attributes("-topmost", True)
            self.root.after(500, lambda: self.root.attributes("-topmost", False))

        tk.Label(self.root, text="idGuru",
                 bg="#050f1f", fg="#38bdf8",
                 font=("Helvetica", 18, "bold")).pack(pady=(22, 2))
        tk.Label(self.root, text="liquidGuru's AI-powered critter finder",
                 bg="#050f1f", fg="#475569",
                 font=("Helvetica", 10)).pack()

        sf = tk.Frame(self.root, bg="#050f1f")
        sf.pack(pady=12)
        self.dot = tk.Label(sf, text="●", bg="#050f1f", fg="#1e3a5f",
                            font=("Helvetica", 12))
        self.dot.pack(side="left", padx=(0, 6))
        self.status_lbl = tk.Label(sf, text="Starting...", bg="#050f1f",
                                   fg="#f59e0b", font=("Helvetica", 10))
        self.status_lbl.pack(side="left")

        bf = tk.Frame(self.root, bg="#050f1f")
        bf.pack(pady=4)
        self.start_btn = tk.Button(bf, text="Start", command=self.start_server,
                                   bg="#16a34a", fg="white",
                                   font=("Helvetica", 10, "bold"),
                                   width=10, relief="flat", cursor="hand2",
                                   pady=6, state="disabled")
        self.start_btn.pack(side="left", padx=6)
        self.open_btn = tk.Button(bf, text="Open Browser",
                                  command=self.open_browser,
                                  bg="#0ea5e9", fg="white",
                                  font=("Helvetica", 10),
                                  width=12, relief="flat", cursor="hand2",
                                  pady=6, state="disabled")
        self.open_btn.pack(side="left", padx=6)
        self.stop_btn = tk.Button(bf, text="Stop", command=self.stop_server,
                                  bg="#7f1d1d", fg="#f87171",
                                  font=("Helvetica", 10),
                                  width=10, relief="flat", cursor="hand2",
                                  pady=6, state="disabled")
        self.stop_btn.pack(side="left", padx=6)

        self.log = tk.Text(self.root, height=3, bg="#07172b", fg="#64748b",
                           font=("Courier", 9), relief="flat",
                           state="disabled", wrap="word", padx=8, pady=4)
        self.log.pack(fill="x", padx=16, pady=(10, 0))

    def log_msg(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def set_status(self, text, color):
        self.dot.configure(fg=color)
        self.status_lbl.configure(text=text, fg=color)

    def start_server(self):
        if self.running:
            return
        if not INDEXER.exists():
            messagebox.showerror("Error", f"Cannot find:\n{INDEXER}")
            return

        self.set_status("Starting...", "#f59e0b")
        self.start_btn.configure(state="disabled")
        self.log_msg("Launching server...")

        def run():
            env = os.environ.copy()
            try:
                self.server_proc = subprocess.Popen(
                    [PYTHON, str(INDEXER), "viewer_headless"],
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, env=env, **POPEN_FLAGS
                )
            except Exception as e:
                self.root.after(0, lambda: self.log_msg(f"Failed to start: {e}"))
                self.root.after(0, lambda: self.set_status("Failed", "#ef4444"))
                self.root.after(0, lambda: self.start_btn.configure(state="normal"))
                return

            ready = False
            for i in range(120):
                time.sleep(0.5)
                if self.server_proc.poll() is not None:
                    out = self.server_proc.stdout.read()
                    self.root.after(0, lambda o=out: self.log_msg(f"Process exited:\n{o[:400]}"))
                    self.root.after(0, lambda: self.set_status("Failed — see log", "#ef4444"))
                    self.root.after(0, lambda: self.start_btn.configure(state="normal"))
                    return
                try:
                    urllib.request.urlopen(URL, timeout=1)
                    ready = True
                    break
                except:
                    if i % 10 == 0:
                        self.root.after(0, lambda s=i//2: self.log_msg(f"Waiting... ({s}s)"))

            if not ready:
                self.root.after(0, lambda: self.log_msg("Timed out — trying to open anyway"))
            self.running = True
            self.root.after(0, self.on_server_ready)

            for line in self.server_proc.stdout:
                line = line.strip()
                if line and "127.0.0.1" not in line and "WARNING" not in line:
                    self.root.after(0, lambda l=line: self.log_msg(l))

        threading.Thread(target=run, daemon=True).start()

    def on_server_ready(self):
        self.set_status(f"Running — {URL}", "#22c55e")
        self.dot.configure(fg="#22c55e")
        self.log_msg("Server ready!")
        self.open_btn.configure(state="normal")
        self.stop_btn.configure(state="normal")
        self.open_browser()

    def open_browser(self):
        webbrowser.open(URL)
        self.log_msg("Opened browser.")

    def stop_server(self):
        if self.server_proc:
            self.server_proc.terminate()
            self.server_proc = None
        self.running = False
        self.set_status("Stopped", "#475569")
        self.dot.configure(fg="#1e3a5f")
        self.start_btn.configure(state="normal")
        self.open_btn.configure(state="disabled")
        self.stop_btn.configure(state="disabled")
        self.log_msg("Server stopped.")

    def on_close(self):
        if self.running:
            if messagebox.askyesno("Quit", "Stop the server and quit?"):
                self.stop_server()
                self.root.destroy()
        else:
            self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    app = Launcher()
    app.run()
