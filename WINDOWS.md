# Running Velune on Windows

Velune works on Windows via WSL2 (Windows Subsystem for Linux 2).
This guide covers every step from a fresh Windows installation.

---

## 1. Why WSL2, not native Windows

Most Python AI tools — including Ollama, CUDA drivers for LLMs,
and many native extensions — target Linux first. WSL2 runs a real
Linux kernel inside Windows using lightweight Hyper-V virtualization.
You get:

- Full Linux environment with no performance penalty for CPU/GPU work
- Access to your Windows files from inside Linux (`/mnt/c/Users/...`)
- Ollama and all Velune dependencies work without modification
- GPU passthrough to NVIDIA GPUs via CUDA on Windows

Native Windows support for Velune is planned. For now, WSL2 is the
recommended path on Windows.

---

## 2. Enable WSL2

Open **PowerShell as Administrator** (right-click the Start menu →
"Windows PowerShell (Admin)" or "Terminal (Admin)"):

```powershell
wsl --install
wsl --set-default-version 2
```

The first command installs WSL2 and Ubuntu in one step on Windows 11.
On Windows 10, it may prompt you to enable optional features first.

**Restart your machine** after the install completes. WSL2 requires
a reboot to activate the kernel component.

Verify the installation after restart:

```powershell
wsl --list --verbose
```

You should see a distribution (Ubuntu) with `VERSION 2`.

> **If you see an error about virtualization:** see the
> [Common issues](#9-common-issues) section below.

---

## 3. Install Ubuntu 22.04

If the `wsl --install` command above already installed Ubuntu, skip
to step 4.

Otherwise, open the Microsoft Store, search for
**Ubuntu 22.04.3 LTS**, and click **Install**.

After installation, launch Ubuntu from the Start menu. On first
launch it will ask you to create a Linux username and password.
This is your WSL2 user — it does not need to match your Windows
username.

---

## 4. Install Ollama inside WSL2

Open your Ubuntu terminal and run:

```bash
curl -fsSL https://ollama.com/install.sh | sh
```

Start the Ollama server in the background:

```bash
ollama serve &
```

Pull a coding model (this downloads ~4.7 GB — takes a few minutes):

```bash
ollama pull qwen2.5-coder:7b
```

Verify it works:

```bash
ollama run qwen2.5-coder:7b "say hello"
```

You should see a short response. Press `Ctrl+D` to exit.

---

## 5. Install Python 3.11+

Ubuntu 22.04 ships with Python 3.10. Install 3.11:

```bash
sudo apt update && sudo apt install python3.11 python3-pip python3.11-venv -y
```

Confirm the version:

```bash
python3 --version
```

Expected output: `Python 3.11.x`

> On Ubuntu 22.04, `python3` points to the system Python.
> Always use `python3` (not `python`) unless you set up a virtual
> environment or alias.

---

## 6. Install Velune

```bash
pip3 install velune-cli
```

If `pip3` is not found after the install above, use:

```bash
python3 -m pip install velune-cli
```

Initialize Velune in your project directory:

```bash
cd /your/project
velune init
```

Run the health check to confirm everything is wired up:

```bash
velune doctor
```

A healthy output looks like:

```text
✓ Python 3.11
✓ Ollama reachable (localhost:11434)
✓ Model qwen2.5-coder:7b available
✓ SQLite ok
✓ Hardware: 16 GB RAM · tier: capable
```

Start the REPL:

```bash
velune
```

---

## 7. Access your Windows files from WSL2

Your Windows drives are mounted under `/mnt/`:

```text
C:\  →  /mnt/c/
D:\  →  /mnt/d/
```

To navigate to a Windows project folder:

```bash
cd /mnt/c/Users/YourName/Projects/my-project
```

To resolve your Windows username automatically:

```bash
WIN_USER=$(cmd.exe /c "echo %USERNAME%" 2>/dev/null | tr -d '\r')
cd /mnt/c/Users/$WIN_USER/Projects
```

> **Performance note:** File I/O on `/mnt/c/` is 5–10x slower than
> the native Linux filesystem. For best performance, keep your
> project files inside WSL2 itself:
>
> ```bash
> mkdir -p ~/projects && cd ~/projects
> git clone <your-repo>
> ```
>
> You can still open them in VS Code from Windows using the
> [WSL extension](https://marketplace.visualstudio.com/items?itemName=ms-vscode-remote.remote-wsl).

---

## 8. GPU passthrough (NVIDIA users)

WSL2 can use your NVIDIA GPU directly for Ollama inference.

**Step 1 — Install the NVIDIA driver on Windows (not inside WSL2).**

Download and install the latest Game Ready or Studio driver from
<https://www.nvidia.com/download/index.aspx>. The WSL2 CUDA support
is bundled with the Windows driver since version 515+.

**Step 2 — Do NOT install a separate CUDA toolkit inside WSL2.**

The driver installed on Windows exposes CUDA to WSL2 automatically
via `/usr/lib/wsl/lib/`. Installing a second CUDA toolkit inside WSL2
will conflict with it.

**Step 3 — Verify GPU visibility inside WSL2:**

```bash
nvidia-smi
```

Expected output shows your GPU name, driver version, and VRAM.
If this fails, see the [Common issues](#9-common-issues) section.

**Step 4 — Verify Ollama uses the GPU:**

```bash
ollama run qwen2.5-coder:7b "write a hello world function"
```

Watch GPU memory in a second terminal:

```bash
nvidia-smi --query-gpu=memory.used --format=csv --loop=1
```

You should see VRAM usage increase during inference.

---

## 9. Common issues

**"WSL2 requires a virtual machine platform component" or "virtualization not enabled"**

WSL2 uses Hyper-V. If your CPU supports it but it is disabled,
enable it in your BIOS:

- **Intel:** look for "Intel VT-x" or "Virtualization Technology" → Enable
- **AMD:** look for "AMD-V" or "SVM Mode" → Enable

The setting is usually under "Advanced CPU Configuration" or
"Advanced > CPU Configuration" in your BIOS/UEFI setup screen
(press Del or F2 at startup to enter).

After enabling, save and restart. Then run `wsl --install` again.

---

**Ollama port conflict — REPL shows "no providers reachable"**

This can happen if a previous WSL2 session left Ollama running.
Reset WSL2 and restart:

```powershell
wsl --shutdown
```

Then open Ubuntu again and run `ollama serve &` before starting
Velune.

---

**`python` command not found**

Ubuntu uses `python3`. Either use `python3` directly or create an
alias:

```bash
echo "alias python=python3" >> ~/.bashrc
source ~/.bashrc
```

---

**Slow file access — Velune is sluggish when editing files**

You are likely working on a project inside `/mnt/c/`. Move it to
your WSL2 home directory for 10x faster I/O:

```bash
cp -r /mnt/c/Users/YourName/Projects/my-project ~/projects/
cd ~/projects/my-project
velune init
```

---

**`nvidia-smi` not found inside WSL2**

The `nvidia-smi` binary for WSL2 lives at `/usr/lib/wsl/lib/nvidia-smi`.
If it is not on your PATH:

```bash
export PATH=$PATH:/usr/lib/wsl/lib
```

Add that line to `~/.bashrc` to make it permanent.

---

**WSL2 clock drift causing API request failures**

WSL2 can fall out of sync with the Windows system clock after
suspend/resume. Fix it with:

```bash
sudo hwclock -s
```

---

## 10. Windows Terminal (recommended)

Install **Windows Terminal** from the Microsoft Store for the best
experience with Velune's rich terminal UI (color, unicode, scrollback).

After installation:

1. Open Windows Terminal settings (Ctrl+,)
2. Under **Startup → Default profile**, select **Ubuntu**
3. Optionally set a dark theme under **Appearance**

Windows Terminal renders Velune's colored panels, emoji mode badges,
and the context-window bar correctly. The default `cmd.exe` console
does not support 256-color output and will show garbled characters.

---

## Quick reference

| Goal                            | Command                                      |
|---------------------------------|----------------------------------------------|
| Open WSL2                       | Start menu → Ubuntu                          |
| Restart WSL2                    | `wsl --shutdown` in PowerShell, then reopen  |
| Access Windows files            | `cd /mnt/c/Users/YourName/...`               |
| Start Ollama                    | `ollama serve &`                             |
| Pull a model                    | `ollama pull qwen2.5-coder:7b`               |
| Start Velune                    | `velune`                                     |
| Health check                    | `velune doctor`                              |
| Verify GPU                      | `nvidia-smi`                                 |
