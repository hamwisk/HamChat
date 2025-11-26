# hamchat/splash_worker.py
"""
Cross-platform splash subprocess.

Spawned early by the main loader to give instant visual feedback.
Closes automatically when the parent signals "close" through a Pipe.
"""
from multiprocessing.connection import Connection
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication
from .ui.splash import FunSplash
import sys


def splash_process(conn: Connection, logo_path: str | None = None):
    app = QApplication(sys.argv)
    splash = FunSplash(logo_path=logo_path, closable=False, min_ms=1200)
    splash.show()

    def poll_parent():
        if conn.poll():
            msg = conn.recv()
            if msg == "close":
                splash.request_close()
                # allow the fade-out to complete before exit
                QTimer.singleShot(600, app.quit)

    t = QTimer()
    t.timeout.connect(poll_parent)
    t.start(100)
    app.exec()
