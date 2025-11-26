# export_requirements.py

import subprocess
import sys
from datetime import datetime

def export_requirements(filename="requirements.txt"):
    """Export exact versions of installed packages to a file."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "freeze"],
            check=True,
            capture_output=True,
            text=True
        )
        with open(filename, "w") as f:
            f.write(f"# Requirements exported {datetime.now().isoformat()}\n")
            f.write(result.stdout)
        print(f"✅ Environment exported to {filename}")
    except subprocess.CalledProcessError as e:
        print("❌ Failed to export requirements:")
        print(e.stderr)

if __name__ == "__main__":
    export_requirements()

