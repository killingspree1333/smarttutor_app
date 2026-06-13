from PyQt6.QtWidgets import *
from PyQt6.QtCore import *
from PyQt6.QtGui import *
from api import ApiWorker
from session import save_session


class LoginDialog(QDialog):
    logged_in = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("SmartTutor")
        self.setFixedSize(440, 440)
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self._drag_pos = None
        self._workers = []
        self.setup_ui()

    def setup_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        # Outer card — один контейнер, всё внутри него
        card = QFrame()
        card.setStyleSheet("""
            QFrame {
                background-color: #12121f;
                border-radius: 20px;
                border: 1px solid #252545;
            }
        """)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(32, 24, 32, 28)
        layout.setSpacing(0)

        # ── Close button ──
        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(24, 24)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet("""
            QPushButton {
                background: #1e1e38;
                border: none;
                border-radius: 7px;
                color: #505080;
                font-size: 11px;
            }
            QPushButton:hover { background: #2a2a50; color: #e0e0ff; }
        """)
        close_btn.clicked.connect(self.reject)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)
        layout.addSpacing(8)

        # ── Logo ──
        logo_row = QHBoxLayout()
        logo_row.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo_row.setSpacing(10)

        icon_bg = QLabel("🎓")
        icon_bg.setFixedSize(44, 44)
        icon_bg.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon_bg.setStyleSheet("""
            background: qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #6d5fff,stop:1 #9d7fff);
            border-radius: 13px;
            font-size: 22px;
            border: none;
        """)

        name_lbl = QLabel("SmartTutor")
        name_lbl.setStyleSheet("""
            font-size: 24px;
            font-weight: 800;
            color: #ffffff;
            background: transparent;
            border: none;
            letter-spacing: -0.5px;
        """)

        logo_row.addWidget(icon_bg)
        logo_row.addWidget(name_lbl)
        layout.addLayout(logo_row)
        layout.addSpacing(6)

        sub = QLabel("Войдите в свой аккаунт")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setStyleSheet("""
            font-size: 12px;
            color: #50508a;
            background: transparent;
            border: none;
        """)
        layout.addWidget(sub)
        layout.addSpacing(24)

        # ── Email ──
        email_lbl = QLabel("EMAIL")
        email_lbl.setStyleSheet("""
            font-size: 10px;
            font-weight: 700;
            color: #50508a;
            background: transparent;
            border: none;
            letter-spacing: 0.8px;
        """)
        self.email = QLineEdit()
        self.email.setPlaceholderText("your@email.com")
        self.email.setFixedHeight(42)
        self.email.setStyleSheet("""
            QLineEdit {
                background: #0e0e20;
                border: 1px solid #252545;
                border-radius: 10px;
                padding: 0 14px;
                color: #e8e8f8;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: 1.5px solid #6d5fff;
                background: #13132a;
            }
        """)

        layout.addWidget(email_lbl)
        layout.addSpacing(5)
        layout.addWidget(self.email)
        layout.addSpacing(14)

        # ── Password ──
        pass_lbl = QLabel("ПАРОЛЬ")
        pass_lbl.setStyleSheet("""
            font-size: 10px;
            font-weight: 700;
            color: #50508a;
            background: transparent;
            border: none;
            letter-spacing: 0.8px;
        """)
        self.password = QLineEdit()
        self.password.setPlaceholderText("••••••••")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setFixedHeight(42)
        self.password.setStyleSheet("""
            QLineEdit {
                background: #0e0e20;
                border: 1px solid #252545;
                border-radius: 10px;
                padding: 0 14px;
                color: #e8e8f8;
                font-size: 13px;
            }
            QLineEdit:focus {
                border: 1.5px solid #6d5fff;
                background: #13132a;
            }
        """)
        self.password.returnPressed.connect(self.do_login)

        layout.addWidget(pass_lbl)
        layout.addSpacing(5)
        layout.addWidget(self.password)
        layout.addSpacing(8)

        # ── Error ──
        self.err_lbl = QLabel("")
        self.err_lbl.setStyleSheet("color: #ff5566; font-size: 12px; background: transparent; border: none;")
        self.err_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.err_lbl.hide()
        layout.addWidget(self.err_lbl)
        layout.addSpacing(14)

        # ── Login button ──
        self.login_btn = QPushButton("Войти")
        self.login_btn.setFixedHeight(44)
        self.login_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.login_btn.setStyleSheet("""
            QPushButton {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #6d5fff,stop:1 #9d7fff);
                color: white;
                border: none;
                border-radius: 11px;
                font-size: 14px;
                font-weight: 700;
            }
            QPushButton:hover {
                background: qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #7d6fff,stop:1 #ad8fff);
            }
            QPushButton:pressed { background: #5545ef; }
        """)
        self.login_btn.clicked.connect(self.do_login)
        layout.addWidget(self.login_btn)
        layout.addSpacing(14)

        # ── Register hint ──
        hint = QLabel('Нет аккаунта? <a href="https://smarttutorapp-production.up.railway.app" style="color:#8878ff;text-decoration:none;">Зарегистрируйтесь на сайте →</a>')
        hint.setOpenExternalLinks(True)
        hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        hint.setStyleSheet("font-size: 12px; color: #40405a; background: transparent; border: none;")
        layout.addWidget(hint)

        root.addWidget(card)

    def do_login(self):
        email = self.email.text().strip()
        password = self.password.text()
        if not email or not password:
            self.show_err("Заполните все поля")
            return
        self.login_btn.setText("Входим...")
        self.login_btn.setEnabled(False)
        w = ApiWorker("POST", "/auth/login", data={"email": email, "password": password})
        w.success.connect(self.on_success)
        w.failure.connect(self.on_failure)
        self._workers.append(w)
        w.start()

    def on_success(self, data):
        save_session(data)          # ← сохраняем сессию чтобы не входить заново
        self.logged_in.emit(data)
        self.accept()

    def on_failure(self, _):
        self.show_err("Неверный email или пароль")
        self.login_btn.setText("Войти")
        self.login_btn.setEnabled(True)

    def show_err(self, msg):
        self.err_lbl.setText(msg)
        self.err_lbl.show()
        QTimer.singleShot(4000, self.err_lbl.hide)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = e.globalPosition().toPoint() - self.frameGeometry().topLeft()

    def mouseMoveEvent(self, e):
        if self._drag_pos and e.buttons() == Qt.MouseButton.LeftButton:
            self.move(e.globalPosition().toPoint() - self._drag_pos)

    def mouseReleaseEvent(self, e):
        self._drag_pos = None