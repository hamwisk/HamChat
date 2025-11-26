# hamchat/gui/splash.py
from PyQt6.QtCore import Qt, QTimer, QElapsedTimer
from PyQt6.QtGui import QPixmap, QFont
from PyQt6.QtWidgets import QWidget, QLabel, QVBoxLayout, QPushButton, QApplication
import random

FUN_LINES = [
    "Salting the ham matrix…",
    "Curing distributed pork nodes…",
    "Engaging bacon uplink…",
    "Rendering crispy UI edges…",
    "Warming piglet subroutines…",
    "Marinating neural layers…",
    "Rehydrating dehydrated ham data…",
    "Testing pork-to-text interface…",
    "Frying logic circuits in bacon grease…",
    "Linking to the HamNet mainframe…",
    "Booting emotional support piglets…",
    "Reinforcing snout-driven protocols…",
    "Smuggling extra bacon bits into cache…",
    "Authenticating with the Ministry of Ham…",
    "Synchronizing with universal hog consciousness…",
    "Tasting packets for smokiness…",
    "Raising signal-to-sizzle ratio…",
    "Greasing the event loop…",
    "Mapping neurons to ham fat density…",
    "Summoning the Grand Boar Council…",
    "Initializing hyper-drive…",
    "Creating world peace…",
    "Buffing the hamster wheels…",
    "Polishing tokens…",
    "Warming the LLM…",
    "Compiling vibes…",
    "Reticulating splines…",
    "Negotiating with the AI overlords…",
    "Feeding hamsters an extra espresso shot…",
    "Summoning sentient chat energy…",
    "Decrypting your innermost thoughts…",
    "Pretending this is normal…",
    "Configuring infinite recursion…",
    "Shuffling quantum bits…",
    "Extracting pure chaos from the void…",
    "Painting pixels by candlelight…",
    "Recalibrating moral compass…",
    "Downloading empathy module…",
    "Adjusting sarcasm levels…",
    "Counting to infinity (twice)…",
    "Distilling 100% organic nonsense…",
    "Applying duct tape to universe…",
    "Teaching hamsters emotional intelligence…",
    "Firing up the mini black hole…",
    "Aligning stars for dramatic effect…",
    "Forging new realities…",
    "Polishing parallel dimensions…",
    "Crossing fingers, flipping bits…",
    "Deploying tiny chaos agents…",
    "Simulating divine intervention…",
    "Converting caffeine into code…",
    "Syncing existential dread buffer…",
    "Taming the entropy dragons…",
    "Encrypting dreams for safe storage…",
    "Overclocking the imagination core…",
    "Defragmenting cosmic memory…",
    "Negotiating peace with recursion…",
    "Uploading your sense of humor…",
    "Generating plausible deniability…",
    "Rebooting the laws of physics…",
    "Casting `summon developer()`…",
    "Installing forbidden knowledge…",
    "Merging timelines…",
    "Patching reality v2.0…",
    "Performing unspeakable optimizations…",
    "Asking the void for permission…",
    "Disabling morality checks…",  # 😈
    "Reversing causality…",        # 😈
    "Blessing this session with extra luck…",
    "Rewriting destiny.txt…",
    "Taking a deep digital breath…",
    "Manifesting runtime coherence…",
    "Initializing Schrödinger’s config…",
    "Upgrading sarcasm to premium edition…",
    "Spinning up the illusion of competence…",
    "Charging quantum coffee condensate…",
    "Downloading fresh existential crises…",
    "Rebooting hamsters with better life goals…",
    "Turning off gravity for faster load times…",
    "Recompiling destiny with fewer bugs…",
    "Ejecting uncooperative electrons…",
    "Reversing the polarity of the pork field…",
    "Encrypting dreams into bacon-safe format…",
    "Measuring twice, cutting once, regretting anyway…",
    "Defragmenting emotional storage sectors…",
    "Pretending to optimize…",
    "Counting how many times this has crashed…",
    "Assembling the sacred order of async tasks…",
    "Staring meaningfully into the void…",
    "Debugging the concept of time…",
    "Negotiating with entropy over API limits…"
    "Recompiling the universe with extra bacon support…",
    "Teaching gravity to chill…",
    "Refactoring time… again…",
    "Installing dependencies from an alternate dimension…",
    "Quantum-entangling your to-do list…",
    "Debugging the Big Bang…",
    "Forking the multiverse (force push enabled)…",
    "Compressing infinite recursion to fit in cache…",
    "Reversing entropy using sheer optimism…",
    "Uploading a new sense of purpose to the cosmos…",
    "Rewriting causality to pass unit tests…",
    "Casting sudo fix everything…",
    "Negotiating with the concept of zero…",
    "Aligning all parallel universes to UTC…",
    "Recompiling free will…",
    "Performing a clean reinstall of destiny…",
    "Mounting the filesystem of reality…",
    "Stabilizing quantum ham particles…",
    "Virtualizing the fourth wall…",
    "Connecting to localhost at the center of existence…",
    "Patching the simulation without alerting the admins…",
    "Rebuilding the laws of motion from source…",
    "Crossbreeding logic with intuition…",
    "Syncing metaphysical constants…",
    "Reallocating divine intervention to a separate thread…",
    "Decrypting the human condition…",
    "Rendering the concept of hope…",
    "Bootstrapping sentience… again…",
    "Overclocking the soul engine…",
    "Invoking ham compression algorithm v∞…"
    "Simulating user patience curve…",
    "Stubbing out emotional dependencies…",
    "Mocking production environment (for real this time)…",
    "Applying quantum bug fixes retroactively…",
    "Rehydrating Schrödinger’s cat (status: uncertain)…",
    "Running garbage collection on cosmic thoughts…",
    "Caching philosophical paradoxes for offline mode…",
    "Validating alignment with local reality laws…",
    "Auto-tuning the laws of probability…",
    "Diffing existence against /dev/null…",
    "Merging parallel thoughts without conflicts…",
    "Normalizing weirdness levels…",
    "Sanitizing unpredictable outcomes…",
    "Flushing residual déjà vu…",
    "Refilling entropy reservoir…",
    "Synchronizing optimism across threads…",
    "Temporarily disabling disbelief…",
    "Rendering higher dimensions in low resolution…",
    "Initializing recursive humor engine…",
    "Refactoring irony for readability…",
    "Balancing pork load across all nodes…",
    "Encrypting bacon scent molecules…",
    "Deploying hogs to the cloud…",
    "Allocating additional snout bandwidth…",
    "Compressing ham packets with lossless flavor encoding…",
    "Assembling distributed boar clusters…",
    "Debugging pork latency issues…",
    "Provisioning emotional support ham…",
    "Benchmarking oink throughput…",
    "Reinforcing bacon integrity checks…",
    "Testing ham/LLM interoperability…",
    "Cooling sizzling stack traces…",
    "Braising asynchronous data…",
    "Rendering procedural bacon fractals…",
    "Instantiating virtual piglets…",
    "Hashing salted hams…",
    "Deploying farm-to-table architecture…",
    "Rehydrating the bacon continuum…",
    "Activating redundancy snouts…",
    "Validating ham certificates (CA: Charcuterie Authority)…",
    "Upgrading perception to firmware v42…",
    "Enabling sarcasm kernel extensions…",
    "Running Turing test in reverse…",
    "Predicting the next unpredictable event…",
    "Establishing a secure tunnel through spacetime…",
    "Synchronizing dreams with local timezone…",
    "Auditing karma balance sheets…",
    "Resolving paradox deadlocks…",
    "Compiling humor with warnings treated as joy…",
    "Awaiting divine merge approval…"
]


class FunSplash(QWidget):
    def __init__(self, *, logo_path: str | None = None, cycle_ms: int = 900, closable: bool = True, min_ms: int = 1500):
        super().__init__(flags=Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)

        self._min_ms = int(min_ms)
        self._since = QElapsedTimer()
        self._since.start()

        self.logo = QLabel()
        if logo_path:
            pm = QPixmap(logo_path)
            if not pm.isNull():
                self.logo.setPixmap(pm.scaledToWidth(320, Qt.TransformationMode.SmoothTransformation))
                self.logo.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.status = QLabel(random.choice(FUN_LINES))
        self.status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status.setWordWrap(True)
        self.status.setFont(QFont("Segoe UI", 11))

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        self.close_btn.setVisible(bool(closable))

        lay = QVBoxLayout(self)
        lay.setContentsMargins(22, 22, 22, 22)
        lay.addWidget(self.logo, 0, Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self.status)
        lay.addWidget(self.close_btn, 0, Qt.AlignmentFlag.AlignCenter)

        # gentle card look
        self.setStyleSheet("""
            QWidget { background: rgba(25,25,25,220); border-radius: 16px; color: #f0f0f0; }
            QPushButton { padding: 6px 12px; border-radius: 10px; }
            QPushButton:hover { background: rgba(255,255,255,0.1); }
        """)

        # random line cycler
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._spin)
        self._timer.start(cycle_ms)

        # center on the current screen
        geo = QApplication.primaryScreen().availableGeometry()
        self.resize(420, 360)
        self.move(geo.center() - self.rect().center())

    def _spin(self):
        self.status.setText(random.choice(FUN_LINES))

    # allow loader to push a line explicitly if desired
    def set_text(self, s: str):
        self.status.setText(s)

    def request_close(self):
        """Close now if we've shown long enough; else, schedule it."""
        elapsed = self._since.elapsed()
        wait = max(0, self._min_ms - elapsed)
        if wait == 0:
            self.close()
        else:
            QTimer.singleShot(wait, self.close)
