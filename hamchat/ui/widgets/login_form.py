# hamchat/ui/widgets/login_form.py
from __future__ import annotations
from PyQt6.QtCore import pyqtSignal, Qt, QTimer
from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedWidget,
    QFormLayout, QInputDialog, QMessageBox
)

class LoginForm(QWidget):
    sig_close = pyqtSignal()
    submit_admin_setup = pyqtSignal(str, str)   # username, password
    submit_login = pyqtSignal(str, str)
    submit_signup = pyqtSignal(str, str)

    def __init__(self, parent=None, *, admin: bool, signup_requires_approval: bool = False):
        super().__init__(parent)
        self._signup_requires_approval = bool(signup_requires_approval)

        self._stack = QStackedWidget(self)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        self._title = QLabel("", self)
        self._title.setTextFormat(Qt.TextFormat.RichText)

        # Build pages
        self._pg_admin = self._build_admin_setup()
        self._pg_login = self._build_login()
        self._pg_signup = self._build_signup()

        self._stack.addWidget(self._pg_admin)
        self._stack.addWidget(self._pg_login)
        self._stack.addWidget(self._pg_signup)

        lay.addWidget(self._title)
        lay.addWidget(self._stack, 1)

        # Footer with Close
        foot = QHBoxLayout(); foot.addStretch(1)
        btn_close = QPushButton("Close"); btn_close.clicked.connect(self.sig_close.emit)
        foot.addWidget(btn_close)
        lay.addLayout(foot)

        # Initial mode
        QTimer.singleShot(0, lambda: self.set_mode("admin" if not admin else "login"))

    # ---------- pages ----------
    def _build_admin_setup(self) -> QWidget:
        w = QWidget(self); f = QFormLayout(w)
        self._adm_user = QLineEdit(placeholderText="Admin username")
        self._adm_pass = QLineEdit(placeholderText="Password"); self._adm_pass.setEchoMode(QLineEdit.EchoMode.Password)
        f.addRow("Username", self._adm_user); f.addRow("Password", self._adm_pass)
        btn = QPushButton("Create Admin")
        btn.clicked.connect(lambda: self._on_signup_submit(is_admin=True))
        f.addRow(btn)
        return w

    # --- NEW: confirm password handler ---
    def _on_signup_submit(self, *, is_admin: bool = False):
        # pick the correct widgets for the current page
        if is_admin:
            user_edit = self._adm_user
            pass_edit = self._adm_pass
        else:
            user_edit = self._su_user
            pass_edit = self._su_pass

        username = (user_edit.text() or "").strip()
        password = pass_edit.text() or ""

        if not username:
            QMessageBox.warning(self, "Missing username",
                                "Please enter an admin username." if is_admin else "Please enter a username.")
            return
        if not password:
            QMessageBox.warning(self, "Missing password", "Please enter a password.")
            return

        # confirm password, without changing the layout
        confirm, ok = QInputDialog.getText(self, "Confirm password", "Re-enter password:",
                                           QLineEdit.EchoMode.Password)
        if not ok:
            return
        if confirm != password:
            QMessageBox.warning(self, "Passwords don't match",
                                "The confirmation did not match. Please try again.")
            return

        if len(password) < 4:
            if QMessageBox.question(
                    self, "Very short password",
                    "That password looks very short. Continue anyway?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            ) != QMessageBox.StandardButton.Yes:
                return

        # emit the right signal
        if is_admin:
            self.submit_admin_setup.emit(username, password)
        else:
            self.submit_signup.emit(username, password)

    def _build_login(self) -> QWidget:
        w = QWidget(self); v = QVBoxLayout(w); v.setContentsMargins(0,0,0,0)
        form = QFormLayout(); v.addLayout(form)
        self._lg_user = QLineEdit(placeholderText="Username")
        self._lg_pass = QLineEdit(placeholderText="Password"); self._lg_pass.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username", self._lg_user); form.addRow("Password", self._lg_pass)

        row = QHBoxLayout()
        btn_login = QPushButton("Log in")
        btn_login.clicked.connect(lambda: self.submit_login.emit(self._lg_user.text().strip(), self._lg_pass.text()))
        link_signup = QPushButton("Sign up")
        link_signup.setFlat(True); link_signup.setCursor(Qt.CursorShape.PointingHandCursor)
        link_signup.clicked.connect(lambda: self.set_mode("signup"))
        row.addWidget(btn_login); row.addStretch(1); row.addWidget(link_signup)
        v.addLayout(row)
        return w

    def _build_signup(self) -> QWidget:
        w = QWidget(self);
        v = QVBoxLayout(w);
        v.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout();
        v.addLayout(form)
        self._su_user = QLineEdit(placeholderText="New username")
        self._su_pass = QLineEdit(placeholderText="Password");
        self._su_pass.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Username", self._su_user);
        form.addRow("Password", self._su_pass)

        row = QHBoxLayout()
        btn_text = "Request account" if self._signup_requires_approval else "Create account"
        btn_signup = QPushButton(btn_text)
        btn_signup.clicked.connect(self._on_signup_submit)
        link_back = QPushButton("Back to login");
        link_back.setFlat(True);
        link_back.setCursor(Qt.CursorShape.PointingHandCursor)
        link_back.clicked.connect(lambda: self.set_mode("login"))
        row.addWidget(btn_signup);
        row.addStretch(1);
        row.addWidget(link_back)
        v.addLayout(row)

        if self._signup_requires_approval:
            hint = QLabel("An admin must approve your request before you can log in.")
            hint.setStyleSheet("color:#8b8f97; font-size:12px;")
            v.addWidget(hint)
        return w

    # ---------- helpers ----------
    def set_mode(self, mode: str):
        if mode == "admin":
            self._title.setText("<b>Set up the first Admin</b>")
            self._stack.setCurrentWidget(self._pg_admin)
            self._adm_user.setFocus()  # 👈 focus first field
        elif mode == "login":
            self._title.setText("<b>Welcome back</b>")
            self._stack.setCurrentWidget(self._pg_login)
            self._lg_user.setFocus()  # 👈 focus first field
        else:
            self._title.setText("<b>Create an account</b>")
            self._stack.setCurrentWidget(self._pg_signup)
            self._su_user.setFocus()  # 👈 focus first field

    def keyPressEvent(self, event):
        """Allow Enter/Return to submit the active form."""
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            current = self._stack.currentWidget()
            if current is self._pg_admin:
                self._on_signup_submit(is_admin=True)
            elif current is self._pg_login:
                self.submit_login.emit(
                    self._lg_user.text().strip(), self._lg_pass.text()
                )
            elif current is self._pg_signup:
                self._on_signup_submit(is_admin=False)
            return  # swallow event so it doesn’t beep
        super().keyPressEvent(event)
