# Running Velune on Windows

Velune is fully supported on Windows. You can run it either **natively** (recommended) or inside a **WSL2** (Windows Subsystem for Linux 2) environment.

---

## Option A — Native Windows Setup (Recommended)

Running Velune natively gives you the best CLI performance and integrates directly with the native Windows Terminal, the Windows Credential Locker, and Windows path resolution.

### 1. Prerequisites

Make sure you have the following installed on your system:

- **Python 3.11+**: Download from [python.org](https://www.python.org/downloads/) (ensure you check the box to "Add Python to PATH" during installation) or install via `winget`:
  ```powershell
  winget install Python.Python.3.11
  ```
- **Git**: Download from [git-scm.com](https://git-scm.com/) or install via `winget`:
  ```powershell
  winget install Git.Git
  ```
- **Ollama for Windows** (Optional — only required for local LLMs): Download from [ollama.com](https://ollama.com/) and pull a coding model from your terminal:
  ```powershell
  ollama pull qwen2.5-coder:7b
  ```

### 2. Installation

Open **PowerShell** or **Command Prompt** (running as a normal user is fine) and run:

```powershell
# 1. Install Velune
pip install velune-cli

# 2. Navigate to your project folder
cd C:\path\to\your-project

# 3. Initialize Velune configuration
velune init

# 4. Verify system health and paths
velune doctor
```

A successful `velune doctor` output looks like:
```text
✓ Python 3.11
✓ Ollama reachable (localhost:11434)
✓ Model qwen2.5-coder:7b available
✓ SQLite ok
✓ Hardware: 16 GB RAM · tier: capable
```

### 3. Run Velune

Start the interactive REPL in your project folder:
```powershell
velune
```

### 4. Windows Security & Sandboxing Details

When running natively on Windows, Velune enforces robust security boundaries:

- **Windows Credential Locker**: When you run `velune setup`, Velune securely stores your provider API keys (Groq, OpenAI, Anthropic, Gemini, xAI, etc.) in the Windows Credential Locker using the Python `keyring` package. Your keys are never stored in plain-text configuration files or git history.
- **PATH-Hijack Guard**: Windows-native command execution enforces a strict path guard. Allowlisted binaries (`python`, `git`, `pytest`, etc.) must resolve to trusted directories. On Windows, these trusted directories are:
  - System directories under `%SystemRoot%` or `%windir%` (e.g., `C:\Windows`)
  - Standard Program Files directories (`C:\Program Files`, `C:\Program Files (x86)`)
  - User-local program installations (`%LOCALAPPDATA%\Programs`)
  - A virtual environment folder named `.venv` or `venv` rooted directly in your active workspace folder.
- **Inline-code Blocking**: Allowlisted interpreters (like `python` and `node`) are blocked from executing inline code flags (`python -c`, `node -e`). All code written by the agent must go through file writes, which require your explicit approval via the `DiffPreview` flow.

---

## Option B — WSL2 Setup (Alternative)

If your development team primarily targets Linux environments or you prefer to containerize your tools, you can run Velune via WSL2.

### 1. Enable WSL2 and Install Ubuntu
Open **PowerShell as Administrator** and run:
```powershell
wsl --install
wsl --set-default-version 2
```
*Restart your machine after the installation completes.*

If Ubuntu wasn't automatically installed, get **Ubuntu 22.04 LTS** from the Microsoft Store, open it, and set up your Linux username and password.

### 2. Install Ollama and Python inside WSL2
In your Ubuntu terminal:
```bash
# Install Ollama
curl -fsSL https://ollama.com/install.sh | sh
ollama serve &
ollama pull qwen2.5-coder:7b

# Install Python 3.11
sudo apt update && sudo apt install python3.11 python3-pip python3.11-venv -y
```

### 3. Install and Run Velune
```bash
pip3 install velune-cli
cd /mnt/c/Users/YourName/path/to/project
velune init
velune doctor
velune
```

*Note: For the best file system performance, it is recommended to keep your project files inside the Linux root directory (e.g. `~/projects/`) instead of the Windows mount `/mnt/c/`.*

---

## Recommended Terminal: Windows Terminal

For the best visual experience, run Velune inside **Windows Terminal** (installed from the Microsoft Store or pre-installed on Windows 11). 

Windows Terminal correctly renders Velune's 256-color theme, emoji badges, panel layouts, and interactive arrow-key picker menus. The legacy command console (`cmd.exe`) does not support these features and may render garbled characters.
